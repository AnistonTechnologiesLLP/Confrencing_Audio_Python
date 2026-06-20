"""StreamingPeq — real-time parametric-EQ biquad cascade (hardware-free, synthetic signals)."""
import math

import pytest

np = pytest.importorskip("numpy")
pytest.importorskip("scipy")

from conf_pipeline_control.peq import StreamingPeq

FS = 44100.0


def _sine(f, n, fs=FS, amp=0.3):
    t = np.arange(n, dtype=np.float64) / fs
    return (amp * np.sin(2.0 * math.pi * f * t)).astype(np.float32)


def _rms(x):
    x = np.asarray(x, dtype=np.float64)
    return float(np.sqrt(np.mean(x * x))) if x.size else 0.0


def _run(peq, x, chunk=512):
    """Stream x through the PEQ in chunks and concatenate the output."""
    out = [peq.process(x[i:i + chunk]) for i in range(0, len(x), chunk)]
    return np.concatenate(out) if out else np.asarray([], dtype=np.float32)


def _steady_db(peq, f, n=44100):
    """dB gain at frequency f, measured on the steady-state (second-half) RMS of a sine."""
    x = _sine(f, n)
    y = _run(peq, x)
    half = n // 2
    return 20.0 * math.log10((_rms(y[half:]) + 1e-12) / (_rms(x[half:]) + 1e-12))


# --------------------------------------------------------------------------- #
# no-op / pass-through (Invariant D)
# --------------------------------------------------------------------------- #
def test_no_bands_is_bit_exact_passthrough():
    peq = StreamingPeq(FS, None)
    blk = _sine(1000, 1024)
    assert peq.process(blk) is blk                    # SAME object, no copy
    assert StreamingPeq(FS, []).process(blk) is blk


def test_zero_db_bell_is_passthrough():
    peq = StreamingPeq(FS, [{"freqHz": 1000, "gainDb": 0, "q": 1, "type": "bell"}])
    blk = _sine(1000, 1024)
    assert peq.process(blk) is blk                    # a 0 dB bell is identity → skipped → no-op


def test_disabled_band_is_passthrough():
    peq = StreamingPeq(FS, [{"freqHz": 1000, "gainDb": 9, "q": 1, "type": "bell", "enabled": False}])
    blk = _sine(1000, 1024)
    assert peq.process(blk) is blk


def test_out_of_range_band_is_passthrough():
    peq = StreamingPeq(FS, [{"freqHz": 30000, "gainDb": 9, "q": 1, "type": "bell"}])  # above Nyquist
    blk = _sine(1000, 1024)
    assert peq.process(blk) is blk


# --------------------------------------------------------------------------- #
# band shapes
# --------------------------------------------------------------------------- #
def test_bell_boosts_its_band_and_leaves_others():
    peq = StreamingPeq(FS, [{"freqHz": 1000, "gainDb": 12, "q": 1.0, "type": "bell"}])
    assert abs(_steady_db(peq, 1000) - 12.0) < 1.0    # +12 dB at the centre
    peq.reset()
    assert abs(_steady_db(peq, 6000)) < 1.5           # a far band is ~untouched


def test_bell_cut_attenuates_its_band():
    peq = StreamingPeq(FS, [{"freqHz": 1000, "gainDb": -12, "q": 4.0, "type": "bell"}])
    assert _steady_db(peq, 1000) < -8.0               # a deep narrow cut (hum-notch shape)


def test_highpass_attenuates_lows_passes_highs():
    peq = StreamingPeq(FS, [{"freqHz": 300, "gainDb": 0, "q": 0.707, "type": "highpass"}])
    assert _steady_db(peq, 80) < -10.0                # well below the corner → strongly attenuated
    peq.reset()
    assert abs(_steady_db(peq, 3000)) < 1.5           # well above → passes


def test_lowpass_attenuates_highs_passes_lows():
    peq = StreamingPeq(FS, [{"freqHz": 3000, "gainDb": 0, "q": 0.707, "type": "lowpass"}])
    assert _steady_db(peq, 9000) < -10.0
    peq.reset()
    assert abs(_steady_db(peq, 300)) < 1.5


def test_low_shelf_boosts_lows():
    peq = StreamingPeq(FS, [{"freqHz": 200, "gainDb": 10, "q": 0.707, "type": "lowShelf"}])
    assert _steady_db(peq, 60) > 7.0                  # below the shelf → boosted
    peq.reset()
    assert abs(_steady_db(peq, 8000)) < 1.5           # above → flat


def test_high_shelf_boosts_highs():
    peq = StreamingPeq(FS, [{"freqHz": 4000, "gainDb": 10, "q": 0.707, "type": "highShelf"}])
    assert _steady_db(peq, 12000) > 7.0
    peq.reset()
    assert abs(_steady_db(peq, 200)) < 1.5


# --------------------------------------------------------------------------- #
# streaming continuity + numerics
# --------------------------------------------------------------------------- #
def test_block_size_invariance():
    bands = [{"freqHz": 1000, "gainDb": 8, "q": 1.5, "type": "bell"}]
    x = _sine(1000, 8192) + _sine(3000, 8192)
    big = StreamingPeq(FS, bands).process(x)
    small = _run(StreamingPeq(FS, bands), x, chunk=512)
    assert np.allclose(big, small, atol=1e-5)         # carried state ⇒ chunking is exact


def test_reset_clears_state():
    bands = [{"freqHz": 800, "gainDb": -6, "q": 2.0, "type": "bell"}]
    peq = StreamingPeq(FS, bands)
    x = _sine(800, 4096)
    first = _run(peq, x)
    peq.reset()
    again = _run(peq, x)
    assert np.allclose(first, again, atol=1e-6)       # post-reset run matches a fresh one


def test_output_is_float32():
    peq = StreamingPeq(FS, [{"freqHz": 1000, "gainDb": 6, "q": 1, "type": "bell"}])
    out = peq.process(_sine(1000, 512))
    assert out.dtype == np.float32


def test_high_q_low_freq_hum_notch_stable():
    """The hum-notch stress case: a 50 Hz Q=10 deep cut must stay finite + float32 + bounded over a long
    block (float64 state, no denormal blow-up) — Invariant H-A."""
    peq = StreamingPeq(FS, [{"freqHz": 50, "gainDb": -14, "q": 10.0, "type": "bell"}])
    rng = np.random.default_rng(0)
    y = _run(peq, (0.1 * rng.standard_normal(44100)).astype(np.float32))
    assert y.dtype == np.float32
    assert bool(np.all(np.isfinite(y)))
    assert float(np.max(np.abs(y))) < 5.0             # no runaway


def test_live_rebuild_via_set_bands():
    peq = StreamingPeq(FS, [{"freqHz": 1000, "gainDb": 0, "q": 1, "type": "bell"}])   # starts no-op
    blk = _sine(1000, 1024)
    assert peq.process(blk) is blk
    peq.set_bands([{"freqHz": 1000, "gainDb": 12, "q": 1, "type": "bell"}])           # engage live
    assert abs(_steady_db(peq, 1000) - 12.0) < 1.0
    peq.set_bands(None)                                                               # back to no-op
    assert peq.process(blk) is blk


# --------------------------------------------------------------------------- #
# wiring into the two live chains (engine + controller)
# --------------------------------------------------------------------------- #
_BELL = [{"freqHz": 1000, "gainDb": 9, "q": 1, "type": "bell"}]


def test_peq_wires_into_live_controller():
    from conf_pipeline_control import sensibel_8
    from conf_pipeline_control.live import LiveBeamController
    ctrl = LiveBeamController(sensibel_8(), peq=True, peq_bands=_BELL)
    ctrl._build_post_nr()                                       # device-free build (per its docstring)
    assert ctrl._peq is not None and ctrl._peq._sections is not None      # engaged
    ctrl.set_peq_bands(None)
    assert ctrl._peq._sections is None                         # disabled live
    ctrl.set_peq_bands([{"freqHz": 800, "gainDb": -6, "q": 2, "type": "bell"}])
    assert ctrl._peq._sections is not None                     # re-engaged live


def test_peq_wires_into_polaris_beamformer():
    from conf_pipeline_control.polaris_beamformer import PolarisBeamformer
    bf = PolarisBeamformer(peq=True, peq_bands=_BELL)
    bf._setup_runtime()                                        # device-free runtime build
    assert bf._peq is not None and bf._peq._sections is not None
    bf.set_peq_bands(None)
    assert bf._peq._sections is None                          # disabled live
    bf.set_peq_bands(_BELL)
    assert bf._peq._sections is not None                      # re-engaged live


def test_peq_off_by_default_is_noop_object():
    from conf_pipeline_control.polaris_beamformer import PolarisBeamformer
    bf = PolarisBeamformer()                                   # no peq cfg
    bf._setup_runtime()
    assert bf._peq is not None and bf._peq._sections is None   # built but idle (true no-op)


# --------------------------------------------------------------------------- #
# Speech band — the ~90 Hz capture high-pass (a one-band StreamingPeq highpass)
# --------------------------------------------------------------------------- #
def test_speech_highpass_cuts_rumble_keeps_speech():
    """The 90 Hz speech high-pass strongly attenuates sub-speech rumble / mains hum but passes the speech
    band (the missing half of the speech band-pass; the ~5.6 kHz top is the existing band-limit)."""
    bands = [{"freqHz": 90.0, "gainDb": 0.0, "q": 0.707, "type": "highpass"}]
    assert _steady_db(StreamingPeq(FS, bands), 40) < -12.0     # deep rumble strongly cut
    assert _steady_db(StreamingPeq(FS, bands), 50) < -6.0      # 50 Hz mains-hum region attenuated
    assert abs(_steady_db(StreamingPeq(FS, bands), 250)) < 2.0    # a low speech fundamental ~passes
    assert abs(_steady_db(StreamingPeq(FS, bands), 2000)) < 1.5   # the speech band passes


def test_speech_band_wires_into_both_chains():
    from conf_pipeline_control.live import LiveBeamController
    from conf_pipeline_control.polaris_beamformer import PolarisBeamformer
    from conf_pipeline_control import sensibel_8
    bf = PolarisBeamformer(speech_band=True)
    bf._setup_runtime()
    assert bf._speech_hp is not None and bf._speech_hp._sections is not None   # engaged
    off = PolarisBeamformer()
    off._setup_runtime()
    assert off._speech_hp is None                              # off by default
    ctrl = LiveBeamController(sensibel_8(), speech_band=True)
    ctrl._build_post_nr()
    assert ctrl._speech_hp is not None and ctrl._speech_hp._sections is not None
