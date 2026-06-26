# tests/test_gui_simulate_warnings.py
"""Task 7: probe that SimulatePanel surfaces RoomCoverage.caveats as a 'Coverage warnings' list.

Constructs a single SimulatePanel headlessly (no MainWindow — avoids the local hang).
Installs a polaris-8 two-close-seat config (reuses the config helper from
test_coverage_warnings.py) into AppState and verifies that warnings_text() contains
a separability caveat after refresh().
"""
import os

import pytest

pytest.importorskip("PySide6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import conf_pipeline as cp  # noqa: E402
from conf_pipeline.model import Point2D, RectShape, RoomLayout, CoverageZone  # noqa: E402


def _cfg_two_close_seats_polaris8():
    """Two dedicated zones 0.6 m apart; polaris-8 array — triggers separability + grating-lobe caveats."""
    c = cp.create_config("Room", "2026-06-25T00:00:00Z")
    c.room = RoomLayout(
        vertices=[Point2D(-3, -3), Point2D(3, -3), Point2D(3, 3), Point2D(-3, 3)],
        height=3.0,
    )
    arr = cp.create_microphone_array("a1", "Array A", position=Point2D(0.0, 0.0))
    arr.profile_id = "polaris-8"
    arr.elevation = 0.85
    arr.zones = [
        CoverageZone(
            id="z1", type="dedicated",
            shape=RectShape(Point2D(-0.55, 0.95), 0.5, 0.5),
            always_on=False, label="seat1",
        ),
        CoverageZone(
            id="z2", type="dedicated",
            shape=RectShape(Point2D(0.05, 0.95), 0.5, 0.5),
            always_on=False, label="seat2",
        ),
    ]
    c = cp.add_device(c, arr)
    return c


@pytest.fixture(scope="module")
def qapp():
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


def test_simulate_panel_lists_coverage_caveats(qapp):
    """SimulatePanel.warnings_text() must contain a separability caveat for a polaris-8
    close-seat config after refresh()."""
    from conf_pipeline_gui.panels.simulate import SimulatePanel
    from conf_pipeline_gui.state import AppState

    state = AppState()
    state.set_config(_cfg_two_close_seats_polaris8())

    panel = SimulatePanel(state)   # __init__ calls refresh() already
    text = panel.warnings_text()

    assert "separat" in text.lower(), (
        f"Expected a separability caveat in warnings_text(); got: {text!r}"
    )
    panel.deleteLater()


def test_simulate_panel_warnings_empty_for_legacy_array(qapp):
    """Legacy generic-ceiling-array has no aperture data — coverage warnings list must be empty (no crash)."""
    from conf_pipeline_gui.panels.simulate import SimulatePanel
    from conf_pipeline_gui.state import AppState

    c = cp.create_config("Room", "2026-06-25T00:00:00Z")
    c.room = RoomLayout(
        vertices=[Point2D(-3, -3), Point2D(3, -3), Point2D(3, 3), Point2D(-3, 3)],
        height=3.0,
    )
    arr = cp.create_microphone_array("a1", "Array A", position=Point2D(0.0, 0.0))
    arr.profile_id = "generic-ceiling-array"
    arr.elevation = 0.85
    c = cp.add_device(c, arr)

    state = AppState()
    state.set_config(c)

    panel = SimulatePanel(state)
    text = panel.warnings_text()

    # should be empty or at most geometric caveats — never a separability/grating caveat
    assert "separat" not in text.lower(), (
        f"Legacy array should not produce separability caveats; got: {text!r}"
    )
    panel.deleteLater()
