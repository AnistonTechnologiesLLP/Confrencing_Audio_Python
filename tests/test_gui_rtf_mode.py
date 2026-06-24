# tests/test_gui_rtf_mode.py
import os
import pytest
pytest.importorskip("PySide6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import conf_pipeline as cp
from conf_pipeline.model import Point2D
import conf_pipeline_control as cc


@pytest.fixture(scope="module")
def qapp():
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


def test_beameng_mode_combo_has_rtf_mvdr(qapp):
    from conf_pipeline_gui.panels.live import LivePanel
    from conf_pipeline_gui.state import AppState
    c = cp.create_config("rt", "2026-01-01T00:00:00Z")
    c = cp.add_device(c, cp.create_microphone_array("A", "Array", position=Point2D(0.0, 0.0)))
    st = AppState(); st.set_config(c)
    p = LivePanel(st)
    modes = [p.live_beameng_mode.itemData(i) for i in range(p.live_beameng_mode.count())]
    assert cc.MODE_RTF_MVDR in modes
    p.deleteLater()
