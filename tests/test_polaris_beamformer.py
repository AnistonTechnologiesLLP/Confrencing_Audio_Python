"""Hardware-free tests for the POLARIS real-time beamformer module.

Everything here runs without an audio device: the pure talker-hold state machine,
the stateless delay-and-sum block, SRP-PHAT DOA from synthetic covariances, the
constructor/geometry, device-validation errors (monkeypatched), output-queue
backpressure, and lifecycle parity (streams stubbed). numpy is required for the
DSP paths and skipped if absent.
"""
import math

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
        PolarisBeamformer(device=None, mode="mvdr")                   # not in v1


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
