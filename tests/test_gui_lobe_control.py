"""Offscreen probe for the LIVE Lobe Control section (Phase 11).

Constructs `LivePanel(AppState())` directly (NOT MainWindow, which hangs headless per CLAUDE.md) and checks
the lobe controls build a valid model, the summary + warnings update, and applying is a safe no-op without
hardware. Lobe Control shapes the beamformer pickup pattern (direction / focus / suppress / follow); it is
NOT calibration. Skipped without PySide6.
"""
import os

import pytest

pytest.importorskip("PySide6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@pytest.fixture(scope="module")
def qapp():
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    yield app


def _panel():
    from conf_pipeline_gui.panels.live import LivePanel
    from conf_pipeline_gui.state import AppState
    return LivePanel(AppState())


def test_lobe_panel_renders_offscreen(qapp):
    p = _panel()
    for attr in ("live_lobe_summary", "live_lobe_warnings", "live_lobe_angle", "live_lobe_seat",
                 "live_lobe_width", "live_lobe_null_mode", "live_lobe_null_angle", "live_lobe_preview"):
        assert hasattr(p, attr), attr
    from conf_pipeline_control.lobe_control import LobeControl
    from conf_pipeline_gui.panels.common import LobePreview
    assert isinstance(p.live_lobe_preview, LobePreview)
    lc = p._current_lobe_control()
    assert isinstance(lc, LobeControl) and lc.validate() is lc           # always a valid config


def test_default_lobe_is_safe_and_warns_calibration_off(qapp):
    p = _panel()
    txt = p.live_lobe_summary.text()
    assert "Lobe:" in txt and "calibration OFF" in txt                   # cal is OFF by default ⇒ honest warning
    lc = p._current_lobe_control()
    assert lc.beam_width != "narrow" and lc.nulls == []                  # safe default


def test_direction_updates_summary(qapp):
    p = _panel()
    p.live_listening_mode.setCurrentIndex(p.live_listening_mode.findData("manual"))  # manual ⇒ operator's direction
    p.live_lobe_seat.setCurrentIndex(0)                                  # "Manual angle"
    p.live_lobe_angle.setValue(35.0)
    p._update_lobe_summary()
    assert "35" in p.live_lobe_summary.text()


def test_width_preset_updates_summary(qapp):
    p = _panel()
    p.live_lobe_width.setCurrentIndex(p.live_lobe_width.findData("narrow"))
    p._update_lobe_summary()
    assert "width narrow" in p.live_lobe_summary.text()


def test_null_toggle_updates_summary_and_warns(qapp):
    p = _panel()
    p.live_lobe_null_mode.setCurrentIndex(p.live_lobe_null_mode.findData("angle"))
    p.live_lobe_null_angle.setValue(180.0)
    p._update_lobe_summary()
    assert "null 180" in p.live_lobe_summary.text()
    assert "reduce" in p.live_lobe_warnings.text().lower()               # honest "reduces, not mutes"


def test_placement_bad_warning_appears(qapp):
    p = _panel()
    p.set_lobe_placement_status("BAD")
    assert "placement BAD" in p.live_lobe_summary.text()
    assert "placement" in p.live_lobe_warnings.text().lower()


def test_apply_does_not_crash_without_engine(qapp):
    p = _panel()
    p.live_lobe_angle.setValue(20.0)
    p._apply_lobe_now()                                                  # no engine ⇒ safe no-op
    assert isinstance(p.live_lobe_summary.text(), str)


def test_whole_table_mode_does_not_force_narrow(qapp):
    p = _panel()
    p.live_listening_mode.setCurrentIndex(p.live_listening_mode.findData("table"))
    assert p._current_lobe_control().beam_width != "narrow"              # width is the operator's, not forced
