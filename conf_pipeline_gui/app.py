"""Main window + application entry point."""
from __future__ import annotations

import sys

from PySide6.QtGui import QAction, QActionGroup, QFont, QGuiApplication, QKeySequence, QShortcut
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
from .scenarios import SCENARIOS
from .state import AppState, now_iso

# "Conduit" design system, translated to QSS (a constrained CSS subset: no
# variables/box-shadow/transitions). One template -> dark + light builds.
_PALETTES = {
    "dark": dict(
        bg="#0a0a0e", surface="#0e0e13", surface2="#13131a", surface3="#181820", hover="#1f1f2a", elev="#1a1a23",
        border="#262633", border_soft="#1c1c26", border_strong="#33333f",
        text="#edeef4", text_dim="#c4c5d2", muted="#9197ab", faint="#646a82",
        accent="#6d8bff", accent_bright="#85a0ff", accent_press="#5a78f0", on_accent="#070a16",
        sel="#1b2138", ok="#3ddc97", warn="#f7c948", err="#ff6b81",
    ),
    "light": dict(
        bg="#f5f6f9", surface="#ffffff", surface2="#f7f8fb", surface3="#eef0f5", hover="#e7eaf1", elev="#ffffff",
        border="#e1e3eb", border_soft="#eaecf1", border_strong="#cfd2dd",
        text="#14151c", text_dim="#33353f", muted="#5b6075", faint="#777c90",
        accent="#5871f2", accent_bright="#4a63ec", accent_press="#4a63ec", on_accent="#ffffff",
        sel="#e6ebfd", ok="#0fae72", warn="#b8860b", err="#e23b59",
    ),
}

_QSS_TEMPLATE = """
QMainWindow, QWidget {{ background: {bg}; color: {text}; }}
QToolTip {{ background: {elev}; color: {text}; border: 1px solid {border_strong}; padding: 4px 7px; border-radius: 6px; }}

QToolBar {{ background: {surface}; border: 0; border-bottom: 1px solid {border}; spacing: 4px; padding: 6px 9px; }}
QToolBar::separator {{ background: {border}; width: 1px; margin: 4px 3px; }}
QToolButton {{ background: transparent; color: {text_dim}; border: 1px solid transparent; border-radius: 8px; padding: 6px 11px; font-weight: 500; }}
QToolButton:hover {{ background: {hover}; color: {text}; }}
QToolButton:pressed {{ background: {surface3}; }}
QToolButton:checked {{ background: {surface3}; color: {accent_bright}; border: 1px solid {border_strong}; }}
QToolButton:disabled {{ color: {faint}; }}

QPushButton {{ background: {surface2}; color: {text_dim}; border: 1px solid {border}; border-radius: 8px; padding: 6px 12px; font-weight: 500; }}
QPushButton:hover {{ background: {hover}; border-color: {border_strong}; color: {text}; }}
QPushButton:pressed {{ background: {surface3}; }}
QPushButton:disabled {{ color: {faint}; }}
QPushButton[accent="true"] {{ background: qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 {accent_bright}, stop:1 {accent}); color: {on_accent}; border: 1px solid {accent}; font-weight: 700; }}
QPushButton[accent="true"]:hover {{ background: {accent_bright}; }}
QPushButton[accent="true"]:pressed {{ background: {accent_press}; }}

QGroupBox {{ background: {surface2}; border: 1px solid {border}; border-radius: 12px; margin-top: 15px; padding: 11px 12px 12px; }}
QGroupBox::title {{ subcontrol-origin: margin; subcontrol-position: top left; left: 12px; top: 1px; padding: 1px 6px; color: {muted}; font-weight: 600; background: {surface3}; border: 1px solid {border}; border-radius: 5px; }}

QLineEdit, QPlainTextEdit, QComboBox, QAbstractSpinBox {{ background: {surface3}; color: {text}; border: 1px solid {border}; border-radius: 8px; padding: 6px 8px; selection-background-color: {accent}; selection-color: {on_accent}; }}
QLineEdit:hover, QComboBox:hover, QAbstractSpinBox:hover {{ border-color: {border_strong}; }}
QLineEdit:focus, QPlainTextEdit:focus, QComboBox:focus, QAbstractSpinBox:focus {{ border-color: {accent}; }}
QComboBox::drop-down {{ subcontrol-origin: padding; subcontrol-position: top right; width: 18px; border: 0; }}
QAbstractSpinBox::up-button, QAbstractSpinBox::down-button {{ width: 0; height: 0; border: 0; }}
QComboBox QAbstractItemView {{ background: {elev}; color: {text}; border: 1px solid {border_strong}; border-radius: 8px; padding: 3px; selection-background-color: {accent}; selection-color: {on_accent}; outline: 0; }}

QListWidget {{ background: {surface3}; border: 1px solid {border}; border-radius: 9px; padding: 4px; outline: 0; }}
QListWidget::item {{ background: transparent; color: {text}; border: 1px solid transparent; border-radius: 7px; padding: 7px 9px; margin: 2px 1px; }}
QListWidget::item:hover {{ background: {hover}; }}
QListWidget::item:selected {{ background: {sel}; color: {text}; border: 1px solid {accent}; }}

QTabWidget::pane {{ border: 1px solid {border}; border-radius: 0; top: -1px; }}
QTabBar {{ background: transparent; }}
QTabBar::tab {{ background: transparent; color: {muted}; padding: 9px 13px; border: 0; margin-right: 2px; font-weight: 600; }}
QTabBar::tab:hover {{ color: {text}; }}
QTabBar::tab:selected {{ color: {text}; border-bottom: 2px solid {accent}; }}

QCheckBox {{ color: {text}; spacing: 7px; }}
QCheckBox::indicator {{ width: 16px; height: 16px; border-radius: 5px; border: 1px solid {border_strong}; background: {surface}; }}
QCheckBox::indicator:hover {{ border-color: {accent}; }}
QCheckBox::indicator:checked {{ background: {accent}; border-color: {accent}; }}
QRadioButton {{ color: {text}; spacing: 7px; }}
QRadioButton::indicator {{ width: 15px; height: 15px; border-radius: 8px; border: 1px solid {border_strong}; background: {surface}; }}
QRadioButton::indicator:checked {{ background: {accent}; border-color: {accent}; }}

QSlider::groove:horizontal {{ height: 4px; background: {surface}; border: 1px solid {border}; border-radius: 2px; }}
QSlider::sub-page:horizontal {{ background: {accent}; border-radius: 2px; }}
QSlider::handle:horizontal {{ background: {text_dim}; width: 14px; height: 14px; margin: -6px 0; border-radius: 7px; border: 1px solid {border_strong}; }}
QSlider::handle:horizontal:hover {{ background: {accent_bright}; }}

QScrollBar:vertical {{ background: transparent; width: 11px; margin: 0; }}
QScrollBar::handle:vertical {{ background: {border_strong}; min-height: 28px; border-radius: 5px; margin: 2px; }}
QScrollBar::handle:vertical:hover {{ background: {muted}; }}
QScrollBar:horizontal {{ background: transparent; height: 11px; margin: 0; }}
QScrollBar::handle:horizontal {{ background: {border_strong}; min-width: 28px; border-radius: 5px; margin: 2px; }}
QScrollBar::handle:horizontal:hover {{ background: {muted}; }}
QScrollBar::add-line, QScrollBar::sub-line {{ width: 0; height: 0; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}

QLabel {{ color: {text_dim}; }}
QFrame[card="true"] {{ border: 1px solid {border}; border-radius: 7px; }}
QFrame[frameShape="4"], QFrame[frameShape="5"] {{ color: {border}; background: {border}; max-height: 1px; }}
QStatusBar {{ background: {surface}; color: {muted}; border-top: 1px solid {border}; }}
QStatusBar::item {{ border: 0; }}
"""


def build_qss(theme: str) -> str:
    return _QSS_TEMPLATE.format(**_PALETTES[theme])


DARK_QSS = build_qss("dark")
LIGHT_QSS = build_qss("light")


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Conferencing Audio Pipeline — Configurator")
        self.resize(1280, 820)
        self._light = False
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
        for key, label, _fn in SCENARIOS:
            self.scenario.addItem(label, key)
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
        tb.addSeparator()
        theme = QAction("◐ Theme", self)
        theme.triggered.connect(self._toggle_theme)
        tb.addAction(theme)

    def _build_statusbar(self):
        self.coord_label = QLabel("x — , y —")
        self.val_label = QLabel("")
        self.room_label = QLabel("")
        self.statusBar().addPermanentWidget(self.room_label)
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
        self.state.selection = None
        if which == "empty":
            self.state.set_config(cp.create_config("Untitled", now_iso()))
            return
        entry = next((e for e in SCENARIOS if e[0] == which), None)
        if entry is not None:
            _key, label, builder = entry
            self.state.set_config(builder())
            self.toast(f"Loaded {label} — open the Simulate tab to optimise placement")

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
        room = self.state.config.room
        if room and room.vertices:
            xs = [v.x for v in room.vertices]
            ys = [v.y for v in room.vertices]
            self.room_label.setText(
                f"Room {max(xs) - min(xs):.1f} × {max(ys) - min(ys):.1f} × {room.height:.1f} m"
            )
        else:
            self.room_label.setText("No room")

    def _toggle_theme(self):
        self._light = not self._light
        self.state.theme = "light" if self._light else "dark"
        app = QApplication.instance()
        if app is not None:
            app.setStyleSheet(LIGHT_QSS if self._light else DARK_QSS)
        self.state.changed.emit()  # re-color theme-dependent item foregrounds

    def toast(self, msg: str):
        self.statusBar().showMessage(msg, 3000)


def main():
    app = QApplication.instance() or QApplication(sys.argv)
    f = QFont("Segoe UI")
    f.setPointSize(9)
    app.setFont(f)
    app.setStyleSheet(DARK_QSS)
    QGuiApplication.setApplicationDisplayName("Conferencing Audio Pipeline")
    win = MainWindow()
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
