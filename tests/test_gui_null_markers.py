"""Feature D — active-null map markers: the engine telemetry → overlay → canvas chain.

The DSP that nulls already exists (compose_nulls / zone-cut); this surfaces WHICH directions are
being cut on the live room map. All hardware-free: the AutoSteerController null telemetry, the
LivePanel overlay publish, and the canvas paint (Canvas constructed standalone — not MainWindow).
"""
import os

import pytest

pytest.importorskip("PySide6")
pytest.importorskip("numpy")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import conf_pipeline as cp  # noqa: E402
from conf_pipeline.model import Point2D  # noqa: E402
import conf_pipeline_control as cc  # noqa: E402
from conf_pipeline_control.autosteer import AutoSteerController  # noqa: E402


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


# --------------------------------------------------------------------------- #
# Engine telemetry
# --------------------------------------------------------------------------- #
def test_autosteer_active_nulls_is_a_safe_copy():
    a = AutoSteerController(cc.sensibel_8(0.05), cc.SectorConfig(), samplerate=44100.0)
    a._active_nulls = [30.0, -45.0]
    got = a.active_nulls
    assert got == [30.0, -45.0]
    got.append(999.0)                       # mutating the read-out must not corrupt the controller state
    assert a.active_nulls == [30.0, -45.0]


# --------------------------------------------------------------------------- #
# LivePanel overlay publish
# --------------------------------------------------------------------------- #
class _FakeSector:
    center_deg, half_width_deg, front_offset_deg = 0.0, 60.0, 0.0


class _FakeAutosteer:
    sector = _FakeSector()
    active_nulls = [42.0, -120.0]
    def detections(self):
        return []


def test_publish_overlay_includes_active_nulls(qapp):
    from conf_pipeline_gui.panels.live import LivePanel
    from conf_pipeline_gui.state import AppState
    st = AppState()
    st.set_config(_config_with_array())
    p = LivePanel(st)
    p._session_array_id = "A"
    p._autosteer = _FakeAutosteer()           # makes _live_busy() True; supplies active_nulls
    p._publish_overlay()
    ov = st.live_overlay
    assert ov is not None and ov["nulls"] == [42.0, -120.0]
    p.deleteLater()


# --------------------------------------------------------------------------- #
# Canvas paint
# --------------------------------------------------------------------------- #
def test_canvas_paints_null_markers(qapp):
    from conf_pipeline_gui.canvas import Canvas
    from conf_pipeline_gui.state import AppState
    st = AppState()
    st.set_config(_config_with_array())
    st.set_mode("live")
    st.view = "2d"
    st.set_live_overlay({
        "array_id": "A", "sector": None, "detections": [], "seat": None,
        "bearing": 0.0, "level": 0.3, "steer_az": None,
        "nulls": [30.0, -90.0, 150.0], "kits": None, "connected": True,
    })
    cv = Canvas(st)
    cv.resize(420, 320)
    assert cv.grab().width() > 0              # barred-circle null markers paint without raising
    cv.deleteLater()
