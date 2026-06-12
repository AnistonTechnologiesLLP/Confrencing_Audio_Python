"""Shared application state with undo/redo and a change signal."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from PySide6.QtCore import QObject, Signal

import conf_pipeline as cp


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class AppState(QObject):
    """Holds the current SystemConfig plus editor state. Emits ``changed`` on edits."""

    changed = Signal()
    modeChanged = Signal(str)
    liveOverlayChanged = Signal()

    def __init__(self) -> None:
        super().__init__()
        self.config = cp.create_config("Untitled", now_iso())
        self._history = [self.config]
        self._idx = 0
        self.rooms = [{"id": "room-1", "config": self.config, "history": [self.config], "idx": 0, "last_deployed": None}]
        self.active_room = 0
        self.theme = "dark"
        self.mode = "design"          # design | simulate | route | deploy | live
        self.tool = "select"          # select | connect | room | zone | talker
        self.view = "2d"              # 2d | 3d
        self.snap = 0.25
        self.zone_kind = "dynamic"    # dynamic | dedicated | exclusion
        self.selection: Optional[dict] = None  # {"kind": ..., "id"/"array_id"/"zone_id": ...}
        self.cam = {"yaw": -0.7, "pitch": 0.62, "dist": 16.0}
        self._zone_seq = 1
        self._talker_seq = 1
        # ---- placement simulation (transient view state; never in history) ----
        self.sim_params = cp.SimParams()
        self.sim_target_id: Optional[str] = None      # talker id, or None for array-only
        self.sim_recommendation = None                # cp.Recommendation | None
        self.sim_heatmap = None                       # cp.Heatmap | None
        self.sim_show_heatmap = False
        self.show_coverage = False                    # draw array coverage circles on the canvas
        self.calibrating = False                      # floor-plan scale calibration drag in progress
        # ---- live-session overlay (transient view state; never in history) ----
        # dict published by the Live panel while a session runs:
        # {"array_id", "sector": (center, half_width, front_offset) | None,
        #  "detections": [(azimuth_deg, salience_db, in_sector)], "level": 0..1,
        #  "connected": bool}  — or None when idle.
        self.live_overlay = None

    # ---- mode ----
    def set_mode(self, mode: str) -> None:
        if mode == self.mode:
            return
        self.mode = mode
        self.modeChanged.emit(mode)
        self.changed.emit()

    def set_live_overlay(self, data) -> None:
        # dedup: the Live panel republishes every 60 ms; identical payloads
        # (idle/muted session) must not drive full-canvas repaints
        if data == self.live_overlay:
            return
        self.live_overlay = data
        self.liveOverlayChanged.emit()

    # ---- history ----
    def set_config(self, new, record: bool = True) -> None:
        self.config = new
        if record:
            self._history = self._history[: self._idx + 1]
            self._history.append(new)
            if len(self._history) > 120:
                self._history.pop(0)
            self._idx = len(self._history) - 1
        self._prune_selection()
        self.changed.emit()

    def undo(self) -> None:
        if self._idx > 0:
            self._idx -= 1
            self.config = self._history[self._idx]
            self._prune_selection()
            self.changed.emit()

    def redo(self) -> None:
        if self._idx < len(self._history) - 1:
            self._idx += 1
            self.config = self._history[self._idx]
            self._prune_selection()
            self.changed.emit()

    def can_undo(self) -> bool:
        return self._idx > 0

    def can_redo(self) -> bool:
        return self._idx < len(self._history) - 1

    # ---- rooms (multi-room project) ----
    def _snapshot(self) -> None:
        r = self.rooms[self.active_room]
        r["config"], r["history"], r["idx"] = self.config, self._history, self._idx

    def _load(self, i: int) -> None:
        self.active_room = i
        r = self.rooms[i]
        self.config, self._history, self._idx = r["config"], r["history"], r["idx"]
        self.selection = None
        self.changed.emit()

    def add_room(self) -> None:
        self._snapshot()
        n = len(self.rooms) + 1
        ids = {r["id"] for r in self.rooms}
        k = n
        while f"room-{k}" in ids:
            k += 1
        cfg = cp.create_config(f"Room {n}", now_iso())
        self.rooms.append({"id": f"room-{k}", "config": cfg, "history": [cfg], "idx": 0, "last_deployed": None})
        self._load(len(self.rooms) - 1)

    def switch_room(self, i: int) -> None:
        if i == self.active_room or i < 0 or i >= len(self.rooms):
            return
        self._snapshot()
        self._load(i)

    def remove_room(self, i: int) -> None:
        if len(self.rooms) <= 1:
            return
        self.rooms.pop(i)
        new_active = self.active_room - 1 if i < self.active_room else self.active_room
        self._load(min(max(new_active, 0), len(self.rooms) - 1))

    def load_rooms(self, rooms: list[dict], active: int = 0) -> None:
        """Replace the whole workspace (e.g. crash recovery). Each room dict needs
        ``id`` / ``config`` / ``history`` / ``idx`` / ``last_deployed`` keys."""
        if not rooms:
            return
        self.rooms = rooms
        self._load(min(max(active, 0), len(rooms) - 1))

    def rename_room(self, i: int, name: str) -> None:
        import copy
        r = self.rooms[i]
        cfg = copy.copy(r["config"])
        cfg.metadata = {**r["config"].metadata, "name": name}
        r["config"] = cfg
        if i == self.active_room:
            self.config = cfg
        self.changed.emit()

    def deploy(self):
        room = self.rooms[self.active_room]
        base = room["last_deployed"] or cp.create_config("∅", "")
        diff = cp.deployment_diff(base, self.config)
        self.config = cp.mark_deployed(self.config, now_iso())
        room["last_deployed"] = self.config
        # a deploy "ships" the design: newly designed devices get installed in
        # the (simulated) physical room, so a later discovery can find them
        t = room.get("transport")
        if t is not None:
            for d in self.config.devices:
                if not t.has_device(d.id):
                    t.add_device(d)
        self.set_config(self.config)
        return diff

    # ---- online room (simulated transport; A2) ----
    @property
    def transport(self):
        return self.rooms[self.active_room].get("transport")

    @property
    def online(self) -> bool:
        return bool(self.rooms[self.active_room].get("online"))

    def go_online(self) -> None:
        """Open the (simulated) room: seed the transport on first use — from the
        last deployed snapshot when there is one, else from the current design —
        then connect everything discoverable."""
        room = self.rooms[self.active_room]
        t = room.get("transport")
        if t is None:
            seed = room.get("last_deployed") or self.config
            t = cp.SimulatedTransport(seed.devices)
            room["transport"] = t
        for d in t.discover():
            try:
                t.connect(d.id)
            except cp.TransportError:
                pass                       # went offline between discover and connect
        room["online"] = True
        self.changed.emit()

    def go_offline(self) -> None:
        room = self.rooms[self.active_room]
        t = room.get("transport")
        if t is not None:
            t.disconnect_all()
        room["online"] = False
        self.changed.emit()

    def device_status(self) -> list:
        """Per-device online state (empty when the room is offline)."""
        room = self.rooms[self.active_room]
        t = room.get("transport")
        if not room.get("online") or t is None:
            return []
        return cp.online_room_status(self.config, room.get("last_deployed"), t)

    def simulate_device_offline(self, device_id: str, offline: bool = True) -> None:
        """Demo/test control: unplug (or re-plug) a device in the simulated room."""
        t = self.rooms[self.active_room].get("transport")
        if t is None or not isinstance(t, cp.SimulatedTransport):
            return
        try:
            t.set_offline(device_id, offline)
        except ValueError:
            return                         # designed but not installed — nothing to unplug
        self.changed.emit()

    def push_online(self):
        """Deploy to online devices: push the design through the transport, read
        back, reconcile. The last-deployed snapshot updates **only** when every
        device was pushed and reads back matching — a partial push leaves the
        changed-since-deploy badges nagging about exactly what didn't make it.
        Returns the ``cp.PushReport`` (None while offline)."""
        room = self.rooms[self.active_room]
        t = room.get("transport")
        if not room.get("online") or t is None:
            return None
        report = cp.push_to_online(self.config, t)
        if report.complete and report.clean:
            self.config = cp.mark_deployed(self.config, now_iso())
            room["last_deployed"] = self.config
            self.set_config(self.config)
        else:
            self.changed.emit()            # statuses moved even without a snapshot
        return report

    # ---- selection ----
    def select(self, sel: Optional[dict]) -> None:
        self.selection = sel
        self.changed.emit()

    def _prune_selection(self) -> None:
        s = self.selection
        if not s:
            return
        if s["kind"] == "device" and not any(d.id == s["id"] for d in self.config.devices):
            self.selection = None
        elif s["kind"] == "talker" and not any(t.id == s["id"] for t in self.config.talkers):
            self.selection = None

    # ---- id helpers ----
    def next_zone_id(self, array_id: str) -> str:
        zid = f"{array_id}-z{self._zone_seq}"
        self._zone_seq += 1
        return zid

    def next_talker_id(self) -> str:
        n = 1
        existing = {t.id for t in self.config.talkers}
        while f"T{n}" in existing:
            n += 1
        return f"T{n}"

    def next_device_id(self, dtype: str) -> str:
        prefix = {"processor": "P", "microphoneArray": "A", "wirelessMic": "WM",
                  "wiredMic": "WD", "loudspeaker": "L", "codec": "C"}.get(dtype, "D")
        n = 1
        existing = {d.id for d in self.config.devices}
        while f"{prefix}{n}" in existing:
            n += 1
        return f"{prefix}{n}"
