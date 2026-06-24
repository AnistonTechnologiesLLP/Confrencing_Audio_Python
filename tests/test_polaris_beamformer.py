"""Hardware-free tests for the POLARIS real-time beamformer module.

Everything here runs without an audio device: the pure talker-hold state machine,
the stateless delay-and-sum block, SRP-PHAT DOA from synthetic covariances, the
constructor/geometry, device-validation errors (monkeypatched), output-queue
backpressure, and lifecycle parity (streams stubbed). numpy is required for the
DSP paths and skipped if absent.
"""
import math
from types import SimpleNamespace

import pytest

np = pytest.importorskip("numpy")

import conf_pipeline_control as cc
from conf_pipeline_control import doa
from conf_pipeline_control.audio import InputDevice
import conf_pipeline_control.polaris_beamformer as pb
from conf_pipeline_control.polaris_beamformer import (
    DEFAULT_BEAM_BANDLIMIT_HZ,
    DeviceConfigError,
    DoaReading,
    PolarisBeamformer,
    _TalkerTracker,
    _lowpass_kernel,
    delay_and_sum_block,
)


FREQS = np.linspace(doa.DEFAULT_F_LO_HZ, doa.DEFAULT_F_HI_HZ, 40)
C = 343.0


def _unit(az_deg, off_nadir_deg=90.0):
    az = math.radians(az_deg)
    n = math.radians(off_nadir_deg)
    s = math.sin(n)
    return np.array([s * math.sin(az), s * math.cos(az), -math.cos(n)])


def _cov_from_sources(geom, azimuths, *, off_nadir=90.0, noise=1e-3):
    """Synthesize a band covariance R(f) from plane-wave sources (cf. test_doa)."""
    elems = np.array(geom.elements, dtype=float)
    M = geom.n_channels
    R = np.zeros((len(FREQS), M, M), dtype=complex)
    for fi, f in enumerate(FREQS):
        k = 2.0 * np.pi * f / C
        acc = noise * np.eye(M, dtype=complex)
        for az in azimuths:
            a = np.exp(1j * k * (elems @ _unit(az, off_nadir)))
            acc += np.outer(a, np.conj(a))
        R[fi] = acc
    return R


def _plane_wave_block(geom, az_deg, sr, n, tones=(2000.0, 3000.0, 4000.0)):
    """An (n, M) time-domain plane wave from az_deg: each capsule is the source
    advanced by its propagation lead proj_m/c (mic toward the source arrives first)."""
    elems = np.array(geom.elements, dtype=float)
    proj = elems @ _unit(az_deg)                       # (M,)
    t = np.arange(n) / sr
    s = sum(np.sin(2 * np.pi * f * t) for f in tones)
    x = np.zeros((n, geom.n_channels), dtype=float)
    for m in range(geom.n_channels):
        lead = int(round(proj[m] / C * sr))            # samples this capsule leads centre
        x[:, m] = np.roll(s, -lead)                    # earlier arrival → shift left
    return x


def _analytic_plane_wave(geom, az_deg, sr, n, freq):
    """Continuous-time single tone sampled at each capsule's *exact* (fractional) propagation
    lead — not integer-rounded like :func:`_plane_wave_block`. This fairly exercises sub-sample
    steering: the integer beamformer cannot perfectly align it, the fractional one can."""
    elems = np.array(geom.elements, dtype=float)
    proj = elems @ _unit(az_deg)                        # (M,) metres; toward source ⇒ earlier
    t = np.arange(n) / sr
    x = np.zeros((n, geom.n_channels), dtype=float)
    for m in range(geom.n_channels):
        x[:, m] = np.sin(2 * np.pi * freq * (t + proj[m] / C))   # capsule m leads centre by proj/c
    return x


def _run_blocks(beam, sig, block):
    """Feed `sig` (n, M) through `beam.process` in `block`-row chunks; return the concatenated mono."""
    outs = [np.asarray(beam.process(sig[i:i + block])) for i in range(0, sig.shape[0], block)]
    return np.concatenate(outs)


def _interferer_noise_cov(geom, az_n, band_idx, freqs_full, power=1000.0, noise=1.0):
    """Synthesize a per-band noise covariance R(f) = P·aₙaₙᴴ + σ²I from a strong plane-wave
    interferer at `az_n` plus white noise — the shape the MVDR provider feeds in."""
    elems = np.array(geom.elements, dtype=float)                 # (M, 3) all capsules
    proj = elems @ _unit(az_n)                                   # (M,)
    M = geom.n_channels
    R = np.zeros((len(band_idx), M, M), dtype=complex)
    for i, bi in enumerate(band_idx):
        k = 2.0 * np.pi * freqs_full[bi] / C
        a = np.exp(1j * k * proj)                               # (M,) interferer manifold
        R[i] = power * np.outer(a, np.conj(a)) + noise * np.eye(M, dtype=complex)
    return R


# --------------------------------------------------------------------------- #
# SRP-PHAT DOA reuse
# --------------------------------------------------------------------------- #
def test_doa_recovers_known_azimuth():
    bf = PolarisBeamformer(device=None)                # default: all 8 active, radius 0.040
    R = _cov_from_sources(bf.geometry, [80.0])
    az, sal, dets = bf._detect_dominant(R, FREQS)
    assert az is not None
    assert doa._circular_sep(az, 80.0) <= 8.0
    assert sal > 0.0 and len(dets) >= 1


def test_current_doa_none_on_silence_then_recovers():
    bf = PolarisBeamformer(device=None)
    flat = np.broadcast_to(np.eye(8, dtype=complex), (5, 8, 8)).copy()
    az, sal, _ = bf._detect_dominant(flat, np.linspace(300.0, 3800.0, 5))
    assert az is None and sal == 0.0
    r = bf._tracker.update(az, sal, t=0.0)
    assert r.azimuth_deg is None and not r.active
    az2, _, _ = bf._detect_dominant(_cov_from_sources(bf.geometry, [120.0]), FREQS)
    assert doa._circular_sep(az2, 120.0) <= 8.0


# --------------------------------------------------------------------------- #
# Talker-hold smoothing (pure state machine)
# --------------------------------------------------------------------------- #
def test_hold_keeps_angle_through_dropout():
    tk = _TalkerTracker(hold_seconds=0.4, switch_margin_deg=20.0)
    assert tk.update(90.0, 8.0, t=0.0).azimuth_deg == 90.0
    r1 = tk.update(None, 0.0, t=0.2)                   # brief silence → coast
    assert r1.azimuth_deg == 90.0 and r1.held and not r1.active
    r2 = tk.update(None, 0.0, t=0.39)
    assert r2.azimuth_deg == 90.0 and r2.held
    r3 = tk.update(None, 0.0, t=0.5)                   # past the hold → release
    assert r3.azimuth_deg is None and not r3.held


def test_switch_margin_ignores_small_move_but_switches_past_it():
    tk = _TalkerTracker(hold_seconds=0.4, switch_margin_deg=20.0)
    tk.update(90.0, 8.0, t=0.0)
    assert tk.update(100.0, 8.0, t=0.1).azimuth_deg == 90.0   # 10° < margin → ignored
    assert tk.update(120.0, 9.0, t=0.2).azimuth_deg == 120.0  # 30° ≥ margin → switch


def test_hold_then_far_talker_switches_immediately():
    tk = _TalkerTracker(hold_seconds=0.5, switch_margin_deg=20.0)
    tk.update(90.0, 8.0, t=0.0)
    assert tk.update(None, 0.0, t=0.1).held                   # coasting on 90
    assert tk.update(200.0, 7.0, t=0.2).azimuth_deg == 200.0  # new far talker jumps in


def test_switch_margin_is_wrap_aware():
    tk = _TalkerTracker(hold_seconds=0.4, switch_margin_deg=20.0)
    tk.update(350.0, 8.0, t=0.0)
    # 15° away across the 0/360 wrap → within the 20° margin → ignored
    assert tk.update(5.0, 8.0, t=0.1).azimuth_deg == 350.0


def test_talker_tracker_reset_lifecycle():
    from conf_pipeline_control.tracking import Tracker

    tk = _TalkerTracker(hold_seconds=0.4, switch_margin_deg=20.0)
    assert isinstance(tk, Tracker)                       # shares the unified Tracker lifecycle
    tk.update(90.0, 8.0, t=0.0)
    assert tk.current() == 90.0
    tk.reset()                                           # wiped; config (hold/margin) preserved
    assert tk.current() is None and tk.hold_seconds == 0.4
    assert tk.update(200.0, 7.0, t=1.0).azimuth_deg == 200.0   # re-acquires fresh, no hold of 90


def test_steered_noise_only_reflects_vad():
    bf = PolarisBeamformer(device=None)
    bf._reading = DoaReading(90.0, 6.0, held=False, active=True)
    assert bf.noise_only is False                        # someone talking
    bf._reading = DoaReading(None, 0.0, held=False, active=False)
    assert bf.noise_only is True                         # VAD silent → noise-only frame


# --------------------------------------------------------------------------- #
# Delay-and-sum beam selectivity
# --------------------------------------------------------------------------- #
def test_beam_selectivity_on_axis_vs_off_axis():
    geom = cc.sensibel_8(radius_m=0.040)
    block = _plane_wave_block(geom, 60.0, 44100.0, 4096)
    on = delay_and_sum_block(block, geom, 60.0, sample_rate=44100.0)
    off = delay_and_sum_block(block, geom, 150.0, sample_rate=44100.0)
    assert on.shape == (4096,)
    assert float((on ** 2).sum()) > 1.5 * float((off ** 2).sum())


def test_beam_block_handles_dead_capsule_mask():
    geom = cc.with_active_channels(cc.sensibel_8(radius_m=0.040),
                                   [i != 5 for i in range(8)])   # capsule 5 off → 7 active
    block = _plane_wave_block(geom, 60.0, 44100.0, 4096)
    on = delay_and_sum_block(block, geom, 60.0, sample_rate=44100.0)
    off = delay_and_sum_block(block, geom, 150.0, sample_rate=44100.0)
    assert on.shape == (4096,)
    assert float((on ** 2).sum()) > 1.5 * float((off ** 2).sum())


# --------------------------------------------------------------------------- #
# Fractional-delay strategy (sub-sample windowed-sinc steering, mode="fracdelay")
# --------------------------------------------------------------------------- #
def test_frac_delay_kernel_unity_dc_and_subsample_shift():
    for frac in (0.0, 0.25, 0.5, 0.75):
        h = pb._frac_delay_kernel(frac, 15)
        assert len(h) == 15 and len(h) % 2 == 1                  # odd → integral centre
        assert abs(float(h.sum()) - 1.0) < 1e-9                  # unity DC gain → no level shift
        centroid = float(np.sum(np.arange(len(h)) * h))          # ≈ group delay in samples
        assert abs(centroid - (7.0 + frac)) < 0.1                # centre=(15-1)/2=7, plus frac
    imp = pb._frac_delay_kernel(0.0, 15)
    assert int(imp.argmax()) == 7 and imp[7] > 0.999             # frac 0 ⇒ unit impulse at centre
    clamped = pb._frac_delay_kernel(0.5, 3)                      # 3 taps degenerate (hanning→[0,1,0]) ⇒ floored to 5
    assert len(clamped) == 5
    assert abs(float(np.sum(np.arange(5) * clamped)) - 2.5) < 0.05   # real 0.5-sample shift, not an impulse at 2


def test_steer_real_delays_round_to_integer_delays():
    geom = cc.sensibel_8(radius_m=0.040)
    idx_r, real = pb._steer_real_delays(geom, 73.0, 90.0, 44100.0, C)
    idx_i, di, maxd = pb._steer_delays(geom, 73.0, 90.0, 44100.0, C)
    assert idx_r == idx_i
    assert [int(round(d)) for d in real] == list(di)             # integer path = rounded real path
    assert maxd == max(di) and min(real) >= 0.0                  # delays are non-negative


def test_fracdelay_strategy_selectivity():
    geom = cc.sensibel_8(radius_m=0.040)
    block = _analytic_plane_wave(geom, 60.0, 44100.0, 8192, 3000.0)
    beam = pb._FracDelaySumBeam(geom, 44100.0, C)
    beam.set_look(60.0)
    on = beam.process(block)[64:]                                # skip the FIR/ring warm-up
    beam.set_look(150.0)
    off = beam.process(block)[64:]
    assert on.shape == (8192 - 64,)
    assert float((on ** 2).sum()) > 1.5 * float((off ** 2).sum())


def test_fracdelay_aligns_better_than_integer_off_grid():
    """On a fractional-lead plane wave the integer beam cannot fully align; the fractional one
    reconstructs the tone near-coherently (RMS → 1/√2 ≈ 0.707 for a unit sine)."""
    geom = cc.sensibel_8(radius_m=0.040)
    fs, n, az, freq = 44100.0, 8192, 53.0, 3400.0               # off-grid azimuth, in-band tone
    block = _analytic_plane_wave(geom, az, fs, n, freq)
    frac = pb._FracDelaySumBeam(geom, fs, C); frac.set_look(az)
    integ = pb._DelaySumBeam(geom, fs, C); integ.set_look(az)
    rms = lambda v: float(np.sqrt(np.mean(v * v)))
    yf, yi = rms(frac.process(block)[64:]), rms(integ.process(block)[64:])
    assert yf > 0.69                                            # fractional ⇒ near-ideal coherence
    assert yf > yi                                              # and better than integer rounding


def test_mode_fracdelay_builds_strategy_and_runs():
    bf = PolarisBeamformer(device=None, mode="fracdelay")
    assert bf.mode == "fracdelay"
    assert isinstance(bf._beam, pb._FracDelaySumBeam)
    bf._setup_runtime()
    out = bf.process_block(_analytic_plane_wave(bf.geometry, 0.0, bf.sample_rate, bf.blocksize, 1000.0))
    assert out.shape == (bf.blocksize,) and bool(np.all(np.isfinite(out)))
    bf.reset_transient()                                        # drops both strategy buffers
    assert bf._beam._hist is None and bf._beam._frac_tail is None


def test_fracdelay_streaming_continuity_across_blocks():
    """The realtime path calls process() once per audio block; the _frac_tail overlap-save and the
    _hist integer ring must keep the output identical to processing the whole signal at once — no
    per-block click at the block rate. Guards the load-bearing cross-block carry (a zeroed tail
    would still pass every other fracdelay test, which only ever feed one block)."""
    geom = cc.sensibel_8(radius_m=0.040)
    sig = _analytic_plane_wave(geom, 41.0, 44100.0, 4096, 2500.0)
    ref = pb._FracDelaySumBeam(geom, 44100.0, C); ref.set_look(41.0)
    whole = ref.process(sig)
    for chunk in (5, 256, 1000):                                # 5 < (taps-1) exercises the n < L1 tail path
        s = pb._FracDelaySumBeam(geom, 44100.0, C); s.set_look(41.0)
        streamed = np.concatenate([s.process(sig[i:i + chunk]) for i in range(0, len(sig), chunk)])
        assert streamed.shape == whole.shape
        assert np.allclose(streamed, whole, atol=1e-6), f"per-block discontinuity at chunk={chunk}"


# --------------------------------------------------------------------------- #
# Frequency-domain superdirective strategy (mode="superdirective")
# --------------------------------------------------------------------------- #
def test_superdirective_unit_gain_at_look():
    """The MVDR/superdirective constraint is unit gain toward the look direction: wᴴa(u0) = 1
    at every bin (R real-symmetric ⇒ exact)."""
    geom = cc.sensibel_8(radius_m=0.040)
    beam = pb._FreqDomainBeam(geom, 44100.0, C)
    beam.set_look(72.0)
    W, freqs = beam._W, beam._freqs
    elems = np.array(geom.elements, dtype=float)
    u = _unit(72.0)
    for bi in (0, 12, 60, 200):                                 # DC + in-band + above the cutoff
        k = 2.0 * np.pi * freqs[bi] / C
        a = np.exp(1j * k * (elems @ u))                        # (M,) manifold at the look
        resp = complex(np.sum(np.conj(W[bi]) * a))
        assert abs(abs(resp) - 1.0) < 1e-6


def test_superdirective_set_look_recomputes_weights():
    geom = cc.sensibel_8(radius_m=0.040)
    beam = pb._FreqDomainBeam(geom, 44100.0, C)
    beam.set_look(0.0)
    w0 = beam._W.copy()
    beam.set_look(90.0)
    assert not np.allclose(w0, beam._W)                         # re-solved for the new look (atomic swap)


def test_superdirective_selectivity():
    geom = cc.sensibel_8(radius_m=0.040)
    sig = _analytic_plane_wave(geom, 60.0, 44100.0, 8192, 2000.0)
    on_beam = pb._FreqDomainBeam(geom, 44100.0, C); on_beam.set_look(60.0)
    off_beam = pb._FreqDomainBeam(geom, 44100.0, C); off_beam.set_look(150.0)
    on = _run_blocks(on_beam, sig, 1411)[2048:]                 # skip the OLA warm-up (~one frame + prime)
    off = _run_blocks(off_beam, sig, 1411)[2048:]
    assert float((on ** 2).sum()) > 1.5 * float((off ** 2).sum())


def test_superdirective_block_size_adapter():
    """The input/output FIFO frames at a fixed 512-hop internally, so the mono stream is identical
    regardless of how the caller chunks the input."""
    geom = cc.sensibel_8(radius_m=0.040)
    sig = _analytic_plane_wave(geom, 35.0, 44100.0, 8192, 2500.0)
    a = pb._FreqDomainBeam(geom, 44100.0, C); a.set_look(35.0)
    b = pb._FreqDomainBeam(geom, 44100.0, C); b.set_look(35.0)
    out_a = _run_blocks(a, sig, 512)
    out_b = _run_blocks(b, sig, 1411)
    L = min(len(out_a), len(out_b))
    assert L > 4096
    assert np.allclose(out_a[:L], out_b[:L], atol=1e-6)         # block-size-agnostic


def test_mode_superdirective_builds_and_runs():
    bf = PolarisBeamformer(device=None, mode="superdirective")
    assert bf.mode == "superdirective"
    assert isinstance(bf._beam, pb._FreqDomainBeam)
    assert bf.superdirective_loading == pb.DEFAULT_SUPERDIRECTIVE_LOADING
    bf._setup_runtime()
    out = bf.process_block(_analytic_plane_wave(bf.geometry, 0.0, bf.sample_rate, bf.blocksize, 1500.0))
    assert out.shape == (bf.blocksize,) and bool(np.all(np.isfinite(out)))
    bf.reset_transient()                                        # drops the STFT FIFO/OLA state
    assert bf._beam._inq.shape[0] == 0 and bf._beam._outq.shape[0] == pb._STFT_FRAME


def test_superdirective_loading_zero_constructs():
    """loading=0 ('max directivity', invited by --loading help) must not crash — the DC bin's Γ is
    the rank-1 all-ones matrix and is singular without a diagonal floor."""
    bf = PolarisBeamformer(device=None, mode="superdirective", superdirective_loading=0.0)
    assert isinstance(bf._beam, pb._FreqDomainBeam)
    assert bf._beam._loading >= 1e-9                            # floored, so the solve stays valid
    assert bool(np.all(np.isfinite(bf._beam._W)))              # finite weights incl. the DC bin


def test_superdirective_plan_look_off_lock_then_commit_publishes():
    """plan_look does the heavy per-bin solve WITHOUT mutating shared state (so the owner can call
    it outside _beam_lock); commit_look installs it by a single atomic publish."""
    geom = cc.sensibel_8(radius_m=0.040)
    beam = pb._FreqDomainBeam(geom, 44100.0, C)
    beam.set_look(0.0)
    w0 = beam._W
    plan = beam.plan_look(90.0)                                 # computed off-lock — no mutation yet
    assert beam._W is w0                                        # _W untouched until commit
    beam.commit_look(plan)
    assert beam._W is plan and not np.allclose(w0, beam._W)     # atomic publish of the new weights


# --------------------------------------------------------------------------- #
# Data-adaptive MVDR (mode="mvdr") — measured noise covariance overlaid on the band
# --------------------------------------------------------------------------- #
def test_mvdr_nulls_measured_interferer():
    """The flagship: given a measured noise covariance dominated by an interferer at az_n, the MVDR
    weights null az_n far more than the fixed superdirective design while keeping unit gain at az_s."""
    geom = cc.sensibel_8(radius_m=0.040)
    fs = 44100.0
    freqs_full = np.fft.rfftfreq(1024, d=1.0 / fs)
    band = doa.band_indices(freqs_full, doa.DEFAULT_F_LO_HZ, doa.DEFAULT_F_HI_HZ)
    az_s, az_n = 40.0, 120.0
    Rn = _interferer_noise_cov(geom, az_n, band, freqs_full)
    mvdr = pb._FreqDomainBeam(geom, fs, C, noise_cov_provider=lambda: (Rn, band))
    mvdr.set_look(az_s)
    analytic = pb._FreqDomainBeam(geom, fs, C)                  # no provider -> fixed superdirective
    analytic.set_look(az_s)
    bi = int(band[len(band) // 2])                             # a mid-speech-band bin (~2 kHz)
    k = 2.0 * np.pi * freqs_full[bi] / C
    elems = np.array(geom.elements, dtype=float)
    a_s = np.exp(1j * k * (elems @ _unit(az_s)))
    a_n = np.exp(1j * k * (elems @ _unit(az_n)))
    resp = lambda W, a: abs(complex(np.sum(np.conj(W[bi]) * a)))
    assert abs(resp(mvdr._W, a_s) - 1.0) < 1e-6                # distortionless: unit gain at the look
    assert resp(mvdr._W, a_n) < 0.5 * resp(analytic._W, a_n)   # MVDR rejects the measured interferer
    assert resp(mvdr._W, a_n) < 0.3                            # to a low absolute level


def test_mode_mvdr_builds_and_cold_start_is_analytic():
    bf = PolarisBeamformer(device=None, mode="mvdr")
    assert bf.mode == "mvdr"
    assert isinstance(bf._beam, pb._FreqDomainBeam)
    assert bf._beam._noise_cov_provider is not None
    bf._setup_runtime()
    assert bf._noise_cov is not None                           # gated accumulator allocated for mvdr
    # cold start (no warmed noise frames): the provider returns None, so weights equal the fixed
    # superdirective design exactly -- graceful fallback.
    superd = pb._FreqDomainBeam(bf.geometry, bf.sample_rate, bf.speed_of_sound,
                                loading=bf.superdirective_loading)
    superd.set_look(30.0)
    bf._beam.set_look(30.0)
    assert np.allclose(bf._beam._W, superd._W)
    out = bf.process_block(_analytic_plane_wave(bf.geometry, 0.0, bf.sample_rate, bf.blocksize, 1500.0))
    assert out.shape == (bf.blocksize,) and bool(np.all(np.isfinite(out)))


def test_mvdr_noise_gate_accumulates_only_on_noise_frames():
    bf = PolarisBeamformer(device=None, mode="mvdr")
    bf._setup_runtime()
    rng = np.random.RandomState(0)
    blk = rng.standard_normal((bf.blocksize, 8)).astype(float)
    bf._noise_gate = False                                     # "speech" present -> freeze the noise estimate
    for _ in range(6):
        bf._accumulate_covariance(blk)
    assert bf._noise_frames == 0
    bf._noise_gate = True                                      # "noise-only" -> accumulate
    for _ in range(6):
        bf._accumulate_covariance(blk)
    assert bf._noise_frames > 0


def test_mvdr_provider_warmup_then_reset():
    bf = PolarisBeamformer(device=None, mode="mvdr")
    bf._setup_runtime()
    assert bf._noise_cov_snapshot() is None                    # cold: below the warmup gate
    rng = np.random.RandomState(1)
    blk = rng.standard_normal((bf.blocksize, 8)).astype(float)
    bf._noise_gate = True
    while bf._noise_frames < pb._NOISE_WARMUP_FRAMES:
        bf._accumulate_covariance(blk)
    snap = bf._noise_cov_snapshot()
    assert snap is not None
    R, band = snap
    assert R.shape == (len(band), 8, 8)
    bf.reset_transient()                                       # clears the gated noise state
    assert bf._noise_frames == 0 and bf._noise_cov_snapshot() is None


def test_mvdr_nondefault_nfft_aligns_bins():
    """The beam STFT frame is tied to the DOA nfft, so the measured-R overlay lands on the right
    bins at any nfft (regression: a hardcoded 1024 frame mis-mapped the cov or IndexError'd)."""
    bf = PolarisBeamformer(device=None, mode="mvdr", nfft=2048)
    assert bf._beam._F == 2048 and len(bf._beam._freqs) == 2048 // 2 + 1
    bf._setup_runtime()
    rng = np.random.RandomState(2)
    blk = rng.standard_normal((bf.blocksize, 8)).astype(float)
    bf._noise_gate = True
    while bf._noise_frames < pb._NOISE_WARMUP_FRAMES:
        bf._accumulate_covariance(blk)
    snap = bf._noise_cov_snapshot()
    assert snap is not None and int(np.max(snap[1])) < len(bf._beam._freqs)   # band indices in range
    plan = bf._beam.plan_look(30.0)                            # overlays measured R — no IndexError
    assert plan.shape[0] == 2048 // 2 + 1 and bool(np.all(np.isfinite(plan)))


# --------------------------------------------------------------------------- #
# Explicit LCMV nulls on the freq-domain beam (#13-1: auto-null on the steered beam)
# --------------------------------------------------------------------------- #
def _resp_at_bin(W, freqs, geom, bi, az):
    """|beam response| toward `az` at rfft bin `bi`: |Σ conj(W)·a(az)| (1 at the look, 0 at a null)."""
    elems = np.array(geom.elements, dtype=float)
    k = 2.0 * np.pi * freqs[bi] / C
    a = np.exp(1j * k * (elems @ _unit(az)))
    return abs(complex(np.sum(np.conj(W[bi]) * a)))


def _band_bins(freqs):
    band = doa.band_indices(freqs, doa.DEFAULT_F_LO_HZ, doa.DEFAULT_F_HI_HZ)
    return [int(band[len(band) // 4]), int(band[len(band) // 2]), int(band[-1])]


def test_lcmv_null_is_exact_and_keeps_unit_gain_at_look():
    """An explicit null places an EXACT zero at the interferer bearing (the LCMV constraint), while
    the look stays distortionless — across in-band bins."""
    geom = cc.sensibel_8(radius_m=0.040)
    az_s, az_n = 30.0, 110.0
    nulled = pb._FreqDomainBeam(geom, 44100.0, C); nulled.set_look(az_s, nulls=[az_n])
    plain = pb._FreqDomainBeam(geom, 44100.0, C); plain.set_look(az_s)
    freqs = nulled._freqs
    for bi in _band_bins(freqs):
        assert abs(_resp_at_bin(nulled._W, freqs, geom, bi, az_s) - 1.0) < 1e-6   # distortionless look
        assert _resp_at_bin(nulled._W, freqs, geom, bi, az_n) < 1e-6              # exact null at φ
        # and that's far below the un-nulled beam's response toward the interferer
        assert _resp_at_bin(nulled._W, freqs, geom, bi, az_n) < 1e-3 * max(
            _resp_at_bin(plain._W, freqs, geom, bi, az_n), 1e-12)


def test_lcmv_zero_or_filtered_nulls_match_plain_mvdr_bit_for_bit():
    """K=0 is the existing MVDR path unchanged; a null inside the look guard is filtered, so both
    produce identical weights to the no-nulls design."""
    geom = cc.sensibel_8(radius_m=0.040)
    plain = pb._FreqDomainBeam(geom, 44100.0, C); plain.set_look(45.0)
    empty = pb._FreqDomainBeam(geom, 44100.0, C); empty.set_look(45.0, nulls=[])
    near = pb._FreqDomainBeam(geom, 44100.0, C); near.set_look(45.0, nulls=[47.0])  # within 5° guard
    assert np.array_equal(plain._W, empty._W)                  # bit-identical (same K=0 path)
    assert np.array_equal(plain._W, near._W)                   # near-look null dropped → same weights


def test_lcmv_caps_nulls_at_m_minus_1_and_stays_finite():
    """More nulls than the M−1 budget: ``_acceptable_nulls`` truncates to M−1 (request order); the
    beam stays finite and distortionless. A 40 mm array can't form M−1 *exact* simultaneous nulls —
    fully constrained, the manifolds go near-collinear and the nulls land deep-but-not-exact — so the
    budget itself is checked on the pure filter, and the beam check only asserts deep nulls + finite."""
    geom = cc.sensibel_8(radius_m=0.040)                       # 8 active mics → budget 7 nulls
    many = [10.0, 40.0, 70.0, 100.0, 130.0, 160.0, 190.0, 220.0, 250.0]   # 9 distinct (>5° apart)
    assert pb._acceptable_nulls(many, 0.0, geom.n_active - 1) == many[:7]  # cap = M−1, in request order
    beam = pb._FreqDomainBeam(geom, 44100.0, C); beam.set_look(0.0, nulls=many)
    assert bool(np.all(np.isfinite(beam._W)))                 # the ridge keeps the maxed budget finite
    # (At the full M−1 budget the 8×8 constraint matrix is near-collinear on a 40 mm array, so both
    # look gain and null depth are conditioning-limited — exact gain/null is proven at a sane budget by
    # test_lcmv_null_is_exact_* and test_lcmv_nulls_compose_*; here we only assert it stays finite.)


def test_lcmv_nulls_compose_with_measured_mvdr():
    """In mvdr mode the explicit null and the measured-R interferer suppression coexist: distortionless
    look, exact zero at the explicit bearing, and the measured interferer still attenuated."""
    geom = cc.sensibel_8(radius_m=0.040)
    fs = 44100.0
    freqs = np.fft.rfftfreq(1024, d=1.0 / fs)
    band = doa.band_indices(freqs, doa.DEFAULT_F_LO_HZ, doa.DEFAULT_F_HI_HZ)
    az_s, az_meas, az_expl = 40.0, 120.0, 210.0
    Rn = _interferer_noise_cov(geom, az_meas, band, freqs)
    beam = pb._FreqDomainBeam(geom, fs, C, noise_cov_provider=lambda: (Rn, band))
    beam.set_look(az_s, nulls=[az_expl])
    plain = pb._FreqDomainBeam(geom, fs, C); plain.set_look(az_s)   # no provider, no nulls
    bi = int(band[len(band) // 2])
    assert abs(_resp_at_bin(beam._W, freqs, geom, bi, az_s) - 1.0) < 1e-6     # distortionless
    assert _resp_at_bin(beam._W, freqs, geom, bi, az_expl) < 1e-6             # explicit LCMV null exact
    assert _resp_at_bin(beam._W, freqs, geom, bi, az_meas) < _resp_at_bin(plain._W, freqs, geom, bi, az_meas)


def test_time_domain_modes_ignore_nulls():
    """delaysum / fracdelay have no null degrees of freedom — passing nulls must not change the plan."""
    geom = cc.sensibel_8(radius_m=0.040)
    ds = pb._DelaySumBeam(geom, 44100.0, C)
    assert ds.plan_look(30.0) == ds.plan_look(30.0, nulls=[120.0])
    fd = pb._FracDelaySumBeam(geom, 44100.0, C)
    p0, p1 = fd.plan_look(30.0), fd.plan_look(30.0, nulls=[120.0])
    assert p0[0] == p1[0] and p0[1] == p1[1] and p0[2] == p1[2]               # idx / int delays / maxd
    assert all(np.array_equal(a, b) for a, b in zip(p0[3], p1[3]))            # fractional FIR kernels


# --------------------------------------------------------------------------- #
# Null-budget arbitration: detected interferers win, seats fill the remainder (P2a.1)
# --------------------------------------------------------------------------- #
def test_compose_nulls_budget_cap_detected_win_seats_fill():
    det = [10.0, 40.0, 70.0]
    seats = [100.0, 130.0, 160.0, 190.0, 220.0, 250.0]
    final = pb.compose_nulls(det, seats, 0.0, 7)
    assert len(final) == 7
    assert all(d in final for d in det)                  # every detected null kept
    assert sum(1 for s in seats if s in final) == 4      # exactly 4 of 6 seats fill the remainder


def test_compose_nulls_detected_never_displaced_by_seats():
    det = [10.0, 40.0, 70.0, 100.0, 130.0, 160.0, 190.0]   # 7 = the full M−1 budget
    final = pb.compose_nulls(det, [220.0, 250.0], 0.0, 7)
    assert final == det                                  # all detected, zero seats, none dropped


def test_compose_nulls_drops_nulls_near_the_look_from_both_lists():
    final = pb.compose_nulls([5.0, 120.0], [3.0, 200.0], 0.0, 7, min_sep_deg=8.0)
    assert 5.0 not in final and 3.0 not in final         # within 8° of the 0° look → dropped
    assert final == [120.0, 200.0]                       # budget not consumed by the dropped ones


def test_compose_nulls_cross_source_dedupe():
    final = pb.compose_nulls([120.0], [122.0, 200.0], 0.0, 7, merge_sep_deg=4.0)
    assert 122.0 not in final                            # ~coincident with the detected 120° → dropped
    assert final == [120.0, 200.0]                       # one constraint for that null, not two


def test_compose_nulls_seat_self_cap_reserves_headroom():
    final = pb.compose_nulls([120.0], [40.0, 70.0, 100.0, 160.0, 200.0], 0.0, 7, seat_null_max_count=2)
    assert final[0] == 120.0 and len(final) == 3         # 1 detected + 2 seats (nearest-to-look)
    assert final[1:] == [40.0, 70.0]


def test_compose_nulls_degenerate_paths():
    assert pb.compose_nulls([], [], 0.0, 7) == []
    assert pb.compose_nulls([40.0, 120.0], [], 0.0, 7) == [40.0, 120.0]   # seats=[] → #13-only behaviour
    assert pb.compose_nulls([], [200.0, 40.0], 0.0, 7) == [40.0, 200.0]   # seats ordered nearest-to-look


def test_compose_nulls_is_deterministic():
    det, seats = [10.0, 250.0], [100.0, 40.0, 160.0]
    assert pb.compose_nulls(det, seats, 0.0, 7) == pb.compose_nulls(det, seats, 0.0, 7)


# --------------------------------------------------------------------------- #
# Auto-null wiring on the steered path (#13-2: detection → null hand-off)
# --------------------------------------------------------------------------- #
def test_compose_nulls_method_time_domain_empty_freq_domain_composes():
    """The PolarisBeamformer wrapper: time-domain modes have no null DOF (empty); the freq-domain modes
    route detected + seat nulls through the compose_nulls arbiter (detected win the budget, the seat
    null fills after them, and the look's own detected source is dropped near the look)."""
    ds = PolarisBeamformer(device=None, mode="delaysum", auto_null=True)
    ds.set_nulls([90.0, 200.0])
    assert ds._compose_nulls([SimpleNamespace(azimuth_deg=120.0, salience_db=9.0)], 30.0) == []
    assert ds._nulls_engaged() is False
    sd = PolarisBeamformer(device=None, mode="superdirective", auto_null=True)
    sd.set_nulls([300.0])                                      # a seat / manual null
    dets = [SimpleNamespace(azimuth_deg=30.0, salience_db=20.0),   # the look's source → dropped near-look
            SimpleNamespace(azimuth_deg=120.0, salience_db=10.0),  # detected interferer (priority)
            SimpleNamespace(azimuth_deg=210.0, salience_db=6.0)]   # detected interferer (priority)
    assert sd._compose_nulls(dets, 30.0) == [120.0, 210.0, 300.0]  # detected first, the seat fills after
    assert sd._nulls_engaged() is True
    sd.auto_null = False
    sd.set_nulls(None)
    assert sd._compose_nulls(dets, 30.0) == [] and sd._nulls_engaged() is False


def test_compose_nulls_excludes_the_drifted_tracked_talker():
    """Regression: the tracked talker's raw detection can drift up to switch_margin (20°) from the
    COMMITTED look before the tracker re-steers — it must NOT be nulled. Only sources past the switch
    margin are interferers (a real interferer is ≥ min_separation_deg=40 away, so it survives)."""
    bf = PolarisBeamformer(device=None, mode="superdirective", auto_null=True)   # default switch_margin 20
    dets = [SimpleNamespace(azimuth_deg=15.0, salience_db=20.0),    # tracked talker drifted 15° from look 0
            SimpleNamespace(azimuth_deg=135.0, salience_db=10.0)]   # a real interferer
    assert bf._compose_nulls(dets, 0.0) == [135.0]                  # talker (15°) excluded, interferer nulled
    # the switch margin (not the small conditioning margin) is what bounds the talker exclusion:
    assert pb._az_sep(15.0, 0.0) < bf._switch_margin_deg and pb._az_sep(135.0, 0.0) >= bf._switch_margin_deg


def test_detect_dominant_max_talkers_scales_with_auto_null(monkeypatch):
    """auto_null asks SRP-PHAT for extra peaks (dominant + interferers); otherwise just the dominant."""
    seen = {}
    def fake_detect(cov, freqs, geom, **kw):
        seen["max"] = kw.get("max_talkers")
        return SimpleNamespace(active=False, detections=[])
    monkeypatch.setattr(doa, "detect", fake_detect)
    PolarisBeamformer(device=None, mode="superdirective", auto_null=False)._detect_dominant(object(), FREQS)
    assert seen["max"] == 1
    PolarisBeamformer(device=None, mode="superdirective", auto_null=True, auto_null_max=3)._detect_dominant(object(), FREQS)
    assert seen["max"] == 4                                    # 1 dominant + 3 interferers


def test_doa_tick_auto_null_commits_a_beam_that_nulls_the_interferer(monkeypatch):
    """End-to-end wiring: a tick with a dominant at 30° and an interferer at 120° commits a beam that
    is distortionless at 30° and nulls 120°, and reports it via active_nulls."""
    bf = PolarisBeamformer(device=None, mode="superdirective", auto_null=True)
    monkeypatch.setattr(bf, "_snapshot_covariance", lambda: (object(), FREQS))
    dets = [SimpleNamespace(azimuth_deg=30.0, salience_db=20.0),
            SimpleNamespace(azimuth_deg=120.0, salience_db=10.0)]
    monkeypatch.setattr(doa, "detect", lambda *a, **k: SimpleNamespace(active=True, detections=dets))
    bf.set_steering(30.0)                                      # pin the look (deterministic, no smoothing)
    bf._doa_tick()
    assert bf.active_nulls == [120.0]
    W, freqs, geom = bf._beam._W, bf._beam._freqs, bf.geometry
    bi = _band_bins(freqs)[1]
    assert abs(_resp_at_bin(W, freqs, geom, bi, 30.0) - 1.0) < 1e-6   # distortionless at the look
    assert _resp_at_bin(W, freqs, geom, bi, 120.0) < 1e-6             # interferer nulled exactly


def test_active_nulls_reports_only_the_nulls_actually_applied():
    """Telemetry matches the committed beam: a requested null within 5° of the look (dropped by the
    LCMV filter) is NOT advertised by active_nulls."""
    bf = PolarisBeamformer(device=None, mode="superdirective")
    bf.set_nulls([33.0, 120.0])                  # 33° is within 5° of the 30° look → filtered out
    bf.set_steering(30.0)                        # pins the look and publishes active_nulls via the filter
    assert bf.active_nulls == [120.0]            # only the actually-applied null is reported


# --------------------------------------------------------------------------- #
# Constructor / geometry / mask resolution
# --------------------------------------------------------------------------- #
def test_constructor_geometry_and_defaults():
    bf = PolarisBeamformer(device=None)
    assert bf.geometry.n_channels == 8
    assert bf.geometry.n_active == 8                   # default: all 8 active
    assert bf.blocksize == round(44100 * 0.032)        # ~32 ms
    assert abs(bf.geometry.aperture_m() - 0.080) < 1e-6
    assert bf.backend == "polaris"


def test_dead_capsule_and_active_mask_override():
    bf_dead = PolarisBeamformer(device=None, dead_capsule=5)
    assert bf_dead.geometry.n_active == 7
    assert bf_dead.geometry.active_indices() == (0, 1, 2, 3, 4, 6, 7)
    mask = [i != 2 for i in range(8)]
    bf_mask = PolarisBeamformer(device=None, active_mask=mask, dead_capsule=5)
    assert bf_mask.geometry.active_indices() == (0, 1, 3, 4, 5, 6, 7)  # active_mask wins


def test_invalid_masks_and_mode_raise():
    with pytest.raises(ValueError):
        PolarisBeamformer(device=None, active_mask=[True] * 7)        # wrong length
    with pytest.raises(ValueError):
        PolarisBeamformer(device=None, active_mask=[False] * 8)       # all off
    with pytest.raises(ValueError):
        PolarisBeamformer(device=None, mode="lcmv")                   # unknown mode (not in _BEAM_MODES)


# --------------------------------------------------------------------------- #
# Beam output band-limit (windowed-sinc FIR, on by default)
# --------------------------------------------------------------------------- #
def test_lowpass_kernel_unity_dc_and_rejects_hf():
    fs = 44100.0
    h = _lowpass_kernel(DEFAULT_BEAM_BANDLIMIT_HZ, fs)
    assert len(h) % 2 == 1                                  # odd → exact linear phase
    assert abs(float(h.sum()) - 1.0) < 1e-9                 # unity DC gain

    def resp(f):                                            # FIR magnitude response at f
        n = np.arange(len(h))
        return abs(complex(np.sum(h * np.exp(-2j * np.pi * f / fs * n))))

    assert resp(1000.0) > 0.9                               # passband: speech preserved
    assert resp(9000.0) < 0.1                               # stopband: aliased band killed


def test_beam_bandlimit_default_on_and_disable():
    on = PolarisBeamformer(device=None)
    assert on.beam_bandlimit_hz == DEFAULT_BEAM_BANDLIMIT_HZ       # default = aliasing cutoff
    on._setup_runtime()
    assert on._lp_kernel is not None and on._lp_tail is not None
    off = PolarisBeamformer(device=None, beam_bandlimit_hz=None)   # opt out
    off._setup_runtime()
    assert off._lp_kernel is None and off._lp_tail is None


def test_process_block_bandlimit_attenuates_high_band():
    fs = 44100.0
    on = PolarisBeamformer(device=None)                            # FIR on (default)
    off = PolarisBeamformer(device=None, beam_bandlimit_hz=None)   # same beam, no FIR
    on._setup_runtime()
    off._setup_runtime()
    # Identical delay-and-sum beam (both look at the initial 0°); the only delta is the FIR.
    hf = _plane_wave_block(on.geometry, 0.0, fs, on.blocksize, tones=(9000.0,))   # above the cutoff
    lf = _plane_wave_block(on.geometry, 0.0, fs, on.blocksize, tones=(800.0,))    # well in band
    for _ in range(3):                                             # prime the FIR history ring
        y_on, y_off = on.process_block(hf), off.process_block(hf)
    assert float((y_on ** 2).sum()) < 0.2 * float((y_off ** 2).sum())   # HF strongly cut
    for _ in range(3):
        z_on, z_off = on.process_block(lf), off.process_block(lf)
    assert float((z_on ** 2).sum()) > 0.7 * float((z_off ** 2).sum())   # LF preserved


# --------------------------------------------------------------------------- #
# Target-loudness AGC on the beam output (P2b)
# --------------------------------------------------------------------------- #
def _agc_rms(x):
    return float(np.sqrt(np.mean(x * x))) if x.size else 0.0


def test_agc_normalizes_quiet_and_loud_output_toward_target():
    """A quiet vs a loud talker both land at the target output loudness after the gain ramps.
    Band-limit off so the only processing is beam + AGC and steady-state RMS == the target exactly."""
    fs = 44100.0
    target_db = -20.0
    target_rms = 10.0 ** (target_db / 20.0)                              # 0.1
    base = _plane_wave_block(PolarisBeamformer(device=None).geometry, 0.0, fs, 1411)
    for scale in (0.05, 0.5):                                            # a quiet then a loud source (both within ±18 dB)
        bf = PolarisBeamformer(device=None, agc_target_db=target_db, beam_bandlimit_hz=None)
        bf._setup_runtime()
        blk = scale * base
        out = None
        for _ in range(150):                                            # let the EMA-slewed gain settle
            out = bf.process_block(blk)
        assert abs(_agc_rms(out) - target_rms) / target_rms < 0.08       # converged to target either way
        assert bf._agc_gain_min <= bf._agc_gain.value <= bf._agc_gain_max  # gain stayed inside the clamp


def test_agc_apply_clamps_gain_and_holds_through_silence():
    """Unit-level on _apply_agc (deterministic, no beam): the gain clamps at ±agc_max_gain_db and a
    silent block HOLDS the current gain instead of ramping the noise floor up to the clamp."""
    # (a) signal far BELOW target → desired gain saturates at +max; output never reaches target
    quiet = PolarisBeamformer(device=None, agc_target_db=-20.0, agc_max_gain_db=18.0)
    quiet._np = np
    q = np.full(512, 0.005, dtype=np.float32)                            # rms 0.005, above the -55 dB floor
    last = None
    for _ in range(150):
        last = quiet._apply_agc(q)
    assert abs(quiet._agc_gain.value - quiet._agc_gain_max) / quiet._agc_gain_max < 0.02   # pinned at +18 dB
    assert _agc_rms(last) < 10.0 ** (-20.0 / 20.0)                       # clamp kept it short of target

    # (b) signal far ABOVE target → desired gain saturates at -max
    loud = PolarisBeamformer(device=None, agc_target_db=-20.0, agc_max_gain_db=18.0)
    loud._np = np
    big = np.full(512, 0.9, dtype=np.float32)
    for _ in range(150):
        loud._apply_agc(big)
    assert abs(loud._agc_gain.value - loud._agc_gain_min) / loud._agc_gain_min < 0.02      # pinned at -18 dB

    # (c) after the gain has moved, DIGITAL SILENCE holds it (does not pump back up / to the clamp)
    held_gain = loud._agc_gain.value
    for _ in range(50):
        loud._apply_agc(np.zeros(512, dtype=np.float32))
    assert abs(loud._agc_gain.value - held_gain) < 1e-6                  # frozen through silence

    # (d) starting in silence holds unity — the floor never gets boosted
    cold = PolarisBeamformer(device=None, agc_target_db=-20.0)
    cold._np = np
    for _ in range(50):
        cold._apply_agc(np.zeros(512, dtype=np.float32))
    assert abs(cold._agc_gain.value - 1.0) < 1e-9


def test_agc_off_is_a_noop_and_reset_rebinds_gain():
    fs = 44100.0
    base = 0.3 * _plane_wave_block(PolarisBeamformer(device=None).geometry, 0.0, fs, 1411)
    off = PolarisBeamformer(device=None, beam_bandlimit_hz=None)         # agc default None
    ref = PolarisBeamformer(device=None, beam_bandlimit_hz=None)         # identical, agc None
    on = PolarisBeamformer(device=None, beam_bandlimit_hz=None, agc_target_db=-20.0)
    for bf in (off, ref, on):
        bf._setup_runtime()
    assert off._agc_gain is None and on._agc_gain is not None
    o = off.process_block(base)
    r = ref.process_block(base)
    n = on.process_block(base)
    assert np.array_equal(o, r)                                          # agc-off path is the unchanged beam output
    assert not np.allclose(o, n)                                         # agc-on scaled it (here: a loud block, down)

    # reset_transient rebinds a FRESH slew tracker (mirrors _tracker) so a re-activated beam doesn't replay gain
    assert on._agc_gain.value is not None                               # has adapted
    on.reset_transient()
    assert on._agc_gain is not None and on._agc_gain.value is None       # fresh, re-acquires on next block


def test_doa_tick_yields_to_a_concurrent_set_steering(monkeypatch):
    """Steering epoch (review HIGH fix): if a set_steering lands DURING a DOA tick's off-lock solve, the
    tick's now-stale commit is skipped — it can't clobber the just-applied seat lock."""
    bf = PolarisBeamformer(device=None, mode="superdirective")
    bf._setup_runtime()
    bf.steer_to_doa = True
    for _ in range(8):                              # accumulate a covariance with a talker at ~100°
        bf.process_block(_plane_wave_block(bf.geometry, 100.0, bf.sample_rate, bf.blocksize))
    real_plan = bf._beam.plan_look

    def racing_plan(az, off=90.0, nulls=()):
        plan = real_plan(az, off, nulls)
        with bf._beam_lock:                         # simulate a concurrent set_steering(45°) landing mid-solve
            bf._steer_gen += 1
            bf._steered_az = 45.0
        return plan

    monkeypatch.setattr(bf._beam, "plan_look", racing_plan)
    bf._doa_tick()
    assert bf._steered_az == 45.0                   # the lock survived; the stale DOA commit was dropped


# --------------------------------------------------------------------------- #
# Post-beam noise suppression (P3) — single-channel spectral gate on the mono output
# --------------------------------------------------------------------------- #
_NR_FLOOR = 10.0 ** (-15.0 / 20.0)        # default post_nr_floor_db = -15 dB → linear floor


def _nr_rms(x):
    return float(np.sqrt(np.mean(x * x))) if x.size else 0.0


def test_post_nr_builds_runs_shape_finite():
    bf = PolarisBeamformer(device=None, post_nr=True, post_nr_warmup_frames=2)
    assert bf.post_nr is True
    bf._setup_runtime()
    assert isinstance(bf._post_nr, pb._LevelPreservingCleaner)            # makeup wraps the gate
    assert isinstance(bf._post_nr._inner, pb._PostNoiseSuppressor)
    out = bf.process_block(_analytic_plane_wave(bf.geometry, 0.0, bf.sample_rate, bf.blocksize, 2000.0))
    assert out.shape == (bf.blocksize,) and bool(np.all(np.isfinite(out)))


def test_post_nr_off_is_a_noop():
    fs = 44100.0
    base = 0.2 * _plane_wave_block(PolarisBeamformer(device=None).geometry, 0.0, fs, 1411)
    off = PolarisBeamformer(device=None, beam_bandlimit_hz=None)            # post_nr defaults False
    ref = PolarisBeamformer(device=None, beam_bandlimit_hz=None)
    on = PolarisBeamformer(device=None, beam_bandlimit_hz=None, post_nr=True, post_nr_warmup_frames=2)
    for bf in (off, ref, on):
        bf._setup_runtime()
    on._noise_gate = True                                                  # simulate DOA-confirmed noise (no thread here)
    assert off._post_nr is None and on._post_nr is not None
    o = r = n = None
    for _ in range(8):                                                      # let `on` engage and diverge
        o, r, n = off.process_block(base), ref.process_block(base), on.process_block(base)
    assert np.array_equal(o, r)                                            # off path is the unchanged beam
    assert not np.allclose(o, n)                                           # NR engaged and changed the output


def test_post_nr_warmup_passthrough_is_byte_identical():
    fs = 44100.0
    base = 0.2 * _plane_wave_block(PolarisBeamformer(device=None).geometry, 0.0, fs, 1411)
    ref = PolarisBeamformer(device=None, beam_bandlimit_hz=None)                      # no NR
    warm = PolarisBeamformer(device=None, beam_bandlimit_hz=None, post_nr=True,
                             post_nr_warmup_frames=10_000)                            # never engages here
    ref._setup_runtime()
    warm._setup_runtime()
    for _ in range(6):
        assert np.array_equal(ref.process_block(base), warm.process_block(base))      # bypass = byte-identical
    assert warm._post_nr._engaged is False


def test_post_nr_attenuates_noise_after_warmup():
    rng = np.random.default_rng(0)
    nr = pb._PostNoiseSuppressor(44100.0, frame=512, floor_db=-15.0, oversub=1.5, warmup_frames=8)
    for _ in range(40):
        nr.process((0.05 * rng.standard_normal(1411)).astype(float), True)            # learn floor → engage
    assert nr._engaged
    blk = (0.05 * rng.standard_normal(4096)).astype(float)
    out = np.concatenate([nr.process(blk[i:i + 512], True) for i in range(0, 4096, 512)])
    rin, rout = _nr_rms(blk), _nr_rms(out)
    assert 0.3 * rin < rout < 0.8 * rin              # meaningfully suppressed, but NOT hard-muted to silence


def test_post_nr_preserves_above_floor_tone():
    rng = np.random.default_rng(1)
    nr = pb._PostNoiseSuppressor(44100.0, frame=512, floor_db=-15.0, oversub=1.5, warmup_frames=8)
    for _ in range(40):
        nr.process((0.03 * rng.standard_normal(1411)).astype(float), True)            # learn a LOW floor
    assert nr._engaged
    t = np.arange(8192) / 44100.0
    tone = (0.4 * np.sin(2 * np.pi * 1200.0 * t)).astype(float)                        # well above the floor
    out = np.concatenate([nr.process(tone[i:i + 512], False) for i in range(0, 8192, 512)])  # floor frozen
    assert _nr_rms(out[1024:]) > 0.85 * _nr_rms(tone[1024:])    # near-distortionless (Wiener G≈1 when P≫N²)


def test_post_nr_gain_is_bounded_by_the_floor():
    """Gentle by construction: even against strong noise + heavy over-subtraction, no bin is ever
    pushed below the floor (never hard-mutes → no musical noise)."""
    rng = np.random.default_rng(2)
    nr = pb._PostNoiseSuppressor(44100.0, frame=512, floor_db=-15.0, oversub=3.0, warmup_frames=4)
    for _ in range(60):
        nr.process((0.1 * rng.standard_normal(1411)).astype(float), True)
    assert nr._engaged
    assert float(np.min(nr._gain_prev)) >= _NR_FLOOR - 1e-9                            # bounded below by g_floor


def test_post_nr_block_size_invariance():
    """The FIFO frames at a fixed hop, so the suppressed stream is identical regardless of how the
    caller chunks the input (warmup 0 so both engage on frame 1)."""
    rng = np.random.default_rng(3)
    sig = (0.05 * rng.standard_normal(8192)).astype(float)
    a = pb._PostNoiseSuppressor(44100.0, frame=512, warmup_frames=0)
    b = pb._PostNoiseSuppressor(44100.0, frame=512, warmup_frames=0)
    out_a = np.concatenate([a.process(sig[i:i + 512], True) for i in range(0, 8192, 512)])
    out_b = np.concatenate([b.process(sig[i:i + 1411], True) for i in range(0, 8192, 1411)])
    L = min(len(out_a), len(out_b))
    assert L > 4096
    assert np.allclose(out_a[1024:L], out_b[1024:L], atol=1e-6)                        # chunk-size-agnostic


def test_post_nr_process_reset_are_length_safe_under_concurrency():
    """Review fix (HIGH): the suppressor's lock serializes process() (audio thread) vs reset() (control
    thread, via BeamEngine set_mode→reset_transient), so process() always returns exactly n samples even
    under a concurrent reset storm — a torn FIFO read would otherwise emit a wrong-length block and crash
    the BeamEngine crossfade mix."""
    import threading
    nr = pb._PostNoiseSuppressor(44100.0, frame=512, warmup_frames=0)
    rng = np.random.default_rng(11)
    blocks = [(0.05 * rng.standard_normal(1411)).astype(float) for _ in range(160)]
    stop = threading.Event()

    def resetter():
        while not stop.is_set():
            nr.reset()

    t = threading.Thread(target=resetter, daemon=True)
    t.start()
    try:
        for b in blocks:
            assert nr.process(b, True).shape == (1411,)       # exactly n — never a torn-FIFO wrong length
    finally:
        stop.set()
        t.join(timeout=2.0)


def test_post_nr_reset_transient_wipes_state():
    bf = PolarisBeamformer(device=None, beam_bandlimit_hz=None, post_nr=True, post_nr_warmup_frames=2)
    bf._setup_runtime()
    bf._noise_gate = True                                                  # simulate DOA-confirmed noise (no thread here)
    for _ in range(6):
        bf.process_block(_plane_wave_block(bf.geometry, 0.0, bf.sample_rate, bf.blocksize))
    nr = bf._post_nr
    assert nr._engaged and nr._total_frames > 0                            # min-stat engages on total frames
    bf.reset_transient()
    assert bf._post_nr is nr                                                           # reset in place
    assert (not nr._engaged) and nr._noise_frames == 0 and nr._total_frames == 0
    assert float(np.max(np.abs(nr._noise_mag))) == 0.0
    assert nr._inq.shape[0] == 0 and bool(np.all(nr._outq == 0.0))        # FIFO drained (primed zeros)
    assert bool(np.all(nr._gain_prev == 1.0))


def test_post_nr_does_not_train_on_speech_when_gate_is_false():
    """LEGACY (post_nr_minstat=False) cold-start safety: `_noise_gate` starts False (unknown ⇒ don't train),
    so until the DOA thread confirms a noise-only frame the gated-EMA NR never learns/engages — an active
    talker passes through byte-identical and the floor can't train on speech."""
    fs = 44100.0
    tone = 0.3 * _plane_wave_block(PolarisBeamformer(device=None).geometry, 0.0, fs, 1411)
    ref = PolarisBeamformer(device=None, beam_bandlimit_hz=None)                       # no NR
    on = PolarisBeamformer(device=None, beam_bandlimit_hz=None, post_nr=True,
                           post_nr_minstat=False, post_nr_warmup_frames=2)             # legacy gated EMA
    ref._setup_runtime()
    on._setup_runtime()
    assert on._noise_gate is False                                                     # default: unknown ⇒ don't train
    for _ in range(8):
        assert np.array_equal(ref.process_block(tone), on.process_block(tone))         # talker untouched
    assert on._post_nr._engaged is False and float(np.max(on._post_nr._noise_mag)) == 0.0


def test_post_nr_minstat_learns_steady_noise_without_the_gate():
    """The fix: with minimum statistics (default), the NR learns the steady noise floor and engages even
    when `noise_gate` stays FALSE the whole time (a steady directional fan reads as a talker) — where the
    legacy gated-EMA path stays byte-identical and never suppresses."""
    rng = np.random.default_rng(3)
    blocks = [0.05 * rng.standard_normal(512) for _ in range(120)]
    nr = pb._PostNoiseSuppressor(44100.0, frame=512, warmup_frames=8, minstat=True)       # default
    out = [nr.process(b, noise_gate=False) for b in blocks]                            # VAD 'active' throughout
    assert nr._engaged                                                                 # engaged with no gated frame
    rout = _nr_rms(np.concatenate(out[60:]))
    assert 0.3 * 0.05 < rout < 0.85 * 0.05                                             # steady noise attenuated
    # the legacy path, same input + gate False, never engages → byte-identical passthrough
    leg = pb._PostNoiseSuppressor(44100.0, frame=512, warmup_frames=8, minstat=False)
    assert np.array_equal(leg.process(blocks[0], noise_gate=False), blocks[0].astype(np.float32))
    assert not leg._engaged


def test_post_nr_minstat_preserves_speech():
    """Min-stat keeps speech: a strong tone sits ABOVE the learned per-bin minimum, so after warming on
    steady low noise the tone passes near-distortionless (the floor doesn't rise to brief speech)."""
    rng = np.random.default_rng(4)
    nr = pb._PostNoiseSuppressor(44100.0, frame=512, warmup_frames=8, minstat=True)
    for _ in range(40):
        nr.process(0.01 * rng.standard_normal(512), noise_gate=False)                 # learn a low floor
    t = np.arange(4096) / 44100.0
    tone = (0.3 * np.sin(2 * np.pi * 700.0 * t)).astype(float)
    out = nr.process(tone, noise_gate=False)
    assert _nr_rms(out[1024:]) > 0.85 * _nr_rms(tone[1024:])                           # near-distortionless


def test_post_nr_floor_db_positive_never_boosts():
    """floor_db > 0 is clamped at construction so the floor can never exceed unity (a gate attenuates,
    it must never amplify the noise)."""
    rng = np.random.default_rng(7)
    nr = pb._PostNoiseSuppressor(44100.0, frame=512, floor_db=6.0, oversub=1.5, warmup_frames=4)
    assert nr._g_floor <= 1.0                                                          # clamped at construction
    for _ in range(40):
        nr.process((0.05 * rng.standard_normal(1411)).astype(float), True)
    assert nr._engaged and float(np.max(nr._gain_prev)) <= 1.0 + 1e-9                  # no bin is boosted


def test_post_nr_frame_param_plumbed():
    bf = PolarisBeamformer(device=None, post_nr=True, post_nr_frame=1024)
    bf._setup_runtime()
    assert bf._post_nr._F == 1024 and len(bf._post_nr._freqs) == 1024 // 2 + 1
    assert pb._PostNoiseSuppressor(44100.0, frame=513)._F == 512                       # odd frame floored to even (COLA)


# --------------------------------------------------------------------------- #
# Error handling (device validation, missing extra)
# --------------------------------------------------------------------------- #
def test_device_not_found_raises(monkeypatch):
    monkeypatch.setattr(pb, "controls_available", lambda: True)
    monkeypatch.setattr(pb, "list_input_devices", lambda: [InputDevice(7, "POLARIS", 8, 44100.0)])
    bf = PolarisBeamformer(device=99)
    with pytest.raises(ValueError, match="not found"):
        bf.connect()


def test_too_few_channels_raises(monkeypatch):
    monkeypatch.setattr(pb, "controls_available", lambda: True)
    monkeypatch.setattr(pb, "list_input_devices", lambda: [InputDevice(3, "Stereo Mic", 2, 44100.0)])
    bf = PolarisBeamformer(device=3)
    # structural (present but wrong) → DeviceConfigError, which is still a ValueError
    with pytest.raises(DeviceConfigError, match="needs 8"):
        bf.connect()


def test_missing_extra_raises_install_hint(monkeypatch):
    monkeypatch.setattr(pb, "controls_available", lambda: False)
    bf = PolarisBeamformer(device=None)
    with pytest.raises(RuntimeError, match=r"\[control\]"):
        bf.connect()


# --------------------------------------------------------------------------- #
# Output delivery (queue drop-oldest + callback)
# --------------------------------------------------------------------------- #
def test_output_queue_drop_oldest_and_callback():
    seen = []
    bf = PolarisBeamformer(device=None, output_queue_size=2, output_callback=seen.append)
    for i in range(4):
        bf._emit(np.full(4, float(i), dtype=np.float32))
    q = bf.output_queue
    got = [q.get_nowait() for _ in range(q.qsize())]
    assert len(got) == 2                                # bounded to newest 2
    assert got[0][0] == 2.0 and got[1][0] == 3.0        # oldest dropped, newest kept
    assert [b[0] for b in seen] == [0.0, 1.0, 2.0, 3.0]  # callback fired per block


# --------------------------------------------------------------------------- #
# Lifecycle parity (streams stubbed — no hardware)
# --------------------------------------------------------------------------- #
def test_lifecycle_parity_and_start_stop(monkeypatch):
    bf = PolarisBeamformer(device=None)
    assert bf.read_level() == 0.0                       # disconnected → 0
    # stub the hardware-touching hooks so start()/stop() run without a device
    monkeypatch.setattr(bf, "_open", lambda: None)
    monkeypatch.setattr(bf, "_close", lambda: None)

    bf.stop()                                           # safe before start
    bf.start()
    assert bf.connected
    bf._level = 0.5
    assert abs(bf.read_level() - 0.5) < 1e-6
    bf.set_mute(True)
    assert bf.read_level() == 0.0
    bf.set_mute(False)
    bf.set_gain_db(6.0)
    assert bf.read_level() > 0.5                        # +6 dB ≈ ×2 (clamped ≤ 1)
    assert bf.state().backend == "polaris"
    assert bf.state().active_channels == 8

    bf.stop()
    assert not bf.connected
    assert bf._doa_thread is None
    bf.stop()                                           # idempotent


def test_set_steering_overrides_and_resumes():
    bf = PolarisBeamformer(device=None)
    bf.set_steering(45.0)
    assert not bf.steer_to_doa and bf._steered_az == 45.0
    bf.set_steering(None)
    assert bf.steer_to_doa


# --------------------------------------------------------------------------- #
# Device supervision: wait-for-device + auto-reconnect (wait_for_device=True)
# --------------------------------------------------------------------------- #
def test_streaming_false_until_open():
    assert PolarisBeamformer(device=None).streaming is False


def test_supervisor_retries_until_device_opens(monkeypatch):
    bf = PolarisBeamformer(device=7, wait_for_device=True)
    n = {"calls": 0}

    def fake_open():
        n["calls"] += 1
        if n["calls"] == 1:
            raise ValueError("input device index 7 not found")   # not present yet
        bf._streaming = True
        bf._last_block_monotonic = 0.0
    monkeypatch.setattr(bf, "_open_stream", fake_open)

    bf._supervise_once(0.0)
    assert not bf.streaming and "not found" in bf.error
    bf._supervise_once(1.0)
    assert bf.streaming and bf.error == ""


def test_supervisor_gives_up_on_structural_error(monkeypatch):
    bf = PolarisBeamformer(device=7, wait_for_device=True)

    def fake_open():
        raise DeviceConfigError("device 7 exposes 2 input channels but POLARIS needs 8")
    monkeypatch.setattr(bf, "_open_stream", fake_open)

    bf._supervise_once(0.0)
    assert bf.device_fatal and not bf.streaming and "needs 8" in bf.error


def test_supervisor_keeps_retrying_on_absence(monkeypatch):
    bf = PolarisBeamformer(device=7, wait_for_device=True)

    def fake_open():
        raise ValueError("input device index 7 not found")   # absent → NOT fatal
    monkeypatch.setattr(bf, "_open_stream", fake_open)

    bf._supervise_once(0.0)
    assert not bf.device_fatal and not bf.streaming and "not found" in bf.error


def test_supervisor_reconnects_on_stall(monkeypatch):
    bf = PolarisBeamformer(device=7, wait_for_device=True, device_stall_timeout_s=2.0)
    bf._streaming = True
    bf._last_block_monotonic = 0.0
    closed = {"n": 0}

    def fake_close():
        closed["n"] += 1
        bf._streaming = False
    monkeypatch.setattr(bf, "_close_stream", fake_close)

    bf._supervise_once(5.0)                       # 5 - 0 > 2 s → watchdog trips → reconnect
    assert closed["n"] == 1 and not bf.streaming
    assert "stall" in bf.error.lower()


def test_supervisor_holds_when_stream_fresh(monkeypatch):
    bf = PolarisBeamformer(device=7, wait_for_device=True, device_stall_timeout_s=2.0)
    bf._streaming = True
    bf._last_block_monotonic = 4.5
    monkeypatch.setattr(bf, "_close_stream", lambda: pytest.fail("should not reconnect when fresh"))
    bf._supervise_once(5.0)                       # 0.5 s < 2 s → no action
    assert bf.streaming


def test_wait_mode_start_does_not_raise_when_device_absent(monkeypatch):
    def _raise():
        raise ValueError("input device index 7 not found")

    bf = PolarisBeamformer(device=7, wait_for_device=True, reconnect_interval_s=0.05)
    monkeypatch.setattr(pb, "controls_available", lambda: True)
    monkeypatch.setattr(bf, "_open_stream", _raise)
    monkeypatch.setattr(bf, "_close_stream", lambda: None)

    bf.start()                                    # supervisor + DOA threads, no raise
    assert bf.connected and not bf.streaming
    bf.stop()
    assert not bf.connected
    assert bf._supervisor_thread is None and bf._doa_thread is None


def test_rtf_mvdr_mode_is_accepted():
    from conf_pipeline_control.polaris_beamformer import (
        PolarisBeamformer, MODE_RTF_MVDR, _BEAM_MODES,
    )
    assert MODE_RTF_MVDR == "rtf_mvdr"
    assert MODE_RTF_MVDR in _BEAM_MODES
    bf = PolarisBeamformer(device=None, mode=MODE_RTF_MVDR)   # constructs without raising
    assert bf.mode == MODE_RTF_MVDR


def test_target_and_noise_covariances_are_gated_separately():
    import numpy as np
    from conf_pipeline_control.polaris_beamformer import PolarisBeamformer, MODE_RTF_MVDR
    bf = PolarisBeamformer(device=None, mode=MODE_RTF_MVDR)
    bf._setup_runtime()                                  # device-free allocation
    assert bf._target_cov is not None and bf._noise_cov is not None
    assert bf._target_frames == 0
    nb = bf._target_cov.shape[0]; M = bf._target_cov.shape[1]
    inst = np.tile(np.eye(M, dtype=complex), (nb, 1, 1))
    bf._accumulate_rtf_covariance(inst, target_present=True)    # talker frame
    assert bf._target_frames == 1
    bf._accumulate_rtf_covariance(inst, target_present=False, noise_only=True)   # noise frame
    assert bf._noise_frames >= 1 and bf._target_frames == 1
    bf._accumulate_rtf_covariance(inst, target_present=False, noise_only=False)  # ambiguous → neither
    assert bf._target_frames == 1


def test_rtf_cov_snapshot_none_until_both_warm():
    import numpy as np
    from conf_pipeline_control.polaris_beamformer import (
        PolarisBeamformer, MODE_RTF_MVDR, _NOISE_WARMUP_FRAMES,
    )
    bf = PolarisBeamformer(device=None, mode=MODE_RTF_MVDR)
    bf._setup_runtime()
    assert bf._rtf_cov_snapshot() is None                       # cold
    nb, M = bf._target_cov.shape[0], bf._target_cov.shape[1]
    inst = np.tile(np.eye(M, dtype=complex), (nb, 1, 1))
    for _ in range(_NOISE_WARMUP_FRAMES + 1):
        bf._accumulate_rtf_covariance(inst, target_present=True)
        bf._accumulate_rtf_covariance(inst, target_present=False, noise_only=True)
    snap = bf._rtf_cov_snapshot()
    assert snap is not None
    tcov, ncov, band = snap
    assert tcov.shape == bf._target_cov.shape and ncov.shape == bf._noise_cov.shape
    assert len(band) == tcov.shape[0]


def test_freqdomain_rtf_branch_nulls_interferer_better_than_planewave():
    import numpy as np
    from conf_pipeline_control.geometry import sensibel_8
    from conf_pipeline_control.polaris_beamformer import _FreqDomainBeam

    geom = sensibel_8()
    # measured covariances on the beam's band bins: target at az0, interferer at az1, + diffuse.
    beam = _FreqDomainBeam(geom, 44100.0, 343.0)
    band = np.arange(20, 60)                       # a slice of in-band bins
    M = geom.n_channels
    # build synthetic (n_band, M, M) target/noise covs from manifolds at two azimuths
    def manifold_band(az):
        idx = list(geom.active_indices()); el = np.array([geom.elements[i] for i in idx])
        from conf_pipeline_control.beamformer import _unit_from_az_offnadir
        u = np.array(_unit_from_az_offnadir(az, 90.0))
        k = 2 * np.pi * beam._freqs[band] / 343.0
        a = np.zeros((len(band), M), complex)
        a[:, idx] = np.exp(1j * k[:, None] * (el @ u)[None, :])
        return a
    at, ai = manifold_band(20.0), manifold_band(80.0)
    ncov = np.einsum("bi,bj->bij", ai, ai.conj()) * 4.0 + np.eye(M)[None] * 1.0
    tcov = np.einsum("bi,bj->bij", at, at.conj()) * 10.0 + ncov
    full_t = np.zeros((len(beam._freqs), M, M), complex); full_t[band] = tcov
    full_n = np.zeros((len(beam._freqs), M, M), complex); full_n[band] = ncov
    beam._rtf_cov_provider = lambda: (full_t, full_n, band)

    W = beam._compute_weights(20.0, 90.0, ())     # RTF branch active
    # response of the beam to the interferer manifold should be well below the target response
    resp_t = np.abs(np.sum(np.conj(W[band]) * at, axis=1))
    resp_i = np.abs(np.sum(np.conj(W[band]) * ai, axis=1))
    assert np.median(resp_t) > np.median(resp_i) * 10.0         # RTF-MVDR suppression >> plane-wave (plane-wave ~4.7x, RTF ~43x)


def test_freqdomain_rtf_provider_none_is_identical_to_planewave():
    import numpy as np
    from conf_pipeline_control.geometry import sensibel_8
    from conf_pipeline_control.polaris_beamformer import _FreqDomainBeam
    geom = sensibel_8()
    a = _FreqDomainBeam(geom, 44100.0, 343.0)                   # no rtf provider
    b = _FreqDomainBeam(geom, 44100.0, 343.0)
    b._rtf_cov_provider = lambda: None                         # cold start → fallback
    Wa = a._compute_weights(33.0, 90.0, ())
    Wb = b._compute_weights(33.0, 90.0, ())
    assert np.allclose(Wa, Wb)                                  # byte-equivalent fallback


def test_make_beam_wires_rtf_providers():
    from conf_pipeline_control.polaris_beamformer import PolarisBeamformer, MODE_RTF_MVDR
    bf = PolarisBeamformer(device=None, mode=MODE_RTF_MVDR)
    beam = bf._make_beam(bf.geometry)
    assert beam._rtf_cov_provider is not None
    assert beam._noise_cov_provider is not None        # measured noise overlay still active
