"""Device transport — the device-facing seam of the commissioning workflow.

A :class:`DeviceTransport` represents the link to one *site/room* of physical
devices: discover what is reachable, connect to individual devices, read back
the configuration a device reports, push a designed configuration to it, and
poll its status. It mirrors the ``MicController`` / ``SimulatedMicController``
pattern from :mod:`conf_pipeline_control`: the abstract base owns the
connection bookkeeping; backends implement only the raw primitives.

**No real protocol is implemented** (we cannot talk to real Shure/Dante
hardware): :class:`SimulatedTransport` stands in for a room full of devices so
the whole workflow — discover → connect → push → read back → reconcile — is
exercisable offline and under test, and a real transport can slot in behind
the same interface later. Pure stdlib, like the rest of the engine.
"""
from __future__ import annotations

import copy
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Iterable, Optional

from .deployment import deployment_diff
from .model import Device, SystemConfig


class TransportError(RuntimeError):
    """A device could not be reached, or an operation needs a connection."""


@dataclass(frozen=True)
class DiscoveredDevice:
    """One reachable device, as discovery reports it."""

    id: str               # the device's own id (matches the design id when commissioned)
    label: str
    device_type: str      # DeviceType value ("processor", "microphoneArray", …)
    address: str          # transport address (simulated: "sim://<id>")
    firmware: str = ""


@dataclass(frozen=True)
class DeviceStatus:
    """A status poll for one device (works without a connection)."""

    device_id: str
    online: bool          # reachable on the transport right now
    connected: bool       # this transport currently holds a connection to it
    firmware: str = ""
    detail: str = ""      # backend-specific free text


class DeviceTransport(ABC):
    """Site-level transport. Subclass and implement the ``_…`` primitives.

    The base class owns the connection registry: ``connect`` / ``disconnect``
    are idempotent, config I/O requires a connection, and ``read_status`` is
    deliberately allowed without one (status polling must work for offline
    detection). Usable as a context manager — leaving the block disconnects
    every device.
    """

    backend = "base"

    def __init__(self) -> None:
        self._connected: set[str] = set()

    # ---- discovery ----
    def discover(self) -> list[DiscoveredDevice]:
        """Devices currently reachable on this transport (deterministic order)."""
        return self._discover()

    # ---- connection lifecycle ----
    def connect(self, device_id: str) -> None:
        """Open a connection to one device. Raises :class:`TransportError` if
        the device is unknown or offline. Idempotent."""
        if device_id in self._connected:
            return
        self._open(device_id)
        self._connected.add(device_id)

    def disconnect(self, device_id: str) -> None:
        if device_id not in self._connected:
            return
        try:
            self._close(device_id)
        finally:
            self._connected.discard(device_id)

    def disconnect_all(self) -> None:
        for device_id in list(self._connected):
            self.disconnect(device_id)

    def is_connected(self, device_id: str) -> bool:
        return device_id in self._connected

    @property
    def connected_ids(self) -> tuple[str, ...]:
        return tuple(sorted(self._connected))

    def __enter__(self) -> "DeviceTransport":
        return self

    def __exit__(self, *exc: object) -> None:
        self.disconnect_all()

    # ---- config I/O (requires a connection) ----
    def read_config(self, device_id: str) -> Device:
        """The configuration the device itself reports (for the reconcile diff)."""
        self._require_connected(device_id)
        return self._read_config(device_id)

    def push_config(self, device: Device) -> None:
        """Push a designed device configuration to the physical device."""
        self._require_connected(device.id)
        self._push_config(device)

    # ---- status ----
    def read_status(self, device_id: str) -> DeviceStatus:
        return self._read_status(device_id)

    def _require_connected(self, device_id: str) -> None:
        if device_id not in self._connected:
            raise TransportError(f"device {device_id!r} is not connected (connect() first)")

    # ---- backend hooks ----
    @abstractmethod
    def _discover(self) -> list[DiscoveredDevice]: ...

    @abstractmethod
    def _open(self, device_id: str) -> None:
        """Reach the device or raise :class:`TransportError`."""

    @abstractmethod
    def _close(self, device_id: str) -> None: ...

    @abstractmethod
    def _read_config(self, device_id: str) -> Device: ...

    @abstractmethod
    def _push_config(self, device: Device) -> None: ...

    @abstractmethod
    def _read_status(self, device_id: str) -> DeviceStatus: ...


class SimulatedTransport(DeviceTransport):
    """A hardware-free room of devices.

    Seed it with the *device-side* configurations (deep-copied — typically the
    designed devices, or deliberately drifted copies to exercise the reconcile
    diff). ``set_offline`` simulates unplugging a device: it disappears from
    discovery, drops its connection, and refuses new ones. Deterministic — no
    randomness, no I/O.
    """

    backend = "simulated"

    def __init__(self, devices: Iterable[Device] = (), *, firmware: str = "1.0.0-sim") -> None:
        super().__init__()
        self._firmware = firmware
        self._store: dict[str, Device] = {}
        for d in devices:
            if d.id in self._store:
                raise ValueError(f"duplicate simulated device id: {d.id}")
            self._store[d.id] = copy.deepcopy(d)
        self._offline: set[str] = set()
        self.push_count = 0   # plain counter for tests / the GUI status line

    # ---- simulation controls ----
    def set_offline(self, device_id: str, offline: bool = True) -> None:
        """Simulate unplugging (or re-plugging) a device."""
        if device_id not in self._store:
            raise ValueError(f"unknown simulated device: {device_id}")
        if offline:
            self._offline.add(device_id)
            self._connected.discard(device_id)   # a dead link drops its connection
        else:
            self._offline.discard(device_id)

    def add_device(self, device: Device) -> None:
        """Plug in a new device (e.g. found by a later discovery pass)."""
        if device.id in self._store:
            raise ValueError(f"duplicate simulated device id: {device.id}")
        self._store[device.id] = copy.deepcopy(device)

    def has_device(self, device_id: str) -> bool:
        """True if the device exists in the simulated room (online or not)."""
        return device_id in self._store

    # ---- backend hooks ----
    def _discover(self) -> list[DiscoveredDevice]:
        return [
            DiscoveredDevice(
                id=d.id,
                label=d.label,
                device_type=d.type,
                address=f"sim://{d.id}",
                firmware=self._firmware,
            )
            for d in sorted(self._store.values(), key=lambda d: d.id)
            if d.id not in self._offline
        ]

    def _open(self, device_id: str) -> None:
        if device_id not in self._store:
            raise TransportError(f"device {device_id!r} not found on the transport")
        if device_id in self._offline:
            raise TransportError(f"device {device_id!r} is offline")

    def _close(self, device_id: str) -> None:
        pass

    def _read_config(self, device_id: str) -> Device:
        return copy.deepcopy(self._store[device_id])

    def _push_config(self, device: Device) -> None:
        self._store[device.id] = copy.deepcopy(device)
        self.push_count += 1

    def _read_status(self, device_id: str) -> DeviceStatus:
        known = device_id in self._store
        online = known and device_id not in self._offline
        return DeviceStatus(
            device_id=device_id,
            online=online,
            connected=device_id in self._connected,
            firmware=self._firmware if known else "",
            detail="simulated" if known else "unknown device",
        )


# --------------------------------------------------------------------------- #
# Online-room status — per-device state for the DEPLOY workflow (A2)
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class OnlineDeviceState:
    """One designed device's live state while the room is online.

    ``changed_since_deploy`` / ``new_since_deploy`` compare the *design* against
    the last deployed snapshot (via :func:`~conf_pipeline.deployment.deployment_diff`);
    ``online`` / ``connected`` come from the transport. Device-*reported* drift
    (room vs design) is the reconcile diff's job, not this one's."""

    device_id: str
    label: str
    online: bool                  # reachable on the transport right now
    connected: bool               # the transport holds a connection to it
    changed_since_deploy: bool    # designed config drifted from the last deploy
    new_since_deploy: bool        # designed after the last deploy (or never deployed)

    @property
    def in_sync(self) -> bool:
        return self.online and not (self.changed_since_deploy or self.new_since_deploy)


def online_room_status(
    config: SystemConfig,
    last_deployed: Optional[SystemConfig],
    transport: DeviceTransport,
) -> list[OnlineDeviceState]:
    """Per-designed-device online state, sorted by device id.

    Reuses :func:`deployment_diff` for the changed/new-since-deploy axis; a
    never-deployed design marks every device new. Status polling never needs a
    connection, so this is safe to call on every refresh."""
    if last_deployed is not None:
        diff = deployment_diff(last_deployed, config)
        changed, added = set(diff.devices_changed), set(diff.devices_added)
    else:
        changed, added = set(), {d.id for d in config.devices}
    out = []
    for d in sorted(config.devices, key=lambda dev: dev.id):
        st = transport.read_status(d.id)
        out.append(
            OnlineDeviceState(
                device_id=d.id,
                label=d.label,
                online=st.online,
                connected=st.connected,
                changed_since_deploy=d.id in changed,
                new_since_deploy=d.id in added,
            )
        )
    return out
