"""Headless probe: _apply_learned_bearing sets the array bearing via pure solve (no audio).

The live DOA capture (the button's full flow) is hardware and is not tested here.
The SOLVE WIRING — _apply_learned_bearing — is pure geometry and IS tested here.
"""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import pytest

pytest.importorskip("PySide6")
from PySide6.QtWidgets import QApplication

import conf_pipeline as cp
from conf_pipeline.model import Point2D
from conf_pipeline_gui.state import AppState
from conf_pipeline_gui.panels.design import DesignPanel


@pytest.fixture(scope="module")
def qapp():
    yield QApplication.instance() or QApplication([])


def test_apply_learned_bearing_sets_array_bearing(qapp):
    cfg = cp.create_config("T", "2026-01-01T00:00:00Z")
    arr = cp.create_microphone_array("a1", "Array", position=Point2D(0.0, 0.0))
    cfg = cp.add_device(cfg, arr)
    st = AppState()
    st.set_config(cfg)
    st.select({"kind": "device", "id": "a1"})
    panel = DesignPanel(st)
    panel.refresh()
    # reference 2 m straight ahead (+Y); DOA measured it at 30° → bearing should be 0 - 30 = -30 → 330
    panel._apply_learned_bearing("a1", Point2D(0.0, 2.0), 30.0)
    arr2 = next(d for d in st.config.devices if d.id == "a1")
    assert abs(((arr2.bearing_deg - 330.0 + 180.0) % 360.0) - 180.0) < 1e-6
    panel.deleteLater()


def test_apply_learned_bearing_one_undo(qapp):
    """One call to _apply_learned_bearing creates exactly one undo step."""
    cfg = cp.create_config("T", "2026-01-01T00:00:00Z")
    arr = cp.create_microphone_array("b1", "Array", position=Point2D(0.0, 0.0))
    cfg = cp.add_device(cfg, arr)
    st = AppState()
    st.set_config(cfg)
    st.select({"kind": "device", "id": "b1"})
    idx_before = st._idx
    panel = DesignPanel(st)
    panel.refresh()
    panel._apply_learned_bearing("b1", Point2D(0.0, 2.0), 45.0)
    assert st._idx == idx_before + 1
    panel.deleteLater()


def test_apply_learned_bearing_position_none_is_guarded(qapp):
    """When the array has no position the call is a no-op (toast guard)."""
    cfg = cp.create_config("T", "2026-01-01T00:00:00Z")
    arr = cp.create_microphone_array("c1", "Array")  # no position
    cfg = cp.add_device(cfg, arr)
    st = AppState()
    st.set_config(cfg)
    st.select({"kind": "device", "id": "c1"})
    panel = DesignPanel(st)
    panel.refresh()
    cfg_before = st.config
    panel._apply_learned_bearing("c1", Point2D(0.0, 2.0), 30.0)
    # config must be unchanged — position guard fired
    assert st.config is cfg_before
    panel.deleteLater()
