"""Global Issues drawer: validation errors/warnings, reachable from every mode.

Slides in over the right panel when the top-bar validation pill is clicked.
Clicking an issue selects the offending device on the canvas (and the Design
panel's selection card follows).
"""
from __future__ import annotations

from PySide6.QtCore import QEasingCurve, QPropertyAnimation, QRect, Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QVBoxLayout,
)

import conf_pipeline as cp

from .panels.common import ISSUE_COLORS

WIDTH = 420


class IssuesDrawer(QFrame):
    def __init__(self, state, parent=None):
        super().__init__(parent)
        self.state = state
        self.setProperty("drawer", "true")
        self.setVisible(False)
        self._anim = None

        lay = QVBoxLayout(self)
        lay.setContentsMargins(12, 10, 12, 10)
        head = QHBoxLayout()
        title = QLabel("Issues")
        title.setProperty("panelTitle", "true")
        head.addWidget(title)
        head.addStretch(1)
        close = QPushButton("✕")
        close.setMaximumWidth(30)
        close.setToolTip("Close (Esc)")
        close.clicked.connect(self.close_drawer)
        head.addWidget(close)
        lay.addLayout(head)

        self.issue_badge = QLabel()
        lay.addWidget(self.issue_badge)
        self.issue_list = QListWidget()
        self.issue_list.itemClicked.connect(self._on_issue_click)
        lay.addWidget(self.issue_list, 1)
        lay.addWidget(QLabel("Coverage (array pickup circles)"))
        self.coverage_lbl = QLabel()
        self.coverage_lbl.setWordWrap(True)
        lay.addWidget(self.coverage_lbl)

        state.changed.connect(self._on_changed)

    # ---- open / close ----
    def open_drawer(self, host_rect: QRect):
        """Slide in over the right edge of ``host_rect`` (central-widget coords)."""
        self.refresh()
        end = QRect(host_rect.right() - WIDTH, host_rect.top(), WIDTH, host_rect.height())
        start = QRect(host_rect.right(), host_rect.top(), WIDTH, host_rect.height())
        self.setGeometry(start)
        self.setVisible(True)
        self.raise_()
        self._anim = QPropertyAnimation(self, b"geometry", self)
        self._anim.setDuration(160)
        self._anim.setEasingCurve(QEasingCurve.OutCubic)
        self._anim.setStartValue(start)
        self._anim.setEndValue(end)
        self._anim.start()
        self.setFocus(Qt.PopupFocusReason)

    def close_drawer(self):
        self.setVisible(False)

    def reposition(self, host_rect: QRect):
        if self.isVisible():
            self.setGeometry(QRect(host_rect.right() - WIDTH, host_rect.top(), WIDTH, host_rect.height()))

    def keyPressEvent(self, e):  # noqa: N802 (Qt override)
        if e.key() == Qt.Key_Escape:
            self.close_drawer()
            return
        super().keyPressEvent(e)

    # ---- content ----
    def _on_changed(self):
        if self.isVisible():
            self.refresh()

    def _on_issue_click(self, item):
        refs = item.data(Qt.UserRole) or []
        dev = next((r for r in refs if any(d.id == r for d in self.state.config.devices)), None)
        if dev:
            self.state.select({"kind": "device", "id": dev})

    def refresh(self):
        cfg = self.state.config
        res = cp.validate(cfg)
        self.issue_badge.setText(
            ("✓ Valid" if res.ok else f"✗ {len(res.errors)} error(s)") + f" · {len(res.warnings)} warning(s)"
        )
        self.issue_list.clear()
        ic = ISSUE_COLORS[getattr(self.state, "theme", "dark")]
        for i in [*res.errors, *res.warnings]:
            it = QListWidgetItem(f"[{i.code}] {i.message}")
            it.setForeground(QColor(ic["error"] if i.severity == "error" else ic["warning"]))
            it.setData(Qt.UserRole, i.refs)
            self.issue_list.addItem(it)
        rep = cp.coverage_report(cfg)
        if not cfg.talkers:
            self.coverage_lbl.setText("No talkers placed.")
        else:
            label_of = {t.id: t.label for t in cfg.talkers}
            parts = [f"{len(rep.covered)}/{len(cfg.talkers)} talkers covered"]
            if rep.uncovered:
                parts.append("uncovered: " + ", ".join(label_of.get(t, t) for t in rep.uncovered))
            if rep.overlaps:
                parts.append(f"{len(rep.overlaps)} array overlap(s)")
            zrep = cp.zone_coverage_report(cfg)
            if zrep.zones:
                covered_areas = sum(1 for z in zrep.zones if z.centroid_covered)
                parts.append(f"{covered_areas}/{len(zrep.zones)} coverage area(s) in pickup")
                if zrep.contended:
                    parts.append(f"{len(zrep.contended)} area(s) contended")
            self.coverage_lbl.setText("  ·  ".join(parts))
