"""Tests for Task 4: transient AppState.live_fence_polygon + Canvas "fence" draw tool.

Hardware-free — constructs AppState and Canvas directly (NOT MainWindow, which hangs
headless on Windows per CLAUDE.md).  Pattern mirrors test_gui_autosteer_sectors.py.
"""
from __future__ import annotations

import os

import pytest

pytest.importorskip("PySide6")

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import conf_pipeline as cp  # noqa: E402
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
