"""Offscreen probe for the Phase-9 Audio Room Profiles window.

Constructs `AudioRoomProfilesWindow` directly (a single QWidget) — NOT MainWindow, which hangs headless
on Windows per CLAUDE.md. The window is profile-management only: New / Load / Save / Import / Export /
Validate + a "not applied automatically" safety note. It never touches the DSP engine. Skipped without
PySide6.
"""
import json
import os

import pytest

pytest.importorskip("PySide6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from conf_pipeline_control.room_profile import AudioRoomProfile  # noqa: E402
from conf_pipeline_gui.panels.room_profile import AudioRoomProfilesWindow  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    yield app


def test_window_renders_default_profile(qapp):
    w = AudioRoomProfilesWindow()
    assert isinstance(w.profile(), AudioRoomProfile)
    assert "POLARIS" in w.summary_text()


def test_window_shows_not_applied_safety_note(qapp):
    note = AudioRoomProfilesWindow().safety_note_text().lower()
    assert "not apply" in note and "room-specific" in note and "re-measure" in note


def test_window_new_profile_resets(qapp):
    w = AudioRoomProfilesWindow()
    w.set_name("Temp")
    assert w.profile().name == "Temp"
    w.new_profile()
    assert w.profile().name == ""


def test_window_save_load_roundtrip(qapp, tmp_path):
    w = AudioRoomProfilesWindow()
    w.set_name("Room A")
    w.profile().pre_nr_cleanup.hpf_hz = 120.0
    p = str(tmp_path / "roomA.json")
    w.save_path(p)
    assert os.path.exists(p)
    w2 = AudioRoomProfilesWindow()
    w2.load_path(p)
    assert w2.profile().name == "Room A" and w2.profile().pre_nr_cleanup.hpf_hz == 120.0
    assert "Room A" in w2.summary_text()


def test_window_validate_renders_warnings(qapp):
    w = AudioRoomProfilesWindow()
    w.profile().safety.dfn3_forced_on = True
    warns = w.validate()
    assert any("dfn3" in x.lower() for x in warns)
    assert "dfn3" in w.warnings_text().lower()


def test_window_import_export(qapp, tmp_path):
    w = AudioRoomProfilesWindow()
    w.set_name("Exported Room")
    out = str(tmp_path / "exp.json")
    w.export_path(out)
    assert os.path.exists(out)
    with open(out, encoding="utf-8") as f:
        d = json.load(f)
    assert d["name"] == "Exported Room" and "preNrCleanup" in d and "safety" in d
    w2 = AudioRoomProfilesWindow()
    w2.import_path(out)
    assert w2.profile().name == "Exported Room"


def test_window_copy_placement_suggestions_does_not_enable(qapp, tmp_path):
    from conf_pipeline_control.placement import STATUS_BAD, PlacementResult
    r = PlacementResult(status=STATUS_BAD, score=42, notch_suggestions_hz=(140.0,), hpf_suggestion_hz=120.0)
    pp = str(tmp_path / "placement.json")
    r.save(pp)
    w = AudioRoomProfilesWindow()
    w.copy_placement_path(pp)
    assert w.profile().pre_nr_cleanup.notches_hz == [140.0]
    assert w.profile().pre_nr_cleanup.hpf_hz == 120.0
    assert w.profile().pre_nr_cleanup.enabled is False           # NOT forced on
    assert w.profile().placement.auto_apply_suggestions is False  # NOT auto-applied
