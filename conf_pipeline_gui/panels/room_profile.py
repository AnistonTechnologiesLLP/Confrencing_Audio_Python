"""Audio Room Profiles window — a profile-management surface (Phase 9).

A single ``QWidget`` (NOT MainWindow, which hangs headless on Windows per CLAUDE.md), opened from the
app menu. It creates / loads / saves / imports / exports / validates an
:class:`~conf_pipeline_control.room_profile.AudioRoomProfile` and shows a summary + warnings + a
prominent safety note. **It is profile-management only: it never touches the running audio engine, never
enables a feature, and never auto-applies placement suggestions.**

The path-taking methods (``load_path`` / ``save_path`` / ``import_path`` / ``export_path`` /
``copy_placement_path``) hold the logic and are offscreen-testable; the buttons are thin ``QFileDialog``
wrappers around them.
"""
from __future__ import annotations

from typing import List, Optional

from PySide6.QtWidgets import (
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from conf_pipeline_control.room_profile import AudioRoomProfile, RoomProfileError

SAFETY_NOTE = (
    "Profiles are room-specific.\n"
    "Loading a profile does not apply it to the running audio engine.\n"
    "Placement suggestions are not auto-applied.\n"
    "Measured notch frequencies must be re-measured per room."
)


class AudioRoomProfilesWindow(QWidget):
    """Manage a room-specific audio profile. Read/preview/validate/export only — no engine apply."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Audio Room Profiles")
        self._profile = AudioRoomProfile()
        self._path: Optional[str] = None
        self._last_warnings: List[str] = []

        self._name_edit = QLineEdit()
        self._name_edit.setPlaceholderText("Profile name (e.g. Conference Room A - AC On)")
        self._summary = QPlainTextEdit()
        self._summary.setReadOnly(True)
        self._warn = QPlainTextEdit()
        self._warn.setReadOnly(True)
        self._warn.setPlaceholderText("Validation warnings appear here.")
        self._line = QLabel("")
        self._line.setWordWrap(True)

        note = QLabel(SAFETY_NOTE)
        note.setWordWrap(True)

        def _btn(text, slot):
            b = QPushButton(text)
            b.clicked.connect(slot)
            return b

        row1 = QHBoxLayout()
        for b in (_btn("New", self._on_new), _btn("Load…", self._on_load), _btn("Save…", self._on_save)):
            row1.addWidget(b)
        row1.addStretch(1)
        row2 = QHBoxLayout()
        for b in (_btn("Import…", self._on_import), _btn("Export…", self._on_export),
                  _btn("Validate", self.validate),
                  _btn("Copy placement suggestions…", self._on_copy_placement)):
            row2.addWidget(b)
        row2.addStretch(1)

        lay = QVBoxLayout(self)
        lay.addWidget(note)
        lay.addWidget(QLabel("Profile name:"))
        lay.addWidget(self._name_edit)
        lay.addLayout(row1)
        lay.addLayout(row2)
        lay.addWidget(QLabel("Summary (not applied to the engine):"))
        lay.addWidget(self._summary)
        lay.addWidget(QLabel("Warnings:"))
        lay.addWidget(self._warn)
        lay.addWidget(self._line)
        self.resize(560, 560)
        self._render()

    # ---- core logic (testable, no dialogs) ----
    def profile(self) -> AudioRoomProfile:
        return self._profile

    def set_name(self, name: str) -> None:
        self._profile.name = str(name)
        self._name_edit.setText(self._profile.name)

    def new_profile(self) -> None:
        self._profile = AudioRoomProfile()
        self._path = None
        self._last_warnings = []
        self._warn.setPlainText("")
        self._line.setText("New profile.")
        self._render()

    def load_path(self, path: str) -> None:
        try:
            self._profile = AudioRoomProfile.load(path)
            self._path = path
            self._line.setText(f"Loaded {path} (not applied to the engine).")
        except RoomProfileError as exc:
            self._line.setText(f"Load failed: {exc}")
            return
        self._render()
        self.validate()

    def save_path(self, path: str) -> None:
        self._sync_name()
        try:
            self._profile.save(path)
            self._path = path
            self._line.setText(f"Saved → {path}")
        except OSError as exc:
            self._line.setText(f"Save failed: {exc}")

    def import_path(self, path: str) -> None:
        """Import = load + preview from any location (the operator can then Save into their profile store)."""
        self.load_path(path)
        if self._path == path:
            self._line.setText(f"Imported (preview) {path} — Save to keep it. Not applied to the engine.")

    def export_path(self, path: str) -> None:
        """Export = write the current profile JSON to a chosen location."""
        self.save_path(path)

    def copy_placement_path(self, path: str) -> None:
        """Attach a saved placement result and copy its suggestions into the DRAFT (not the engine)."""
        from conf_pipeline_control.placement import PlacementError, PlacementResult
        try:
            result = PlacementResult.load(path)
        except PlacementError as exc:
            self._line.setText(f"Could not read placement result: {exc}")
            return
        self._profile.copy_placement_suggestions(result, result_path=path)
        self._line.setText("Copied placement suggestions into the draft — NOT applied, pre-NR stays off.")
        self._render()

    def validate(self) -> List[str]:
        self._last_warnings = self._profile.validate()
        self._warn.setPlainText("\n".join(f"• {w}" for w in self._last_warnings)
                                if self._last_warnings else "No warnings — profile looks consistent.")
        return self._last_warnings

    # ---- accessors for tests / introspection ----
    def summary_text(self) -> str:
        return self._summary.toPlainText()

    def warnings_text(self) -> str:
        return self._warn.toPlainText()

    def safety_note_text(self) -> str:
        return SAFETY_NOTE

    # ---- internals ----
    def _sync_name(self) -> None:
        self._profile.name = self._name_edit.text()

    def _render(self) -> None:
        self._name_edit.setText(self._profile.name)
        self._summary.setPlainText(self._build_summary())

    def _build_summary(self) -> str:
        p = self._profile
        tones = ", ".join(f"{t:.0f}" for t in p.placement.detected_tones_hz) or "—"
        notches = ", ".join(f"{t:.0f}" for t in p.pre_nr_cleanup.notches_hz) or "—"
        return "\n".join([
            f"Name: {p.name or '(unnamed)'}",
            f"Device: {p.device} · {p.sample_rate:.0f} Hz · {p.channels} ch",
            f"Calibration: {'attached' if p.calibration.profile_path else 'none'}"
            f" (enabled in profile: {p.calibration.enabled})"
            + (f"\n  → {p.calibration.profile_path}" if p.calibration.profile_path else ""),
            f"Placement: {p.placement.last_status or 'none'}"
            + (f" ({p.placement.last_score}/100)" if p.placement.last_score is not None else "")
            + f"\n  detected tones (Hz): {tones}",
            f"Pre-NR cleanup (draft): enabled={p.pre_nr_cleanup.enabled} · "
            f"HPF={p.pre_nr_cleanup.hpf_hz} · notches={notches}",
            f"Egress: 48k={p.egress.clean_mono_48k} · 16k ASR={p.egress.asr_16k} · "
            f"wav={p.egress.wav_recording} · external sink={p.egress.external_sink}",
            f"Transcription: enabled={p.transcription.enabled} · provider={p.transcription.provider} "
            f"· {p.transcription.sample_rate} Hz",
            f"Notes: {p.notes or '—'}",
            "",
            "(This is a saved document — nothing here is applied to the running engine.)",
        ])

    # ---- button slots (thin QFileDialog wrappers) ----
    def _on_new(self) -> None:
        self.new_profile()

    def _on_load(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Load room profile", "", "JSON (*.json)")
        if path:
            self.load_path(path)

    def _on_save(self) -> None:
        path, _ = QFileDialog.getSaveFileName(self, "Save room profile", "room-profile.json", "JSON (*.json)")
        if path:
            self.save_path(path)

    def _on_import(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Import room profile", "", "JSON (*.json)")
        if path:
            self.import_path(path)

    def _on_export(self) -> None:
        path, _ = QFileDialog.getSaveFileName(self, "Export room profile", "room-profile.json", "JSON (*.json)")
        if path:
            self.export_path(path)

    def _on_copy_placement(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Attach placement result", "", "JSON (*.json)")
        if path:
            self.copy_placement_path(path)
