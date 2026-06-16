"""Versioned, round-trippable JSON persistence (TS-compatible schema)."""
from __future__ import annotations

import json
from typing import Any

from .model import CONFIG_VERSION, SystemConfig, config_from_dict, to_jsonable
from .profiles import default_profile_id


class DeserializeError(Exception):
    pass


def serialize(config: SystemConfig, pretty: bool = False) -> str:
    data = to_jsonable(config)
    if pretty:
        return json.dumps(data, indent=2)
    return json.dumps(data, separators=(",", ":"))


def deserialize(text: str) -> SystemConfig:
    try:
        parsed: Any = json.loads(text)
    except json.JSONDecodeError as err:
        raise DeserializeError(f"Invalid JSON: {err}") from err
    if not isinstance(parsed, dict):
        raise DeserializeError("Config must be a JSON object.")
    if not isinstance(parsed.get("version"), int):
        raise DeserializeError('Missing numeric "version".')
    if parsed["version"] not in (1, 2, 3, 4, CONFIG_VERSION):
        raise DeserializeError(f'Unsupported config version {parsed["version"]}; expected 1, 2, 3, 4 or {CONFIG_VERSION}.')
    for fld in ("devices", "routes", "matrix", "automixer", "muteLinks", "metadata"):
        if fld not in parsed:
            raise DeserializeError(f'Missing required field "{fld}".')
    if not isinstance(parsed["devices"], list) or not isinstance(parsed["routes"], list):
        raise DeserializeError('"devices" and "routes" must be arrays.')
    # Backward-compatible: talkers added after v1 shipped.
    if not isinstance(parsed.get("talkers"), list):
        parsed["talkers"] = []
    # Migration chain: each step is lossless and bumps one version.
    if parsed["version"] == 1:
        _migrate_v1_to_v2(parsed)
    if parsed["version"] == 2:
        _migrate_v2_to_v3(parsed)
    if parsed["version"] == 3:
        _migrate_v3_to_v4(parsed)
    if parsed["version"] == 4:
        _migrate_v4_to_v5(parsed)
    return config_from_dict(parsed)


def _migrate_v1_to_v2(obj: dict) -> None:
    """Fill default profile + empty DSP chain for each device, then bump version."""
    for d in obj["devices"]:
        if not isinstance(d.get("profileId"), str):
            d["profileId"] = default_profile_id(d["type"])
        if not isinstance(d.get("dspBlocks"), list):
            d["dspBlocks"] = []
    obj["version"] = 2


def _migrate_v2_to_v3(obj: dict) -> None:
    """v3 adds ``control.scenes`` (named recallable scenes) — purely additive:
    a v2 file gains an empty scene list (when it has a control section at all)
    and loses nothing."""
    control = obj.get("control")
    if isinstance(control, dict) and not isinstance(control.get("scenes"), list):
        control["scenes"] = []
    obj["version"] = 3


def _migrate_v3_to_v4(obj: dict) -> None:
    """v4 adds conferencing cameras, loudspeaker aim, and furniture geometry on
    ``room.objects`` — all optional, additive fields. A v3 file gains nothing it
    didn't have (every new field is omit-when-absent), so this is a pure version
    bump; existing devices/objects reconstruct identically."""
    obj["version"] = 4


def _migrate_v4_to_v5(obj: dict) -> None:
    """v5 adds ``bearingDeg`` (mounting bearing) to ``microphoneArray`` devices — a single
    optional, omit-when-absent field. A v4 file gains nothing it didn't have, so this is a
    pure version bump; existing arrays reconstruct byte-identically."""
    obj["version"] = CONFIG_VERSION
