"""AGC in the single-array live path — LiveBeamController gained a TargetLoudnessAgc stage so the
Follow / Lock-to-seat / Whole-table modes normalize loudness (previously AGC was 2-kit-only)."""
import pytest

np = pytest.importorskip("numpy")

from conf_pipeline_control import sensibel_8
from conf_pipeline_control.agc import DEFAULT_AGC_CEILING_DB, TargetLoudnessAgc
from conf_pipeline_control.live import LiveBeamController

_AGC_CEILING = 10.0 ** (DEFAULT_AGC_CEILING_DB / 20.0)


def test_agc_output_is_peak_safe_under_boost():
    """The AGC's RMS gain ignores crest factor — boosting a quiet, peaky signal toward the target drove
    peaks to 3-5x full scale and hard-clipped (the residual after the makeup was fixed). The AGC output
    limiter must keep peaks under the ceiling with zero clipped samples while still raising the level."""
    agc = TargetLoudnessAgc(target_db=-12.0)               # loud target → the AGC boosts hard
    rng = np.random.default_rng(2)
    out = []
    for _ in range(400):
        b = (0.05 * rng.standard_normal(512)).astype(np.float32)        # quiet → AGC ramps the gain up
        b[[60, 200, 400]] = np.array([0.4, -0.45, 0.5], np.float32)     # high crest factor (peaks the RMS misses)
        out.append(agc.process(b))
    y = np.concatenate(out)
    assert float(np.max(np.abs(y))) <= _AGC_CEILING + 1e-3             # capped at the ceiling
    assert float(np.mean(np.abs(y) >= 0.999)) == 0.0                   # zero hard-clipped samples
    assert 20.0 * np.log10(np.sqrt(np.mean(y ** 2)) + 1e-12) > -24.0   # still boosted the quiet input


def test_agc_reset_clears_limiter():
    agc = TargetLoudnessAgc(target_db=-12.0)
    agc.process((0.5 * np.ones(512, np.float32)))          # engage the limiter (loud block)
    agc.reset()
    assert agc._lim == 1.0                                  # limiter duck dropped on reconnect


def test_livebeam_builds_agc_when_target_set():
    ctrl = LiveBeamController(sensibel_8(), agc_target_db=-20.0)
    assert ctrl._agc is not None                       # AGC engaged when a target is given


def test_livebeam_no_agc_by_default():
    ctrl = LiveBeamController(sensibel_8())
    assert ctrl._agc is None                            # off unless a target is set (escape hatch)


def test_autosteer_threads_agc_target_to_controller():
    from conf_pipeline_control.autosteer import AutoSteerController
    import conf_pipeline_control as cc
    a = AutoSteerController(sensibel_8(), cc.SectorConfig(), agc_target_db=-20.0)
    assert a.ctrl._agc is not None                      # AutoSteer → LiveBeamController carries the AGC


def test_livebeam_dereverb_param_builds_the_stage():
    ctrl = LiveBeamController(sensibel_8(), dereverb=True)
    ctrl._build_post_nr()                               # device-free build
    assert ctrl._dereverb is not None                  # the room-echo (dereverb) stage engages
