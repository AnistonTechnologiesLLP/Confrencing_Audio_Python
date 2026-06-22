"""Auto-steer multiple-sectors UI: the dynamic add/remove sector list in the LIVE panel.

All hardware-free — constructs ``LivePanel`` directly (NOT MainWindow, which hangs headless on
Windows per CLAUDE.md) and pokes the sector-row methods. Verifies the GUI → SectorConfig mapping
(full width ÷2, one global front offset shared by every sector) and the add/remove lifecycle.
"""
import os

import pytest

pytest.importorskip("PySide6")

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import conf_pipeline as cp  # noqa: E402
from conf_pipeline.model import Point2D  # noqa: E402
import conf_pipeline_control as cc  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    yield app


def _config_with_array():
    c = cp.create_config("rt", "2026-01-01T00:00:00Z")
    c = cp.add_device(c, cp.create_microphone_array("A", "Array", position=Point2D(0.0, 0.0)))
    c = cp.set_array_bearing(c, "A", 0.0)
    return c


@pytest.fixture
def panel(qapp):
    from conf_pipeline_gui.panels.live import LivePanel
    from conf_pipeline_gui.state import AppState
    st = AppState()
    st.set_config(_config_with_array())
    p = LivePanel(st)
    yield p
    p.deleteLater()


def test_default_is_one_sector(panel):
    # first launch is byte-identical to the old single-sector default (centre 0°, 120° wide)
    assert len(panel._sector_rows) == 1
    sectors = panel._autosteer_sectors()
    assert len(sectors) == 1
    s = sectors[0]
    assert (s.center_deg, s.half_width_deg, s.front_offset_deg) == (0.0, 60.0, 0.0)


def test_add_second_sector_converts_with_shared_front_offset(panel):
    panel.live_front_offset.setValue(30.0)            # one global offset, shared by every sector
    row = panel._add_sector_row(center=90.0, width=80.0)
    assert row in panel._sector_rows
    sectors = panel._autosteer_sectors()
    assert len(sectors) == 2
    assert (sectors[0].center_deg, sectors[0].half_width_deg, sectors[0].front_offset_deg) == (0.0, 60.0, 30.0)
    assert (sectors[1].center_deg, sectors[1].half_width_deg, sectors[1].front_offset_deg) == (90.0, 40.0, 30.0)


def test_cannot_remove_last_sector(panel):
    assert len(panel._sector_rows) == 1
    panel._remove_sector_row(panel._sector_rows[0])    # refused — always keep >= 1
    assert len(panel._sector_rows) == 1
    # with a single sector the Remove button is disabled so the user can't even try
    assert panel._sector_rows[0].remove_btn.isEnabled() is False


def test_remove_drops_the_right_row(panel):
    first = panel._sector_rows[0]
    second = panel._add_sector_row(center=180.0, width=60.0)
    assert len(panel._sector_rows) == 2
    panel._remove_sector_row(second)
    assert panel._sector_rows == [first]
    assert panel._autosteer_sectors()[0].center_deg == 0.0


def test_remove_buttons_enable_only_with_two_or_more(panel):
    panel.live_autosteer.setChecked(True)
    panel._on_autosteer_toggled()
    # single row → Remove disabled
    assert panel._sector_rows[0].remove_btn.isEnabled() is False
    panel._add_sector_row(center=200.0, width=40.0)
    # two rows → both Removes enabled
    assert all(r.remove_btn.isEnabled() for r in panel._sector_rows)


def test_rows_enabled_track_autosteer_toggle(panel):
    panel.live_autosteer.setChecked(False)
    panel._on_autosteer_toggled()
    assert panel._sector_rows[0].center.isEnabled() is False
    assert panel.live_add_sector.isEnabled() is False
    panel.live_autosteer.setChecked(True)
    panel._on_autosteer_toggled()
    assert panel._sector_rows[0].center.isEnabled() is True
    assert panel.live_add_sector.isEnabled() is True


class _FakeMultiAutosteer:
    sectors = [cc.SectorConfig(0.0, 30.0, 0.0), cc.SectorConfig(180.0, 20.0, 0.0)]
    sector = sectors[0]
    active_nulls: list = []

    def detections(self):
        return []


def test_publish_overlay_lists_all_sectors(panel):
    panel._session_array_id = "A"
    panel._autosteer = _FakeMultiAutosteer()      # also makes _live_busy() True
    panel._publish_overlay()
    ov = panel.state.live_overlay
    assert ov is not None
    assert len(ov["sectors"]) == 2
    assert ov["sectors"][0][0] == 0.0 and ov["sectors"][1][0] == 180.0


def test_canvas_paints_multiple_sector_wedges(qapp):
    from conf_pipeline_gui.canvas import Canvas
    from conf_pipeline_gui.state import AppState
    st = AppState()
    st.set_config(_config_with_array())
    st.set_mode("live")
    st.view = "2d"
    st.set_live_overlay({
        "array_id": "A", "sector": (0.0, 30.0, 0.0),
        "sectors": [(0.0, 30.0, 0.0), (180.0, 20.0, 0.0)],
        "detections": [], "seat": None, "bearing": 0.0, "level": 0.3,
        "steer_az": None, "nulls": [], "kits": None, "connected": True,
    })
    cv = Canvas(st)
    cv.resize(420, 320)
    assert cv.grab().width() > 0                   # both wedges paint without raising
    cv.deleteLater()
