"""Headless GUI tests for the v4 coverage-simulation UI: the SimBar overlays,
furniture authoring (one undo per edit), camera aim, and multi-room furniture ids.
Skipped when PySide6 is absent.
"""
import os

import pytest

pytest.importorskip("PySide6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import conf_pipeline as cp  # noqa: E402
from conf_pipeline.model import Point2D  # noqa: E402


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
    w.state.set_config(cp.set_room(w.state.config, cp.rectangular_room(6, 5, 3)))
    yield w
    w.close()


def test_simbar_toggles_flip_state_and_paint(win):
    st = win.state
    win._toggle_pickup(True)
    win._toggle_fov(True)
    win._toggle_dispersion(True)
    win._toggle_occlusion(True)
    assert st.sim_show_pickup and st.sim_show_fov and st.sim_show_dispersion and st.sim_show_occlusion
    # bar reflects state after a chrome sync
    win._sync_chrome()
    assert win.simbar.buttons["pickup"].isChecked()
    # both views paint with every overlay on
    st.view = "2d"
    assert win.canvas.grab().width() > 0
    st.view = "3d"
    assert win.canvas.grab().width() > 0


def test_entering_simulate_seeds_pickup_and_fov(win):
    assert not win.state.sim_show_pickup
    win.state.set_mode("simulate")
    assert win.state.sim_show_pickup and win.state.sim_show_fov
    assert win.simbar.buttons["pickup"].isChecked()


def test_furniture_place_and_edit_one_undo_each(win):
    st = win.state
    st.furniture_kind = "table"
    # place via the canvas furniture tool
    st.tool = "furniture"
    win.canvas._down2d(_screen(win, Point2D(3, 2.5)))
    assert len(st.config.room.objects) == 1
    fid = st.config.room.objects[0].id
    base = st._idx
    # a move drag commits exactly one undo step on release
    win.canvas.drag = {"kind": "furniture-move", "id": fid, "grab": (0.0, 0.0), "pos": Point2D(4, 3)}
    win.canvas._up2d()
    assert st._idx == base + 1
    assert st.config.room.objects[0].position.x == 4
    # rotate is one more step
    win.canvas.drag = {"kind": "furniture-rotate", "id": fid, "rotation_deg": 45.0}
    win.canvas._up2d()
    assert st._idx == base + 2
    assert st.config.room.objects[0].rotation_deg == 45.0
    # undo unwinds one gesture at a time
    st.undo()
    assert st.config.room.objects[0].rotation_deg in (None, 0.0)


def test_multi_room_furniture_ids_dont_collide(win):
    st = win.state
    st.set_config(cp.add_furniture(st.config, st.next_furniture_id(), "table", Point2D(2, 2)))
    assert st.config.room.objects[0].id == "F1"
    st.add_room()
    st.set_config(cp.set_room(st.config, cp.rectangular_room(4, 4, 3)))
    # second room mints its own F1 (deduped against the active room, not global)
    assert st.next_furniture_id() == "F1"
    st.set_config(cp.add_furniture(st.config, st.next_furniture_id(), "chair", Point2D(2, 2)))
    assert st.config.room.objects[0].id == "F1"
    # coverage cache is per-room: switching rooms recomputes against the active config
    st.switch_room(0)
    assert st.config.room.objects[0].kind == "table"


def test_add_camera_and_aim_via_design_panel(win):
    st = win.state
    design = win.panels["design"]
    design.dev_type.setCurrentIndex([design.dev_type.itemData(i) for i in range(design.dev_type.count())].index("camera"))
    design._add_device()
    cams = [d for d in st.config.devices if d.type == "camera"]
    assert len(cams) == 1
    cam = cams[0]
    st2 = cp.set_camera_bearing(st.config, cam.id, 123.0)
    st.set_config(st2)
    assert cp.find_device(st.config, cam.id).bearing_deg == 123.0


def test_array_bearing_settable_in_design_panel(win):
    """Microphone arrays now expose a Bearing control in Design (their mounting heading) — the prerequisite
    for room-aware steering (snap-steer / click-to-aim / seat-nulling), previously settable only via the API.
    The props form renders a 'Bearing (°)' row for an array and _set_bearing routes to set_array_bearing."""
    from PySide6.QtWidgets import QLabel
    st = win.state
    st.set_config(cp.add_device(st.config, cp.create_microphone_array("A", "Array", position=Point2D(1.0, 1.0))))
    design = win.panels["design"]
    arr = cp.find_device(st.config, "A")
    design._device_props(arr)                                  # render the array's property form
    assert "Bearing (°)" in [w.text() for w in design.findChildren(QLabel)]   # the new control is shown
    assert "Tilt (°)" not in [w.text() for w in design.findChildren(QLabel)]  # planar array: no tilt
    design._set_bearing(arr, 90.0)                             # the setter routes to set_array_bearing
    assert cp.find_device(st.config, "A").bearing_deg == 90.0


def test_destructive_buttons_carry_the_danger_style(win):
    """Destructive actions get the QSS [danger] property so they read as destructive, not neutral —
    Devices 'Remove selected', the selection editor 'Delete device', and the Route 'Remove' buttons."""
    from PySide6.QtWidgets import QPushButton
    st = win.state
    st.set_config(cp.add_device(st.config, cp.create_microphone_array("A", "Array", position=Point2D(1.0, 1.0))))
    design = win.panels["design"]
    design._device_props(cp.find_device(st.config, "A"))       # build the selection editor (Delete device)
    danger = {b.text() for b in design.findChildren(QPushButton) if b.property("danger") == "true"}
    assert "Remove selected" in danger and "Delete device" in danger
    # a neutral action must NOT be styled destructive
    assert not any(b.property("danger") == "true" for b in design.findChildren(QPushButton)
                   if b.text() == "Add")
    route = win.panels["route"]
    assert sum(b.property("danger") == "true" for b in route.findChildren(QPushButton)) >= 2  # 2x "Remove"


def test_disconnect_button_flips_to_danger_while_a_session_runs(win):
    """The Connect/Disconnect toggle is neutral while idle and reads destructive (danger) while a live
    session runs — driven from the one chokepoint _notify_session_changed."""
    import conf_pipeline_control as cc
    panel = win.panels["live"]
    assert not panel.live_connect.property("danger")           # idle -> 'Connect' -> neutral
    panel._beam_engine = cc.BeamEngine(device=None)            # a running session (no start needed)
    panel._notify_session_changed()
    assert panel.live_connect.property("danger") == "true"     # 'Disconnect' -> danger
    panel._beam_engine = None
    panel._notify_session_changed()
    assert not panel.live_connect.property("danger")           # back to neutral


def test_canvas_click_cb_consumes_a_2d_click(win):
    """The opt-in click_cb fires with the clicked ROOM point on a 2D press; returning True consumes the
    click (skips tool handling), False lets it fall through to normal selection, and None (default) is a
    no-op."""
    st = win.state
    st.tool = "select"
    got = []

    win.canvas.click_cb = lambda p: (got.append(p) or True)        # consume the click
    win.canvas._down2d(_screen(win, Point2D(2.0, 1.5)))
    assert len(got) == 1
    assert got[0].x == pytest.approx(2.0, abs=0.05) and got[0].y == pytest.approx(1.5, abs=0.05)

    got.clear()                                                    # returning False → still fires, but falls through
    win.canvas.click_cb = lambda p: (got.append(p) or False)
    win.canvas._down2d(_screen(win, Point2D(2.0, 1.5)))            # normal select handling runs, no crash
    assert len(got) == 1

    win.canvas.click_cb = None                                     # default: no callback, normal behaviour
    win.canvas._down2d(_screen(win, Point2D(2.0, 1.5)))            # must not raise


def _screen(win, world):
    """World point → screen QPointF using the canvas's current 2D transform."""
    win.state.view = "2d"
    v = win.canvas.view2d()
    return win.canvas.w2s(world, v)
