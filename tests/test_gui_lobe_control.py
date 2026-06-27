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


def test_no_steered_engine_warns_instead_of_silent(qapp):
    # Manual lobe direction with NO steered engine running must TELL the operator (not silently no-op).
    p = _panel()
    p.live_listening_mode.setCurrentIndex(p.live_listening_mode.findData("manual"))
    p.live_lobe_seat.setCurrentIndex(0)                                  # Manual angle ⇒ fixed look
    p.live_lobe_angle.setValue(40.0)
    p._update_lobe_summary()
    assert p._beam_engine is None
    assert "A/B engine" in p.live_lobe_warnings.text()                   # honest feedback on how to steer


def test_manual_angle_dial_usable_in_steered_modes(qapp):
    # Picking "Manual angle" in a steerable listening mode (seat / manual) enables the dial (fixed look).
    p = _panel()
    for mode in ("seat", "manual"):
        p.live_listening_mode.setCurrentIndex(p.live_listening_mode.findData(mode))
        p.live_lobe_seat.setCurrentIndex(0)                             # Manual angle
        p._update_lobe_summary()
        assert p._current_lobe_control().mode == "fixed", mode
        assert p.live_lobe_angle.isEnabled(), mode


def test_apply_actually_steers_a_running_engine(qapp):
    # With a steered engine present, _apply_lobe_now MUST call set_steering(angle) + set_nulls(bearings).
    p = _panel()

    class _StubEngine:
        def __init__(self):
            self.steered = []
            self.nulled = []

        def set_steering(self, az):
            self.steered.append(az)

        def set_nulls(self, bearings):
            self.nulled.append(bearings)

    stub = _StubEngine()
    p._beam_engine = stub
    p.live_listening_mode.setCurrentIndex(p.live_listening_mode.findData("manual"))
    p.live_lobe_seat.setCurrentIndex(0)
    p.live_lobe_angle.setValue(35.0)
    p.live_lobe_null_mode.setCurrentIndex(p.live_lobe_null_mode.findData("angle"))
    p.live_lobe_null_angle.setValue(180.0)
    p._apply_lobe_now()
    assert stub.steered and abs(stub.steered[-1] - 35.0) < 1e-6         # steered to the lobe angle
    assert stub.nulled and stub.nulled[-1] == [180.0]                  # and the null applied
    p._beam_engine = None


def test_lobe_preview_is_a_draggable_aim_dial(qapp):
    # The preview maps a dragged point to an azimuth (0°=up, clockwise) so you aim by dragging, not typing.
    from conf_pipeline_gui.panels.common import LobePreview
    lp = LobePreview()
    lp.resize(200, 200)
    c = lp.rect().center()
    cx, cy = c.x(), c.y()
    assert abs(lp._az_for_point(cx, cy - 50)) < 6           # straight up  ≈ 0°
    assert abs(lp._az_for_point(cx + 50, cy) - 90) < 6     # right        ≈ +90°
    assert abs(lp._az_for_point(cx - 50, cy) + 90) < 6     # left         ≈ -90°
    assert abs(abs(lp._az_for_point(cx, cy + 50)) - 180) < 6  # down       ≈ ±180°
    assert hasattr(lp, "aimed")                              # emits the dragged azimuth


def test_dragging_preview_drives_lobe_direction(qapp):
    # Dragging the preview sets the lobe direction with NO sidebar dial / degree typing.
    p = _panel()
    p.live_listening_mode.setCurrentIndex(p.live_listening_mode.findData("manual"))
    p.live_lobe_preview.aimed.emit(42.0)                     # simulate a drag-to-42°
    assert abs(p.live_lobe_angle.value() - 42.0) < 1e-6
    assert "42" in p.live_lobe_summary.text()               # the drag drove the lobe
