"""Main window + application entry point — the "Stagebar" workflow-modes shell.

Navigation is the workflow itself: five top-level modes (DESIGN → SIMULATE →
ROUTE → DEPLOY → LIVE) in a centered ModeBar with live status dots. The left
tool rail shows only the current mode's canvas tools, the right panel follows
the mode, and a validation pill in the top bar is visible from everywhere.
"""
from __future__ import annotations

import sys
from pathlib import Path

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QAction, QFont, QGuiApplication, QIcon, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QApplication,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QMainWindow,
    QMenu,
    QMessageBox,
    QSplitter,
    QStackedWidget,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

import conf_pipeline as cp

from . import icons, workflow
from .canvas import Canvas
from .issues import IssuesDrawer
from .modebar import ModeBar
from .panels import DesignPanel, DeployPanel, LivePanel, RoutePanel, SimulatePanel
from .scenarios import SCENARIOS
from .simbar import SimBar
from .state import AppState, now_iso
from .theme import DARK_QSS, LIGHT_QSS, build_qss, palette  # noqa: F401 (re-exported)
from .toolrail import MODE_TOOLS, ToolRail
from .viewbar import ViewBar

# Pressing a tool key outside its home mode hops there first (Fusion-style).
TOOL_HOME_MODE = {"room": "design", "zone": "design", "talker": "design", "furniture": "design", "connect": "route"}

_ASSETS = Path(__file__).resolve().parent / "assets"


def app_icon() -> QIcon:
    """The Aniston wren mark as the window/taskbar icon.

    Uses the white mark — the app ships dark-themed (and Windows 11's taskbar is
    dark by default), so white reads better than the black original. Prefers the
    multi-resolution .ico (crisp 16-256 px on Windows) and falls back through the
    white PNG to the black assets. All are loaded by Qt's built-in image readers,
    so no extra image-format plugin (or Pillow) is needed at runtime.
    """
    for name in ("aniston_white.ico", "aniston_mark_white.png", "aniston.ico", "aniston_mark.png"):
        p = _ASSETS / name
        if p.exists():
            return QIcon(str(p))
    return QIcon()


AUTOSAVE_INTERVAL_MS = 30_000


class MainWindow(QMainWindow):
    def __init__(self, files: cp.ProjectFileManager | None = None):
        super().__init__()
        self.setWindowTitle("Aniston Room Designer")
        self.setWindowIcon(app_icon())
        self.resize(1320, 840)
        self.state = AppState()
        # project file manager: recent files, autosave, crash recovery
        self.files = files if files is not None else cp.ProjectFileManager()
        self._dirty = False
        self._sim_overlays_seeded = False   # seed pickup+FOV once on entering Simulate/Live

        self.canvas = Canvas(self.state)
        self.canvas.coord_cb = lambda s: self.coord_label.setText(s)

        # per-mode right panels
        self.panels = {
            "design": DesignPanel(self.state),
            "simulate": SimulatePanel(self.state),
            "route": RoutePanel(self.state),
            "deploy": DeployPanel(self.state),
            "live": LivePanel(self.state),
        }
        self.panels["live"]._canvas = self.canvas   # so the Live panel can arm "click to aim"
        self.panel_stack = QStackedWidget()
        self.panel_stack.setMinimumWidth(380)
        for mode in workflow.MODES:
            self.panel_stack.addWidget(self.panels[mode])

        # floating view bar over the canvas's top-left corner
        self.viewbar = ViewBar(self.canvas)
        self.viewbar.move(12, 12)
        self.viewbar.viewSelected.connect(self._set_view)
        self.viewbar.coverageToggled.connect(self._toggle_coverage)
        self.viewbar.heatmapToggled.connect(self._toggle_heatmap)

        # floating simulation bar over the canvas's top-right corner
        self.simbar = SimBar(self.canvas)
        self.simbar.pickupToggled.connect(self._toggle_pickup)
        self.simbar.fovToggled.connect(self._toggle_fov)
        self.simbar.dispersionToggled.connect(self._toggle_dispersion)
        self.simbar.occlusionToggled.connect(self._toggle_occlusion)
        self.canvas.on_resize = self._position_simbar  # re-anchor top-right on canvas resize
        self._position_simbar()

        self.toolrail = ToolRail(self.state.theme)
        self.toolrail.toolSelected.connect(self._set_tool)
        self.toolrail.zoneKindSelected.connect(self._zone_kind_selected)
        self.toolrail.furnitureKindSelected.connect(self._furniture_kind_selected)
        self.toolrail.set_mode(self.state.mode)

        split = QSplitter()
        split.addWidget(self.canvas)
        split.addWidget(self.panel_stack)
        split.setStretchFactor(0, 1)
        split.setStretchFactor(1, 0)
        split.setSizes([860, 420])

        self._row = QWidget()
        row_lay = QHBoxLayout(self._row)
        row_lay.setContentsMargins(0, 0, 0, 0)
        row_lay.setSpacing(0)
        row_lay.addWidget(self.toolrail)
        row_lay.addWidget(split, 1)

        central = QWidget()
        col = QVBoxLayout(central)
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(0)
        col.addWidget(self._build_topbar())
        col.addWidget(self._row, 1)
        self.setCentralWidget(central)

        # the Issues drawer slides over the right panel, in every mode
        self.issues_drawer = IssuesDrawer(self.state, central)

        self._build_statusbar()
        self._shortcuts()
        self.state.changed.connect(self._sync_chrome)
        self.state.changed.connect(self._mark_dirty)
        self.state.modeChanged.connect(self._apply_mode)
        self._autosave_timer = QTimer(self)
        self._autosave_timer.setInterval(AUTOSAVE_INTERVAL_MS)
        self._autosave_timer.timeout.connect(self._autosave_tick)
        self._autosave_timer.start()
        self._sync_chrome()

    # ------------------------------------------------------------------ top bar
    def _tool_btn(self, icon_name: str, tip: str, slot=None) -> QToolButton:
        b = QToolButton()
        b.setToolTip(tip)
        b.setCursor(Qt.PointingHandCursor)
        if icon_name:
            b.setIcon(icons.icon(icon_name, palette(self.state.theme)["muted"],
                                 active_color=palette(self.state.theme)["accent_bright"]))
        if slot is not None:
            b.clicked.connect(slot)
        return b

    def _build_topbar(self) -> QFrame:
        bar = QFrame()
        bar.setProperty("topbar", "true")
        lay = QHBoxLayout(bar)
        lay.setContentsMargins(8, 5, 8, 5)
        lay.setSpacing(6)

        self.menu_btn = self._tool_btn("menu", "All commands: samples, import/export, room, deploy, theme")
        self.menu_btn.setPopupMode(QToolButton.InstantPopup)
        self.menu_btn.setMenu(self._build_app_menu())
        lay.addWidget(self.menu_btn)

        self.room_btn = QToolButton()
        self.room_btn.setToolTip("Rooms in this project — switch, add, rename")
        self.room_btn.setCursor(Qt.PointingHandCursor)
        self.room_btn.setPopupMode(QToolButton.InstantPopup)
        self.room_btn.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        self.room_btn.setIcon(icons.icon("rooms", palette(self.state.theme)["muted"]))
        room_menu = QMenu(self.room_btn)
        room_menu.aboutToShow.connect(lambda m=room_menu: self._fill_room_menu(m))
        self.room_btn.setMenu(room_menu)
        lay.addWidget(self.room_btn)

        lay.addStretch(1)
        self.modebar = ModeBar(self.state.theme)
        self.modebar.modeSelected.connect(self.state.set_mode)
        lay.addWidget(self.modebar)
        lay.addStretch(1)

        self.val_pill = QToolButton()
        self.val_pill.setProperty("pill", "true")
        self.val_pill.setToolTip("Validation state — click to review the issues")
        self.val_pill.setCursor(Qt.PointingHandCursor)
        self.val_pill.clicked.connect(self._show_issues)
        lay.addWidget(self.val_pill)

        self.undo_btn = self._tool_btn("undo", "Undo the last change (Ctrl+Z)", self.state.undo)
        self.redo_btn = self._tool_btn("redo", "Redo (Ctrl+Shift+Z)", self.state.redo)
        lay.addWidget(self.undo_btn)
        lay.addWidget(self.redo_btn)
        return bar

    def _build_app_menu(self) -> QMenu:
        m = QMenu(self)
        m.setToolTipsVisible(True)  # QMenu hides action tooltips by default

        samples = m.addMenu("Load sample")
        for key, label, _fn in SCENARIOS:
            samples.addAction(label, lambda k=key: self._load_scenario(k))
        samples.addSeparator()
        samples.addAction("Empty", lambda: self._load_scenario("empty"))

        m.addAction("Import config…", self._import)
        self.recent_menu = m.addMenu("Open recent")
        self.recent_menu.aboutToShow.connect(self._fill_recent_menu)
        m.addAction("Export config…", self._export)
        m.addAction("Export design report…", self._export_report)
        a_comm = m.addAction("Export commissioning report…", self._export_commissioning)
        a_comm.setToolTip("As-built config + measured live state (latency, AEC/ERLE, A/B noise proof) + sign-off")
        a_diag = m.addAction("Audio operator diagnostics…", self._show_operator_diagnostics)
        a_diag.setToolTip("Read-only Device / Calibration / Placement / Pipeline / Egress / Transcription status + export")
        a_rp = m.addAction("Audio room profiles…", self._show_room_profiles)
        a_rp.setToolTip("Save / load / validate room-specific audio setup profiles (not applied to the engine)")
        m.addSeparator()

        self.act_optimize = QAction("✨ Optimize room", self)
        self.act_optimize.setToolTip("One click: place arrays, assign coverage channels, route everything")
        self.act_optimize.setShortcut(QKeySequence("Ctrl+Shift+O"))
        self.act_optimize.triggered.connect(self._optimize_room)
        m.addAction(self.act_optimize)
        self.act_auto_route = QAction("⚡ Auto-Route", self)
        self.act_auto_route.setToolTip("AEC references + automixer + far-end & near-end routing")
        self.act_auto_route.triggered.connect(self._auto_route)
        m.addAction(self.act_auto_route)
        self.act_auto = QAction("Auto-configure", self)
        self.act_auto.setToolTip("AEC + automixer buses only (no reinforcement routing)")
        self.act_auto.triggered.connect(self._auto)
        m.addAction(self.act_auto)
        m.addSeparator()

        m.addAction("Rectangular room", self._rect_room)
        m.addAction("Import floor plan…", self._import_floor_plan)
        m.addAction("Calibrate floor-plan scale…", self._calibrate_scale)
        m.addAction("Auto-name devices", self._auto_name)
        m.addSeparator()
        m.addAction("Deploy (snapshot design)", self._deploy)
        m.addSeparator()
        m.addAction("Toggle dark / light theme", self._toggle_theme)
        m.addSeparator()
        m.addAction("Show LIVE getting-started", self._show_live_guide)
        return m

    def _show_live_guide(self):
        self.state.set_mode("live")
        self.panels["live"].show_first_run_guide(force=True)

    def _fill_room_menu(self, menu: QMenu):
        menu.clear()
        for i, r in enumerate(self.state.rooms):
            name = r["config"].metadata.get("name", r["id"])
            a = QAction(name, menu, checkable=True)
            a.setChecked(i == self.state.active_room)
            a.triggered.connect(lambda _c=False, idx=i: self.state.switch_room(idx))
            menu.addAction(a)
        menu.addSeparator()
        menu.addAction("Add room", self.state.add_room)
        if len(self.state.rooms) > 1:
            menu.addAction("Remove current room", lambda: self.state.remove_room(self.state.active_room))
        menu.addAction("Rename room…", self._rename_room)

    def _rename_room(self):
        cur = self.state.config.metadata.get("name", "")
        name, ok = QInputDialog.getText(self, "Rename room", "Room name:", text=cur)
        if ok and name.strip():
            self.state.rename_room(self.state.active_room, name.strip())

    # ------------------------------------------------------------------ chrome
    def _build_statusbar(self):
        self.coord_label = QLabel("x — , y —")
        self.room_label = QLabel("")
        self.statusBar().addPermanentWidget(self.room_label)
        self.statusBar().addPermanentWidget(self.coord_label)

    def _shortcuts(self):
        QShortcut(QKeySequence.Undo, self, self.state.undo)
        QShortcut(QKeySequence.Redo, self, self.state.redo)
        QShortcut(QKeySequence("Delete"), self, self._delete_selection)
        QShortcut(QKeySequence("Backspace"), self, self._delete_selection)
        for k, tool in [("V", "select"), ("C", "connect"), ("R", "room"), ("Z", "zone"), ("T", "talker"), ("F", "furniture")]:
            QShortcut(QKeySequence(k), self, lambda t=tool: self._shortcut_tool(t))
        QShortcut(QKeySequence("2"), self, lambda: self._set_view("2d"))
        QShortcut(QKeySequence("3"), self, lambda: self._set_view("3d"))
        for i, mode in enumerate(workflow.MODES, start=1):
            QShortcut(QKeySequence(f"Ctrl+{i}"), self, lambda m=mode: self.state.set_mode(m))
        QShortcut(QKeySequence("Ctrl+Shift+J"), self, self._show_json)

    def _show_json(self):
        """Jump straight to the raw config JSON (Deploy panel's Data card)."""
        self.state.set_mode("deploy")
        deploy = self.panels["deploy"]
        deploy.data_card.set_open(True)
        deploy._maybe_fill_json()

    def _sync_chrome(self):
        self.undo_btn.setEnabled(self.state.can_undo())
        self.redo_btn.setEnabled(self.state.can_redo())
        self.room_btn.setText(self.state.config.metadata.get("name", "Room"))
        res = cp.validate(self.state.config)
        if res.ok:
            warn = len(res.warnings)
            self.val_pill.setText("✓ Valid" + (f" · {warn} ⚠" if warn else ""))
            self.val_pill.setProperty("level", "warn" if warn else "ok")
        else:
            self.val_pill.setText(f"✗ {len(res.errors)} error{'s' if len(res.errors) != 1 else ''}")
            self.val_pill.setProperty("level", "error")
        self.val_pill.style().unpolish(self.val_pill)
        self.val_pill.style().polish(self.val_pill)
        self.modebar.set_status(workflow.stage_status(self.state, res))
        room = self.state.config.room
        if room and room.vertices:
            xs = [v.x for v in room.vertices]
            ys = [v.y for v in room.vertices]
            self.room_label.setText(
                f"Room {max(xs) - min(xs):.1f} × {max(ys) - min(ys):.1f} × {room.height:.1f} m"
            )
        else:
            self.room_label.setText("No room")
        self.viewbar.set_view(self.state.view)
        self.viewbar.set_coverage(self.state.show_coverage)
        self.viewbar.set_heatmap(self.state.sim_show_heatmap)
        self.simbar.set_pickup(self.state.sim_show_pickup)
        self.simbar.set_fov(self.state.sim_show_fov)
        self.simbar.set_dispersion(self.state.sim_show_dispersion)
        self.simbar.set_occlusion(self.state.sim_show_occlusion)
        self._refresh_sim_summary()
        self.toolrail.set_tool(self.state.tool)
        self.toolrail.set_zone_kind(self.state.zone_kind)
        self.toolrail.set_furniture_kind(self.state.furniture_kind)

    # ------------------------------------------------------------------- modes
    def _apply_mode(self, mode: str):
        self.state.calibrating = False  # an armed calibration drag is a DESIGN affair
        self.modebar.set_active(mode)
        self.toolrail.set_mode(mode)
        if self.state.tool not in MODE_TOOLS.get(mode, ["select"]):
            self._set_tool("select")
        self.panel_stack.setCurrentWidget(self.panels[mode])
        # per-mode overlay defaults (user-overridable afterwards)
        if mode in ("simulate", "live") and not self.state.show_coverage:
            self.state.show_coverage = True
        # entering SIMULATE/LIVE turns on the coverage view once, so the angles are
        # visible without hunting for the SimBar; the user can toggle them off after
        if mode in ("simulate", "live") and not self._sim_overlays_seeded:
            self.state.sim_show_pickup = True
            self.state.sim_show_fov = True
            self._sim_overlays_seeded = True
            self.simbar.set_pickup(True)
            self.simbar.set_fov(True)
            self._refresh_sim_summary()
        if mode != "live" and self.panels["live"]._live_busy():
            self.toast("Live session running — the LIVE dot stays red until you disconnect")
        if mode == "live":
            self.panels["live"].show_first_run_guide()   # self-gates: only on first run, once per session
        self.canvas.update()

    def _goto_mode(self, mode: str):
        self.state.set_mode(mode)

    def _show_issues(self):
        self.issues_drawer.open_drawer(self._row.geometry())

    def _live_session_changed(self, busy: bool):
        self.modebar.set_live_connected(busy)

    def resizeEvent(self, e):  # noqa: N802 (Qt override)
        super().resizeEvent(e)
        if hasattr(self, "issues_drawer"):
            self.issues_drawer.reposition(self._row.geometry())

    def closeEvent(self, e):  # noqa: N802 (Qt override)
        live = self.panels.get("live")
        if live is not None and live._live_busy():
            live._live_disconnect()
        self.files.clear_autosave()       # clean exit — the autosave is the crash marker
        super().closeEvent(e)

    # ------------------------------------------------------------------ actions
    def _set_tool(self, tool):
        self.state.tool = tool
        self.canvas.connect_from = None
        self.canvas.draw_pts = []
        self.toolrail.set_tool(tool)
        self.canvas.update()

    def _shortcut_tool(self, tool):
        """Tool shortcut from anywhere: hop to the tool's home mode if needed."""
        if tool not in MODE_TOOLS.get(self.state.mode, ["select"]):
            home = TOOL_HOME_MODE.get(tool)
            if home is None:
                return
            self.state.set_mode(home)
        self._set_tool(tool)

    def _zone_kind_selected(self, kind):
        self.state.zone_kind = kind
        if self.state.mode == "design":
            self._set_tool("zone")

    def _set_view(self, view):
        self.state.view = view
        if view == "3d":
            b = self.canvas.bounds()
            span = max(b[2] - b[0], b[3] - b[1], self.canvas._room_h() * 1.4)
            self.state.cam["dist"] = max(7.0, span * 1.6)
        self.viewbar.set_view(view)
        self.canvas.update()

    def _auto(self):
        self.state.set_config(cp.auto_configure(self.state.config))
        self.toast("Auto-configured")

    def _auto_route(self):
        res = cp.auto_route(self.state.config)
        self.state.set_config(res.config)  # one undo step
        QMessageBox.information(self, "Auto-Route", "\n".join(f"• {c}" for c in res.changes) or "No changes.")
        self.toast("Auto-Route complete")

    def _optimize_room(self):
        res = cp.optimize_room(self.state.config)
        self.state.set_config(res.config)  # one undo step
        QMessageBox.information(self, "Optimize room", "\n".join(f"• {c}" for c in res.changes) or "No changes.")
        self.toast("Optimize room complete")

    def _rect_room(self):
        self.state.set_config(cp.set_room(self.state.config, cp.rectangular_room(9, 7, 3)))

    def _guide_add_array(self):
        """Quick-add a ceiling array at the room centre (one click, room if needed)."""
        cfg = self.state.config
        if cfg.room is None:
            cfg = cp.set_room(cfg, cp.rectangular_room(9, 7, 3))
        did = self.state.next_device_id("microphoneArray")
        cfg = cp.add_device(cfg, cp.create_microphone_array(did, f"Ceiling Array {did}", "automatic"))
        xs = [v.x for v in cfg.room.vertices]
        ys = [v.y for v in cfg.room.vertices]
        center = cp.Point2D(round((sum(xs) / len(xs)) * 4) / 4, round((sum(ys) / len(ys)) * 4) / 4)
        cfg = cp.set_device_position(cfg, did, center)
        self.state.selection = {"kind": "device", "id": did}
        self.state.set_config(cfg)
        self.toast(f"Added {did} at the room centre")

    def _toggle_coverage(self, on):
        self.state.show_coverage = bool(on)
        self.canvas.update()

    def _toggle_heatmap(self, on):
        # Bridge to the Simulate panel's checkbox so its compute logic runs.
        self.panels["simulate"].set_heatmap(bool(on))

    # ---- coverage-simulation overlays (SimBar) ----
    def _position_simbar(self):
        self.simbar.adjustSize()
        x = max(12, self.canvas.width() - self.simbar.width() - 12)
        self.simbar.move(x, 12)

    def _toggle_pickup(self, on):
        self.state.sim_show_pickup = bool(on)
        self._refresh_sim_summary()
        self.canvas.update()

    def _toggle_fov(self, on):
        self.state.sim_show_fov = bool(on)
        self._refresh_sim_summary()
        self.canvas.update()

    def _toggle_dispersion(self, on):
        self.state.sim_show_dispersion = bool(on)
        self._refresh_sim_summary()
        self.canvas.update()

    def _toggle_occlusion(self, on):
        self.state.sim_show_occlusion = bool(on)
        self._refresh_sim_summary()
        self.canvas.update()

    def _furniture_kind_selected(self, kind):
        self.state.furniture_kind = kind
        if self.state.mode == "design":
            self._set_tool("furniture")

    def _refresh_sim_summary(self):
        st = self.state
        any_on = st.sim_show_pickup or st.sim_show_fov or st.sim_show_dispersion or st.sim_show_occlusion
        if not any_on:
            self.simbar.set_summary("")
            return
        s = st.room_coverage().summary
        n = s.get("target_count", 0)
        if n == 0:
            self.simbar.set_summary("No talkers/seats to cover")
            return
        gaps = len(s.get("mic_gaps", []))
        parts = [f"Pickup {s.get('mic_coverage_pct', 0):.0f}%"]
        if gaps:
            parts.append(f"{gaps} gap{'s' if gaps != 1 else ''}")
        if s.get("has_camera"):
            parts.append(f"Framed {s.get('camera_framed_pct', 0):.0f}%")
        self.simbar.set_summary(" · ".join(parts))

    def _import_floor_plan(self):
        path, _ = QFileDialog.getOpenFileName(self, "Floor plan image", "", "Images (*.png *.jpg *.jpeg *.bmp *.gif)")
        if not path:
            return
        cfg = self.state.config
        if cfg.room is None:
            cfg = cp.set_room(cfg, cp.rectangular_room(9, 7, 3))
            self.toast("Added a default room for the floor plan")
        from PySide6.QtGui import QImageReader
        sz = QImageReader(path).size()
        w_px, h_px = (sz.width(), sz.height()) if sz.isValid() else (0, 0)
        if w_px <= 0 or h_px <= 0:
            return self.toast("Could not read that image.")
        xs = [v.x for v in cfg.room.vertices]
        ys = [v.y for v in cfg.room.vertices]
        room_w = (max(xs) - min(xs)) or 9.0
        cfg = cp.set_room_background(cfg, path, w_px, h_px, scale_m_per_px=room_w / w_px, origin=cp.Point2D(min(xs), min(ys)))
        self.state.set_config(cfg)
        self.toast("Floor plan added — use Calibrate to set the true scale")

    def _calibrate_scale(self):
        if self.state.config.room is None or self.state.config.room.background is None:
            return self.toast("Import a floor plan first.")
        self.state.set_mode("design")
        self._set_view("2d")
        self.state.calibrating = True
        self.toast("Drag a line over a known distance, then enter its length.")

    def _delete_selection(self):
        sel = self.state.selection
        if not sel:
            return
        cfg = self.state.config
        try:
            if sel["kind"] == "device":
                self.state.set_config(cp.remove_device(cfg, sel["id"]))
            elif sel["kind"] == "talker":
                self.state.set_config(cp.remove_talker(cfg, sel["id"]))
            elif sel["kind"] == "zone":
                self.state.set_config(cp.remove_coverage_zone(cfg, sel["array_id"], sel["zone_id"]))
            elif sel["kind"] == "furniture":
                self.state.set_config(cp.remove_furniture(cfg, sel["id"]))
            self.state.selection = None
        except Exception as exc:
            self.toast(str(exc))

    def _load_scenario(self, which):
        self.state.selection = None
        if which == "empty":
            self.state.set_config(cp.create_config("Untitled", now_iso()))
            return
        entry = next((e for e in SCENARIOS if e[0] == which), None)
        if entry is not None:
            _key, label, builder = entry
            self.state.set_config(builder())
            self.toast(f"Loaded {label} — switch to Simulate to optimise placement")

    def _export(self):
        path, _ = QFileDialog.getSaveFileName(self, "Export config", "config.json", "JSON (*.json)")
        if path:
            self.files.save_config(self.state.config, path)
            self.toast("Exported")

    def _export_report(self):
        path, sel = QFileDialog.getSaveFileName(self, "Export design report", "design-report.md",
                                                "Markdown (*.md);;HTML (*.html)")
        if not path:
            return
        fmt = "html" if (path.lower().endswith(".html") or "html" in sel.lower()) else "markdown"
        with open(path, "w", encoding="utf-8") as f:
            f.write(cp.design_report(self.state.config, fmt))
        self.toast("Report exported")

    def _export_commissioning(self):
        path, sel = QFileDialog.getSaveFileName(self, "Export commissioning report", "commissioning-report.md",
                                                "Markdown (*.md);;HTML (*.html)")
        if not path:
            return
        fmt = "html" if (path.lower().endswith(".html") or "html" in sel.lower()) else "markdown"
        info = self.panels["live"].commissioning_info()
        with open(path, "w", encoding="utf-8") as f:
            f.write(cp.commissioning_report(self.state.config, info, fmt))
        self.toast("Commissioning report exported")

    def _operator_status(self):
        """Build a read-only OperatorStatus from the running engine (or all-off defaults when not live).
        Reads flags only — changes no default and applies nothing."""
        from conf_pipeline_control.operator import OperatorStatus
        try:
            eng = self.panels["live"].active_engine()
        except Exception:
            eng = None
        calib_path = getattr(self.panels["live"], "_calibration_path", None)   # show the applied profile path
        return OperatorStatus.build(engine=eng, calibration_path=calib_path, generated_at=now_iso())

    def _show_operator_diagnostics(self):
        """Open the read-only Audio Operator Diagnostics window (Device / Calibration / Placement /
        Pipeline / Egress / Transcription + warnings), built from the running engine. No DSP controls,
        no auto-apply — diagnostics only."""
        from .panels.operator import OperatorDiagnosticsWindow
        win = getattr(self, "_operator_diag_win", None)
        if win is None:
            win = OperatorDiagnosticsWindow(status_provider=self._operator_status)
            self._operator_diag_win = win
        win.refresh()
        win.show()
        win.raise_()
        win.activateWindow()

    def _show_room_profiles(self):
        """Open the Audio Room Profiles window (save / load / validate room-specific audio setup).
        Profile management only — it never applies anything to the running audio engine."""
        from .panels.room_profile import AudioRoomProfilesWindow
        win = getattr(self, "_room_profiles_win", None)
        if win is None:
            win = AudioRoomProfilesWindow()
            self._room_profiles_win = win
        win.show()
        win.raise_()
        win.activateWindow()

    def _import(self):
        path, _ = QFileDialog.getOpenFileName(self, "Import config", "", "JSON (*.json)")
        if path:
            self._open_path(path)

    def _open_path(self, path: str):
        """Open a config file with the migration-notice contract."""
        try:
            res = self.files.open_config(path)
        except Exception as exc:
            QMessageBox.warning(self, "Import failed", str(exc))
            return
        self.state.set_config(res.config)
        if res.migrated:
            QMessageBox.information(self, "File upgraded", res.migration_notice())
        else:
            self.toast("Imported")

    def _fill_recent_menu(self):
        self.recent_menu.clear()
        recent = self.files.recent_files()
        if not recent:
            a = self.recent_menu.addAction("(no recent files)")
            a.setEnabled(False)
            return
        for p in recent:
            self.recent_menu.addAction(p, lambda _c=False, path=p: self._open_path(path))
        self.recent_menu.addSeparator()
        self.recent_menu.addAction("Clear list", self.files.clear_recent)

    # ---- autosave / crash recovery ----
    def _mark_dirty(self):
        self._dirty = True

    def _workspace_payload(self) -> str:
        """The whole multi-room workspace as a project JSON (all rooms survive)."""
        self.state._snapshot()            # fold the live room back into the rooms list
        rooms = [cp.ProjectRoom(id=r["id"], config=r["config"]) for r in self.state.rooms]
        project = cp.Project(
            version=cp.PROJECT_VERSION,
            metadata={"name": self.state.config.metadata.get("name", "Untitled"), "createdAt": now_iso()},
            rooms=rooms,
            active_room_id=self.state.rooms[self.state.active_room]["id"],
        )
        return cp.serialize_project(project, pretty=True)

    def _autosave_tick(self):
        if not self._dirty:
            return
        self.files.autosave(self._workspace_payload(),
                            origin=self.state.config.metadata.get("name", "Untitled"))
        self._dirty = False

    def _offer_recovery(self):
        """Startup crash recovery: offer a leftover autosave back to the user.
        Called from main() — never from __init__, so embedding/tests stay modal-free."""
        info = self.files.pending_recovery()
        if info is None:
            return
        when = f" (saved {info.saved_at})" if info.saved_at else ""
        what = f' "{info.origin}"' if info.origin else ""
        ans = QMessageBox.question(
            self, "Recover unsaved work?",
            f"The last session did not exit cleanly. Recover the autosaved workspace{what}{when}?",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.Yes,
        )
        if ans == QMessageBox.Yes:
            try:
                self._restore_workspace(self.files.read_recovery())
                self.toast("Workspace recovered")
            except Exception as exc:
                QMessageBox.warning(self, "Recovery failed", str(exc))
        self.files.clear_autosave()       # offered once, either way

    def _restore_workspace(self, payload: str):
        project = cp.deserialize_project(payload)
        rooms = [
            {"id": r.id, "config": r.config, "history": [r.config], "idx": 0, "last_deployed": None}
            for r in project.rooms
        ]
        active = next((i for i, r in enumerate(project.rooms) if r.id == project.active_room_id), 0)
        self.state.load_rooms(rooms, active)

    def _auto_name(self):
        self.state.set_config(cp.apply_naming_scheme(self.state.config))
        self.toast("Applied naming scheme")

    def _deploy(self):
        diff = self.state.deploy()
        if diff.identical:
            return self.toast("Deployed — no changes since last deploy")
        parts = []
        if diff.devices_added:
            parts.append(f"+{len(diff.devices_added)} dev")
        if diff.devices_removed:
            parts.append(f"-{len(diff.devices_removed)} dev")
        if diff.devices_changed:
            parts.append(f"~{len(diff.devices_changed)} dev")
        if diff.routes_added:
            parts.append(f"+{len(diff.routes_added)} route")
        if diff.routes_removed:
            parts.append(f"-{len(diff.routes_removed)} route")
        self.toast("Deployed — " + ", ".join(parts))

    def _toggle_theme(self):
        self.state.theme = "light" if self.state.theme == "dark" else "dark"
        app = QApplication.instance()
        if app is not None:
            app.setStyleSheet(LIGHT_QSS if self.state.theme == "light" else DARK_QSS)
        icons.clear_cache()
        self.toolrail.set_theme(self.state.theme)
        self.modebar.set_theme(self.state.theme)
        pal = palette(self.state.theme)
        self.menu_btn.setIcon(icons.icon("menu", pal["muted"], active_color=pal["accent_bright"]))
        self.room_btn.setIcon(icons.icon("rooms", pal["muted"]))
        self.undo_btn.setIcon(icons.icon("undo", pal["muted"], active_color=pal["accent_bright"]))
        self.redo_btn.setIcon(icons.icon("redo", pal["muted"], active_color=pal["accent_bright"]))
        self.state.changed.emit()  # re-color theme-dependent item foregrounds

    def toast(self, msg: str):
        self.statusBar().showMessage(msg, 3000)


def _set_windows_app_id(app_id: str = "Aniston.RoomDesigner") -> None:
    """Tell Windows this is its own app so the taskbar shows our icon, not
    python.exe's. No-op off Windows or if the shell call is unavailable."""
    if sys.platform != "win32":
        return
    try:
        import ctypes

        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(app_id)
    except Exception:
        pass


def main():
    _set_windows_app_id()
    app = QApplication.instance() or QApplication(sys.argv)
    f = QFont("Segoe UI")
    f.setPointSize(9)
    app.setFont(f)
    app.setStyleSheet(DARK_QSS)
    QGuiApplication.setApplicationDisplayName("Aniston Room Designer")
    # Stable org/app names so QSettings (e.g. the LIVE first-run guide flag) persist
    # under a fixed key across runs rather than an empty/derived one.
    app.setOrganizationName("Aniston")
    app.setApplicationName("RoomDesigner")
    app.setWindowIcon(app_icon())
    win = MainWindow()
    win.show()
    win._offer_recovery()                 # crash recovery, after the window is up
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
