"""Headless GUI smoke tests for the v1.12.0 UI/UX surfaces.

These build a real ``MainWindow`` under the Qt *offscreen* platform and drive the
new control paths (mute-group editor, guide-panel progress, inspector banner,
canvas context-menu helpers). They guard on PySide6 being importable and force a
synchronous ``inspector.refresh()`` where the live app would rely on a coalesced
timer. Skipped entirely when PySide6 is absent.
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
    yield w
    w.close()


def test_window_builds(win):
    # the Stagebar shell exposes the five workflow modes + a panel per mode
    from conf_pipeline_gui import workflow

    assert set(win.modebar.buttons) == set(workflow.MODES)
    assert set(win.panels) == set(workflow.MODES)
    assert "Optimize room" in win.act_optimize.text()
    assert "Auto-Route" in win.act_auto_route.text()


def test_mode_switch_updates_shell(win):
    win.state.set_mode("route")
    assert win.state.mode == "route"
    assert win.modebar.buttons["route"].isChecked()
    # ROUTE exposes Select + Connect only
    assert win.toolrail.buttons["connect"].isVisible() or not win.toolrail.isVisible()
    assert not win.toolrail.buttons["room"].isVisible()
    # a gated tool key hops back to its home mode
    win._shortcut_tool("zone")
    assert win.state.mode == "design"
    assert win.state.tool == "zone"
    win.state.set_mode("design")


def test_workflow_dots_track_progress(win):
    from conf_pipeline_gui import workflow

    win.state.set_config(cp.create_config("Empty", "2026-06-11T00:00:00Z"))
    assert workflow.stage_status(win.state)["design"] == workflow.TODO
    win._guide_add_array()  # adds room + array
    st = workflow.stage_status(win.state)
    assert st["design"] == workflow.PARTIAL  # room + array done, zone + talker missing
    # the ModeBar received the same status via _sync_chrome
    assert win.modebar._status["design"] == workflow.PARTIAL
    # panel hint chip points at the next step
    win.panels["design"].refresh()
    assert "zone" in win.panels["design"].hint_chip.text().lower()


def test_mute_group_add_toggle_remove(win):
    ins = win.panels["route"]
    c = cp.create_config("T", "2026-06-11T00:00:00Z")
    c = cp.add_device(c, cp.create_microphone_array("A", "Array"))
    c = cp.add_device(c, cp.create_wireless_mic("WM", "Lapel", "dante"))
    win.state.set_config(c)

    ins.mute_group_name.setText("Room mute")
    ins._add_mute_group()
    ins.refresh()  # app uses a coalesced timer; force it
    assert len(win.state.config.control.mute_groups) == 1
    assert ins.mute_group_list.count() == 1

    ins.mute_group_list.setCurrentRow(0)
    ins._toggle_selected_mute_group()
    assert win.state.config.control.mute_groups[0].muted is True

    ins.refresh()
    ins.mute_group_list.setCurrentRow(0)
    ins._remove_selected_mute_group()
    assert not (win.state.config.control.mute_groups or [])
    assert cp.validate(win.state.config).ok


def test_mute_group_no_mics_is_noop(win):
    ins = win.panels["route"]
    win.state.set_config(cp.create_config("Empty", "2026-06-11T00:00:00Z"))
    ins._add_mute_group()  # no mute-capable mics → must not raise or add
    assert win.state.config.control is None or not win.state.config.control.mute_groups


def test_validation_pill_and_issues_drawer(win):
    win.state.set_config(cp.create_config("Empty", "2026-06-11T00:00:00Z"))
    assert win.val_pill.text()  # non-empty status pill
    assert win.val_pill.property("level") in ("ok", "warn", "error")
    win._show_issues()
    assert win.issues_drawer.isVisible()
    assert win.issues_drawer.issue_badge.text()
    win.issues_drawer.close_drawer()
    assert not win.issues_drawer.isVisible()


def test_hidden_panel_catches_up_on_show(win):
    # edit in DESIGN while ROUTE is hidden; switching must show fresh data
    win.state.set_mode("design")
    c = cp.create_config("T", "2026-06-11T00:00:00Z")
    c = cp.add_device(c, cp.create_processor("P", "DSP"))
    c = cp.add_device(c, cp.create_codec("C", "Codec", "dante"))
    win.state.set_config(cp.auto_route(c).config)
    win.state.set_mode("route")
    route = win.panels["route"]
    route.refresh()  # the app refreshes via showEvent; force it offscreen
    assert "route" in route.routing_summary_lbl.text()


def test_canvas_context_helpers(win):
    cv = win.canvas
    win.state.set_config(cp.set_room(cp.create_config("T", "2026-06-11T00:00:00Z"), cp.rectangular_room(8, 6, 3)))
    cv._ctx_add_array(Point2D(4, 3))
    assert any(d.type == "microphoneArray" for d in win.state.config.devices)
    array_id = next(d.id for d in win.state.config.devices if d.type == "microphoneArray")
    cv._ctx_add_zone(array_id, Point2D(4, 3))
    assert next(d for d in win.state.config.devices if d.id == array_id).zones
    cv._ctx_add_talker(Point2D(5, 3))
    assert win.state.config.talkers


def test_canvas_hover_cursor_all_tools(win):
    cv = win.canvas
    for tool in ("select", "room", "zone", "talker", "connect"):
        win.state.tool = tool
        cv._update_hover_cursor(Point2D(3, 3), cv.view2d())  # must not raise


def test_empty_canvas_paints(win):
    win.state.set_config(cp.create_config("Empty", "2026-06-11T00:00:00Z"))
    win.canvas.repaint()  # empty-state hint path must not raise
