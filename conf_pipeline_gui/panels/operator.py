"""Operator status panel — a read-only render of the Phase-6 ``OperatorStatus`` model.

A single ``QWidget`` (NOT ``MainWindow``), so it can be constructed + probed headless/offscreen exactly
like ``StageStrip`` (the full app runs in CI; MainWindow hangs headless on this box per CLAUDE.md). It
only DISPLAYS the operator status — no controls, no DSP, no auto-apply. The status itself is built by
``conf_pipeline_control.operator.OperatorStatus`` from the running engine + the Phase 1–5 objects.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from PySide6.QtWidgets import QLabel, QVBoxLayout, QWidget


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
