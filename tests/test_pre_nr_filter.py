"""Tests for the pre-NR linear cleanup stage (Phase 2 — HPF/notch BEFORE the post-NR/DFN3 denoiser).

The pre-NR stage reuses the existing ``StreamingPeq`` biquad cascade (it already has ``highpass`` and
``bell`` types), so this file covers:
  * the pure-stdlib band builders / preset in ``conf_pipeline_control.pre_nr`` (run in the no-numpy suite);
  * the HPF / notch attenuation behaviour through ``StreamingPeq`` (numpy + scipy);
  * (in test_pre_nr_filter_wiring section) the engine wiring + the order proof that the pre-NR stage
    runs BEFORE post-NR in both chains.
"""
import pytest

from conf_pipeline_control.pre_nr import (
    build_pre_nr_bands,
    hpf_band,
    notch_band,
    office_ac_preset,
)


def _np():
    return pytest.importorskip("numpy")


def _np_scipy():
    pytest.importorskip("numpy")
    pytest.importorskip("scipy")
    import numpy as np
    return np


def _sine(f, n=8192, sr=48000.0):
    np = _np_scipy()
    t = np.arange(n) / sr
    return np.sin(2 * np.pi * f * t).astype(np.float32)


def _rms(x):
    import numpy as np
    a = np.asarray(x, dtype=np.float64)
    return float(np.sqrt(np.mean(a * a))) if a.size else 0.0


# --------------------------------------------------------------------------- #
# Band builders / preset — pure stdlib (no numpy), run in the default suite
# --------------------------------------------------------------------------- #
def test_hpf_band_is_a_highpass():
    b = hpf_band(120.0)
    assert b["type"] == "highpass"
    assert b["freqHz"] == 120.0
    assert b["q"] > 0.0


def test_notch_band_is_a_negative_gain_bell():
    b = notch_band(140.0, q=8.0, depth_db=12.0)
    assert b["type"] == "bell"
    assert b["freqHz"] == 140.0
    assert b["gainDb"] == -12.0          # a notch = a dip, always negative gain
    assert b["q"] == 8.0


def test_notch_depth_is_always_attenuation_even_if_given_positive():
    assert notch_band(100.0, depth_db=9.0)["gainDb"] == -9.0   # depth is a magnitude; a notch never boosts


def test_build_pre_nr_bands_hpf_first_then_notches():
    bands = build_pre_nr_bands(hpf_hz=120.0, notches=[102.0, 140.0])
    assert [b["type"] for b in bands] == ["highpass", "bell", "bell"]
    assert bands[0]["freqHz"] == 120.0
    assert bands[1]["freqHz"] == 102.0 and bands[2]["freqHz"] == 140.0


def test_build_pre_nr_bands_accepts_float_tuple_and_dict_notches():
    bands = build_pre_nr_bands(notches=[100.0, (200.0, 10.0, 18.0),
                                        {"freqHz": 300.0, "q": 6.0, "depthDb": 9.0}])
    assert bands[0]["freqHz"] == 100.0
    assert bands[1]["freqHz"] == 200.0 and bands[1]["q"] == 10.0 and bands[1]["gainDb"] == -18.0
    assert bands[2]["freqHz"] == 300.0 and bands[2]["q"] == 6.0 and bands[2]["gainDb"] == -9.0


def test_build_pre_nr_bands_empty_when_nothing_requested():
    assert build_pre_nr_bands() == []
    assert build_pre_nr_bands(hpf_hz=0.0, notches=[]) == []   # 0 Hz HPF is "off", not a band


def test_office_ac_preset_is_hpf_plus_three_measured_notches():
    bands = office_ac_preset()
    assert bands[0]["type"] == "highpass" and bands[0]["freqHz"] == 120.0
    assert [b["freqHz"] for b in bands if b["type"] == "bell"] == [102.0, 140.0, 177.0]


def test_pre_nr_builders_exported_from_package_root():
    import conf_pipeline_control as cc
    assert cc.build_pre_nr_bands is build_pre_nr_bands
    assert cc.office_ac_preset is office_ac_preset


# --------------------------------------------------------------------------- #
# Filter behaviour through the reused StreamingPeq (numpy + scipy)
# --------------------------------------------------------------------------- #
def test_pre_nr_hpf_attenuates_rumble_preserves_speech():
    np = _np_scipy()
    from conf_pipeline_control.peq import StreamingPeq
    peq = StreamingPeq(48000.0, build_pre_nr_bands(hpf_hz=120.0))
    low = _sine(50.0)
    high = _sine(1000.0)
    lo_out = peq.process(low)
    peq.reset()
    hi_out = peq.process(high)
    # measure the steady-state second half (skip the IIR transient)
    assert _rms(lo_out[4096:]) < 0.4 * _rms(low[4096:])      # 50 Hz rumble strongly cut
    assert _rms(hi_out[4096:]) > 0.9 * _rms(high[4096:])     # 1 kHz speech band preserved


def test_pre_nr_notch_attenuates_configured_tone():
    np = _np_scipy()
    from conf_pipeline_control.peq import StreamingPeq
    peq = StreamingPeq(48000.0, build_pre_nr_bands(notches=[140.0]))
    on_tone = _sine(140.0)
    off_tone = _sine(500.0)
    a = peq.process(on_tone)
    peq.reset()
    b = peq.process(off_tone)
    assert _rms(a[4096:]) < 0.5 * _rms(on_tone[4096:])       # the 140 Hz tone is notched
    assert _rms(b[4096:]) > 0.85 * _rms(off_tone[4096:])     # a tone off the notch is preserved


def test_pre_nr_multiple_notches_each_attenuated():
    np = _np_scipy()
    from conf_pipeline_control.peq import StreamingPeq
    peq = StreamingPeq(48000.0, build_pre_nr_bands(notches=[100.0, 220.0]))
    for f in (100.0, 220.0):
        peq.reset()
        out = peq.process(_sine(f))
        assert _rms(out[4096:]) < 0.5 * _rms(_sine(f)[4096:])


def test_pre_nr_invalid_bands_are_dropped_safely():
    np = _np_scipy()
    from conf_pipeline_control.peq import StreamingPeq
    # garbage: 0 Hz / negative-Q highpass + unknown type ⇒ both dropped ⇒ bit-exact no-op
    peq = StreamingPeq(48000.0, [{"type": "highpass", "freqHz": 0.0, "q": -1.0, "gainDb": 0.0},
                                 {"type": "bogus", "freqHz": 100.0, "q": 1.0, "gainDb": -6.0}])
    x = _sine(300.0)
    assert peq.process(x) is x


def test_pre_nr_empty_bands_is_byte_identical():
    np = _np_scipy()
    from conf_pipeline_control.peq import StreamingPeq
    peq = StreamingPeq(48000.0, build_pre_nr_bands())        # [] ⇒ off
    x = _sine(300.0)
    assert peq.process(x) is x


# --------------------------------------------------------------------------- #
# Engine wiring + ORDER proof (pre-NR runs BEFORE post-NR in both chains)
# --------------------------------------------------------------------------- #
def _tone8(n, sr, f):
    """An in-band tone on all 8 capsules (a delay-sum beam of it is the same tone)."""
    np = _np_scipy()
    t = np.arange(n) / sr
    s = np.sin(2 * np.pi * f * t)
    return np.repeat(s[:, None], 8, axis=1).astype(np.float32)


class _PostNrRecorder:
    """Stands in for the post-NR stage; captures the block that REACHES it (the order probe).

    Honors the streaming-stage ``process(block[, noise_gate]) -> block`` / ``reset()`` contract, so the
    engine drives it exactly like a real cleaner — we only observe the *input* it is handed."""
    mode = "gate"

    def __init__(self):
        self.last = None

    def process(self, block, noise_gate=None):
        import numpy as np
        self.last = np.asarray(block, dtype=np.float64).copy()
        return block

    def reset(self):
        pass


def test_polaris_pre_nr_default_off_is_byte_identical():
    np = _np_scipy()
    from conf_pipeline_control.polaris_beamformer import PolarisBeamformer
    bf = PolarisBeamformer(device=None)
    bf._setup_runtime()
    assert bf._pre_nr_peq is not None                # always built (a true no-op when off)
    mono = _sine(300.0)
    assert bf._pre_nr_peq.process(mono) is mono      # off ⇒ same object (pipeline byte-identical)


def test_polaris_pre_nr_runs_before_post_nr_in_process_block():
    """The pre-NR HPF must filter the audio that REACHES post-NR — proof of order, not just presence."""
    np = _np_scipy()
    from conf_pipeline_control.polaris_beamformer import PolarisBeamformer
    bands = build_pre_nr_bands(hpf_hz=2000.0)        # aggressive HPF: cut everything below 2 kHz
    on = PolarisBeamformer(device=None, beam_bandlimit_hz=None, pre_nr=True, pre_nr_bands=bands)
    off = PolarisBeamformer(device=None, beam_bandlimit_hz=None)
    on._setup_runtime()
    off._setup_runtime()
    rec_on, rec_off = _PostNrRecorder(), _PostNrRecorder()
    on._post_nr = rec_on
    off._post_nr = rec_off                            # observe the block handed to post-NR on each engine
    blk = _tone8(on.blocksize, on.sample_rate, 300.0)   # 300 Hz — well below the 2 kHz HPF
    for _ in range(6):
        on.process_block(blk)
        off.process_block(blk)
    e_on = float(np.mean(rec_on.last ** 2))
    e_off = float(np.mean(rec_off.last ** 2))
    assert e_on < 0.25 * e_off                        # post-NR saw HPF-attenuated audio ⇒ pre-NR ran first


def test_polaris_pre_nr_with_real_post_nr_runs_clean():
    np = _np_scipy()
    from conf_pipeline_control.polaris_beamformer import PolarisBeamformer
    bf = PolarisBeamformer(device=None, beam_bandlimit_hz=None,
                           pre_nr=True, pre_nr_bands=build_pre_nr_bands(hpf_hz=120.0),
                           post_nr=True, post_nr_engine="gate")
    bf._setup_runtime()
    assert bf._pre_nr_peq is not None and bf._post_nr is not None
    blk = _tone8(bf.blocksize, bf.sample_rate, 800.0)
    out = None
    for _ in range(5):
        out = bf.process_block(blk)
    assert out.ndim == 1 and out.dtype == np.float32 and bool(np.all(np.isfinite(out)))


def test_polaris_pre_nr_and_existing_peq_coexist_as_distinct_stages():
    np = _np_scipy()
    from conf_pipeline_control.polaris_beamformer import PolarisBeamformer
    from conf_pipeline_control.peq import StreamingPeq
    bf = PolarisBeamformer(device=None,
                           pre_nr=True, pre_nr_bands=build_pre_nr_bands(hpf_hz=120.0),
                           peq=True, peq_bands=[{"type": "bell", "freqHz": 3000.0, "gainDb": 4.0, "q": 1.0}])
    bf._setup_runtime()
    assert isinstance(bf._pre_nr_peq, StreamingPeq) and isinstance(bf._peq, StreamingPeq)
    assert bf._pre_nr_peq is not bf._peq             # two distinct stages, distinct positions


def test_polaris_calibration_and_pre_nr_together_preserve_shape_dtype():
    np = _np_scipy()
    from conf_pipeline_control.polaris_beamformer import PolarisBeamformer
    from conf_pipeline_control.calibration import CalibrationProfile
    prof = CalibrationProfile(channels=8, sample_rate=44100.0, gain_db=(3.0,) + (0.0,) * 7)
    bf = PolarisBeamformer(device=None, sample_rate=44100.0, beam_bandlimit_hz=None,
                           calibration=prof, pre_nr=True, pre_nr_bands=build_pre_nr_bands(hpf_hz=120.0))
    bf._setup_runtime()
    blk = _tone8(bf.blocksize, bf.sample_rate, 700.0)
    out = None
    for _ in range(4):
        out = bf.process_block(blk)
    assert out.ndim == 1 and out.dtype == np.float32 and bool(np.all(np.isfinite(out)))


def test_polaris_pre_nr_does_not_change_latency_estimate():
    np = _np_scipy()
    from conf_pipeline_control.polaris_beamformer import PolarisBeamformer
    off = PolarisBeamformer(device=None)
    on = PolarisBeamformer(device=None, pre_nr=True, pre_nr_bands=office_ac_preset())
    off._setup_runtime()
    on._setup_runtime()
    assert on.estimated_latency_ms == off.estimated_latency_ms   # IIR biquads add zero latency


def test_live_pre_nr_built_when_on_and_noop_when_off():
    np = _np_scipy()
    from conf_pipeline_control.live import LiveBeamController
    from conf_pipeline_control.geometry import sensibel_8
    from conf_pipeline_control.peq import StreamingPeq
    off = LiveBeamController(sensibel_8(radius_m=0.035))
    off._build_post_nr()                              # device-free build (per its docstring)
    assert off._pre_nr_peq is not None
    mono = _sine(300.0)
    assert off._pre_nr_peq.process(mono) is mono      # off ⇒ no-op
    on = LiveBeamController(sensibel_8(radius_m=0.035),
                            pre_nr=True, pre_nr_bands=build_pre_nr_bands(hpf_hz=120.0))
    on._build_post_nr()
    assert isinstance(on._pre_nr_peq, StreamingPeq)
    out = on._pre_nr_peq.process(_sine(50.0))
    assert _rms(out[4096:]) < 0.5 * _rms(_sine(50.0)[4096:])     # the live pre-NR HPF really filters
