"""Floating view bar in the canvas's top-left corner: 2D|3D + overlays popover.

A plain child widget of the canvas — children always paint above the parent's
QPainter content, so no special compositing is needed. Overlay defaults are
set per mode by the shell; the popover lets the user override them.
"""
from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QAction
from PySide6.QtWidgets import QButtonGroup, QFrame, QHBoxLayout, QMenu, QToolButton


class ViewBar(QFrame):
    viewSelected = Signal(str)        # "2d" | "3d"
    coverageToggled = Signal(bool)
    heatmapToggled = Signal(bool)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setProperty("viewbar", "true")
        lay = QHBoxLayout(self)
        lay.setContentsMargins(4, 3, 4, 3)
        lay.setSpacing(2)
        self.group = QButtonGroup(self)
        self.group.setExclusive(True)
        self.view_buttons: dict[str, QToolButton] = {}
        for key, tip in (("2d", "Top-down 2D plan view (2)"), ("3d", "Orbit 3D view (3)")):
            b = QToolButton()
            b.setText(key.upper())
            b.setToolTip(tip)
            b.setCheckable(True)
            b.setCursor(Qt.PointingHandCursor)
            b.clicked.connect(lambda _c=False, k=key: self.viewSelected.emit(k))
            self.group.addButton(b)
            self.view_buttons[key] = b
            lay.addWidget(b)
        self.view_buttons["2d"].setChecked(True)

        sep = QFrame()
        sep.setFrameShape(QFrame.VLine)
        lay.addWidget(sep)

        self.overlays_btn = QToolButton()
        self.overlays_btn.setText("Overlays ▾")
        self.overlays_btn.setToolTip("Toggle canvas overlays (coverage circles, score heatmap)")
        self.overlays_btn.setCursor(Qt.PointingHandCursor)
        self.overlays_btn.setPopupMode(QToolButton.InstantPopup)
        menu = QMenu(self.overlays_btn)
        self.act_coverage = QAction("Coverage circles", menu, checkable=True)
        self.act_coverage.toggled.connect(self.coverageToggled.emit)
        menu.addAction(self.act_coverage)
        self.act_heatmap = QAction("Score heatmap", menu, checkable=True)
        self.act_heatmap.toggled.connect(self.heatmapToggled.emit)
        menu.addAction(self.act_heatmap)
        self.overlays_btn.setMenu(menu)
        lay.addWidget(self.overlays_btn)

    # ---- shell API (block re-emit while syncing from state) ----
    def set_view(self, view: str) -> None:
        b = self.view_buttons.get(view)
        if b is not None and not b.isChecked():
            b.setChecked(True)

    def set_coverage(self, on: bool) -> None:
        if self.act_coverage.isChecked() != on:
            self.act_coverage.blockSignals(True)
            self.act_coverage.setChecked(on)
            self.act_coverage.blockSignals(False)

    def set_heatmap(self, on: bool) -> None:
        if self.act_heatmap.isChecked() != on:
            self.act_heatmap.blockSignals(True)
            self.act_heatmap.setChecked(on)
            self.act_heatmap.blockSignals(False)
