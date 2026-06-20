"""StreamingTransientSuppressor — duck table taps, preserve speech plosives (hardware-free)."""
import math

import pytest

np = pytest.importorskip("numpy")
pytest.importorskip("scipy")

from conf_pipeline_control.transient import StreamingTransientSuppressor

FS = 44100.0


def _tone(f, n, amp=0.1):
    t = np.arange(n, dtype=np.float64) / FS
    return (amp * np.sin(2.0 * math.pi * f * t)).astype(np.float32)


def _rms(x):
    x = np.asarray(x, dtype=np.float64)
    return float(np.sqrt(np.mean(x * x))) if x.size else 0.0


def _run(ts, x):
    """Stream x through, flush the lookahead, and return the input-aligned output (≈ gain·x)."""
    out1 = ts.process(x)
    out2 = ts.process(np.zeros(ts._D, dtype=np.float32))
    full = np.concatenate([out1, out2])
    return full[ts._D:ts._D + len(x)]


def _ms(n):
    return int(round(n * FS / 1000.0))


def test_output_length_matches_input_per_block():
    ts = StreamingTransientSuppressor(FS)
    for blk in (256, 512, 1024):
        out = ts.process(_tone(300, blk))
        assert out.shape[0] == blk and out.dtype == np.float32


def test_isolated_tap_is_ducked():
    """A sharp impulse in a quiet gap (decays back to the floor) is attenuated."""
    ts = StreamingTransientSuppressor(FS)
    n = _ms(400)
    x = (0.01 * np.random.default_rng(0).standard_normal(n)).astype(np.float32)   # quiet background
    t0 = _ms(200)
    x[t0:t0 + _ms(2)] += 0.8                                  # ~2 ms tap burst
    y = _run(ts, x)
    reg = slice(t0 - _ms(3), t0 + _ms(8))
    assert np.max(np.abs(y[reg])) < 0.5 * np.max(np.abs(x[reg]))      # tap peak ducked ≥ 6 dB
    pre = slice(0, t0 - _ms(20))
    assert abs(20 * math.log10((_rms(y[pre]) + 1e-9) / (_rms(x[pre]) + 1e-9))) < 1.0   # background preserved


def test_plosive_followed_by_vowel_is_preserved():
    """A short burst IMMEDIATELY followed by sustained voicing (a plosive→vowel) is NOT ducked — the
    vowel survives (Invariant H-B)."""
    ts = StreamingTransientSuppressor(FS)
    burst = _ms(5)
    vowel = _ms(120)
    rng = np.random.default_rng(7)
    x = np.concatenate([
        _tone(150, _ms(60), amp=0.02),                       # quiet lead-in
        (0.22 * rng.standard_normal(burst)).astype(np.float32),   # plosive burst (noise-like, ~vowel level)
        _tone(200, vowel, amp=0.28),                         # the vowel at a comparable level
        _tone(150, _ms(60), amp=0.02),
    ])
    y = _run(ts, x)
    v0 = _ms(60) + burst + _ms(15)                           # inside the vowel (skip the onset edge)
    v1 = _ms(60) + burst + vowel - _ms(5)
    drop_db = 20 * math.log10((_rms(y[v0:v1]) + 1e-9) / (_rms(x[v0:v1]) + 1e-9))
    assert drop_db > -1.5                                     # vowel kept (no sustained duck)


def test_steady_speech_is_untouched():
    ts = StreamingTransientSuppressor(FS)
    x = _tone(220, _ms(300), amp=0.12)
    y = _run(ts, x)
    mid = slice(_ms(50), _ms(250))
    assert abs(20 * math.log10((_rms(y[mid]) + 1e-9) / (_rms(x[mid]) + 1e-9))) < 0.6   # ~unity, no false duck
    assert ts.duck_active is False


def test_duck_active_and_reduction_telemetry():
    ts = StreamingTransientSuppressor(FS)
    n = _ms(120)
    x = (0.01 * np.random.default_rng(1).standard_normal(n)).astype(np.float32)
    x[_ms(60):_ms(60) + _ms(2)] += 0.9
    ts.process(x)                                            # the tap block engages the duck
    assert ts.duck_active is True and ts.last_reduction_db > 3.0


def test_reset_clears_state():
    ts = StreamingTransientSuppressor(FS)
    x = _tone(300, 2048, amp=0.1)
    ts.process(x)
    ts.reset()
    assert ts._tail is None and ts._slow == 0.0 and ts._gain == 1.0 and ts.duck_active is False


def test_finite_and_bounded():
    ts = StreamingTransientSuppressor(FS)
    rng = np.random.default_rng(2)
    y = _run(ts, (0.2 * rng.standard_normal(_ms(200))).astype(np.float32))
    assert bool(np.all(np.isfinite(y))) and float(np.max(np.abs(y))) < 4.0


# --------------------------------------------------------------------------- #
# wiring + AGC freeze (Invariant B)
# --------------------------------------------------------------------------- #
def test_wires_into_polaris_beamformer():
    from conf_pipeline_control.polaris_beamformer import PolarisBeamformer
    bf = PolarisBeamformer(transient_suppress=True)
    bf._setup_runtime()
    assert bf._transient is not None
    off = PolarisBeamformer()
    off._setup_runtime()
    assert off._transient is None                         # off by default


def test_agc_freeze_holds_gain_while_ducking():
    """The AGC must NOT adapt while a transient is ducking (else it chases the dip) — freeze=True holds the
    gain; unfrozen, the same level moves it."""
    from conf_pipeline_control.agc import TargetLoudnessAgc
    agc = TargetLoudnessAgc(target_db=-20.0)
    loud = (0.2 * np.ones(512)).astype(np.float32)
    for _ in range(30):
        agc.process(loud)                                # converge the gain
    g = agc.tracker.value
    dip = (0.05 * np.ones(512)).astype(np.float32)
    for _ in range(10):
        agc.process(dip, freeze=True)                    # frozen → gain held through the dip
    assert abs(agc.tracker.value - g) < 1e-6
    agc.process(dip)                                     # unfrozen → the gain adapts to the new level
    assert abs(agc.tracker.value - g) > 1e-9
