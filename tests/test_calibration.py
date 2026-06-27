"""Hardware-free tests for per-capsule calibration (:mod:`conf_pipeline_control.calibration`).

Phase 1 of the audio front-end hardening. Covers three layers:
  * ``CalibrationProfile`` — the pure-stdlib (numpy-free) profile dataclass + camelCase JSON
    round-trip + controlled validation. These run in the default (no-numpy) suite.
  * ``CapsuleCalibrator`` — the runtime per-block (N, M) corrector: bit-exact no-op when neutral
    (Invariant: off path is byte-identical), float32 preservation (the NEP-50 upcast trap), per-channel
    gain / polarity / integer-sample delay, and dead-capsule safety (never revive a masked channel).
  * ``estimate_calibration`` — the synthetic-signal estimator (gain from RMS, polarity from
    correlation sign, delay from cross-correlation) with honest per-channel confidence.

Mirrors the style of ``tests/test_preamp.py`` (deterministic, RNG-free blocks; ``out is x`` identity
for the off path). numpy-requiring tests skip when the ``[control]`` extra is absent.
"""
import json

import pytest

from conf_pipeline_control.calibration import (
    CalibrationError,
    CalibrationEstimate,
    CalibrationProfile,
    CapsuleCalibrator,
    estimate_calibration,
)
from conf_pipeline_control.preamp import _db_to_lin


def _np():
    """numpy or a clean skip — lets the pure-stdlib profile tests run in the no-numpy suite."""
    return pytest.importorskip("numpy")


# --------------------------------------------------------------------------- #
# CalibrationProfile — pure stdlib (no numpy), runs in the default suite
# --------------------------------------------------------------------------- #
def test_profile_defaults_are_neutral():
    p = CalibrationProfile()
    assert p.channels == 8
    assert p.gain_db == (0.0,) * 8
    assert p.delay_samples == (0,) * 8
    assert p.polarity == (1,) * 8
    assert p.reference_channel == 0
    assert p.is_neutral


def test_profile_is_neutral_false_when_any_correction():
    assert not CalibrationProfile(gain_db=(1.0,) + (0.0,) * 7).is_neutral
    assert not CalibrationProfile(polarity=(-1,) + (1,) * 7).is_neutral
    assert not CalibrationProfile(delay_samples=(2,) + (0,) * 7).is_neutral


def test_profile_json_roundtrip_is_camelcase(tmp_path):
    p = CalibrationProfile(
        sample_rate=44100.0, channels=8,
        gain_db=(0.0, 1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0),
        polarity=(1, -1, 1, 1, 1, 1, 1, 1),
        delay_samples=(0, 1, 0, 2, 0, 0, 0, 0),
        reference_channel=2, notes="bench A",
    )
    d = p.to_dict()
    # wire keys are camelCase (parity with the rest of the project's JSON)
    for key in ("sampleRate", "gainDb", "delaySamples", "referenceChannel", "createdAt"):
        assert key in d
    assert "gain_db" not in d
    # dict + JSON-text + on-disk round-trips all reconstruct the same profile
    assert CalibrationProfile.from_dict(d) == p
    assert CalibrationProfile.from_json(json.dumps(d)) == p
    fp = tmp_path / "cal.json"
    p.save(fp)
    assert CalibrationProfile.load(fp) == p


def test_profile_validate_rejects_wrong_length():
    with pytest.raises(CalibrationError):
        CalibrationProfile(channels=8, gain_db=(0.0, 0.0, 0.0)).validate()


def test_profile_validate_rejects_bad_polarity():
    with pytest.raises(CalibrationError):
        CalibrationProfile(channels=8, polarity=(1, 1, 1, 1, 1, 1, 1, 2)).validate()


def test_profile_load_missing_file_raises_controlled(tmp_path):
    with pytest.raises(CalibrationError):
        CalibrationProfile.load(tmp_path / "does_not_exist.json")


def test_profile_from_malformed_json_raises_controlled():
    with pytest.raises(CalibrationError):
        CalibrationProfile.from_json("{ this is not json")


def test_profile_exported_from_package_root():
    import conf_pipeline_control as cc
    assert cc.CalibrationProfile is CalibrationProfile
    assert cc.CapsuleCalibrator is CapsuleCalibrator
    assert cc.estimate_calibration is estimate_calibration


# --------------------------------------------------------------------------- #
# CapsuleCalibrator — runtime per-block corrector (numpy)
# --------------------------------------------------------------------------- #
def _block8(scale=0.1):
    np = _np()
    n = 64
    t = np.arange(n)[:, None]
    ch = np.arange(8)[None, :]
    return (scale * np.sin(2 * np.pi * (t + ch) / 16.0)).astype(np.float32)


def test_calibrator_neutral_is_identity_noop():
    """A neutral profile ⇒ process_block returns the SAME array object (byte-identical off path)."""
    np = _np()
    cal = CapsuleCalibrator(CalibrationProfile(channels=8))
    x = _block8()
    assert cal.is_neutral
    assert cal.process_block(x) is x


def test_calibrator_gain_scales_per_channel():
    np = _np()
    prof = CalibrationProfile(channels=8, gain_db=(6.0,) + (0.0,) * 7)
    cal = CapsuleCalibrator(prof)
    x = _block8()
    out = cal.process_block(x)
    assert out is not x
    assert np.allclose(out[:, 0], x[:, 0] * _db_to_lin(6.0), rtol=1e-5, atol=1e-7)
    assert np.allclose(out[:, 1:], x[:, 1:], rtol=1e-5, atol=1e-7)   # other channels untouched


def test_calibrator_preserves_float32():
    """Invariant: a float32 block stays float32 (no NEP-50 float64 upcast)."""
    np = _np()
    cal = CapsuleCalibrator(CalibrationProfile(channels=8, gain_db=(3.0,) * 8))
    out = cal.process_block(_block8())
    assert out.dtype == np.float32


def test_calibrator_does_not_mutate_input():
    np = _np()
    cal = CapsuleCalibrator(CalibrationProfile(channels=8, gain_db=(6.0,) * 8))
    x = _block8()
    before = x.copy()
    cal.process_block(x)
    assert np.array_equal(x, before)


def test_calibrator_polarity_flips_channel():
    np = _np()
    prof = CalibrationProfile(channels=8, polarity=(1, -1, 1, 1, 1, 1, 1, 1))
    cal = CapsuleCalibrator(prof)
    x = _block8()
    out = cal.process_block(x)
    assert np.allclose(out[:, 1], -x[:, 1], rtol=1e-5, atol=1e-7)
    assert np.allclose(out[:, 0], x[:, 0], rtol=1e-5, atol=1e-7)


def test_calibrator_integer_delay_shifts_channel():
    np = _np()
    prof = CalibrationProfile(channels=8, delay_samples=(0, 3, 0, 0, 0, 0, 0, 0))
    cal = CapsuleCalibrator(prof)
    x = np.zeros((8, 8), dtype=np.float32)
    x[0, :] = 1.0                                   # an impulse at t=0 on every channel
    out = cal.process_block(x)
    assert out[0, 1] == 0.0                          # channel 1 impulse pushed back by 3 samples
    assert np.isclose(out[3, 1], 1.0)
    assert np.allclose(out[:, 0], x[:, 0])           # channel 0 (delay 0) unchanged


def test_calibrator_delay_is_continuous_across_blocks():
    """The per-channel history ring carries the delayed tail into the next block (no edge loss)."""
    np = _np()
    prof = CalibrationProfile(channels=8, delay_samples=(0, 2, 0, 0, 0, 0, 0, 0))
    cal = CapsuleCalibrator(prof)
    b1 = np.zeros((4, 8), dtype=np.float32)
    b1[3, 1] = 1.0                                   # impulse at the LAST sample of block 1
    cal.process_block(b1)
    out2 = cal.process_block(np.zeros((4, 8), dtype=np.float32))
    assert np.isclose(out2[1, 1], 1.0)               # delayed by 2 ⇒ surfaces at index 1 of block 2


def test_calibrator_reset_clears_delay_state():
    np = _np()
    prof = CalibrationProfile(channels=8, delay_samples=(0, 2, 0, 0, 0, 0, 0, 0))
    cal = CapsuleCalibrator(prof)
    b1 = np.zeros((4, 8), dtype=np.float32)
    b1[3, 1] = 1.0
    cal.process_block(b1)
    cal.reset()
    out2 = cal.process_block(np.zeros((4, 8), dtype=np.float32))
    assert np.allclose(out2[:, 1], 0.0)              # stale tail wiped


def test_calibrator_active_mask_skips_dead_channel():
    """A masked (dead) capsule must NOT be gained up / revived, even if the profile names a gain for it."""
    np = _np()
    prof = CalibrationProfile(channels=8, gain_db=(0.0, 6.0, 0.0, 0.0, 0.0, 12.0, 0.0, 0.0))
    active = [True] * 8
    active[5] = False                                # capsule 5 dead
    cal = CapsuleCalibrator(prof, active_mask=active)
    x = _block8()
    out = cal.process_block(x)
    assert np.allclose(out[:, 5], x[:, 5], rtol=1e-5, atol=1e-7)              # dead channel untouched
    assert np.allclose(out[:, 1], x[:, 1] * _db_to_lin(6.0), rtol=1e-5, atol=1e-7)  # live channel corrected


# --------------------------------------------------------------------------- #
# estimate_calibration — synthetic-signal estimator (numpy)
# --------------------------------------------------------------------------- #
def _chirp(n=2048, sr=48000.0):
    """A deterministic in-band linear chirp (sharp autocorrelation ⇒ clean delay/polarity)."""
    np = _np()
    t = np.arange(n) / sr
    f0, f1 = 300.0, 3500.0
    k = (f1 - f0) / (n / sr)
    return np.sin(2 * np.pi * (f0 * t + 0.5 * k * t * t)).astype(np.float64)


def test_estimator_recovers_gain_offsets():
    np = _np()
    base = _chirp()
    gains = np.array([1.0, 2.0, 0.5, 1.0, 1.0, 1.0, 1.0, 1.0])
    cap = (base[:, None] * gains[None, :]).astype(np.float32)
    est = estimate_calibration(cap, sample_rate=48000.0, reference_channel=0,
                               estimate_polarity=False, estimate_delay=False)
    p = est.profile
    assert np.isclose(p.gain_db[0], 0.0, atol=1e-6)
    assert np.isclose(p.gain_db[1], -20 * np.log10(2.0), atol=0.2)    # 2x louder ⇒ correct down ~6 dB
    assert np.isclose(p.gain_db[2], -20 * np.log10(0.5), atol=0.2)    # half as loud ⇒ correct up ~6 dB


def test_estimator_detects_polarity_inversion():
    np = _np()
    base = _chirp()
    cap = np.stack([base, -base] + [base] * 6, axis=1).astype(np.float32)
    est = estimate_calibration(cap, sample_rate=48000.0, reference_channel=0, estimate_polarity=True)
    assert est.profile.polarity[1] == -1
    assert est.profile.polarity[0] == 1


def test_estimator_detects_integer_delay():
    np = _np()
    base = _chirp()
    d = 5
    ch1 = np.concatenate([np.zeros(d), base[:-d]])      # channel 1 lags channel 0 by 5 samples
    cap = np.stack([base, ch1] + [base] * 6, axis=1).astype(np.float32)
    est = estimate_calibration(cap, sample_rate=48000.0, reference_channel=0,
                               estimate_delay=True, max_delay_samples=32)
    # channel 1 arrives latest ⇒ align everything else to it: ch0 delayed by 5, ch1 by 0
    assert est.profile.delay_samples[0] == d
    assert est.profile.delay_samples[1] == 0


def test_estimator_flags_low_confidence_for_silent_channel():
    """A near-silent capsule cannot be calibrated — the estimator must NOT fake a correction."""
    np = _np()
    base = _chirp()
    cap = np.stack([base, np.zeros_like(base)] + [base] * 6, axis=1).astype(np.float32)
    est = estimate_calibration(cap, sample_rate=48000.0, reference_channel=0)
    assert 1 in est.low_confidence_channels
    assert est.profile.gain_db[1] == 0.0
    assert isinstance(est, CalibrationEstimate)


# --------------------------------------------------------------------------- #
# Host wiring — calibration runs at the FRONT of BOTH DSP chains, default-OFF
# --------------------------------------------------------------------------- #
def _tone8(n, sr, f=1000.0):
    """An in-band tone on all 8 capsules (energy in the 300–3800 Hz DOA band)."""
    np = _np()
    t = np.arange(n) / sr
    s = np.sin(2 * np.pi * f * t)
    return np.repeat(s[:, None], 8, axis=1).astype(np.float32)


def test_polaris_calibration_default_off_is_byte_identical():
    np = _np()
    from conf_pipeline_control.polaris_beamformer import PolarisBeamformer
    bf = PolarisBeamformer(device=None)
    assert bf._calib is None
    x = _block8()
    assert bf._apply_calibration(x) is x          # off ⇒ same object (pipeline byte-identical)


def test_polaris_calibration_param_builds_corrector():
    np = _np()
    from conf_pipeline_control.polaris_beamformer import PolarisBeamformer
    prof = CalibrationProfile(channels=8, sample_rate=44100.0, polarity=(1, -1, 1, 1, 1, 1, 1, 1))
    bf = PolarisBeamformer(device=None, sample_rate=44100.0, calibration=prof)
    assert bf._calib is not None
    x = _block8()
    out = bf._apply_calibration(x)
    assert np.allclose(out[:, 1], -x[:, 1], rtol=1e-5, atol=1e-7)


def test_polaris_calibration_runs_before_covariance_and_doa():
    """A per-capsule gain must show up in the spatial covariance the DOA thread reads — proof the
    correction is applied BEFORE beamforming / DOA, not after."""
    np = _np()
    from conf_pipeline_control.polaris_beamformer import PolarisBeamformer
    prof = CalibrationProfile(channels=8, sample_rate=44100.0, gain_db=(12.0,) + (0.0,) * 7)
    off = PolarisBeamformer(device=None, sample_rate=44100.0, beam_bandlimit_hz=None)
    on = PolarisBeamformer(device=None, sample_rate=44100.0, beam_bandlimit_hz=None, calibration=prof)
    off._setup_runtime()
    on._setup_runtime()
    blk = _tone8(off.blocksize, off.sample_rate)
    for _ in range(6):
        off.process_block(blk)
        on.process_block(blk)
    with off._cov_lock:
        d_off = float(np.mean(np.abs(off._cov[:, 0, 0])))
    with on._cov_lock:
        d_on = float(np.mean(np.abs(on._cov[:, 0, 0])))
    assert d_on > 4.0 * d_off                      # ch0 boosted +12 dB ⇒ ~15.8x auto-power in the cov


def test_polaris_calibration_channel_mismatch_falls_back_off():
    np = _np()
    from conf_pipeline_control.polaris_beamformer import PolarisBeamformer
    prof = CalibrationProfile(channels=4, gain_db=(6.0,) * 4, delay_samples=(0,) * 4, polarity=(1,) * 4)
    bf = PolarisBeamformer(device=None, calibration=prof)    # 8-ch engine vs 4-ch profile
    assert bf._calib is None                                  # incompatible ⇒ safely OFF, no crash
    x = _block8()
    assert bf._apply_calibration(x) is x


def test_polaris_calibration_path_missing_falls_back_off(tmp_path):
    np = _np()
    from conf_pipeline_control.polaris_beamformer import PolarisBeamformer
    bf = PolarisBeamformer(device=None, calibration_path=str(tmp_path / "nope.json"))
    assert bf._calib is None                                  # missing file ⇒ safely OFF


def test_polaris_calibration_samplerate_mismatch_drops_delays_keeps_gain():
    np = _np()
    from conf_pipeline_control.polaris_beamformer import PolarisBeamformer
    prof = CalibrationProfile(channels=8, sample_rate=16000.0,
                              gain_db=(6.0,) + (0.0,) * 7, delay_samples=(0, 4, 0, 0, 0, 0, 0, 0))
    bf = PolarisBeamformer(device=None, sample_rate=44100.0, calibration=prof)
    assert bf._calib is not None
    assert bf._calib.latency_samples == 0                     # sample-delays don't transfer ⇒ dropped
    x = _block8()
    out = bf._apply_calibration(x)
    assert np.allclose(out[:, 0], x[:, 0] * _db_to_lin(6.0), rtol=1e-5, atol=1e-7)   # gain kept


def test_live_calibration_default_off_is_byte_identical():
    np = _np()
    from conf_pipeline_control.live import LiveBeamController
    from conf_pipeline_control.geometry import sensibel_8
    lc = LiveBeamController(sensibel_8(radius_m=0.035))
    assert lc._calib is None
    x = _block8()
    assert lc._apply_calibration(x) is x


def test_live_calibration_param_applies_at_seam():
    np = _np()
    from conf_pipeline_control.live import LiveBeamController
    from conf_pipeline_control.geometry import sensibel_8
    prof = CalibrationProfile(channels=8, sample_rate=48000.0, polarity=(1, -1, 1, 1, 1, 1, 1, 1))
    lc = LiveBeamController(sensibel_8(radius_m=0.035), calibration=prof)
    assert lc._calib is not None
    x = _block8()
    out = lc._apply_calibration(x)
    assert np.allclose(out[:, 1], -x[:, 1], rtol=1e-5, atol=1e-7)
