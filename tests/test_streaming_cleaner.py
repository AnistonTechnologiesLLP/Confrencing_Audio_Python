"""Hardware-free tests for the streaming OM-LSA cleaner (OCTOVOX NR ported to live).

Mirrors the post-beam-NR tests in ``test_polaris_beamformer.py``: the cleaner is a
drop-in for ``_PostNoiseSuppressor`` (same ``process(block, noise_gate)``/``reset()``
contract and overlap-add/minimum-statistics machinery), so the same guarantees are
checked here — warmup passthrough, real noise suppression that never hard-mutes,
near-distortionless on above-floor tones, gain never boosts, chunk-size invariance,
and process/reset length-safety under a concurrent reset — plus the OM-LSA-specific
pieces: the vendored ``_exp1`` against ``scipy.special.exp1`` and the
``post_nr_engine`` wiring that selects the cleaner inside ``PolarisBeamformer``.
numpy is required for the DSP paths and skipped if absent.
"""
import threading

import pytest

np = pytest.importorskip("numpy")

import conf_pipeline_control as cc
import conf_pipeline_control.polaris_beamformer as pb
from conf_pipeline_control.autosteer import AutoSteerController
from conf_pipeline_control.live import LiveBeamController
from conf_pipeline_control.polaris_beamformer import PolarisBeamformer
from conf_pipeline_control.streaming_cleaner import StreamingCleaner, _exp1

_GMIN = 10.0 ** (-18.0 / 20.0)            # default gmin_db = -18 dB → linear OM-LSA floor


def _rms(x):
    return float(np.sqrt(np.mean(x * x))) if x.size else 0.0


# --------------------------------------------------------------------------- #
# Vendored exponential integral E1 vs scipy
# --------------------------------------------------------------------------- #
def test_exp1_matches_scipy():
    special = pytest.importorskip("scipy.special")
    x = np.logspace(-3.0, np.log10(500.0), 400)        # the ν clamp range [nu_min, nu_max]
    mine = _exp1(x)
    ref = special.exp1(x)
    assert np.allclose(mine, ref, rtol=3e-3, atol=1e-7)


def test_exp1_is_vectorized_and_finite():
    x = np.linspace(1e-3, 500.0, 50)
    out = _exp1(x)
    assert out.shape == x.shape and bool(np.all(np.isfinite(out))) and bool(np.all(out > 0.0))


# --------------------------------------------------------------------------- #
# StreamingCleaner — behaviour (mirrors _PostNoiseSuppressor tests)
# --------------------------------------------------------------------------- #
def test_streaming_cleaner_builds_runs_shape_finite():
    nr = StreamingCleaner(44100.0, frame=512, warmup_frames=2)
    assert nr.mode == "omlsa"
    rng = np.random.default_rng(0)
    out = np.concatenate([nr.process((0.05 * rng.standard_normal(1411)).astype(float), True)
                          for _ in range(8)])
    assert out.ndim == 1 and out.dtype == np.float32 and bool(np.all(np.isfinite(out)))


def test_streaming_cleaner_warmup_passthrough_is_byte_identical():
    rng = np.random.default_rng(1)
    nr = StreamingCleaner(44100.0, frame=512, warmup_frames=10_000)        # never engages here
    for _ in range(6):
        blk = (0.05 * rng.standard_normal(1411)).astype(np.float32)
        assert np.array_equal(nr.process(blk, True), blk)                  # bypass = byte-identical
    assert nr._engaged is False


def test_streaming_cleaner_attenuates_noise_after_warmup():
    rng = np.random.default_rng(2)
    nr = StreamingCleaner(44100.0, frame=512, warmup_frames=8)
    for _ in range(60):
        nr.process((0.05 * rng.standard_normal(1411)).astype(float), True)  # learn the floor → engage
    assert nr._engaged
    blk = (0.05 * rng.standard_normal(8192)).astype(float)
    out = np.concatenate([nr.process(blk[i:i + 512], True) for i in range(0, 8192, 512)])
    rin, rout = _rms(blk), _rms(out)
    assert 0.02 * rin < rout < 0.75 * rin            # meaningfully suppressed, but NOT muted to silence


def test_streaming_cleaner_preserves_above_floor_tone():
    rng = np.random.default_rng(3)
    nr = StreamingCleaner(44100.0, frame=512, warmup_frames=8)
    for _ in range(40):
        nr.process((0.02 * rng.standard_normal(1411)).astype(float), True)  # learn a LOW floor
    assert nr._engaged
    t = np.arange(8192) / 44100.0
    tone = (0.4 * np.sin(2 * np.pi * 1200.0 * t)).astype(float)             # well above the floor
    out = np.concatenate([nr.process(tone[i:i + 512], False) for i in range(0, 8192, 512)])
    assert _rms(out[1024:]) > 0.80 * _rms(tone[1024:])    # near-distortionless (G≈1 when P≫N²)


def test_streaming_cleaner_gain_never_boosts():
    """OM-LSA is a suppression gain — the LSA E1 lift is hard-capped at 1, so no bin is ever amplified."""
    rng = np.random.default_rng(4)
    nr = StreamingCleaner(44100.0, frame=512, warmup_frames=4)
    for _ in range(60):
        nr.process((0.1 * rng.standard_normal(1411)).astype(float), True)
    assert nr._engaged
    assert float(np.max(nr._gain_prev)) <= 1.0 + 1e-9


def test_streaming_cleaner_block_size_invariance():
    """The FIFO frames at a fixed hop, so the cleaned stream is identical regardless of how the caller
    chunks the input (warmup 0 so both engage on frame 1)."""
    rng = np.random.default_rng(5)
    sig = (0.05 * rng.standard_normal(8192)).astype(float)
    a = StreamingCleaner(44100.0, frame=512, warmup_frames=0)
    b = StreamingCleaner(44100.0, frame=512, warmup_frames=0)
    out_a = np.concatenate([a.process(sig[i:i + 512], True) for i in range(0, 8192, 512)])
    out_b = np.concatenate([b.process(sig[i:i + 1411], True) for i in range(0, 8192, 1411)])
    L = min(len(out_a), len(out_b))
    assert L > 4096
    assert np.allclose(out_a[1024:L], out_b[1024:L], atol=1e-6)            # chunk-size-agnostic


def test_streaming_cleaner_process_reset_are_length_safe_under_concurrency():
    """The inherited lock serializes process() (audio thread) vs reset() (control thread), so process()
    always returns exactly n samples even under a concurrent reset storm."""
    nr = StreamingCleaner(44100.0, frame=512, warmup_frames=0)
    rng = np.random.default_rng(6)
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


def test_streaming_cleaner_reset_clears_decision_directed_state():
    rng = np.random.default_rng(7)
    nr = StreamingCleaner(44100.0, frame=512, warmup_frames=0)
    for _ in range(10):
        nr.process((0.05 * rng.standard_normal(1411)).astype(float), True)
    assert nr._engaged and nr._prev_clean is not None
    nr.reset()
    assert nr._engaged is False and nr._prev_clean is None      # decision-directed + floor history dropped


def test_streaming_cleaner_wiener_mode_runs():
    rng = np.random.default_rng(8)
    nr = StreamingCleaner(44100.0, mode="wiener", frame=512, warmup_frames=4)
    for _ in range(40):
        nr.process((0.05 * rng.standard_normal(1411)).astype(float), True)
    assert nr._engaged
    out = nr.process((0.05 * rng.standard_normal(1411)).astype(float), True)
    assert out.shape == (1411,) and bool(np.all(np.isfinite(out)))
    assert float(np.max(nr._gain_prev)) <= 1.0 + 1e-9


def test_streaming_cleaner_gate_mode_delegates_to_base():
    """mode='gate' must reproduce the base spectral gate exactly (gain bounded below by the floor)."""
    rng = np.random.default_rng(9)
    nr = StreamingCleaner(44100.0, mode="gate", frame=512, warmup_frames=4)
    for _ in range(60):
        nr.process((0.1 * rng.standard_normal(1411)).astype(float), True)
    assert nr._engaged
    assert float(np.min(nr._gain_prev)) >= _GMIN - 1e-9        # base gate floors every bin (never hard-mutes)


# --------------------------------------------------------------------------- #
# PolarisBeamformer wiring — post_nr_engine selects the cleaner
# --------------------------------------------------------------------------- #
def test_engine_omlsa_builds_streaming_cleaner_and_runs():
    bf = PolarisBeamformer(device=None, post_nr=True, post_nr_engine="omlsa", post_nr_warmup_frames=2)
    bf._setup_runtime()
    assert isinstance(bf._post_nr, StreamingCleaner) and bf._post_nr.mode == "omlsa"
    rng = np.random.default_rng(10)
    blk = (0.1 * rng.standard_normal((bf.blocksize, bf.n_channels))).astype(float)
    out = bf.process_block(blk)
    assert out.shape == (bf.blocksize,) and bool(np.all(np.isfinite(out)))


def test_engine_wiener_builds_streaming_cleaner():
    bf = PolarisBeamformer(device=None, post_nr=True, post_nr_engine="wiener", post_nr_warmup_frames=2)
    bf._setup_runtime()
    assert isinstance(bf._post_nr, StreamingCleaner) and bf._post_nr.mode == "wiener"


def test_engine_default_gate_is_the_base_suppressor():
    bf = PolarisBeamformer(device=None, post_nr=True, post_nr_warmup_frames=2)   # default engine "gate"
    bf._setup_runtime()
    assert isinstance(bf._post_nr, pb._PostNoiseSuppressor)
    assert not isinstance(bf._post_nr, StreamingCleaner)


def test_engine_reset_transient_resets_the_cleaner_in_place():
    bf = PolarisBeamformer(device=None, beam_bandlimit_hz=None, post_nr=True,
                           post_nr_engine="omlsa", post_nr_warmup_frames=2)
    bf._setup_runtime()
    nr = bf._post_nr
    rng = np.random.default_rng(11)
    for _ in range(6):
        bf.process_block((0.1 * rng.standard_normal((bf.blocksize, bf.n_channels))).astype(float))
    bf.reset_transient()
    assert bf._post_nr is nr and nr._engaged is False and nr._prev_clean is None


# --------------------------------------------------------------------------- #
# Auto-steer / LiveBeamController wiring — the cleaner on the auto-steer path
# --------------------------------------------------------------------------- #
def _polaris_geometry():
    return PolarisBeamformer(device=None).geometry


def _wire_live(bf):
    """Allocate the device-free numpy state LiveBeamController._process_block needs (mirrors _open without
    opening a stream): passthrough beam, fresh OLA, and the post-NR engine."""
    from conf_pipeline_control.live import _FRAME, _HOP
    bf._np = np
    bf._win = np.hanning(_FRAME).astype(float)
    bf._inbuf = np.zeros((_FRAME, bf.n_channels), dtype=float)
    bf._ola = np.zeros(_FRAME, dtype=float)
    bf._weights = None                        # passthrough (average capsules) — beam math not under test
    bf._build_post_nr()
    return _HOP


def test_live_controller_omlsa_builds_streaming_cleaner():
    bf = LiveBeamController(_polaris_geometry(), post_nr=True, post_nr_engine="omlsa")
    bf._build_post_nr()
    assert isinstance(bf._post_nr, StreamingCleaner) and bf._post_nr.mode == "omlsa"


def test_live_controller_gate_builds_base_suppressor():
    bf = LiveBeamController(_polaris_geometry(), post_nr=True, post_nr_engine="gate")
    bf._build_post_nr()
    assert isinstance(bf._post_nr, pb._PostNoiseSuppressor)
    assert not isinstance(bf._post_nr, StreamingCleaner)


def test_live_controller_off_builds_no_cleaner():
    bf = LiveBeamController(_polaris_geometry())          # post_nr defaults False
    bf._build_post_nr()
    assert bf._post_nr is None


def test_live_controller_process_block_runs_the_cleaner():
    bf = LiveBeamController(_polaris_geometry(), post_nr=True, post_nr_engine="omlsa", post_nr_warmup_frames=4)
    hop = _wire_live(bf)
    rng = np.random.default_rng(20)
    out = None
    for _ in range(40):                                   # enough HOPs to engage the cleaner
        blk = (0.05 * rng.standard_normal((hop, bf.n_channels))).astype(float)
        out = bf._process_block(blk)
        assert out.shape == (hop,) and bool(np.all(np.isfinite(out)))
    assert bf._post_nr._engaged                           # the cleaner actually ran and engaged on the live path


def test_autosteer_forwards_post_nr_to_the_live_controller():
    sector = cc.SectorConfig(center_deg=0.0, half_width_deg=60.0)
    a = AutoSteerController(_polaris_geometry(), sector, samplerate=44100.0,
                           post_nr=True, post_nr_engine="omlsa",
                           post_nr_floor_db=-22.0, post_nr_oversub=2.0)
    assert a.ctrl.post_nr is True and a.ctrl._post_nr_engine == "omlsa"
    assert a.ctrl._post_nr_floor_db == -22.0 and a.ctrl._post_nr_oversub == 2.0
    a.ctrl._build_post_nr()
    assert isinstance(a.ctrl._post_nr, StreamingCleaner)
