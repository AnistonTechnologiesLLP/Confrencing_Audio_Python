"""Project file manager: open/save with migration notice, recent files,
autosave + crash recovery (pure stdlib; per-user state dir pointed at tmp)."""
import json
from pathlib import Path

import pytest

import conf_pipeline as cp


def _mgr(tmp_path):
    return cp.ProjectFileManager(state_dir=tmp_path / "state")


def _config(name="Room"):
    c = cp.create_config(name, "2026-06-12T00:00:00Z")
    c = cp.add_device(c, cp.create_processor("P1", "DSP"))
    return c


def _v1_file(tmp_path) -> Path:
    """A schema-v1 config file (built the way the migration tests do)."""
    doc = json.loads(cp.serialize(_config("Legacy")))
    doc["version"] = 1
    for d in doc["devices"]:
        d.pop("profileId", None)
        d.pop("dspBlocks", None)
    p = tmp_path / "legacy.json"
    p.write_text(json.dumps(doc), encoding="utf-8")
    return p


# --- open / save ---
def test_save_open_round_trip_updates_recent(tmp_path):
    m = _mgr(tmp_path)
    path = tmp_path / "room.json"
    m.save_config(_config("Boardroom"), path)
    res = m.open_config(path)
    assert res.config.metadata["name"] == "Boardroom"
    assert res.migrated is False and res.migration_notice() == ""
    assert m.recent_files() == [str(path.resolve())]


def test_open_v1_file_reports_upgrade(tmp_path):
    m = _mgr(tmp_path)
    res = m.open_config(_v1_file(tmp_path))
    assert res.config.version == cp.CONFIG_VERSION       # migrated on open
    assert res.migrated_from == 1 and res.migrated
    assert "version 1" in res.migration_notice()
    assert f"version {cp.CONFIG_VERSION}" in res.migration_notice()


def test_open_invalid_json_raises_without_touching_recent(tmp_path):
    m = _mgr(tmp_path)
    bad = tmp_path / "bad.json"
    bad.write_text("not json", encoding="utf-8")
    with pytest.raises(Exception):
        m.open_config(bad)
    assert m.recent_files() == []


# --- recent files ---
def test_recent_is_mru_deduped_capped_and_persistent(tmp_path):
    m = _mgr(tmp_path)
    paths = []
    for i in range(cp.RECENT_MAX + 3):
        p = tmp_path / f"f{i}.json"
        m.save_config(_config(), p)
        paths.append(str(p.resolve()))
    m.add_recent(paths[0])                                # re-open the oldest
    recent = m.recent_files()
    assert recent[0] == paths[0]                          # bumped to the front
    assert len(recent) == cp.RECENT_MAX                   # capped
    assert len(set(recent)) == len(recent)                # deduped
    # a fresh manager over the same state dir sees the same list
    assert cp.ProjectFileManager(state_dir=tmp_path / "state").recent_files() == recent


def test_recent_prunes_deleted_files(tmp_path):
    m = _mgr(tmp_path)
    keep, gone = tmp_path / "keep.json", tmp_path / "gone.json"
    m.save_config(_config(), keep)
    m.save_config(_config(), gone)
    gone.unlink()
    assert m.recent_files() == [str(keep.resolve())]
    m.clear_recent()
    assert m.recent_files() == []


# --- autosave / crash recovery ---
def test_autosave_recovery_lifecycle(tmp_path):
    m = _mgr(tmp_path)
    assert m.pending_recovery() is None                   # healthy start
    m.autosave('{"hello": 1}', origin="Boardroom")
    info = m.pending_recovery()
    assert info is not None and info.origin == "Boardroom"
    assert info.saved_at.endswith("Z")                    # ISO-8601 UTC stamp
    assert m.read_recovery() == '{"hello": 1}'
    m.clear_autosave()                                    # clean exit
    assert m.pending_recovery() is None
    with pytest.raises(FileNotFoundError):
        m.read_recovery()


def test_autosave_overwrites_previous_snapshot(tmp_path):
    m = _mgr(tmp_path)
    m.autosave("one", origin="a")
    m.autosave("two", origin="b")
    assert m.read_recovery() == "two"
    info = m.pending_recovery()
    assert info is not None
    assert info.origin == "b"


def test_recovery_survives_missing_meta(tmp_path):
    m = _mgr(tmp_path)
    m.autosave("payload", origin="x")
    (m.state_dir / "autosave.meta.json").unlink()         # half a crash, even
    info = m.pending_recovery()
    assert info is not None and info.origin == "" and m.read_recovery() == "payload"


def test_autosave_payload_round_trips_a_project(tmp_path):
    m = _mgr(tmp_path)
    project = cp.create_project("Recover me", "2026-06-12T00:00:00Z")
    project = cp.add_room(project, "Second room")
    m.autosave(cp.serialize_project(project), origin="Recover me")
    restored = cp.deserialize_project(m.read_recovery())
    assert [r.id for r in restored.rooms] == [r.id for r in project.rooms]
    assert restored.active_room_id == project.active_room_id


# --- state dir ---
def test_env_var_overrides_default_state_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("CONF_PIPELINE_STATE_DIR", str(tmp_path / "custom"))
    assert cp.default_state_dir() == tmp_path / "custom"
    m = cp.ProjectFileManager()                           # picks up the env default
    assert m.state_dir == tmp_path / "custom"
    assert m.state_dir.is_dir()                           # created on first use
