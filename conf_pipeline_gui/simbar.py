"""Floating simulation bar in the canvas's top-right corner.

Sibling of the ViewBar (top-left): toggles the coverage-simulation overlays —
mic voice-pickup angles, camera field of view, loudspeaker dispersion, and
furniture line-of-sight occlusion — and shows a live coverage-quality readout.
A plain child widget of the canvas, so it paints above the QPainter scene.
"""
from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QToolButton

# (key, label, tooltip)
_TOGGLES = [
    ("pickup", "Pickup", "Microphone voice-pickup angles (P)"),
    ("fov", "FOV", "Camera field-of-view coverage"),
    ("dispersion", "Speaker", "Loudspeaker dispersion cones"),
    ("occlusion", "Occlusion", "Furniture blocking a camera's line of sight"),
]


class SimBar(QFrame):
    pickupToggled = Signal(bool)
    fovToggled = Signal(bool)
    dispersionToggled = Signal(bool)
    occlusionToggled = Signal(bool)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setProperty("simbar", "true")
        lay = QHBoxLayout(self)
        lay.setContentsMargins(4, 3, 4, 3)
        lay.setSpacing(2)

        self.buttons: dict[str, QToolButton] = {}
        signals = {
            "pickup": self.pickupToggled, "fov": self.fovToggled,
            "dispersion": self.dispersionToggled, "occlusion": self.occlusionToggled,
        }
        for key, label, tip in _TOGGLES:
            b = QToolButton()
            b.setText(label)
            b.setToolTip(tip)
            b.setCheckable(True)
            b.setCursor(Qt.PointingHandCursor)
            b.toggled.connect(signals[key].emit)
            self.buttons[key] = b
            lay.addWidget(b)

        sep = QFrame()
        sep.setFrameShape(QFrame.VLine)
        lay.addWidget(sep)

        self.summary = QLabel("")
        self.summary.setProperty("simSummary", "true")
        self.summary.setToolTip("Coverage quality for the current design")
        lay.addWidget(self.summary)

    # ---- shell API (block re-emit while syncing from state) ----
    def _set(self, key: str, on: bool) -> None:
        b = self.buttons[key]
        if b.isChecked() != bool(on):
            b.blockSignals(True)
            b.setChecked(bool(on))
            b.blockSignals(False)

    def set_pickup(self, on: bool) -> None:
        self._set("pickup", on)

    def set_fov(self, on: bool) -> None:
        self._set("fov", on)

    def set_dispersion(self, on: bool) -> None:
        self._set("dispersion", on)

    def set_occlusion(self, on: bool) -> None:
        self._set("occlusion", on)

    def set_summary(self, text: str) -> None:
        self.summary.setText(text)
