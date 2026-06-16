"""Room-aware LIVE seat readout (Commit C): the dominant live DOA → nearest room
seat, surfaced in the Live panel readout and as a canvas highlight.

Two layers, both hardware-free:
- ``_dominant_seat`` — the pure selection + mapping helper (no Qt session needed).
- the canvas ``_paint_live_overlay`` seat branch — exercised via a published overlay.

Skipped when PySide6 is absent.
"""
import os

import pytest

pytest.importorskip("PySide6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import conf_pipeline as cp  # noqa: E402
from conf_pipeline.model import Point2D, RoomLayout, RoomObject, SeatAnchor  # noqa: E402
from conf_pipeline_gui.panels.live import _dominant_seat  # noqa: E402


def _config_with_array_and_seats(bearing=0.0, position=Point2D(0.0, 0.0)):
    c = cp.create_config("rt", "2026-01-01T00:00:00Z")
    c = cp.add_device(c, cp.create_microphone_array("A", "Array", position=position))
    if bearing is not None:
        c = cp.set_array_bearing(c, "A", bearing)
    c.room = RoomLayout(
        vertices=[Point2D(-3, -3), Point2D(3, -3), Point2D(3, 3), Point2D(-3, 3)],
        height=3.0, units="meters",
        objects=[RoomObject(
            id="sofa", kind="sofa", position=Point2D(0.0, 3.0),
            seats=[SeatAnchor(position=Point2D(0.0, 3.0)),     # sofa-seat1 -> bearing 0 (north)
                   SeatAnchor(position=Point2D(3.0, 0.0))],    # sofa-seat2 -> bearing 90 (east)
        )],
    )
    return c


# --------------------------------------------------------------------------- #
# _dominant_seat — pure selection + mapping
# --------------------------------------------------------------------------- #
def test_dominant_seat_maps_the_loudest_detection():
    c = _config_with_array_and_seats(bearing=0.0)
    m = _dominant_seat(c, "A", [(0.0, 10.0, True)])
    assert m is not None and m.seat_id == "sofa-seat1"
    assert _dominant_seat(c, "A", [(90.0, 10.0, True)]).seat_id == "sofa-seat2"


def test_dominant_seat_prefers_in_sector_over_a_louder_outsider():
    # the quieter IN-sector talker (the one actually followed) wins over a louder outsider
    c = _config_with_array_and_seats(bearing=0.0)
    dets = [(0.0, 9.0, False), (90.0, 3.0, True)]
    assert _dominant_seat(c, "A", dets).seat_id == "sofa-seat2"


def test_dominant_seat_falls_back_to_loudest_when_none_in_sector():
    c = _config_with_array_and_seats(bearing=0.0)
    dets = [(0.0, 9.0, False), (90.0, 3.0, False)]
    assert _dominant_seat(c, "A", dets).seat_id == "sofa-seat1"   # loudest overall


def test_dominant_seat_uses_the_array_bearing():
    # re-mounting the array (bearing 90) re-maps the same detected azimuth
    c = _config_with_array_and_seats(bearing=90.0)
    assert _dominant_seat(c, "A", [(0.0, 10.0, True)]).seat_id == "sofa-seat2"   # 0 + 90 -> east


def test_dominant_seat_none_cases():
    assert _dominant_seat(_config_with_array_and_seats(), "A", []) is None        # no detections
    assert _dominant_seat(_config_with_array_and_seats(), None, [(0.0, 9.0, True)]) is None  # no session array
    # array exists but has no bearing -> orientation unknown -> None
    assert _dominant_seat(_config_with_array_and_seats(bearing=None), "A", [(0.0, 9.0, True)]) is None
    # detection with a null azimuth is skipped
    assert _dominant_seat(_config_with_array_and_seats(), "A", [(None, 9.0, True)]) is None


# --------------------------------------------------------------------------- #
# Canvas paint — the seat-highlight branch of _paint_live_overlay
# --------------------------------------------------------------------------- #
@pytest.fixture(scope="module")
def qapp():
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture
def win(qapp):
    from conf_pipeline_gui.app import MainWindow, build_qss
    qapp.setStyleSheet(build_qss("dark"))
    w = MainWindow()
    w.show()
    yield w
    w.close()


def test_live_overlay_with_seat_paints(win):
    st = win.state
    st.set_config(_config_with_array_and_seats(bearing=0.0))
    st.set_mode("live")
    st.view = "2d"
    st.set_live_overlay({
        "array_id": "A",
        "sector": (0.0, 60.0, 0.0),               # exercise the bearing-rotated wedge branch
        "detections": [(0.0, 10.0, True)],
        "seat": {"id": "sofa-seat2", "x": 3.0, "y": 0.0},
        "bearing": 90.0,                          # rotated array: rays + wedge lifted into room frame
        "level": 0.5,
        "connected": True,
    })
    assert win.canvas.grab().width() > 0          # 2D paints the wedge, ray and seat highlight
    st.view = "3d"
    assert win.canvas.grab().width() > 0          # 3D falls back to the hint, no crash


def test_beameng_seat_nulling_pushes_other_seats(win):
    """The A/B-engine 'Null the other seats' path: with a matched target seat, push the OTHER seats'
    bearings to the steered back-end via the engine; clear when disabled."""
    import conf_pipeline_control as cc
    st = win.state
    st.set_config(_config_with_array_and_seats(bearing=0.0))
    panel = win.panels["live"]
    panel._session_array_id = "A"
    eng = cc.BeamEngine(device=None, mode="steered", steered_cfg={"mode": "superdirective"})
    panel._beam_engine = eng
    panel._live_seat = cp.nearest_seat_for_array(st.config, "A", 0.0)        # listening to sofa-seat1 (north)
    assert panel._live_seat is not None and panel._live_seat.seat_id == "sofa-seat1"
    panel.live_beameng_mode.setCurrentIndex(panel.live_beameng_mode.findData("steered"))
    panel.live_beameng_nullseats.setChecked(True)
    panel._push_seat_nulls()
    # the one OTHER seat (sofa-seat2) is pushed to the steered back-end as a null
    assert eng._steered._explicit_nulls == cp.seat_null_azimuths(st.config, "A", exclude_seat_id="sofa-seat1")
    panel.live_beameng_nullseats.setChecked(False)                          # disabling clears the pushed nulls
    panel._push_seat_nulls()
    assert eng._steered._explicit_nulls == []
