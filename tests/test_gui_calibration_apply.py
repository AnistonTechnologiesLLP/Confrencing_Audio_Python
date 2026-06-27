"""Offscreen probe for the LIVE 'Load calibration profile…' apply action.

Constructs `LivePanel(AppState())` directly (NOT MainWindow, which hangs headless per CLAUDE.md) and
checks the apply path WITHOUT hardware: calibration is OFF by default (no path, no auto-enable on
startup), a valid profile is accepted + flows into the engine build cfg, and an invalid profile is
rejected without changing state. The actual rebuild/reconnect needs hardware and is exercised manually.
Skipped without PySide6.
"""
import json
import os

import pytest

pytest.importorskip("PySide6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from conf_pipeline_control.calibration import CalibrationProfile


@pytest.fixture(scope="module")
def qapp():
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    yield app


def _panel():
    from conf_pipeline_gui.panels.live import LivePanel
    from conf_pipeline_gui.state import AppState
    return LivePanel(AppState())


def _valid_cal(tmp_path) -> str:
    prof = CalibrationProfile(channels=8, sample_rate=44100.0, polarity=(1, -1, 1, 1, 1, 1, 1, 1),
                              gain_db=(1.5,) + (0.0,) * 7)
    p = tmp_path / "cal.json"
    p.write_text(json.dumps(prof.to_dict()))
    return str(p)


def test_calibration_off_by_default(qapp):
    p = _panel()
    assert p._calibration_path is None                      # not ON, no auto-enable on startup
    assert hasattr(p, "live_load_calib_btn")                # the Load action exists
    assert hasattr(p, "live_calib_profile_status")


def test_apply_valid_calibration_sets_path(qapp, tmp_path):
    p = _panel()
    path = _valid_cal(tmp_path)
    assert p.apply_calibration_profile(path) is True
    assert p._calibration_path == path
    assert "Calibration" in p.live_calib_profile_status.text()


def test_apply_invalid_calibration_is_rejected(qapp, tmp_path):
    p = _panel()
    bad = tmp_path / "bad.json"
    bad.write_text("{ not valid json")
    assert p.apply_calibration_profile(str(bad)) is False
    assert p._calibration_path is None                      # validation failure ⇒ no state change


def test_applied_calibration_flows_into_engine_cfg(qapp, tmp_path):
    p = _panel()
    assert "calibration_path" not in p._beameng_steered_cfg({"radius_m": 0.04})   # absent by default
    path = _valid_cal(tmp_path)
    p.apply_calibration_profile(path)
    assert p._beameng_steered_cfg({"radius_m": 0.04}).get("calibration_path") == path
