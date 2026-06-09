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
    if parsed["version"] not in (1, CONFIG_VERSION):
        raise DeserializeError(f'Unsupported config version {parsed["version"]}; expected 1 or {CONFIG_VERSION}.')
    for fld in ("devices", "routes", "matrix", "automixer", "muteLinks", "metadata"):
        if fld not in parsed:
            raise DeserializeError(f'Missing required field "{fld}".')
    if not isinstance(parsed["devices"], list) or not isinstance(parsed["routes"], list):
        raise DeserializeError('"devices" and "routes" must be arrays.')
    # Backward-compatible: talkers added after v1 shipped.
    if not isinstance(parsed.get("talkers"), list):
        parsed["talkers"] = []
    if parsed["version"] == 1:
        _migrate_v1_to_v2(parsed)
    return config_from_dict(parsed)


def _migrate_v1_to_v2(obj: dict) -> None:
    """Fill default profile + empty DSP chain for each device, then bump version."""
    for d in obj["devices"]:
        if not isinstance(d.get("profileId"), str):
            d["profileId"] = default_profile_id(d["type"])
        if not isinstance(d.get("dspBlocks"), list):
            d["dspBlocks"] = []
    obj["version"] = CONFIG_VERSION
