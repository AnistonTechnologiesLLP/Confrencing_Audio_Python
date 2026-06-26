"""Probe test: 'Cut (no pickup)' checkbox on coverage zones in DesignPanel."""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import pytest

pytest.importorskip("PySide6")
from PySide6.QtWidgets import QApplication

import conf_pipeline as cp
from conf_pipeline.model import Point2D, RectShape
from conf_pipeline_gui.state import AppState
from conf_pipeline_gui.panels.design import DesignPanel


@pytest.fixture(scope="module")
def qapp():
    yield QApplication.instance() or QApplication([])


def _state_with_zone():
    cfg = cp.create_config("Test", "2026-01-01T00:00:00Z")
    cfg = cp.add_device(cfg, cp.create_microphone_array("a1", "Array", position=Point2D(0.0, 0.0)))
    cfg = cp.add_coverage_zone(cfg, "a1", cp.dynamic_zone("z1", "Zone", RectShape(Point2D(0.0, 0.0), 1.0, 1.0)))
    st = AppState()
    st.set_config(cfg)
    return st


def _zone_type(st):
    return next(d for d in st.config.devices if d.id == "a1").zones[0].type


def test_cut_checkbox_flips_zone_one_undo(qapp):
    st = _state_with_zone()
    # Real selection shape: uses "zone_id" and "array_id" (not "id") — confirmed in design.py line 265
    st.select({"kind": "zone", "zone_id": "z1", "array_id": "a1"})
    panel = DesignPanel(st)
    panel.refresh()
    base = st._idx
    panel._toggle_zone_cut(True)       # cut → exclusion
    assert _zone_type(st) == "exclusion"
    assert st._idx == base + 1         # exactly one undo step
    panel._toggle_zone_cut(False)      # un-cut → dynamic
    assert _zone_type(st) == "dynamic"
    panel.deleteLater()
