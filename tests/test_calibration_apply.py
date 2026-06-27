"""Apply-an-existing-calibration-JSON feature (engine + diagnostics level).

Proves the chain the GUI relies on: a calibration is **OFF by default** on every live engine, and only
turns **ON after an explicit calibration_path is applied** — surfaced by the operator diagnostics as
"Calibration ON". Calibration *math* is unchanged (covered by tests/test_calibration.py); this file only
covers the apply path: AutoSteerController forwarding + OperatorStatus reflecting the result. Hardware-free
(device=None — no stream is opened until .start()). Needs numpy ([control] extra).
"""
import json

import pytest

np = pytest.importorskip("numpy")

import conf_pipeline_control as cc
from conf_pipeline_control.calibration import CalibrationProfile
from conf_pipeline_control.live import LiveBeamController
from conf_pipeline_control.operator import OperatorStatus


def _geom():
    return cc.sensibel_8(radius_m=0.035)


def _nonneutral_profile_json(tmp_path) -> str:
    """A valid, NON-neutral 8-ch profile (so the corrector actually engages, not a no-op)."""
    prof = CalibrationProfile(channels=8, sample_rate=44100.0, polarity=(1, -1, 1, 1, 1, 1, 1, 1),
                              gain_db=(1.5,) + (0.0,) * 7)
    p = tmp_path / "cal.json"
    p.write_text(json.dumps(prof.to_dict()))
    return str(p)


def test_live_engine_calibration_off_by_default():
    eng = LiveBeamController(_geom())
    assert eng._calib is None                                   # no calibration unless one is applied
    assert OperatorStatus.build(engine=eng).calibration_section()["enabled"] is False


def test_operator_status_calibration_on_after_apply(tmp_path):
    path = _nonneutral_profile_json(tmp_path)
    eng = LiveBeamController(_geom(), calibration_path=path)
    assert eng._calib is not None                               # the corrector engaged
    sec = OperatorStatus.build(engine=eng, calibration_path=path).calibration_section()
    assert sec["enabled"] is True and "ON" in sec["status"]
    assert sec["profilePath"] == path


def test_autosteer_calibration_off_by_default():
    a = cc.AutoSteerController(_geom(), cc.SectorConfig(center_deg=0.0, half_width_deg=45.0), device=None)
    assert a.ctrl._calib is None                                # default OFF on the auto-steer path too


def test_autosteer_forwards_calibration_path(tmp_path):
    path = _nonneutral_profile_json(tmp_path)
    a = cc.AutoSteerController(_geom(), cc.SectorConfig(center_deg=0.0, half_width_deg=45.0),
                               device=None, calibration_path=path)
    assert a.ctrl._calib is not None                            # forwarded to the inner LiveBeamController
    assert OperatorStatus.build(engine=a.ctrl).calibration_section()["enabled"] is True


def test_bad_calibration_path_degrades_off(tmp_path):
    eng = LiveBeamController(_geom(), calibration_path=str(tmp_path / "nope.json"))
    assert eng._calib is None                                   # missing/bad file ⇒ safely OFF (never raises)
    assert OperatorStatus.build(engine=eng).calibration_section()["enabled"] is False


def test_neutral_profile_stays_off(tmp_path):
    prof = CalibrationProfile(channels=8, sample_rate=44100.0)   # all-identity ⇒ neutral
    p = tmp_path / "neutral.json"
    p.write_text(json.dumps(prof.to_dict()))
    eng = LiveBeamController(_geom(), calibration_path=str(p))
    assert eng._calib is None                                   # a no-op profile stays OFF (correct)
