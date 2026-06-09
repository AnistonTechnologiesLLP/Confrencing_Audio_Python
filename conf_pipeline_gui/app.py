"""Main window + application entry point."""
from __future__ import annotations

import sys

from PySide6.QtGui import QAction, QActionGroup, QGuiApplication, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QFileDialog,
    QLabel,
    QMainWindow,
    QMessageBox,
    QSplitter,
    QToolBar,
    QWidget,
)

import conf_pipeline as cp

from .canvas import Canvas
from .inspector import Inspector
from .scenarios import boardroom, huddle
from .state import AppState, now_iso

DARK_QSS = """
QMainWindow, QWidget { background: #0f1320; color: #e6e9f2; font-size: 13px; }
QToolBar { background: #141a2c; border-bottom: 1px solid #283250; spacing: 4px; padding: 4px; }
QToolButton { padding: 5px 9px; border-radius: 7px; }
QToolButton:hover { background: #222d4a; }
QToolButton:checked { background: #6d8bff; color: #08101f; }
QGroupBox { border: 1px solid #283250; border-radius: 8px; margin-top: 10px; padding-top: 8px; }
QGroupBox::title { subcontrol-origin: margin; left: 8px; padding: 0 4px; color: #9aa4c2; }
QPushButton { background: #232a42; border: 1px solid #283250; border-radius: 7px; padding: 5px 10px; }
QPushButton:hover { background: #2c3454; }
QListWidget, QPlainTextEdit, QComboBox, QLineEdit, QDoubleSpinBox { background: #161d31; border: 1px solid #283250; border-radius: 6px; padding: 3px; }
QTabBar::tab { background: transparent; padding: 8px 12px; color: #9aa4c2; }
QTabBar::tab:selected { color: #e6e9f2; border-bottom: 2px solid #6d8bff; }
QTabWidget::pane { border: 1px solid #283250; }
QLabel { color: #c8cee0; }
"""


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Conferencing Audio Pipeline — Configurator")
        self.resize(1280, 820)
        self.state = AppState()

        self.canvas = Canvas(self.state)
        self.inspector = Inspector(self.state)
        self.canvas.coord_cb = lambda s: self.coord_label.setText(s)

        split = QSplitter()
        split.addWidget(self.canvas)
        split.addWidget(self.inspector)
        split.setStretchFactor(0, 1)
        split.setStretchFactor(1, 0)
        split.setSizes([820, 420])
        self.setCentralWidget(split)

        self._build_toolbar()
        self._build_statusbar()
        self._shortcuts()
        self.state.changed.connect(self._sync_toolbar)
        self._sync_toolbar()

    # ----------------------------------------------------------------- toolbar
    def _build_toolbar(self):
        tb = QToolBar("Main")
        tb.setMovable(False)
        self.addToolBar(tb)

        self.tool_group = QActionGroup(self)
        self.tool_group.setExclusive(True)
        for key, label in [("select", "Select"), ("connect", "Connect"), ("room", "Room"), ("zone", "Zone"), ("talker", "Talker")]:
            act = QAction(label, self, checkable=True)
            act.setData(key)
            act.triggered.connect(lambda _checked, k=key: self._set_tool(k))
            self.tool_group.addAction(act)
            tb.addAction(act)
            if key == "select":
                act.setChecked(True)
        tb.addSeparator()

        self.view_group = QActionGroup(self)
        for key, label in [("2d", "2D"), ("3d", "3D")]:
            act = QAction(label, self, checkable=True)
            act.triggered.connect(lambda _c, k=key: self._set_view(k))
            self.view_group.addAction(act)
            tb.addAction(act)
            if key == "2d":
                act.setChecked(True)
        tb.addSeparator()

        self.act_undo = QAction("Undo", self)
        self.act_undo.triggered.connect(self.state.undo)
        self.act_redo = QAction("Redo", self)
        self.act_redo.triggered.connect(self.state.redo)
        tb.addAction(self.act_undo)
        tb.addAction(self.act_redo)
        tb.addSeparator()

        auto = QAction("Auto-configure", self)
        auto.triggered.connect(self._auto)
        tb.addAction(auto)
        rect = QAction("Rect room", self)
        rect.triggered.connect(self._rect_room)
        tb.addAction(rect)
        tb.addSeparator()

        self.scenario = QComboBox()
        self.scenario.addItem("Load sample…", "")
        self.scenario.addItem("Boardroom (reference AEC)", "boardroom")
        self.scenario.addItem("Huddle (auto-configured)", "huddle")
        self.scenario.addItem("Empty", "empty")
        self.scenario.currentIndexChanged.connect(self._load_scenario)
        tb.addWidget(self.scenario)
        tb.addSeparator()

        tb.addWidget(QLabel(" Room "))
        self.room_combo = QComboBox()
        self.room_combo.setMinimumWidth(120)
        self._updating_rooms = False
        self.room_combo.currentIndexChanged.connect(self._room_changed)
        tb.addWidget(self.room_combo)
        add_room = QAction("+ Room", self)
        add_room.triggered.connect(lambda: self.state.add_room())
        tb.addAction(add_room)
        rm_room = QAction("− Room", self)
        rm_room.triggered.connect(lambda: self.state.remove_room(self.state.active_room))
        tb.addAction(rm_room)
        name_act = QAction("Auto-name", self)
        name_act.triggered.connect(self._auto_name)
        tb.addAction(name_act)
        deploy_act = QAction("Deploy", self)
        deploy_act.triggered.connect(self._deploy)
        tb.addAction(deploy_act)
        tb.addSeparator()

        exp = QAction("Export JSON", self)
        exp.triggered.connect(self._export)
        imp = QAction("Import JSON", self)
        imp.triggered.connect(self._import)
        tb.addAction(exp)
        tb.addAction(imp)

    def _build_statusbar(self):
        self.coord_label = QLabel("x — , y —")
        self.val_label = QLabel("")
        self.statusBar().addPermanentWidget(self.coord_label)
        self.statusBar().addWidget(self.val_label)

    def _shortcuts(self):
        QShortcut(QKeySequence.Undo, self, self.state.undo)
        QShortcut(QKeySequence.Redo, self, self.state.redo)
        QShortcut(QKeySequence("Delete"), self, self._delete_selection)
        QShortcut(QKeySequence("Backspace"), self, self._delete_selection)
        for k, tool in [("V", "select"), ("C", "connect"), ("R", "room"), ("Z", "zone"), ("T", "talker")]:
            QShortcut(QKeySequence(k), self, lambda t=tool: self._set_tool(t, sync=True))
        QShortcut(QKeySequence("2"), self, lambda: self._set_view("2d", sync=True))
        QShortcut(QKeySequence("3"), self, lambda: self._set_view("3d", sync=True))

    # ------------------------------------------------------------------ actions
    def _set_tool(self, tool, sync=False):
        self.state.tool = tool
        self.canvas.connect_from = None
        self.canvas.draw_pts = []
        if sync:
            for a in self.tool_group.actions():
                a.setChecked(a.data() == tool)
        self.canvas.update()

    def _set_view(self, view, sync=False):
        self.state.view = view
        if view == "3d":
            b = self.canvas.bounds()
            span = max(b[2] - b[0], b[3] - b[1], self.canvas._room_h() * 1.4)
            self.state.cam["dist"] = max(7.0, span * 1.6)
        if sync:
            for a in self.view_group.actions():
                a.setChecked(a.text().lower() == view)
        self.canvas.update()

    def _auto(self):
        self.state.set_config(cp.auto_configure(self.state.config))
        self.toast("Auto-configured")

    def _rect_room(self):
        self.state.set_config(cp.set_room(self.state.config, cp.rectangular_room(9, 7, 3)))

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
            self.state.selection = None
        except Exception as exc:
            self.toast(str(exc))

    def _load_scenario(self, idx):
        which = self.scenario.itemData(idx)
        if not which:
            return
        self.scenario.setCurrentIndex(0)
        if which == "empty":
            self.state.selection = None
            self.state.set_config(cp.create_config("Untitled", now_iso()))
        elif which == "boardroom":
            self.state.set_config(boardroom())
            self.toast("Loaded Boardroom — select the Presenter to see angles")
        elif which == "huddle":
            self.state.set_config(huddle())

    def _export(self):
        path, _ = QFileDialog.getSaveFileName(self, "Export config", "config.json", "JSON (*.json)")
        if path:
            with open(path, "w", encoding="utf-8") as f:
                f.write(cp.serialize(self.state.config, pretty=True))
            self.toast("Exported")

    def _import(self):
        path, _ = QFileDialog.getOpenFileName(self, "Import config", "", "JSON (*.json)")
        if not path:
            return
        try:
            with open(path, "r", encoding="utf-8") as f:
                self.state.set_config(cp.deserialize(f.read()))
            self.toast("Imported")
        except Exception as exc:
            QMessageBox.warning(self, "Import failed", str(exc))

    def _room_changed(self, i):
        if not self._updating_rooms and i >= 0:
            self.state.switch_room(i)

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

    def _refresh_rooms(self):
        self._updating_rooms = True
        self.room_combo.clear()
        for r in self.state.rooms:
            self.room_combo.addItem(r["config"].metadata.get("name", r["id"]))
        self.room_combo.setCurrentIndex(self.state.active_room)
        self._updating_rooms = False

    def _sync_toolbar(self):
        self.act_undo.setEnabled(self.state.can_undo())
        self.act_redo.setEnabled(self.state.can_redo())
        self._refresh_rooms()
        res = cp.validate(self.state.config)
        self.val_label.setText(("✓ Valid" if res.ok else f"✗ {len(res.errors)} error(s)") + f" · {len(res.warnings)} warn")

    def toast(self, msg: str):
        self.statusBar().showMessage(msg, 3000)


def main():
    app = QApplication.instance() or QApplication(sys.argv)
    app.setStyleSheet(DARK_QSS)
    QGuiApplication.setApplicationDisplayName("Conferencing Audio Pipeline")
    win = MainWindow()
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
