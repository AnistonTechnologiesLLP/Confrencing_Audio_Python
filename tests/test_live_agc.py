"""AGC in the single-array live path — LiveBeamController gained a TargetLoudnessAgc stage so the
Follow / Lock-to-seat / Whole-table modes normalize loudness (previously AGC was 2-kit-only)."""
import pytest

pytest.importorskip("numpy")

from conf_pipeline_control import sensibel_8
from conf_pipeline_control.live import LiveBeamController


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
