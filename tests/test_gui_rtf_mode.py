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


def test_beameng_rtf_mode_wiring(qapp):
    """RTF-MVDR item: _beameng_mode() returns 'steered'; steered_cfg carries MODE_RTF_MVDR."""
    from conf_pipeline_gui.panels.live import LivePanel
    from conf_pipeline_gui.state import AppState
    c = cp.create_config("rt", "2026-01-01T00:00:00Z")
    c = cp.add_device(c, cp.create_microphone_array("A", "Array", position=Point2D(0.0, 0.0)))
    st = AppState(); st.set_config(c)
    p = LivePanel(st)

    # Select the RTF-MVDR item in the strategy picker
    idx = p.live_beameng_mode.findData(cc.MODE_RTF_MVDR)
    assert idx >= 0, "RTF-MVDR not found in combo"
    p.live_beameng_mode.setCurrentIndex(idx)

    # (a) _beameng_mode() must return "steered" — BeamEngine only knows "steered" / "grid"
    assert p._beameng_mode() == "steered", (
        f"_beameng_mode() returned {p._beameng_mode()!r}, expected 'steered'"
    )

    # (b) the steered_cfg the panel would pass to BeamEngine must carry MODE_RTF_MVDR
    steered_cfg = p._beameng_steered_cfg({})
    assert steered_cfg.get("mode") == cc.MODE_RTF_MVDR, (
        f"steered_cfg['mode'] = {steered_cfg.get('mode')!r}, expected {cc.MODE_RTF_MVDR!r}"
    )

    p.deleteLater()
