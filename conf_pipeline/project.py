"""Projects (Designer multi-room 'design files')."""
from __future__ import annotations

import copy
import json
from dataclasses import dataclass, field
from typing import Optional

from .api import create_config
from .model import SystemConfig
from .persistence import DeserializeError, deserialize, serialize

PROJECT_VERSION = 1


@dataclass
class ProjectRoom:
    id: str
    config: SystemConfig


@dataclass
class Project:
    version: int
    metadata: dict
    rooms: list[ProjectRoom] = field(default_factory=list)
    active_room_id: Optional[str] = None


def create_project(name: str, created_at: str) -> Project:
    room = ProjectRoom(id="room-1", config=create_config("Room 1", created_at))
    return Project(version=PROJECT_VERSION, metadata={"name": name, "createdAt": created_at}, rooms=[room], active_room_id="room-1")


def _next_room_id(project: Project) -> str:
    n = len(project.rooms) + 1
    while any(r.id == f"room-{n}" for r in project.rooms):
        n += 1
    return f"room-{n}"


def add_room(project: Project, name: Optional[str] = None) -> Project:
    rid = _next_room_id(project)
    room_name = name or f"Room {len(project.rooms) + 1}"
    room = ProjectRoom(id=rid, config=create_config(room_name, project.metadata["createdAt"]))
    new = copy.copy(project)
    new.rooms = [*project.rooms, room]
    new.active_room_id = rid
    return new


def remove_room(project: Project, room_id: str) -> Project:
    rooms = [r for r in project.rooms if r.id != room_id]
    active = (rooms[0].id if rooms else None) if project.active_room_id == room_id else project.active_room_id
    new = copy.copy(project)
    new.rooms = rooms
    new.active_room_id = active
    return new


def rename_room(project: Project, room_id: str, name: str) -> Project:
    def upd(r: ProjectRoom) -> ProjectRoom:
        if r.id != room_id:
            return r
        cfg = copy.copy(r.config)
        cfg.metadata = {**r.config.metadata, "name": name}
        return ProjectRoom(id=r.id, config=cfg)
    new = copy.copy(project)
    new.rooms = [upd(r) for r in project.rooms]
    return new


def set_active_room(project: Project, room_id: str) -> Project:
    if not any(r.id == room_id for r in project.rooms):
        raise ValueError(f"Unknown room: {room_id}")
    new = copy.copy(project)
    new.active_room_id = room_id
    return new


def update_room(project: Project, room_id: str, config: SystemConfig) -> Project:
    new = copy.copy(project)
    new.rooms = [ProjectRoom(id=r.id, config=config) if r.id == room_id else r for r in project.rooms]
    return new


def get_room(project: Project, room_id: str) -> Optional[ProjectRoom]:
    return next((r for r in project.rooms if r.id == room_id), None)


def get_active_room(project: Project) -> Optional[ProjectRoom]:
    return get_room(project, project.active_room_id) if project.active_room_id else None


def serialize_project(project: Project, pretty: bool = False) -> str:
    doc = {
        "version": project.version,
        "metadata": project.metadata,
        "activeRoomId": project.active_room_id,
        "rooms": [{"id": r.id, "config": json.loads(serialize(r.config))} for r in project.rooms],
    }
    return json.dumps(doc, indent=2 if pretty else None, separators=None if pretty else (",", ":"))


def deserialize_project(text: str) -> Project:
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as err:
        raise DeserializeError(f"Invalid JSON: {err}") from err
    if not isinstance(parsed, dict):
        raise DeserializeError("Project must be a JSON object.")
    if parsed.get("version") != PROJECT_VERSION:
        raise DeserializeError(f'Unsupported project version {parsed.get("version")}; expected {PROJECT_VERSION}.')
    if not isinstance(parsed.get("rooms"), list):
        raise DeserializeError('Project "rooms" must be an array.')
    rooms = [ProjectRoom(id=str(r["id"]), config=deserialize(json.dumps(r["config"]))) for r in parsed["rooms"]]
    active = parsed.get("activeRoomId") or (rooms[0].id if rooms else None)
    return Project(version=PROJECT_VERSION, metadata=parsed["metadata"], rooms=rooms, active_room_id=active)
