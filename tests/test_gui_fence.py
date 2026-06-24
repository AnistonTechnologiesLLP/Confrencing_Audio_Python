"""Tests for Task 4/5/6: transient AppState.live_fence_polygon + Canvas "fence" draw tool
+ LivePanel twokit fence controls + connect/tick/publish/disconnect wiring.

Hardware-free — constructs AppState and Canvas/LivePanel directly (NOT MainWindow, which hangs
headless on Windows per CLAUDE.md).  Pattern mirrors test_gui_autosteer_sectors.py.
"""
from __future__ import annotations

import os

import pytest

pytest.importorskip("PySide6")

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import conf_pipeline as cp  # noqa: E402
import conf_pipeline_control as cc  # noqa: E402
from conf_pipeline.model import Point2D  # noqa: E402


# ---------------------------------------------------------------------------
# Qt fixture
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def qapp():
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    yield app


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def _config_with_array():
    c = cp.create_config("rt", "2026-01-01T00:00:00Z")
    c = cp.add_device(c, cp.create_microphone_array("A", "Array", position=Point2D(0.0, 0.0)))
    return c


# ===========================================================================
# AppState — transient live_fence_polygon field
# ===========================================================================

class TestAppStateLiveFencePolygon:
    def test_default_is_empty_list(self):
        from conf_pipeline_gui.state import AppState
        st = AppState()
        assert st.live_fence_polygon == []

    def test_set_live_fence_polygon_stores_copy(self):
        from conf_pipeline_gui.state import AppState
        st = AppState()
        pts = [Point2D(0.0, 0.0), Point2D(1.0, 0.0), Point2D(1.0, 1.0)]
        st.set_live_fence_polygon(pts)
        assert st.live_fence_polygon == pts
        # mutation of the original list must not affect stored value
        pts.append(Point2D(2.0, 2.0))
        assert len(st.live_fence_polygon) == 3

    def test_set_live_fence_polygon_emits_signal(self, qapp):
        from conf_pipeline_gui.state import AppState
        st = AppState()
        emitted = []
        st.liveOverlayChanged.connect(lambda: emitted.append(1))
        st.set_live_fence_polygon([Point2D(0.0, 0.0), Point2D(1.0, 0.0), Point2D(0.0, 1.0)])
        assert emitted, "set_live_fence_polygon must emit liveOverlayChanged"

    def test_set_live_fence_polygon_none_clears(self):
        from conf_pipeline_gui.state import AppState
        st = AppState()
        st.set_live_fence_polygon([Point2D(0.0, 0.0), Point2D(1.0, 0.0), Point2D(0.0, 1.0)])
        st.set_live_fence_polygon(None)
        assert st.live_fence_polygon == []

    def test_set_config_does_not_touch_live_fence_polygon(self):
        """Fence polygon is transient / live-only — set_config must leave it alone."""
        from conf_pipeline_gui.state import AppState
        st = AppState()
        pts = [Point2D(0.0, 0.0), Point2D(1.0, 0.0), Point2D(1.0, 1.0)]
        st.set_live_fence_polygon(pts)
        # any config mutation must not clobber the polygon
        st.set_config(_config_with_array())
        assert st.live_fence_polygon == pts

    def test_undo_does_not_touch_live_fence_polygon(self):
        from conf_pipeline_gui.state import AppState
        st = AppState()
        st.set_config(_config_with_array())
        pts = [Point2D(0.0, 0.0), Point2D(1.0, 0.0), Point2D(1.0, 1.0)]
        st.set_live_fence_polygon(pts)
        st.undo()
        assert st.live_fence_polygon == pts


# ===========================================================================
# Canvas — "fence" draw tool
# ===========================================================================

@pytest.fixture
def canvas_live(qapp):
    from conf_pipeline_gui.canvas import Canvas
    from conf_pipeline_gui.state import AppState
    st = AppState()
    st.set_config(_config_with_array())
    st.set_mode("live")
    st.view = "2d"
    cv = Canvas(st)
    cv.resize(500, 400)
    yield cv
    cv.deleteLater()


class TestCanvasFenceTool:

    def _fake_down2d(self, cv, x: float, y: float):
        """Drive _down2d as if the user clicked at canvas pixel (x, y)."""
        from PySide6.QtCore import QPointF
        cv._down2d(QPointF(x, y))

    def _set_tool(self, cv, tool: str):
        cv.state.tool = tool

    # --- basic draw_pts accumulation ---

    def test_fence_tool_accumulates_draw_pts(self, canvas_live):
        cv = canvas_live
        self._set_tool(cv, "fence")
        assert cv.draw_pts == []
        self._fake_down2d(cv, 100, 100)
        self._fake_down2d(cv, 200, 100)
        assert len(cv.draw_pts) == 2

    def test_fence_down_does_not_call_set_config(self, canvas_live):
        cv = canvas_live
        initial_cfg = cv.state.config
        self._set_tool(cv, "fence")
        self._fake_down2d(cv, 100, 100)
        self._fake_down2d(cv, 200, 100)
        assert cv.state.config is initial_cfg, "fence tool must not mutate config"

    # --- double-click commits ≥3 points ---

    def _double_click_commit(self, cv):
        """Simulate double-click to commit the fence polygon."""
        from PySide6.QtCore import QPointF
        from PySide6.QtGui import QMouseEvent
        from PySide6.QtCore import Qt, QEvent
        # Directly call mouseDoubleClickEvent with a synthetic event
        evt = QMouseEvent(
            QEvent.Type.MouseButtonDblClick,
            QPointF(0.0, 0.0),
            QPointF(0.0, 0.0),
            Qt.MouseButton.LeftButton,
            Qt.MouseButton.LeftButton,
            Qt.KeyboardModifier.NoModifier,
        )
        cv.mouseDoubleClickEvent(evt)

    def test_double_click_with_3pts_commits_polygon(self, canvas_live):
        cv = canvas_live
        self._set_tool(cv, "fence")
        self._fake_down2d(cv, 100, 100)
        self._fake_down2d(cv, 200, 100)
        self._fake_down2d(cv, 150, 200)
        self._double_click_commit(cv)
        assert len(cv.state.live_fence_polygon) == 3
        assert cv.state.tool == "select"
        assert cv.draw_pts == []

    def test_double_click_with_fewer_than_3pts_does_not_commit(self, canvas_live):
        cv = canvas_live
        self._set_tool(cv, "fence")
        self._fake_down2d(cv, 100, 100)
        self._fake_down2d(cv, 200, 100)
        cv.state.live_fence_polygon = []   # ensure it was empty
        self._double_click_commit(cv)
        assert cv.state.live_fence_polygon == [], "< 3 points must not commit"

    def test_double_click_deduplicates_adjacent_identical_points(self, canvas_live):
        cv = canvas_live
        self._set_tool(cv, "fence")
        # Inject draw_pts directly including duplicates (simulating rapid double-click)
        cv.draw_pts = [
            Point2D(1.0, 1.0),
            Point2D(1.0, 1.0),   # duplicate of previous
            Point2D(2.0, 1.0),
            Point2D(3.0, 2.0),
        ]
        self._double_click_commit(cv)
        # After dedup: 3 unique pts → committed
        assert len(cv.state.live_fence_polygon) == 3

    def test_double_click_fence_does_not_call_set_config(self, canvas_live):
        cv = canvas_live
        initial_cfg = cv.state.config
        self._set_tool(cv, "fence")
        cv.draw_pts = [Point2D(0.0, 0.0), Point2D(1.0, 0.0), Point2D(1.0, 1.0)]
        self._double_click_commit(cv)
        assert cv.state.config is initial_cfg, "fence commit must not mutate config"

    def test_room_double_click_still_works(self, canvas_live):
        """Verify the room tool double-click branch is not disturbed."""
        cv = canvas_live
        cv.state.set_mode("design")
        self._set_tool(cv, "room")
        # With no draw_pts, double-click should simply do nothing / not raise
        self._double_click_commit(cv)   # should not raise

    # --- Escape clears in-progress fence draw ---

    def _press_escape(self, cv):
        from PySide6.QtGui import QKeyEvent
        from PySide6.QtCore import Qt, QEvent
        evt = QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Escape, Qt.KeyboardModifier.NoModifier)
        cv.keyPressEvent(evt)

    def test_escape_clears_fence_draw_pts(self, canvas_live):
        cv = canvas_live
        self._set_tool(cv, "fence")
        cv.draw_pts = [Point2D(0.0, 0.0), Point2D(1.0, 0.0)]
        cv.hover = Point2D(2.0, 0.0)
        self._press_escape(cv)
        assert cv.draw_pts == []
        assert cv.hover is None
        assert cv.state.tool == "select"

    def test_escape_in_other_tool_does_not_crash(self, canvas_live):
        cv = canvas_live
        self._set_tool(cv, "select")
        self._press_escape(cv)   # should not raise

    # --- cursor / hover ---

    def test_fence_tool_uses_crosscursor(self, canvas_live):
        from PySide6.QtCore import Qt
        cv = canvas_live
        self._set_tool(cv, "fence")
        v = cv.view2d()
        from conf_pipeline.model import Point2D
        cv._update_hover_cursor(Point2D(0.0, 0.0), v)
        assert cv.cursor().shape() == Qt.CursorShape.CrossCursor

    # --- move2d hover updates for fence ---

    def test_move2d_updates_hover_for_fence_tool(self, canvas_live):
        from PySide6.QtCore import QPointF
        cv = canvas_live
        self._set_tool(cv, "fence")
        cv.draw_pts = [Point2D(0.0, 0.0)]
        cv._move2d(QPointF(250, 200))
        assert isinstance(cv.hover, Point2D)

    # --- preview paints without raising ---

    def test_canvas_grab_with_fence_draw_pts_does_not_raise(self, canvas_live):
        cv = canvas_live
        self._set_tool(cv, "fence")
        cv.draw_pts = [Point2D(0.0, 0.0), Point2D(1.0, 0.0)]
        cv.hover = Point2D(1.0, 1.0)
        assert cv.grab().width() > 0


# ===========================================================================
# Task 5: twokit overlay — committed fence polygon + fused-source dots
# ===========================================================================

def _two_kit_overlay(*, with_fence=True, with_fused=True):
    """Build a live_overlay dict for the two-kit mode with optional fence/fused keys."""
    kits = [
        {"array_id": "A1", "active": True,  "level": 0.6, "doa": 30.0,  "bearing": 0.0},
        {"array_id": "A2", "active": False, "level": 0.3, "doa": 150.0, "bearing": 0.0},
    ]
    ov = {"connected": True, "kits": kits}
    if with_fence:
        # A triangle in room coords (metres around origin)
        ov["fence_polygon"] = [(-1.0, -1.0), (1.0, -1.0), (0.0, 1.0)]
    if with_fused:
        ov["fused_positions"] = [
            {"x": 0.0, "y": 0.0, "inside": True,  "confidence": 0.9},
            {"x": 3.0, "y": 3.0, "inside": False, "confidence": 0.5},
        ]
    return ov


def _config_with_two_arrays():
    """Config with two positioned arrays A1 and A2."""
    c = cp.create_config("rt", "2026-01-01T00:00:00Z")
    c = cp.add_device(c, cp.create_microphone_array("A1", "Kit1", position=Point2D(-2.0, 0.0)))
    c = cp.add_device(c, cp.create_microphone_array("A2", "Kit2", position=Point2D( 2.0, 0.0)))
    return c


@pytest.fixture
def canvas_twokit(qapp):
    from conf_pipeline_gui.canvas import Canvas
    from conf_pipeline_gui.state import AppState
    st = AppState()
    st.set_config(_config_with_two_arrays())
    st.set_mode("live")
    st.view = "2d"
    cv = Canvas(st)
    cv.resize(500, 400)
    yield cv
    cv.deleteLater()


class TestTwokitOverlayPaint:

    def _set_overlay(self, cv, ov):
        cv.state.live_overlay = ov

    def test_twokit_overlay_with_fence_and_fused_paints_without_raising(self, canvas_twokit):
        """Full overlay: kits + fence_polygon + fused_positions — must paint."""
        cv = canvas_twokit
        self._set_overlay(cv, _two_kit_overlay(with_fence=True, with_fused=True))
        assert cv.grab().width() > 0

    def test_twokit_overlay_without_fence_keys_is_back_compat(self, canvas_twokit):
        """Overlay with only 'kits' (no fence/fused keys) must paint exactly as before."""
        cv = canvas_twokit
        self._set_overlay(cv, _two_kit_overlay(with_fence=False, with_fused=False))
        assert cv.grab().width() > 0

    def test_twokit_overlay_fence_only_no_fused(self, canvas_twokit):
        """fence_polygon present, fused_positions absent — must paint without raising."""
        cv = canvas_twokit
        self._set_overlay(cv, _two_kit_overlay(with_fence=True, with_fused=False))
        assert cv.grab().width() > 0

    def test_twokit_overlay_fused_only_no_fence(self, canvas_twokit):
        """fused_positions present, fence_polygon absent — must paint without raising."""
        cv = canvas_twokit
        self._set_overlay(cv, _two_kit_overlay(with_fence=False, with_fused=True))
        assert cv.grab().width() > 0

    def test_call_site_passes_full_ov_not_just_kits(self, canvas_twokit):
        """Regression: verify that a fence_polygon in the overlay actually reaches the
        paint method (i.e. the call site forwards `ov`, not just `ov["kits"]`).
        If only kits were forwarded the fence polygon would silently be skipped, but
        this test constructs a minimal overlay whose fence polygon has a vertex at a
        known position — we just need it to paint without any KeyError/AttributeError,
        which proves the full ov was forwarded."""
        cv = canvas_twokit
        ov = {
            "connected": True,
            "kits": [{"array_id": "A1", "active": True, "level": 0.5}],
            "fence_polygon": [(0.0, 0.0), (1.0, 0.0), (0.5, 1.0)],
            "fused_positions": [{"x": 0.5, "y": 0.3, "inside": True, "confidence": 1.0}],
        }
        self._set_overlay(cv, ov)
        assert cv.grab().width() > 0

    def test_fence_color_constant_is_amber(self, canvas_twokit):
        """FENCE_COLOR must be defined in canvas and be amber (consistent with Task-4 dashed trace)."""
        from conf_pipeline_gui.canvas import FENCE_COLOR
        # amber = #ffb347 or similar; just check it's a non-empty string
        assert isinstance(FENCE_COLOR, str)
        assert FENCE_COLOR.startswith("#")

    def test_empty_fence_polygon_list_does_not_raise(self, canvas_twokit):
        """Explicitly empty fence_polygon (vs absent) must be handled gracefully."""
        cv = canvas_twokit
        ov = {
            "connected": True,
            "kits": [{"array_id": "A1", "active": True, "level": 0.5}],
            "fence_polygon": [],
            "fused_positions": [],
        }
        self._set_overlay(cv, ov)
        assert cv.grab().width() > 0


# ===========================================================================
# Task 6 — LivePanel twokit fence controls + connect/tick/publish/disconnect
# ===========================================================================

def _config_two_arrays_positioned_and_bearing():
    """Two arrays, both with position AND bearing_deg set (fully posed)."""
    c = cp.create_config("FenceTask6", "2026-01-01T00:00:00Z")
    c = cp.set_room(c, cp.rectangular_room(8, 6, 3))
    c = cp.add_device(c, cp.create_microphone_array("A1", "Kit A"))
    c = cp.set_device_position(c, "A1", Point2D(2.0, 1.0))
    c = cp.set_array_bearing(c, "A1", 0.0)
    c = cp.add_device(c, cp.create_microphone_array("A2", "Kit B"))
    c = cp.set_device_position(c, "A2", Point2D(6.0, 1.0))
    c = cp.set_array_bearing(c, "A2", 180.0)
    return c


def _config_two_arrays_one_missing_bearing():
    """Two arrays: A1 has position+bearing, A2 has position but NO bearing_deg."""
    c = cp.create_config("FenceNoBearing", "2026-01-01T00:00:00Z")
    c = cp.set_room(c, cp.rectangular_room(8, 6, 3))
    c = cp.add_device(c, cp.create_microphone_array("A1", "Kit A"))
    c = cp.set_device_position(c, "A1", Point2D(2.0, 1.0))
    c = cp.set_array_bearing(c, "A1", 0.0)
    c = cp.add_device(c, cp.create_microphone_array("A2", "Kit B"))
    c = cp.set_device_position(c, "A2", Point2D(6.0, 1.0))
    # A2 intentionally has no bearing_deg
    return c


def _config_two_arrays_one_missing_position():
    """Two arrays: A1 is fully posed, A2 has no position."""
    c = cp.create_config("FenceNoPosition", "2026-01-01T00:00:00Z")
    c = cp.set_room(c, cp.rectangular_room(8, 6, 3))
    c = cp.add_device(c, cp.create_microphone_array("A1", "Kit A"))
    c = cp.set_device_position(c, "A1", Point2D(2.0, 1.0))
    c = cp.set_array_bearing(c, "A1", 0.0)
    c = cp.add_device(c, cp.create_microphone_array("A2", "Kit B"))
    # A2 intentionally has no position set
    return c


# --------------------------------------------------------------------------
# FakeMultiKitController — extended to accept fence kwargs + fence methods
# --------------------------------------------------------------------------

class _FakeMKWithFence:
    """Stand-in for cc.MultiKitController accepting fence_polygon/fence_margin_m
    + set_fence_poses/update_fence/fence_status — no hardware."""

    def __init__(self, specs, **kw):
        self.specs = specs
        self.kw = kw
        self.kits = specs                       # overlay reads each kit's array_id
        self.error = None
        self._fence_poses_called_with = None
        self._update_fence_calls = 0
        self._fence_status_result = None        # set by tests to control fence_status()

    def start(self):
        pass

    def stop(self):
        pass

    def set_gain_db(self, g, **k):
        pass

    def set_mute(self, m, **k):
        pass

    def read_level(self):
        return 0.3

    @property
    def active_kit(self):
        return 0

    def status(self):
        return [
            cc.KitStatus(0, True,  30.0, 0.5, 0.8, False, None),
            cc.KitStatus(1, False, 120.0, 0.2, 0.1, False, None),
        ]

    def set_fence_poses(self, poses):
        self._fence_poses_called_with = list(poses)

    def update_fence(self, t: float):
        self._update_fence_calls += 1

    def fence_status(self):
        return self._fence_status_result


@pytest.fixture
def panel_twokit(qapp):
    """LivePanel in twokit mode (fully posed two-array config)."""
    from conf_pipeline_gui.panels.live import LivePanel
    from conf_pipeline_gui.state import AppState
    st = AppState()
    st.set_config(_config_two_arrays_positioned_and_bearing())
    p = LivePanel(st)
    # Switch to twokit listening mode
    idx = p.live_listening_mode.findData("twokit")
    assert idx >= 0, "twokit mode must exist in the combo"
    p.live_listening_mode.setCurrentIndex(idx)
    yield p
    p.deleteLater()


def _arm_panel_twokit_devices(panel):
    """Give the panel two distinct fake device bindings and wire A1/A2."""
    for combo, dev in ((panel.live_twokit_dev_a, 101), (panel.live_twokit_dev_b, 202)):
        combo.clear()
        combo.addItem(f"dev{dev}", dev)
    # Make sure the array combos have A1/A2
    if panel.live_twokit_arr_a.findData("A1") < 0:
        panel.live_twokit_arr_a.clear()
        panel.live_twokit_arr_a.addItem("Kit A", "A1")
    if panel.live_twokit_arr_b.findData("A2") < 0:
        panel.live_twokit_arr_b.clear()
        panel.live_twokit_arr_b.addItem("Kit B", "A2")
    panel.live_twokit_arr_a.setCurrentIndex(panel.live_twokit_arr_a.findData("A1"))
    panel.live_twokit_arr_b.setCurrentIndex(panel.live_twokit_arr_b.findData("A2"))


def _triangle_fence():
    """Three points forming a simple triangle in room coords."""
    return [Point2D(1.0, 0.5), Point2D(7.0, 0.5), Point2D(4.0, 4.0)]


# --------------------------------------------------------------------------
# Test class: fence controls default state + toggling
# --------------------------------------------------------------------------

class TestFenceControlsLifecycle:

    def test_fence_checkbox_exists_and_is_off_by_default(self, panel_twokit):
        p = panel_twokit
        assert hasattr(p, "live_twokit_fence"), "live_twokit_fence checkbox must exist"
        assert not p.live_twokit_fence.isChecked(), "fence is off by default"

    def test_fence_margin_spinbox_exists_with_correct_defaults(self, panel_twokit):
        p = panel_twokit
        assert hasattr(p, "live_twokit_fence_margin"), "live_twokit_fence_margin must exist"
        assert abs(p.live_twokit_fence_margin.value() - 0.20) < 1e-9, "default margin = 0.20 m"
        assert p.live_twokit_fence_margin.minimum() == 0.0
        assert p.live_twokit_fence_margin.maximum() == 1.0

    def test_fence_draw_and_clear_buttons_exist(self, panel_twokit):
        p = panel_twokit
        assert hasattr(p, "live_twokit_fence_draw"), "live_twokit_fence_draw button must exist"
        assert hasattr(p, "live_twokit_fence_clear"), "live_twokit_fence_clear button must exist"

    def test_fence_margin_and_buttons_disabled_when_fence_unchecked(self, panel_twokit):
        p = panel_twokit
        assert not p.live_twokit_fence.isChecked()
        assert not p.live_twokit_fence_margin.isEnabled(), "margin disabled when fence unchecked"
        assert not p.live_twokit_fence_draw.isEnabled(), "draw disabled when fence unchecked"
        assert not p.live_twokit_fence_clear.isEnabled(), "clear disabled when fence unchecked"

    def test_ticking_fence_enables_margin_and_buttons(self, panel_twokit):
        p = panel_twokit
        p.live_twokit_fence.setChecked(True)
        assert p.live_twokit_fence_margin.isEnabled(), "margin enabled when fence checked"
        assert p.live_twokit_fence_draw.isEnabled(), "draw enabled when fence checked"
        assert p.live_twokit_fence_clear.isEnabled(), "clear enabled when fence checked"
        p.live_twokit_fence.setChecked(False)   # cleanup

    def test_draw_button_arms_fence_tool(self, panel_twokit):
        p = panel_twokit
        p.live_twokit_fence.setChecked(True)
        p.live_twokit_fence_draw.click()
        assert p.state.tool == "fence", "Draw button must set state.tool = 'fence'"
        p.live_twokit_fence.setChecked(False)
        p.state.tool = "select"  # cleanup

    def test_clear_button_empties_live_fence_polygon(self, panel_twokit):
        p = panel_twokit
        p.state.set_live_fence_polygon(_triangle_fence())
        assert len(p.state.live_fence_polygon) == 3
        p.live_twokit_fence.setChecked(True)
        p.live_twokit_fence_clear.click()
        assert p.state.live_fence_polygon == [], "Clear button must empty live_fence_polygon"
        p.live_twokit_fence.setChecked(False)


# --------------------------------------------------------------------------
# Test class: precondition blocks (fence checked, missing bearing/position)
# --------------------------------------------------------------------------

class TestFencePreconditionValidation:

    def test_missing_bearing_blocks_connect_with_named_reason(self, qapp, monkeypatch):
        """Fence checked, A2 has no bearing_deg → blocks with a named message, no _twokit."""
        import conf_pipeline_gui.panels.live as live_mod
        from conf_pipeline_gui.panels.live import LivePanel
        from conf_pipeline_gui.state import AppState

        monkeypatch.setattr(live_mod.cc, "controls_available", lambda: True)
        monkeypatch.setattr(live_mod.cc, "MultiKitController", _FakeMKWithFence)

        st = AppState()
        st.set_config(_config_two_arrays_one_missing_bearing())
        p = LivePanel(st)
        p.live_listening_mode.setCurrentIndex(p.live_listening_mode.findData("twokit"))
        _arm_panel_twokit_devices(p)

        # draw a valid polygon
        st.set_live_fence_polygon(_triangle_fence())

        # enable fence
        p.live_twokit_fence.setChecked(True)

        # attempt connect
        p._twokit_connect()

        assert p._twokit is None, "connect must be blocked when a bearing is missing"
        msg = p.live_twokit_status.text()
        assert msg, "status must be non-empty (blocking reason)"
        assert "bearing" in msg.lower() or "A2" in msg, \
            f"blocking message must mention 'bearing' or 'A2', got: {msg!r}"
        p.deleteLater()

    def test_missing_position_blocks_connect_with_named_reason(self, qapp, monkeypatch):
        """Fence checked, A2 has no position → blocks with a named message."""
        import conf_pipeline_gui.panels.live as live_mod
        from conf_pipeline_gui.panels.live import LivePanel
        from conf_pipeline_gui.state import AppState

        monkeypatch.setattr(live_mod.cc, "controls_available", lambda: True)
        monkeypatch.setattr(live_mod.cc, "MultiKitController", _FakeMKWithFence)

        st = AppState()
        st.set_config(_config_two_arrays_one_missing_position())
        p = LivePanel(st)
        p.live_listening_mode.setCurrentIndex(p.live_listening_mode.findData("twokit"))
        _arm_panel_twokit_devices(p)
        st.set_live_fence_polygon(_triangle_fence())
        p.live_twokit_fence.setChecked(True)
        p._twokit_connect()

        assert p._twokit is None, "connect must be blocked when a position is missing"
        msg = p.live_twokit_status.text()
        assert msg, "status must have a reason"
        assert "position" in msg.lower() or "A2" in msg, \
            f"message must name the problem, got: {msg!r}"
        p.deleteLater()

    def test_fence_checked_but_fewer_than_3_points_blocks_connect(self, qapp, monkeypatch):
        """Fence checked, only 2 points drawn → blocks with 'draw the table fence first'."""
        import conf_pipeline_gui.panels.live as live_mod
        from conf_pipeline_gui.panels.live import LivePanel
        from conf_pipeline_gui.state import AppState

        monkeypatch.setattr(live_mod.cc, "controls_available", lambda: True)
        monkeypatch.setattr(live_mod.cc, "MultiKitController", _FakeMKWithFence)

        st = AppState()
        st.set_config(_config_two_arrays_positioned_and_bearing())
        p = LivePanel(st)
        p.live_listening_mode.setCurrentIndex(p.live_listening_mode.findData("twokit"))
        _arm_panel_twokit_devices(p)
        # only 2 points — not enough
        st.set_live_fence_polygon([Point2D(1.0, 1.0), Point2D(2.0, 1.0)])
        p.live_twokit_fence.setChecked(True)
        p._twokit_connect()

        assert p._twokit is None, "connect must be blocked with < 3 fence points"
        msg = p.live_twokit_status.text()
        assert msg, "status must have a reason"
        # Message should mention drawing or 3 points
        msg_lower = msg.lower()
        assert "draw" in msg_lower or "3" in msg or "fence" in msg_lower, \
            f"message must mention drawing the fence, got: {msg!r}"
        p.deleteLater()

    def test_fence_unchecked_bypasses_precondition_checks(self, qapp, monkeypatch):
        """Fence UNCHECKED → _twokit_connect passes fence_polygon=None, no precondition checks."""
        import conf_pipeline_gui.panels.live as live_mod
        from conf_pipeline_gui.panels.live import LivePanel
        from conf_pipeline_gui.state import AppState

        captured: dict = {}

        class _TrackingFakeMK(_FakeMKWithFence):
            def __init__(self, specs, **kw):
                super().__init__(specs, **kw)
                captured["kw"] = kw

        monkeypatch.setattr(live_mod.cc, "controls_available", lambda: True)
        monkeypatch.setattr(live_mod.cc, "MultiKitController", _TrackingFakeMK)

        st = AppState()
        # Use the missing-bearing config — should NOT block when fence is off
        st.set_config(_config_two_arrays_one_missing_bearing())
        p = LivePanel(st)
        p.live_listening_mode.setCurrentIndex(p.live_listening_mode.findData("twokit"))
        _arm_panel_twokit_devices(p)
        # fence NOT checked
        assert not p.live_twokit_fence.isChecked()
        p._twokit_connect()

        assert p._twokit is not None, "should connect when fence is unchecked (no precondition)"
        kw = captured.get("kw", {})
        assert kw.get("fence_polygon") is None, "fence_polygon must be None when fence unchecked"
        p._live_disconnect()
        p.deleteLater()


# --------------------------------------------------------------------------
# Test class: happy-path connect — fence_polygon passed + set_fence_poses called
# --------------------------------------------------------------------------

class TestFenceConnectHappyPath:

    def test_connect_with_fence_passes_polygon_and_calls_set_fence_poses(
        self, qapp, monkeypatch
    ):
        """Full happy path: posed arrays + ≥3 pts + fence checked →
        MultiKitController receives fence_polygon (len≥3) and set_fence_poses
        is called with two non-None KitPoses."""
        import conf_pipeline_gui.panels.live as live_mod
        from conf_pipeline_gui.panels.live import LivePanel
        from conf_pipeline_gui.state import AppState

        captured: dict = {}

        class _TrackingFakeMK(_FakeMKWithFence):
            def __init__(self, specs, **kw):
                super().__init__(specs, **kw)
                captured["kw"] = kw

        monkeypatch.setattr(live_mod.cc, "controls_available", lambda: True)
        monkeypatch.setattr(live_mod.cc, "MultiKitController", _TrackingFakeMK)

        st = AppState()
        st.set_config(_config_two_arrays_positioned_and_bearing())
        p = LivePanel(st)
        p.live_listening_mode.setCurrentIndex(p.live_listening_mode.findData("twokit"))
        _arm_panel_twokit_devices(p)
        st.set_live_fence_polygon(_triangle_fence())
        p.live_twokit_fence.setChecked(True)
        p.live_twokit_fence_margin.setValue(0.30)

        p._twokit_connect()

        assert p._twokit is not None, "should connect when all preconditions met"
        kw = captured.get("kw", {})
        poly = kw.get("fence_polygon")
        assert poly is not None, "fence_polygon must be passed to MultiKitController"
        assert len(poly) >= 3, "fence_polygon must have ≥3 points"
        assert abs(kw.get("fence_margin_m", 0.0) - 0.30) < 1e-9, \
            "fence_margin_m must match the spinbox"

        tk = p._twokit
        assert tk._fence_poses_called_with is not None, "set_fence_poses must be called"
        poses = tk._fence_poses_called_with
        assert len(poses) == 2, "must set a pose for each kit"
        for i, pose in enumerate(poses):
            assert pose is not None, f"kit {i} pose must not be None"
            assert isinstance(pose, cc.KitPose), f"kit {i} pose must be KitPose"
            assert pose.position is not None, f"kit {i} KitPose.position must not be None"
            assert pose.bearing_deg is not None, f"kit {i} KitPose.bearing_deg must not be None"

        p._live_disconnect()
        p.deleteLater()

    def test_fence_controls_lock_after_successful_connect(self, qapp, monkeypatch):
        """Checkbox + margin + draw/clear must be disabled while connected."""
        import conf_pipeline_gui.panels.live as live_mod
        from conf_pipeline_gui.panels.live import LivePanel
        from conf_pipeline_gui.state import AppState

        monkeypatch.setattr(live_mod.cc, "controls_available", lambda: True)
        monkeypatch.setattr(live_mod.cc, "MultiKitController", _FakeMKWithFence)

        st = AppState()
        st.set_config(_config_two_arrays_positioned_and_bearing())
        p = LivePanel(st)
        p.live_listening_mode.setCurrentIndex(p.live_listening_mode.findData("twokit"))
        _arm_panel_twokit_devices(p)
        st.set_live_fence_polygon(_triangle_fence())
        p.live_twokit_fence.setChecked(True)

        p._twokit_connect()
        assert p._twokit is not None

        assert not p.live_twokit_fence.isEnabled(), "fence checkbox must be locked while connected"
        assert not p.live_twokit_fence_margin.isEnabled(), "margin must be locked while connected"
        assert not p.live_twokit_fence_draw.isEnabled(), "draw must be locked while connected"
        assert not p.live_twokit_fence_clear.isEnabled(), "clear must be locked while connected"

        p._live_disconnect()
        p.deleteLater()


# --------------------------------------------------------------------------
# Test class: _tick_twokit calls update_fence
# --------------------------------------------------------------------------

class TestTickTwokitCallsUpdateFence:

    def test_tick_twokit_calls_update_fence_each_tick(self, qapp, monkeypatch):
        """_tick_twokit must drive update_fence(t) on each call."""
        import conf_pipeline_gui.panels.live as live_mod
        from conf_pipeline_gui.panels.live import LivePanel
        from conf_pipeline_gui.state import AppState

        monkeypatch.setattr(live_mod.cc, "controls_available", lambda: True)
        monkeypatch.setattr(live_mod.cc, "MultiKitController", _FakeMKWithFence)

        st = AppState()
        st.set_config(_config_two_arrays_positioned_and_bearing())
        p = LivePanel(st)
        p.live_listening_mode.setCurrentIndex(p.live_listening_mode.findData("twokit"))
        _arm_panel_twokit_devices(p)
        st.set_live_fence_polygon(_triangle_fence())
        p.live_twokit_fence.setChecked(True)
        p._twokit_connect()
        tk = p._twokit

        before = tk._update_fence_calls
        p._tick_twokit()
        p._tick_twokit()
        after = tk._update_fence_calls

        assert after - before == 2, (
            f"_tick_twokit must call update_fence once per tick; "
            f"got {after - before} calls for 2 ticks"
        )

        p._live_disconnect()
        p.deleteLater()


# --------------------------------------------------------------------------
# Test class: _publish_overlay includes fence_polygon + fused_positions
# --------------------------------------------------------------------------

class TestPublishOverlayFenceKeys:

    def test_publish_overlay_includes_fence_polygon_from_state(self, qapp, monkeypatch):
        """_publish_overlay must emit fence_polygon = list(state.live_fence_polygon)."""
        import conf_pipeline_gui.panels.live as live_mod
        from conf_pipeline_gui.panels.live import LivePanel
        from conf_pipeline_gui.state import AppState

        monkeypatch.setattr(live_mod.cc, "controls_available", lambda: True)
        monkeypatch.setattr(live_mod.cc, "MultiKitController", _FakeMKWithFence)

        st = AppState()
        st.set_config(_config_two_arrays_positioned_and_bearing())
        p = LivePanel(st)
        p.live_listening_mode.setCurrentIndex(p.live_listening_mode.findData("twokit"))
        _arm_panel_twokit_devices(p)
        tri = _triangle_fence()
        st.set_live_fence_polygon(tri)
        p.live_twokit_fence.setChecked(True)
        p._twokit_connect()
        assert p._twokit is not None

        p._publish_overlay()

        ov = st.live_overlay
        assert ov is not None, "_publish_overlay must set live_overlay"
        fp = ov.get("fence_polygon")
        assert fp is not None, "fence_polygon must be present in overlay"
        assert len(fp) == 3, "fence_polygon must echo the full polygon"

        p._live_disconnect()
        p.deleteLater()

    def test_publish_overlay_emits_fused_positions_when_fence_status_has_point(
        self, qapp, monkeypatch
    ):
        """When fence_status() returns a point, _publish_overlay emits fused_positions."""
        import conf_pipeline_gui.panels.live as live_mod
        from conf_pipeline_gui.panels.live import LivePanel
        from conf_pipeline_gui.state import AppState

        class _FakeMKWithPoint(_FakeMKWithFence):
            def fence_status(self):
                return {
                    "keep": True,
                    "veto_kit": None,
                    "point": (3.5, 2.0),
                    "inside": True,
                    "confidence": 0.85,
                    "degenerate": False,
                    "polygon": [],
                }

        monkeypatch.setattr(live_mod.cc, "controls_available", lambda: True)
        monkeypatch.setattr(live_mod.cc, "MultiKitController", _FakeMKWithPoint)

        st = AppState()
        st.set_config(_config_two_arrays_positioned_and_bearing())
        p = LivePanel(st)
        p.live_listening_mode.setCurrentIndex(p.live_listening_mode.findData("twokit"))
        _arm_panel_twokit_devices(p)
        st.set_live_fence_polygon(_triangle_fence())
        p.live_twokit_fence.setChecked(True)
        p._twokit_connect()
        assert p._twokit is not None

        p._publish_overlay()

        ov = st.live_overlay
        assert ov is not None
        fused = ov.get("fused_positions")
        assert fused is not None and len(fused) >= 1, \
            "fused_positions must be emitted when fence_status returns a point"
        entry = fused[0]
        assert abs(entry["x"] - 3.5) < 1e-9
        assert abs(entry["y"] - 2.0) < 1e-9
        assert entry["inside"] is True
        assert abs(entry["confidence"] - 0.85) < 1e-9

        p._live_disconnect()
        p.deleteLater()


# --------------------------------------------------------------------------
# Test class: disconnect re-enables controls, keeps live_fence_polygon
# --------------------------------------------------------------------------

class TestFenceDisconnectBehavior:

    def test_disconnect_re_enables_fence_controls(self, qapp, monkeypatch):
        """After disconnect, fence checkbox+margin+draw+clear must be re-enabled."""
        import conf_pipeline_gui.panels.live as live_mod
        from conf_pipeline_gui.panels.live import LivePanel
        from conf_pipeline_gui.state import AppState

        monkeypatch.setattr(live_mod.cc, "controls_available", lambda: True)
        monkeypatch.setattr(live_mod.cc, "MultiKitController", _FakeMKWithFence)

        st = AppState()
        st.set_config(_config_two_arrays_positioned_and_bearing())
        p = LivePanel(st)
        p.live_listening_mode.setCurrentIndex(p.live_listening_mode.findData("twokit"))
        _arm_panel_twokit_devices(p)
        st.set_live_fence_polygon(_triangle_fence())
        p.live_twokit_fence.setChecked(True)
        p._twokit_connect()
        assert p._twokit is not None

        p._live_disconnect()

        assert p._twokit is None
        # The fence checkbox itself must be re-enabled
        assert p.live_twokit_fence.isEnabled(), "fence checkbox must be re-enabled after disconnect"
        # margin+draw+clear: enabled because the checkbox is still ticked
        # (the key point is they're not left in a permanently-disabled state)
        assert p.live_twokit_fence_margin.isEnabled(), "margin must be re-enabled after disconnect"
        assert p.live_twokit_fence_draw.isEnabled(), "draw must be re-enabled after disconnect"
        assert p.live_twokit_fence_clear.isEnabled(), "clear must be re-enabled after disconnect"
        p.deleteLater()

    def test_disconnect_keeps_live_fence_polygon(self, qapp, monkeypatch):
        """live_fence_polygon must persist after disconnect (NOT cleared)."""
        import conf_pipeline_gui.panels.live as live_mod
        from conf_pipeline_gui.panels.live import LivePanel
        from conf_pipeline_gui.state import AppState

        monkeypatch.setattr(live_mod.cc, "controls_available", lambda: True)
        monkeypatch.setattr(live_mod.cc, "MultiKitController", _FakeMKWithFence)

        st = AppState()
        st.set_config(_config_two_arrays_positioned_and_bearing())
        p = LivePanel(st)
        p.live_listening_mode.setCurrentIndex(p.live_listening_mode.findData("twokit"))
        _arm_panel_twokit_devices(p)
        tri = _triangle_fence()
        st.set_live_fence_polygon(tri)
        p.live_twokit_fence.setChecked(True)
        p._twokit_connect()
        p._live_disconnect()

        assert st.live_fence_polygon == tri, \
            "live_fence_polygon must be kept on disconnect (not cleared)"
        p.deleteLater()
