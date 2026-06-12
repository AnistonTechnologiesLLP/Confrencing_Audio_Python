"""Left tool rail: per-mode canvas tools, icon + label, Fusion-style.

Only the tools that exist in the current mode are shown — DESIGN gets the
geometry tools, ROUTE gets Connect, everything else is select-only. The Zone
button carries a flyout for the zone kind (records / always-on / no-pickup),
replacing the old Build-tab combo.
"""
from __future__ import annotations

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtGui import QAction, QActionGroup
from PySide6.QtWidgets import QButtonGroup, QFrame, QMenu, QToolButton, QVBoxLayout

from . import icons
from .theme import palette

MODE_TOOLS = {
    "design": ["select", "room", "zone", "talker"],
    "simulate": ["select"],
    "route": ["select", "connect"],
    "deploy": ["select"],
    "live": ["select"],
}
TOOL_META = {
    "select": ("Select", "V", "Move and select devices, zones, talkers, room corners"),
    "connect": ("Connect", "C", "Click device → device to wire a route"),
    "room": ("Room", "R", "Click to draw the room outline; double-click to close"),
    "zone": ("Zone", "Z", "Drag a coverage/exclusion area on the floor"),
    "talker": ("Talker", "T", "Click to drop a person (talker)"),
}
ZONE_KINDS = [("dynamic", "Records (dynamic)"), ("dedicated", "Always-on (dedicated)"),
              ("exclusion", "No-pickup (exclusion)")]


class ToolRail(QFrame):
    toolSelected = Signal(str)
    zoneKindSelected = Signal(str)

    def __init__(self, theme: str = "dark"):
        super().__init__()
        self.setProperty("toolrail", "true")
        self.setFixedWidth(58)
        self._theme = theme
        lay = QVBoxLayout(self)
        lay.setContentsMargins(5, 8, 5, 8)
        lay.setSpacing(4)
        self.group = QButtonGroup(self)
        self.group.setExclusive(True)
        self.buttons: dict[str, QToolButton] = {}
        for tool, (label, key, tip) in TOOL_META.items():
            b = QToolButton()
            b.setText(label)
            b.setToolTip(f"{label} ({key}) — {tip}")
            b.setCheckable(True)
            b.setProperty("railButton", "true")
            b.setToolButtonStyle(Qt.ToolButtonTextUnderIcon)
            b.setIconSize(QSize(20, 20))
            b.setFixedSize(48, 46)
            b.setCursor(Qt.PointingHandCursor)
            b.clicked.connect(lambda _c=False, t=tool: self.toolSelected.emit(t))
            self.group.addButton(b)
            self.buttons[tool] = b
            lay.addWidget(b)
        lay.addStretch(1)
        self.buttons["select"].setChecked(True)

        # zone-kind flyout on the Zone button
        zb = self.buttons["zone"]
        zb.setPopupMode(QToolButton.DelayedPopup)
        menu = QMenu(zb)
        grp = QActionGroup(menu)
        grp.setExclusive(True)
        self.zone_actions: dict[str, QAction] = {}
        for kind, label in ZONE_KINDS:
            a = QAction(label, menu, checkable=True)
            a.triggered.connect(lambda _c=False, k=kind: self.zoneKindSelected.emit(k))
            grp.addAction(a)
            menu.addAction(a)
            self.zone_actions[kind] = a
        self.zone_actions["dynamic"].setChecked(True)
        zb.setMenu(menu)
        self._tint_icons()

    def _tint_icons(self):
        pal = palette(self._theme)
        for tool, b in self.buttons.items():
            b.setIcon(icons.icon(tool, pal["muted"], active_color=pal["accent_bright"]))

    # ---- shell API ----
    def set_theme(self, theme: str) -> None:
        self._theme = theme
        self._tint_icons()

    def set_mode(self, mode: str) -> None:
        tools = MODE_TOOLS.get(mode, ["select"])
        for tool, b in self.buttons.items():
            b.setVisible(tool in tools)

    def set_tool(self, tool: str) -> None:
        b = self.buttons.get(tool)
        if b is not None:
            b.setChecked(True)

    def set_zone_kind(self, kind: str) -> None:
        a = self.zone_actions.get(kind)
        if a is not None:
            a.setChecked(True)
