"""Headless probe: DesignPanel._auto_zones_from_seating button + handler.

Constructs DesignPanel directly (no MainWindow — it hangs headless on this box).
Monkeypatches QMessageBox.information so no modal dialog blocks the test.
"""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import pytest

pytest.importorskip("PySide6")
from PySide6.QtWidgets import QApplication, QMessageBox

import conf_pipeline as cp
from conf_pipeline.model import Point2D, RoomObject, RoomLayout
from conf_pipeline_gui.state import AppState
from conf_pipeline_gui.panels.design import DesignPanel


@pytest.fixture(scope="module")
def qapp():
    yield QApplication.instance() or QApplication([])


def _state_with_polaris_and_chairs():
    cfg = cp.create_config("Test", "2026-06-26T00:00:00Z")
    cfg.room = RoomLayout(
        vertices=[Point2D(-4, -4), Point2D(4, -4), Point2D(4, 4), Point2D(-4, 4)],
        height=3.0,
        objects=[
            RoomObject(id="c1", kind="chair", position=Point2D(-1.8, 2.0)),
            RoomObject(id="c2", kind="chair", position=Point2D(1.8, 2.0)),
        ],
    )
    arr = cp.create_microphone_array("a1", "Array", position=Point2D(0.0, 0.0))
    arr.profile_id = "polaris-8"
    arr.bearing_deg = 0.0
    arr.elevation = 0.75
    cfg = cp.add_device(cfg, arr)
    st = AppState()
    st.set_config(cfg)
    st.select({"kind": "device", "id": "a1"})
    return st


def test_button_generates_zones_one_undo(qapp, monkeypatch):
    monkeypatch.setattr(QMessageBox, "information", staticmethod(lambda *a, **k: None))
    st = _state_with_polaris_and_chairs()
    panel = DesignPanel(st)
    panel.refresh()
    base = st._idx
    panel._auto_zones_from_seating()
    arr = next(d for d in st.config.devices if d.id == "a1")
    assert len(arr.zones) >= 1          # zones were generated
    assert st._idx == base + 1          # exactly one undo step
    panel.deleteLater()


def test_button_no_seats_toasts_no_change(qapp, monkeypatch):
    monkeypatch.setattr(QMessageBox, "information", staticmethod(lambda *a, **k: None))
    cfg = cp.create_config("Test", "2026-06-26T00:00:00Z")
    cfg.room = RoomLayout(
        vertices=[Point2D(-3, -3), Point2D(3, -3), Point2D(3, 3), Point2D(-3, 3)],
        height=3.0,
        objects=[RoomObject(id="t1", kind="table", position=Point2D(0, 1.5))],
    )
    arr = cp.create_microphone_array("a1", "Array", position=Point2D(0.0, 0.0))
    arr.profile_id = "polaris-8"
    cfg = cp.add_device(cfg, arr)
    st = AppState()
    st.set_config(cfg)
    st.select({"kind": "device", "id": "a1"})
    panel = DesignPanel(st)
    panel.refresh()
    base = st._idx
    panel._auto_zones_from_seating()
    assert st._idx == base              # no undo step (no change)
    panel.deleteLater()
