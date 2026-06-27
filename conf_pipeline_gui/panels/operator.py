"""Operator status panel — a read-only render of the Phase-6 ``OperatorStatus`` model.

A single ``QWidget`` (NOT ``MainWindow``), so it can be constructed + probed headless/offscreen exactly
like ``StageStrip`` (the full app runs in CI; MainWindow hangs headless on this box per CLAUDE.md). It
only DISPLAYS the operator status — no controls, no DSP, no auto-apply. The status itself is built by
``conf_pipeline_control.operator.OperatorStatus`` from the running engine + the Phase 1–5 objects.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from PySide6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget


class OperatorStatusPanel(QWidget):
    """Render an ``OperatorStatus.to_dict()`` snapshot as a compact, honest read-out (Device,
    Calibration, Placement, Pipeline, Egress, Transcription + warnings). Fed via :meth:`set_status`;
    :meth:`section` / :meth:`summary` / :meth:`warnings` expose the data for tests + introspection."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._status: Dict[str, Any] = {}
        self._label = QLabel("No status.")
        self._label.setWordWrap(True)
        self._label.setToolTip(
            "Read-only operator status. OFF stages show OFF; a failed calibration and a BAD placement are "
            "shown as warnings (never hidden); placement suggestions are not auto-applied.")
        lay = QVBoxLayout(self)
        lay.addWidget(self._label)
        self.setMinimumWidth(300)

    def set_status(self, status: Dict[str, Any]) -> None:
        self._status = dict(status or {})
        self._label.setText(self.summary())

    def section(self, name: str) -> Optional[Dict[str, Any]]:
        return self._status.get(name)

    def warnings(self) -> List[str]:
        return list(self._status.get("warnings", []))

    def summary(self) -> str:
        s = self._status
        if not s:
            return "No status."
        dev = s.get("device", {})
        cal = s.get("calibration", {})
        pl = s.get("placement", {})
        pp = s.get("pipeline", {})
        eg = s.get("egress", {})
        tr = s.get("transcription", {})
        lines: List[str] = [
            f"Device: {dev.get('name', '?')} · {dev.get('sampleRate')} Hz · "
            f"{dev.get('channels')} ch · latency {dev.get('latencyMs')} ms",
            f"Calibration: {'ON' if cal.get('enabled') else 'OFF'} — {cal.get('status', '')}",
            (f"Placement: {pl.get('status')} ({pl.get('score')}/100)"
             if pl.get("available") else "Placement: not run"),
            f"Pipeline cleaning: {pp.get('activeCleaningStages', '')}",
            (f"Egress: {', '.join(eg.get('routes', []))}" if eg.get("available") else "Egress: none"),
            (f"Transcription: {tr.get('status')} (mock={tr.get('isMock')})"
             if tr.get("available") else "Transcription: none"),
        ]
        lines += [f"⚠ {w}" for w in s.get("warnings", [])]
        return "\n".join(lines)


class OperatorDiagnosticsWindow(QWidget):
    """A separate, read-only diagnostics window: the :class:`OperatorStatusPanel` plus a **Refresh**
    button and an **Export** button. Opened from the app menu ("Audio operator diagnostics…"); on
    refresh it rebuilds an ``OperatorStatus`` from ``status_provider`` (which reads the running engine)
    and renders it. Read-only — it has **no DSP controls and applies nothing**; export reuses
    ``OperatorStatus.save`` to write ``operator_diagnostics_<stamp>.{json,md}``.

    ``status_provider`` is a zero-arg callable returning an ``OperatorStatus`` (or ``None`` when nothing
    is running). Kept Qt-only here; the status model lives in ``conf_pipeline_control.operator``."""

    def __init__(self, parent=None, *, status_provider=None, export_dir: str = "reports/audio") -> None:
        super().__init__(parent)
        self.setWindowTitle("Audio Operator Diagnostics")
        self._provider = status_provider
        self._export_dir = export_dir
        self._status = None                      # the latest OperatorStatus object (for export)
        self.panel = OperatorStatusPanel()
        self._refresh_btn = QPushButton("Refresh")
        self._export_btn = QPushButton("Export JSON + Markdown")
        self._line = QLabel("")
        self._line.setWordWrap(True)
        self._refresh_btn.clicked.connect(self.refresh)
        self._export_btn.clicked.connect(self._do_export)
        row = QHBoxLayout()
        row.addWidget(self._refresh_btn)
        row.addWidget(self._export_btn)
        row.addStretch(1)
        lay = QVBoxLayout(self)
        lay.addWidget(QLabel("Read-only diagnostics — no live controls; suggestions are not auto-applied."))
        lay.addWidget(self.panel)
        lay.addLayout(row)
        lay.addWidget(self._line)
        self.resize(500, 380)

    def refresh(self) -> None:
        """Rebuild the status from the provider and render it. Safe when nothing is running."""
        status = None
        if self._provider is not None:
            try:
                status = self._provider()
            except Exception as exc:                 # a diagnostics read must never crash the app
                self._line.setText(f"Could not read status: {exc}")
        self._status = status
        self.panel.set_status(status.to_dict() if status is not None else {})
        self._export_btn.setEnabled(status is not None)
        if status is None:
            self._line.setText("No running engine — connect a beam to see live status.")
        else:
            self._line.setText("")

    def _do_export(self) -> None:
        if self._status is None:
            self._line.setText("Nothing to export — refresh while a beam is running.")
            return
        from datetime import datetime
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        try:
            paths = self._status.save(self._export_dir, stamp=stamp)
            self._line.setText(f"Saved → {paths[0]}  +  {paths[1]}")
        except Exception as exc:
            self._line.setText(f"Export failed: {exc}")

    # ---- test / introspection passthrough ----
    def section(self, name: str):
        return self.panel.section(name)

    def summary(self) -> str:
        return self.panel.summary()

    def warnings(self):
        return self.panel.warnings()
