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


def test_beameng_monitor_mute_gain_route_to_engine(win):
    """With the A/B engine active, _active_ctl resolves to it and the LIVE Mute/Gain controls route to
    the engine's monitor trim (so monitoring is mute/gain-controllable)."""
    import conf_pipeline_control as cc
    st = win.state
    st.set_config(_config_with_array_and_seats(bearing=0.0))
    panel = win.panels["live"]
    eng = cc.BeamEngine(device=None, mode="steered", monitor=True)
    panel._beam_engine = eng
    assert panel._active_ctl() is eng                              # engine is the active control surface
    panel.live_mute.setChecked(True)
    panel._live_toggle_mute()
    assert eng.muted is True                                       # Mute routes to the engine
    panel.live_mute.setChecked(False)
    panel._live_toggle_mute()
    assert eng.muted is False
    panel.live_gain.setValue(-6)
    panel._live_gain_changed(-6)
    assert eng.gain_db == -6.0                                     # Gain routes to the engine


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


def test_beameng_lock_to_seat_pins_the_steered_beam(win):
    """Snap-steer: picking a seat in the Lock-to-seat combo pins the steered beam to that seat's azimuth
    (via the engine), tracks the locked seat, and makes seat-nulling keep the LOCKED seat; 'Follow talker'
    resumes DOA-follow."""
    import conf_pipeline_control as cc
    st = win.state
    st.set_config(_config_with_array_and_seats(bearing=0.0))
    panel = win.panels["live"]
    panel._session_array_id = "A"
    eng = cc.BeamEngine(device=None, mode="steered", steered_cfg={"mode": "superdirective"})
    panel._beam_engine = eng
    panel.live_beameng_mode.setCurrentIndex(panel.live_beameng_mode.findData("steered"))
    panel._refresh_beameng_lockseat()
    assert panel.live_beameng_lockseat.count() == 4                          # Follow + Manual angle + the two seats

    panel.live_beameng_lockseat.setCurrentIndex(panel.live_beameng_lockseat.findData("sofa-seat2"))
    panel._on_beameng_lockseat_changed()
    assert panel._beameng_locked_seat == "sofa-seat2"
    assert eng._steered.steer_to_doa is False and eng._steered._steered_az == 90.0   # pinned to seat 2 (east)

    # snap-steer + seat-nulling: the nulls keep the LOCKED seat (null seat 1, not seat 2)
    panel.live_beameng_nullseats.setChecked(True)
    panel._push_seat_nulls()
    assert eng._steered._explicit_nulls == cp.seat_null_azimuths(st.config, "A", exclude_seat_id="sofa-seat2")

    panel.live_beameng_lockseat.setCurrentIndex(0)                          # 'Follow talker (DOA)'
    panel._on_beameng_lockseat_changed()
    assert panel._beameng_locked_seat is None and eng._steered.steer_to_doa is True


def test_beameng_lock_survives_a_grid_roundtrip(win):
    """A/B switching steered→grid→steered re-pins the lock (set_mode's reset_transient clears the steered
    back-end's _steered_az, so the GUI re-applies it on the switch back)."""
    import conf_pipeline_control as cc
    st = win.state
    st.set_config(_config_with_array_and_seats(bearing=0.0))
    panel = win.panels["live"]
    panel._session_array_id = "A"
    eng = cc.BeamEngine(device=None, mode="steered", steered_cfg={"mode": "superdirective"})
    panel._beam_engine = eng
    panel.live_beameng_mode.setCurrentIndex(panel.live_beameng_mode.findData("steered"))
    panel._refresh_beameng_lockseat()
    panel.live_beameng_lockseat.setCurrentIndex(panel.live_beameng_lockseat.findData("sofa-seat2"))
    panel._on_beameng_lockseat_changed()
    assert eng._steered._steered_az == 90.0                                  # pinned to seat 2

    panel.live_beameng_mode.setCurrentIndex(panel.live_beameng_mode.findData("grid"))
    panel._on_beameng_mode_changed()                                        # set_mode("grid")
    panel.live_beameng_mode.setCurrentIndex(panel.live_beameng_mode.findData("steered"))
    panel._on_beameng_mode_changed()                                        # set_mode("steered") + re-pin
    assert panel._beameng_locked_seat == "sofa-seat2"
    assert eng._steered.steer_to_doa is False and eng._steered._steered_az == 90.0   # lock restored


def test_beameng_locked_steering_repins_on_pose_change(win):
    """Stale-lock fix: editing the array's bearing mid-session re-pins the locked seat to its NEW
    array-relative azimuth on the next tick (look stays consistent with the live seat-null geometry)."""
    import conf_pipeline_control as cc
    st = win.state
    st.set_config(_config_with_array_and_seats(bearing=0.0))
    panel = win.panels["live"]
    panel._session_array_id = "A"
    eng = cc.BeamEngine(device=None, mode="steered", steered_cfg={"mode": "superdirective"})
    panel._beam_engine = eng
    panel.live_beameng_mode.setCurrentIndex(panel.live_beameng_mode.findData("steered"))
    panel._refresh_beameng_lockseat()
    panel.live_beameng_lockseat.setCurrentIndex(panel.live_beameng_lockseat.findData("sofa-seat2"))
    panel._on_beameng_lockseat_changed()
    assert eng._steered._steered_az == 90.0 and panel._beameng_locked_az == 90.0     # seat 2 (east), bearing 0

    st.set_config(cp.set_array_bearing(st.config, "A", 90.0))               # re-mount: seat 2 now at array az 0
    panel._push_locked_steering()
    assert panel._beameng_locked_az == 0.0 and eng._steered._steered_az == 0.0       # re-pinned to the new pose
    az_before = eng._steered._steered_az
    panel._push_locked_steering()                                           # idempotent when the pose is unchanged
    assert eng._steered._steered_az == az_before


def test_beameng_manual_angle_lock_pins_the_beam(win):
    """Manual lock: 'Manual angle' + the dial pins the steered beam to a fixed array-relative angle
    (disabling DOA-follow) and is NOT a seat lock; 'Follow talker' resumes following + disables the dial."""
    import conf_pipeline_control as cc
    st = win.state
    st.set_config(_config_with_array_and_seats(bearing=0.0))
    panel = win.panels["live"]
    panel._session_array_id = "A"
    eng = cc.BeamEngine(device=None, mode="steered", steered_cfg={"mode": "superdirective"})
    panel._beam_engine = eng
    panel.live_beameng_mode.setCurrentIndex(panel.live_beameng_mode.findData("steered"))
    panel._refresh_beameng_lockseat()

    panel.live_beameng_lockseat.setCurrentIndex(panel.live_beameng_lockseat.findData("__manual__"))
    panel._on_beameng_lockseat_changed()
    assert panel.live_beameng_angle.isEnabled()                            # dial enabled in manual mode
    panel.live_beameng_angle.setValue(100.0)
    panel._on_beameng_angle_changed(100.0)
    assert panel._beameng_locked_manual_az == 100.0 and panel._beameng_locked_seat is None
    assert eng._steered.steer_to_doa is False and eng._steered._steered_az == 100.0

    panel.live_beameng_lockseat.setCurrentIndex(0)                         # 'Follow talker (DOA)'
    panel._on_beameng_lockseat_changed()
    assert panel._beameng_locked_manual_az is None and eng._steered.steer_to_doa is True
    assert not panel.live_beameng_angle.isEnabled()                        # dial disabled when following


def test_beameng_map_click_sets_manual_angle(win):
    """Click-to-aim: a clicked room point becomes a manual lock — the combo flips to 'Manual angle', the
    dial shows the point's array-relative azimuth and the beam pins there (returns True). A click with no
    array bearing is declined (returns False) so the click still falls through to normal selection."""
    import conf_pipeline_control as cc
    from conf_pipeline.model import Point2D
    st = win.state
    st.set_config(_config_with_array_and_seats(bearing=0.0))
    st.set_mode("live")
    panel = win.panels["live"]
    panel._session_array_id = "A"
    eng = cc.BeamEngine(device=None, mode="steered", steered_cfg={"mode": "superdirective"})
    panel._beam_engine = eng
    panel.live_beameng_mode.setCurrentIndex(panel.live_beameng_mode.findData("steered"))
    panel._refresh_beameng_lockseat()

    east = Point2D(3.0, 0.0)                                                # array-relative azimuth 90 (bearing 0)
    assert panel._on_canvas_click_live(east) is True                       # consumed the click
    assert panel.live_beameng_lockseat.currentData() == "__manual__"       # flipped to Manual angle
    assert panel.live_beameng_angle.value() == 90.0 == round(cp.azimuth_for_array_point(st.config, "A", east), 1)
    assert eng._steered.steer_to_doa is False and eng._steered._steered_az == 90.0
    assert panel._beameng_locked_manual_az == 90.0

    st.set_config(_config_with_array_and_seats(bearing=None))              # no room bearing → can't aim
    assert panel._on_canvas_click_live(east) is False                      # declined; the click is not consumed
    assert "Click-to-aim needs" in panel.live_status.text()


def test_beameng_click_to_aim_inert_outside_live_mode(win):
    """The A/B session keeps running when the user leaves Live mode, but the canvas is shared — so a
    map click outside Live mode is NOT hijacked: _on_canvas_click_live returns False (falls through to
    the active tool) even with a connected, aim-able steered engine."""
    import conf_pipeline_control as cc
    from conf_pipeline.model import Point2D
    st = win.state
    st.set_config(_config_with_array_and_seats(bearing=0.0))
    panel = win.panels["live"]
    panel._session_array_id = "A"
    eng = cc.BeamEngine(device=None, mode="steered", steered_cfg={"mode": "superdirective"})
    panel._beam_engine = eng
    panel.live_beameng_mode.setCurrentIndex(panel.live_beameng_mode.findData("steered"))
    panel._refresh_beameng_lockseat()
    east = Point2D(3.0, 0.0)
    st.set_mode("live")
    assert panel._on_canvas_click_live(east) is True                       # armed + active → aims
    st.set_mode("design")                                                  # leave Live (session stays connected)
    assert panel._on_canvas_click_live(east) is False                      # inert: the Design click is not hijacked


def test_beameng_manual_lock_nulls_keep_our_seat(win):
    """Manual lock + 'Null other seats': the pushed nulls keep the seat NEAREST our manual aim (so we don't
    null our own look) — aiming ~east keeps sofa-seat2 and nulls only sofa-seat1."""
    import conf_pipeline_control as cc
    st = win.state
    st.set_config(_config_with_array_and_seats(bearing=0.0))
    panel = win.panels["live"]
    panel._session_array_id = "A"
    eng = cc.BeamEngine(device=None, mode="steered", steered_cfg={"mode": "superdirective"})
    panel._beam_engine = eng
    panel.live_beameng_mode.setCurrentIndex(panel.live_beameng_mode.findData("steered"))
    panel._refresh_beameng_lockseat()
    panel.live_beameng_lockseat.setCurrentIndex(panel.live_beameng_lockseat.findData("__manual__"))
    panel.live_beameng_angle.setValue(85.0)                                # ~east → nearest seat is sofa-seat2
    panel._on_beameng_angle_changed(85.0)
    assert panel._manual_lock_seat_id() == "sofa-seat2"

    panel.live_beameng_nullseats.setChecked(True)
    panel._push_seat_nulls()
    assert eng._steered._explicit_nulls == cp.seat_null_azimuths(st.config, "A", exclude_seat_id="sofa-seat2")


# --------------------------------------------------------------------------- #
# Visual-polish live-state cues (commit 3): meter, steer arrow, (i), disabled hints
# --------------------------------------------------------------------------- #
def test_level_meter_peak_hold_clip_and_plain_fill(qapp):
    """The LevelMeter: set_level tracks a falling peak and latches a clip flag; meter=False is a plain
    fill (no peak/clip) for the OCTOVOX buffer gauge; reset clears everything."""
    from conf_pipeline_gui.panels.common import LevelMeter
    m = LevelMeter()
    m.set_level(0.5)
    assert m.level() == 0.5 and m._peak >= 0.5
    m.set_level(0.1)                                   # falls; peak decays but stays above the new level
    assert m.level() == 0.1 and 0.1 < m._peak < 0.5
    m.set_level(0.99)                                  # >= clip frac latches the clip/hot flag
    assert m._clip is True
    m.reset()
    assert m.level() == 0.0 and m._peak == 0.0 and m._clip is False
    m.set_level(1.0, meter=False)                      # plain fill: no peak, no clip
    assert m.level() == 1.0 and m._peak == 0.0 and m._clip is False


def test_live_panel_uses_the_prominent_level_meter(win):
    from conf_pipeline_gui.panels.common import LevelMeter
    assert isinstance(win.panels["live"].live_meter, LevelMeter)


def test_publish_overlay_includes_the_locked_steer_az(win):
    """_publish_overlay carries steer_az = the committed/locked look (manual angle or snap-steer seat),
    and None while following the talker (the DOA rays already show that)."""
    import conf_pipeline_control as cc
    st = win.state
    st.set_config(_config_with_array_and_seats(bearing=0.0))
    panel = win.panels["live"]
    panel._session_array_id = "A"
    panel._beam_engine = cc.BeamEngine(device=None, mode="steered", steered_cfg={"mode": "superdirective"})
    panel._beameng_locked_manual_az = None
    panel._beameng_locked_seat = None
    panel._publish_overlay()
    assert st.live_overlay is not None and st.live_overlay.get("steer_az") is None     # following → no arrow
    panel._beameng_locked_manual_az = 60.0                                              # manual lock
    panel._publish_overlay()
    assert st.live_overlay["steer_az"] == 60.0
    panel._beameng_locked_manual_az = None                                             # seat lock
    panel._beameng_locked_seat, panel._beameng_locked_az = "sofa-seat2", 90.0
    panel._publish_overlay()
    assert st.live_overlay["steer_az"] == 90.0


def test_live_overlay_with_steer_az_paints(win):
    st = win.state
    st.set_config(_config_with_array_and_seats(bearing=0.0))
    st.set_mode("live")
    st.view = "2d"
    st.set_live_overlay({"array_id": "A", "sector": None, "detections": [(20.0, 9.0, True)],
                         "seat": None, "bearing": 0.0, "steer_az": 60.0, "level": 0.5, "connected": True})
    assert win.canvas.grab().width() > 0               # the steer arrow + ray paint, no crash


def test_live_limits_info_and_disabled_hints(win):
    """The hardware-limit (i) chip surfaces the key limits; Mute/Gain carry a 'needs Monitor' hint."""
    panel = win.panels["live"]
    assert "5.6 kHz" in panel.live_limits_info.toolTip()
    assert "Monitor" in panel.live_mute.toolTip() and "Monitor" in panel.live_gain.toolTip()


def test_steer_az_is_suppressed_in_grid_mode(win):
    """Review fix: the lock arrow is steered-only. Switching the A/B engine to grid (which ignores
    steering) must NOT publish steer_az, even though the lock STATE persists for the switch-back re-pin."""
    import conf_pipeline_control as cc
    st = win.state
    st.set_config(_config_with_array_and_seats(bearing=0.0))
    panel = win.panels["live"]
    panel._session_array_id = "A"
    panel._beam_engine = cc.BeamEngine(device=None, mode="steered", steered_cfg={"mode": "superdirective"})
    panel.live_beameng_mode.setCurrentIndex(panel.live_beameng_mode.findData("steered"))
    panel._beameng_locked_manual_az = 60.0
    panel._publish_overlay()
    assert st.live_overlay["steer_az"] == 60.0                 # steered + locked → arrow
    panel.live_beameng_mode.setCurrentIndex(panel.live_beameng_mode.findData("grid"))
    panel._publish_overlay()
    assert st.live_overlay["steer_az"] is None                 # grid → no arrow (lock persists, unhonoured)
    assert panel._beameng_locked_manual_az == 60.0             # …but the lock state survives the switch


def test_level_meter_paints_in_light_theme(win):
    """Review fix: the LevelMeter reads the live theme (mirrors the canvas), so it adapts to a light-theme
    toggle instead of painting a dark slab."""
    win.state.theme = "light"
    win.panels["live"].live_meter.set_level(0.9)
    assert win.panels["live"].live_meter.grab().width() > 0    # paints against state.theme, no crash
    win.state.theme = "dark"
