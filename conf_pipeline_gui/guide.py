"""Guided getting-started panel.

A dismissible horizontal checklist that sits under the toolbar and tracks the
user's progress through the natural design flow — room → array → coverage zone →
talker → optimize. Each step shows a ✓ once satisfied and a one-click action
button so a new user is never stuck on a blank canvas. It reads the live config
on every ``refresh()`` (driven by ``AppState.changed``), so ticks update as the
design is built by any means (toolbar, canvas, inspector, or a loaded sample).
"""
from __future__ import annotations

from typing import Callable, Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

import conf_pipeline as cp

from .state import AppState


class _Step(QWidget):
    """One checklist step: a status dot, a title, and an optional action button."""

    def __init__(self, title: str, action_label: Optional[str], action: Optional[Callable]):
        super().__init__()
        self.setProperty("guideStep", "true")
        row = QHBoxLayout(self)
        row.setContentsMargins(11, 7, 11, 7)
        row.setSpacing(8)
        self.dot = QLabel("○")
        self.dot.setProperty("guideDot", "true")
        self.title = QLabel(title)
        self.title.setProperty("guideTitle", "true")
        row.addWidget(self.dot)
        row.addWidget(self.title)
        self.btn: Optional[QPushButton] = None
        if action_label and action is not None:
            self.btn = QPushButton(action_label)
            self.btn.setCursor(Qt.PointingHandCursor)
            self.btn.clicked.connect(action)
            row.addWidget(self.btn)

    def set_done(self, done: bool):
        self.dot.setText("✓" if done else "○")
        self.setProperty("done", "true" if done else "false")
        self.title.setProperty("done", "true" if done else "false")
        # nudge Qt to re-evaluate the dynamic-property stylesheet
        for w in (self, self.title, self.dot):
            w.style().unpolish(w)
            w.style().polish(w)
        if self.btn is not None:
            self.btn.setVisible(not done)


class GuidePanel(QFrame):
    """The strip of steps. ``on_action`` is a dict of callables the host wires up."""

    def __init__(self, state: AppState, actions: dict[str, Callable]):
        super().__init__()
        self.state = state
        self.setProperty("guidePanel", "true")
        self.setFrameShape(QFrame.NoFrame)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(10, 8, 10, 8)
        outer.setSpacing(6)

        head = QHBoxLayout()
        title = QLabel("Getting started")
        title.setProperty("guideHeader", "true")
        self.subtitle = QLabel("")
        self.subtitle.setProperty("guideSub", "true")
        head.addWidget(title)
        head.addWidget(self.subtitle, 1)
        dismiss = QPushButton("Dismiss ✕")
        dismiss.setCursor(Qt.PointingHandCursor)
        dismiss.setToolTip("Hide this guide (reopen it from the ？ Guide button)")
        dismiss.clicked.connect(self.hide)
        head.addWidget(dismiss)
        outer.addLayout(head)

        strip = QHBoxLayout()
        strip.setSpacing(8)
        # title, predicate(config) -> bool, action-button label, action key
        self._defs = [
            ("1. Add a room", self._has_room, "Rect room", "rect_room"),
            ("2. Add a mic array", self._has_array, "Add array", "add_array"),
            ("3. Draw a coverage zone", self._has_zone, "Zone tool", "zone_tool"),
            ("4. Place a talker", self._has_talker, "Talker tool", "talker_tool"),
            ("5. Optimize", self._is_optimized, "Optimize room", "optimize"),
        ]
        self.steps: list[_Step] = []
        for i, (title_txt, _pred, lbl, key) in enumerate(self._defs):
            step = _Step(title_txt, lbl, actions.get(key))
            self.steps.append(step)
            strip.addWidget(step)
            if i < len(self._defs) - 1:
                arrow = QLabel("→")
                arrow.setProperty("guideArrow", "true")
                strip.addWidget(arrow)
        strip.addStretch(1)
        outer.addLayout(strip)

        state.changed.connect(self.refresh)
        self.refresh()

    # ---- predicates over the live config ----
    def _has_room(self, c) -> bool:
        return c.room is not None and len(c.room.vertices) >= 3

    def _has_array(self, c) -> bool:
        return any(d.type == "microphoneArray" for d in c.devices)

    def _has_zone(self, c) -> bool:
        return any(d.type == "microphoneArray" and d.zones for d in c.devices)

    def _has_talker(self, c) -> bool:
        return len(c.talkers) > 0

    def _is_optimized(self, c) -> bool:
        # "optimized" ≈ arrays placed AND at least one route exists (auto-route ran)
        arrays = [d for d in c.devices if d.type == "microphoneArray"]
        placed = bool(arrays) and all(d.position is not None for d in arrays)
        return placed and len(c.routes) > 0

    def refresh(self):
        c = self.state.config
        done_count = 0
        first_todo = None
        for step, (title_txt, pred, _lbl, _key) in zip(self.steps, self._defs):
            ok = pred(c)
            step.set_done(ok)
            if ok:
                done_count += 1
            elif first_todo is None:
                first_todo = title_txt
        n = len(self._defs)
        if done_count >= n:
            self.subtitle.setText("All set — your design is placed and routed. 🎉")
        elif first_todo is not None:
            self.subtitle.setText(f"{done_count}/{n} done · next: {first_todo.split('. ', 1)[-1]}")
        else:
            self.subtitle.setText(f"{done_count}/{n} done")
