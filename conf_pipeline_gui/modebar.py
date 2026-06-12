"""The ModeBar: five segmented mode buttons with workflow status dots.

The dot in front of each label is the old guide checklist dissolved into the
navigation itself: ● done · ◔ in progress · ○ untouched. The LIVE dot pulses
red while an audio session is connected — visible from any mode.
"""
from __future__ import annotations

from PySide6.QtCore import QPointF, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QIcon, QPainter, QPen, QPixmap
from PySide6.QtWidgets import QButtonGroup, QFrame, QHBoxLayout, QToolButton

from . import workflow
from .theme import palette

_MODE_TIPS = {
    "design": "Room, floor plan, devices, coverage zones, talkers (Ctrl+1)",
    "simulate": "Placement heatmap, recommendation, physics validation (Ctrl+2)",
    "route": "Routes, AEC references, automixer, DSP chains, mute groups (Ctrl+3)",
    "deploy": "Validate, name, diff, export — ship the design (Ctrl+4)",
    "live": "Drive the real array: beamformer, auto-steer, OCTOVOX (Ctrl+5)",
}


def _dot(status: str, theme: str, live_on: bool = False, dim: bool = False) -> QIcon:
    pal = palette(theme)
    pm = QPixmap(24, 24)
    pm.setDevicePixelRatio(2.0)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing, True)
    r = QRectF(2.5, 2.5, 7, 7)
    if live_on:
        c = QColor(pal["err"])
        c.setAlpha(110 if dim else 255)
        p.setBrush(c)
        p.setPen(Qt.NoPen)
        p.drawEllipse(r)
    elif status == workflow.DONE:
        p.setBrush(QColor(pal["ok"]))
        p.setPen(Qt.NoPen)
        p.drawEllipse(r)
    elif status == workflow.PARTIAL:
        p.setPen(QPen(QColor(pal["accent"]), 1.4))
        p.setBrush(Qt.NoBrush)
        p.drawEllipse(r)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(pal["accent"]))
        p.drawPie(r, 90 * 16, -180 * 16)
    else:
        p.setPen(QPen(QColor(pal["faint"]), 1.3))
        p.setBrush(Qt.NoBrush)
        p.drawEllipse(r)
    p.end()
    return QIcon(pm)


class ModeBar(QFrame):
    modeSelected = Signal(str)

    def __init__(self, theme: str = "dark"):
        super().__init__()
        self._theme = theme
        self._status = {m: workflow.TODO for m in workflow.MODES}
        self._live_connected = False
        self._pulse_dim = False
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(2)
        self.group = QButtonGroup(self)
        self.group.setExclusive(True)
        self.buttons: dict[str, QToolButton] = {}
        for mode in workflow.MODES:
            b = QToolButton()
            b.setText(workflow.MODE_LABELS[mode].upper())
            b.setToolTip(_MODE_TIPS[mode])
            b.setCheckable(True)
            b.setProperty("modeButton", "true")
            b.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
            b.setCursor(Qt.PointingHandCursor)
            b.clicked.connect(lambda _c=False, m=mode: self.modeSelected.emit(m))
            self.group.addButton(b)
            self.buttons[mode] = b
            lay.addWidget(b)
        self.buttons["design"].setChecked(True)
        self._pulse = QTimer(self)
        self._pulse.setInterval(600)
        self._pulse.timeout.connect(self._tick_pulse)
        self._render_dots()

    # ---- shell API ----
    def set_active(self, mode: str) -> None:
        b = self.buttons.get(mode)
        if b is not None and not b.isChecked():
            b.setChecked(True)

    def set_status(self, status: dict) -> None:
        if status != self._status:
            self._status = dict(status)
            self._render_dots()

    def set_live_connected(self, on: bool) -> None:
        if on == self._live_connected:
            return
        self._live_connected = on
        self._pulse_dim = False
        if on:
            self._pulse.start()
        else:
            self._pulse.stop()
        self._render_dots()

    def set_theme(self, theme: str) -> None:
        self._theme = theme
        self._render_dots()

    # ---- internals ----
    def _tick_pulse(self):
        self._pulse_dim = not self._pulse_dim
        live = self.buttons["live"]
        live.setIcon(_dot(self._status["live"], self._theme, live_on=True, dim=self._pulse_dim))

    def _render_dots(self):
        for mode, b in self.buttons.items():
            live_on = self._live_connected and mode == "live"
            b.setIcon(_dot(self._status.get(mode, workflow.TODO), self._theme, live_on=live_on, dim=self._pulse_dim))
