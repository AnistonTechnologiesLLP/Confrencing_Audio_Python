"""Project file management — recent files, autosave, crash recovery, and
open-with-migration-notice (pure stdlib).

String-level (de)serialization lives in :mod:`persistence`; this module owns
the *file-level* concerns the desktop app needs. It is deliberately the only
engine module that touches the filesystem, and all of its state lives in one
per-user directory (override with the ``CONF_PIPELINE_STATE_DIR`` environment
variable or the ``state_dir`` constructor argument — tests point it at a temp
directory):

- ``recent.json``        — the most-recently-used file list
- ``autosave.json``      — the last autosaved workspace payload
- ``autosave.meta.json`` — when it was written and what it contains

Crash-recovery contract: the autosave file doubles as the crash marker. The
app autosaves periodically while work is dirty and **clears the autosave on
clean exit**, so a leftover autosave at startup means the last session died —
:meth:`ProjectFileManager.pending_recovery` reports it and the app offers the
payload back to the user.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Union

from .model import CONFIG_VERSION, SystemConfig
from .persistence import deserialize, serialize

RECENT_MAX = 10

_PathLike = Union[str, Path]


def default_state_dir() -> Path:
    """Per-user state directory (not created until a manager uses it)."""
    env = os.environ.get("CONF_PIPELINE_STATE_DIR")
    if env:
        return Path(env)
    if os.name == "nt":
        base = Path(os.environ.get("APPDATA", str(Path.home() / "AppData" / "Roaming")))
    else:
        base = Path(os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local" / "state")))
    return base / "conf-pipeline"


def _atomic_write(path: Path, text: str) -> None:
    """Write via a sibling temp file + replace, so readers never see a torn file."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


@dataclass(frozen=True)
class OpenResult:
    """An opened config plus what (if anything) had to be upgraded."""

    config: SystemConfig
    path: str
    migrated_from: Optional[int] = None   # schema version found in the file, when older

    @property
    def migrated(self) -> bool:
        return self.migrated_from is not None

    def migration_notice(self) -> str:
        """The user-visible "this file was upgraded" text ('' when no upgrade)."""
        if self.migrated_from is None:
            return ""
        return (
            f"This file used config schema version {self.migrated_from} and was "
            f"upgraded to version {CONFIG_VERSION} on open. Saving will write the "
            f"new format."
        )


@dataclass(frozen=True)
class RecoveryInfo:
    """A leftover autosave from a session that did not exit cleanly."""

    payload_path: str
    saved_at: str       # ISO-8601 UTC timestamp of the autosave
    origin: str = ""    # what the payload is (e.g. the project name)


class ProjectFileManager:
    """Recent files + autosave/recovery + open/save for the desktop app.

    Pure stdlib and Qt-free: the GUI wires it to menus, a timer, and dialogs.
    The autosave payload is an opaque string (the app passes a serialized
    multi-room project), so the manager never constrains what a "workspace" is.
    """

    def __init__(self, state_dir: Optional[_PathLike] = None) -> None:
        self.state_dir = Path(state_dir) if state_dir is not None else default_state_dir()
        self.state_dir.mkdir(parents=True, exist_ok=True)

    # ---- paths ----
    @property
    def _recent_path(self) -> Path:
        return self.state_dir / "recent.json"

    @property
    def _autosave_path(self) -> Path:
        return self.state_dir / "autosave.json"

    @property
    def _autosave_meta_path(self) -> Path:
        return self.state_dir / "autosave.meta.json"

    # ---- open / save (single-config JSON, TS-interoperable) ----
    def open_config(self, path: _PathLike) -> OpenResult:
        """Open a config file; report the old schema version if it was upgraded."""
        p = Path(path)
        text = p.read_text(encoding="utf-8")
        migrated_from = self._peek_old_version(text)
        config = deserialize(text)        # migrates internally (e.g. v1 → v2)
        self.add_recent(p)
        return OpenResult(config=config, path=str(p), migrated_from=migrated_from)

    def save_config(self, config: SystemConfig, path: _PathLike) -> None:
        p = Path(path)
        _atomic_write(p, serialize(config, pretty=True))
        self.add_recent(p)

    @staticmethod
    def _peek_old_version(text: str) -> Optional[int]:
        try:
            v = json.loads(text).get("version")
        except (json.JSONDecodeError, AttributeError):
            return None                   # not a dict / not JSON — deserialize() will explain
        if isinstance(v, int) and v < CONFIG_VERSION:
            return v
        return None

    # ---- recent files ----
    def recent_files(self) -> list[str]:
        """Most-recent-first; entries whose files vanished are pruned."""
        try:
            raw = json.loads(self._recent_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []
        if not isinstance(raw, list):
            return []
        return [str(p) for p in raw if isinstance(p, str) and Path(p).exists()]

    def add_recent(self, path: _PathLike) -> None:
        entry = str(Path(path).resolve())
        items = [p for p in self.recent_files() if Path(p) != Path(entry)]
        items.insert(0, entry)
        _atomic_write(self._recent_path, json.dumps(items[:RECENT_MAX], indent=2))

    def clear_recent(self) -> None:
        try:
            self._recent_path.unlink()
        except FileNotFoundError:
            pass

    # ---- autosave / crash recovery ----
    def autosave(self, payload: str, *, origin: str = "") -> None:
        """Snapshot the workspace. Overwrites the previous autosave."""
        _atomic_write(self._autosave_path, payload)
        meta = {
            "savedAt": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "origin": origin,
        }
        _atomic_write(self._autosave_meta_path, json.dumps(meta, indent=2))

    def pending_recovery(self) -> Optional[RecoveryInfo]:
        """A leftover autosave (= the last session crashed), or None."""
        if not self._autosave_path.exists():
            return None
        saved_at, origin = "", ""
        try:
            meta = json.loads(self._autosave_meta_path.read_text(encoding="utf-8"))
            saved_at = str(meta.get("savedAt", ""))
            origin = str(meta.get("origin", ""))
        except (OSError, json.JSONDecodeError):
            pass                          # payload without meta is still recoverable
        return RecoveryInfo(payload_path=str(self._autosave_path), saved_at=saved_at, origin=origin)

    def read_recovery(self) -> str:
        """The autosaved payload. Raises ``FileNotFoundError`` if there is none."""
        return self._autosave_path.read_text(encoding="utf-8")

    def clear_autosave(self) -> None:
        """Clean-exit marker: removing the autosave declares the session healthy."""
        for p in (self._autosave_path, self._autosave_meta_path):
            try:
                p.unlink()
            except FileNotFoundError:
                pass
