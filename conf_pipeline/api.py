"""Public builder-style API. All functions are pure: they return a new
SystemConfig and never mutate their input."""
from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Callable, Optional

from . import coverage as cov
from . import dsp
from . import matrix as mx
from .angles import Point3D, SteeringAngles, steering_angles
from .model import (
    CONFIG_VERSION,
    DEFAULT_TALKER_ELEVATION_M,
    AecConfig,
    AutomixerConfig,
    CoverageMode,
    CoverageZone,
    Crosspoint,
    Device,
    MatrixMixer,
    Point2D,
    Processor,
    Route,
    RoomLayout,
    SystemConfig,
    Talker,
    ZoneShape,
    default_elevation,
    find_device,
    is_mic_device,
    is_processor,
    point_in_shape,
)

# --------------------------------------------------------------------------- #
# Config lifecycle
# --------------------------------------------------------------------------- #
def create_config(name: str, created_at: str) -> SystemConfig:
    return SystemConfig(
        version=CONFIG_VERSION,
        devices=[],
        routes=[],
        matrix=MatrixMixer(processor_id="", input_buses=[], output_buses=[], cells={}),
        automixer=AutomixerConfig(processor_id="", channels=[], nlp="medium", output_bus_id=None),
        mute_links=[],
        talkers=[],
        metadata={"name": name, "createdAt": created_at},
        room=None,
    )


def _clone(config: SystemConfig, **changes) -> SystemConfig:
    new = copy.copy(config)
    for k, v in changes.items():
        setattr(new, k, v)
    return new


def add_device(config: SystemConfig, device: Device) -> SystemConfig:
    if any(d.id == device.id for d in config.devices):
        raise ValueError(f"Duplicate device id: {device.id}")
    new = _clone(config, devices=[*config.devices, device])
    if is_processor(device) and new.matrix.processor_id == "":
        new.matrix = device.matrix  # type: ignore[attr-defined]
        new.automixer = dsp.create_automixer(device.id)
    return new


def remove_device(config: SystemConfig, device_id: str) -> SystemConfig:
    device = find_device(config, device_id)
    if device is None:
        return config
    port_ids = {p.id for p in device.ports}
    return _clone(
        config,
        devices=[d for d in config.devices if d.id != device_id],
        routes=[r for r in config.routes if r.from_port_id not in port_ids and r.to_port_id not in port_ids],
    )


def rename_device(config: SystemConfig, device_id: str, label: str) -> SystemConfig:
    return _map_device(config, device_id, lambda d: _with(d, label=label))


def _map_device(config: SystemConfig, device_id: str, fn: Callable[[Device], Device]) -> SystemConfig:
    found = False
    devices = []
    for d in config.devices:
        if d.id == device_id:
            found = True
            devices.append(fn(d))
        else:
            devices.append(d)
    if not found:
        raise ValueError(f"Unknown device: {device_id}")
    return _clone(config, devices=devices)


def _with(obj, **changes):
    new = copy.copy(obj)
    for k, v in changes.items():
        setattr(new, k, v)
    return new


# --------------------------------------------------------------------------- #
# Room & placement
# --------------------------------------------------------------------------- #
def set_room(config: SystemConfig, room: RoomLayout) -> SystemConfig:
    return _clone(config, room=room)


def clear_room(config: SystemConfig) -> SystemConfig:
    return _clone(config, room=None)


def rectangular_room(width: float, depth: float, height: float = 3.0) -> RoomLayout:
    return RoomLayout(
        vertices=[Point2D(0, 0), Point2D(width, 0), Point2D(width, depth), Point2D(0, depth)],
        height=height,
        units="meters",
        objects=[],
    )


def set_device_position(config: SystemConfig, device_id: str, position: Point2D) -> SystemConfig:
    return _map_device(config, device_id, lambda d: _with(d, position=position))


def clear_device_position(config: SystemConfig, device_id: str) -> SystemConfig:
    return _map_device(config, device_id, lambda d: _with(d, position=None))


def set_device_elevation(config: SystemConfig, device_id: str, elevation: float) -> SystemConfig:
    return _map_device(config, device_id, lambda d: _with(d, elevation=elevation))


def clear_device_elevation(config: SystemConfig, device_id: str) -> SystemConfig:
    return _map_device(config, device_id, lambda d: _with(d, elevation=None))


def set_zone_shape(config: SystemConfig, array_id: str, zone_id: str, shape: ZoneShape) -> SystemConfig:
    def fn(d: Device) -> Device:
        if d.type != "microphoneArray":
            raise ValueError(f"Device {array_id} is not a microphone array.")
        return cov.update_zone_shape(d, zone_id, shape)  # type: ignore[arg-type]
    return _map_device(config, array_id, fn)


# --------------------------------------------------------------------------- #
# Coverage (config-level)
# --------------------------------------------------------------------------- #
def set_coverage_mode(config: SystemConfig, array_id: str, mode: CoverageMode) -> SystemConfig:
    def fn(d: Device) -> Device:
        if d.type != "microphoneArray":
            raise ValueError(f"Device {array_id} is not a microphone array.")
        return cov.set_coverage_mode(d, mode)  # type: ignore[arg-type]
    return _map_device(config, array_id, fn)


def add_coverage_zone(config: SystemConfig, array_id: str, zone: CoverageZone) -> SystemConfig:
    def fn(d: Device) -> Device:
        if d.type != "microphoneArray":
            raise ValueError(f"Device {array_id} is not a microphone array.")
        return cov.add_coverage_zone(d, zone)  # type: ignore[arg-type]
    return _map_device(config, array_id, fn)


def remove_coverage_zone(config: SystemConfig, array_id: str, zone_id: str) -> SystemConfig:
    def fn(d: Device) -> Device:
        if d.type != "microphoneArray":
            raise ValueError(f"Device {array_id} is not a microphone array.")
        return cov.remove_coverage_zone(d, zone_id)  # type: ignore[arg-type]
    return _map_device(config, array_id, fn)


# --------------------------------------------------------------------------- #
# Routing
# --------------------------------------------------------------------------- #
def _route_id(from_port_id: str, to_port_id: str) -> str:
    return f"r:{from_port_id}->{to_port_id}"


def route(config: SystemConfig, from_port_id: str, to_port_id: str) -> SystemConfig:
    rid = _route_id(from_port_id, to_port_id)
    if any(r.id == rid for r in config.routes):
        return config
    return _clone(config, routes=[*config.routes, Route(id=rid, from_port_id=from_port_id, to_port_id=to_port_id)])


def unroute(config: SystemConfig, from_port_id: str, to_port_id: str) -> SystemConfig:
    rid = _route_id(from_port_id, to_port_id)
    return _clone(config, routes=[r for r in config.routes if r.id != rid])


# --------------------------------------------------------------------------- #
# Matrix accessor
# --------------------------------------------------------------------------- #
class MatrixAccessor:
    def __init__(self, config: SystemConfig, processor_id: str):
        self._config = config
        self._pid = processor_id
        proc = find_device(config, processor_id)
        if proc is None or not is_processor(proc):
            raise ValueError(f"Unknown processor: {processor_id}")
        self._proc: Processor = proc  # type: ignore[assignment]

    def _apply(self, fn: Callable[[MatrixMixer], MatrixMixer]) -> SystemConfig:
        updated: dict[str, MatrixMixer] = {}

        def map_fn(d: Device) -> Device:
            if not is_processor(d):
                raise ValueError(f"Device {self._pid} is not a processor.")
            m = fn(d.matrix)  # type: ignore[attr-defined]
            updated["m"] = m
            nd = _with(d, matrix=m, buses=[*m.input_buses, *m.output_buses])
            return nd

        new = _map_device(self._config, self._pid, map_fn)
        if new.matrix.processor_id == self._pid:
            new = _clone(new, matrix=updated["m"])
        return new

    def set(self, input_bus_id: str, output_bus_id: str, crosspoint: Crosspoint) -> SystemConfig:
        return self._apply(lambda m: mx.set_crosspoint(m, input_bus_id, output_bus_id, crosspoint))

    def route(self, input_bus_id: str, output_bus_id: str, gain_db: float = 0.0) -> SystemConfig:
        return self._apply(lambda m: mx.route(m, input_bus_id, output_bus_id, gain_db))

    def clear(self, input_bus_id: str, output_bus_id: str) -> SystemConfig:
        return self._apply(lambda m: mx.clear(m, input_bus_id, output_bus_id))

    def get(self, input_bus_id: str, output_bus_id: str) -> Optional[Crosspoint]:
        return mx.get(self._proc.matrix, input_bus_id, output_bus_id)

    def is_active(self, input_bus_id: str, output_bus_id: str) -> bool:
        return mx.is_active(self._proc.matrix, input_bus_id, output_bus_id)


def matrix_for(config: SystemConfig, processor_id: str) -> MatrixAccessor:
    return MatrixAccessor(config, processor_id)


# --------------------------------------------------------------------------- #
# AEC + automixer
# --------------------------------------------------------------------------- #
def set_aec(config: SystemConfig, mic_id: str, aec: AecConfig) -> SystemConfig:
    def fn(d: Device) -> Device:
        if not is_mic_device(d):
            raise ValueError(f"Device {mic_id} has no AEC (not a microphone).")
        return _with(d, aec=copy.copy(aec))
    return _map_device(config, mic_id, fn)


def configure_automixer(config: SystemConfig, processor_id: str, automixer: AutomixerConfig) -> SystemConfig:
    if automixer.processor_id != processor_id:
        raise ValueError(f'Automixer processor_id "{automixer.processor_id}" does not match "{processor_id}".')
    return _clone(config, automixer=copy.copy(automixer))


# --------------------------------------------------------------------------- #
# Device profiles & DSP blocks (v1.7.0)
# --------------------------------------------------------------------------- #
def assign_device_profile(config: SystemConfig, device_id: str, profile_id: str) -> SystemConfig:
    return _map_device(config, device_id, lambda d: _with(d, profile_id=profile_id))


def add_dsp_block(config: SystemConfig, device_id: str, block) -> SystemConfig:
    def fn(d: Device) -> Device:
        blocks = list(d.dsp_blocks or [])
        if any(b.id == block.id for b in blocks):
            raise ValueError(f'Duplicate DSP block id "{block.id}" on device "{device_id}".')
        return _with(d, dsp_blocks=[*blocks, block])
    return _map_device(config, device_id, fn)


def update_dsp_block(config: SystemConfig, device_id: str, block_id: str, patch: dict) -> SystemConfig:
    def upd(b):
        nb = copy.copy(b)
        if "enabled" in patch:
            nb.enabled = patch["enabled"]
        if "target_bus_id" in patch:
            nb.target_bus_id = patch["target_bus_id"]
        if "targetBusId" in patch:
            nb.target_bus_id = patch["targetBusId"]
        if "params" in patch:
            nb.params = {**b.params, **patch["params"]}
        return nb
    def fn(d: Device) -> Device:
        return _with(d, dsp_blocks=[upd(b) if b.id == block_id else b for b in (d.dsp_blocks or [])])
    return _map_device(config, device_id, fn)


def remove_dsp_block(config: SystemConfig, device_id: str, block_id: str) -> SystemConfig:
    return _map_device(config, device_id, lambda d: _with(d, dsp_blocks=[b for b in (d.dsp_blocks or []) if b.id != block_id]))


def set_dsp_block_enabled(config: SystemConfig, device_id: str, block_id: str, enabled: bool) -> SystemConfig:
    def upd(b):
        nb = copy.copy(b)
        nb.enabled = enabled
        return nb
    return _map_device(config, device_id, lambda d: _with(d, dsp_blocks=[upd(b) if b.id == block_id else b for b in (d.dsp_blocks or [])]))


# --------------------------------------------------------------------------- #
# Talkers
# --------------------------------------------------------------------------- #
def create_talker(id: str, label: str, position: Point2D, elevation: Optional[float] = None) -> Talker:
    return Talker(id=id, label=label, position=position, elevation=elevation)


def add_talker(config: SystemConfig, talker: Talker) -> SystemConfig:
    if any(t.id == talker.id for t in config.talkers):
        raise ValueError(f"Duplicate talker id: {talker.id}")
    return _clone(config, talkers=[*config.talkers, talker])


def remove_talker(config: SystemConfig, talker_id: str) -> SystemConfig:
    return _clone(config, talkers=[t for t in config.talkers if t.id != talker_id])


def _map_talker(config: SystemConfig, talker_id: str, fn: Callable[[Talker], Talker]) -> SystemConfig:
    found = False
    talkers = []
    for t in config.talkers:
        if t.id == talker_id:
            found = True
            talkers.append(fn(t))
        else:
            talkers.append(t)
    if not found:
        raise ValueError(f"Unknown talker: {talker_id}")
    return _clone(config, talkers=talkers)


def set_talker_position(config: SystemConfig, talker_id: str, position: Point2D) -> SystemConfig:
    return _map_talker(config, talker_id, lambda t: _with(t, position=position))


def set_talker_elevation(config: SystemConfig, talker_id: str, elevation: float) -> SystemConfig:
    return _map_talker(config, talker_id, lambda t: _with(t, elevation=elevation))


def rename_talker(config: SystemConfig, talker_id: str, label: str) -> SystemConfig:
    return _map_talker(config, talker_id, lambda t: _with(t, label=label))


def talker_elevation(talker: Talker) -> float:
    return talker.elevation if talker.elevation is not None else DEFAULT_TALKER_ELEVATION_M


def array_to_talker_angles(config: SystemConfig, array_id: str, talker_id: str) -> Optional[SteeringAngles]:
    array = find_device(config, array_id)
    talker = next((t for t in config.talkers if t.id == talker_id), None)
    if array is None or array.type != "microphoneArray" or array.position is None or talker is None:
        return None
    room_height = config.room.height if config.room is not None else 3.0
    frm = Point3D(array.position.x, array.position.y, array.elevation if array.elevation is not None else default_elevation(array, room_height))
    to = Point3D(talker.position.x, talker.position.y, talker_elevation(talker))
    return steering_angles(frm, to)


@dataclass
class TalkerCoverage:
    captured: bool
    pickup_arrays: list[str] = field(default_factory=list)
    excluded_by: list[str] = field(default_factory=list)


def talker_coverage(config: SystemConfig, talker_id: str) -> TalkerCoverage:
    talker = next((t for t in config.talkers if t.id == talker_id), None)
    pickup_arrays: list[str] = []
    excluded_by: list[str] = []
    if talker is None:
        return TalkerCoverage(False, pickup_arrays, excluded_by)
    for device in config.devices:
        if device.type != "microphoneArray":
            continue
        in_pickup = False
        in_exclusion = False
        for zone in device.zones:  # type: ignore[attr-defined]
            if not point_in_shape(talker.position, zone.shape):
                continue
            if zone.type == "exclusion":
                in_exclusion = True
            else:
                in_pickup = True
        if in_exclusion:
            excluded_by.append(device.id)
        elif in_pickup:
            pickup_arrays.append(device.id)
    return TalkerCoverage(captured=len(pickup_arrays) > 0 and len(excluded_by) == 0, pickup_arrays=pickup_arrays, excluded_by=excluded_by)


# --------------------------------------------------------------------------- #
# Auto-configure
# --------------------------------------------------------------------------- #
def _pick_unused_dante_output_buses(config: SystemConfig, processor: Processor, count: int) -> list[str]:
    speaker_feeds = dsp.output_buses_feeding_loudspeakers(config, processor)
    used: set[str] = set()
    for cols in processor.matrix.cells.values():
        for out_id, cp in cols.items():
            if cp.enabled:
                used.add(out_id)
    picked: list[str] = []
    for bus in processor.matrix.output_buses:
        if len(picked) >= count:
            break
        port = next((p for p in processor.ports if p.id == bus.port_id), None)
        if port is None or port.transport != "dante":
            continue
        if bus.id in used or bus.id in speaker_feeds:
            continue
        picked.append(bus.id)
    return picked


def auto_configure(config: SystemConfig) -> SystemConfig:
    processor = dsp.get_primary_processor(config)
    if processor is None:
        return config
    new = config
    codecs = [d for d in new.devices if d.type == "codec"]
    mics = [d for d in new.devices if is_mic_device(d)]

    far_end_input_buses: set[str] = set()
    for codec in codecs:
        for b in dsp.processor_input_buses_for_device(new, processor, codec.id):
            far_end_input_buses.add(b)

    picked = _pick_unused_dante_output_buses(new, processor, 2)
    ref_bus = picked[0] if len(picked) > 0 else None
    automix_bus = picked[1] if len(picked) > 1 else None

    if ref_bus and far_end_input_buses:
        for in_bus in far_end_input_buses:
            new = matrix_for(new, processor.id).route(in_bus, ref_bus)
        for mic in mics:
            new = set_aec(new, mic.id, AecConfig(enabled=True, reference_bus_id=ref_bus))

    am = dsp.create_automixer(processor.id)
    speaker_feeds = dsp.output_buses_feeding_loudspeakers(new, processor)
    proc_now = dsp.get_primary_processor(new)
    for mic in mics:
        for in_bus in dsp.processor_input_buses_for_device(new, proc_now, mic.id):
            is_reinforced = any(o in speaker_feeds for o in mx.outputs_for_input(proc_now.matrix, in_bus))
            am = dsp.upsert_channel(am, dsp.automixer_channel(in_bus, always_on=is_reinforced, gating_sensitivity=0.5))

    if automix_bus:
        am = dsp.set_automix_output(am, automix_bus)
        for ch in am.channels:
            new = matrix_for(new, processor.id).route(ch.input_bus_id, automix_bus)
        proc_now = dsp.get_primary_processor(new)
        automix_port = next((p for p in proc_now.ports if p.id == automix_bus), None)
        if automix_port is not None:
            for codec in codecs:
                near_end_in = next((p for p in codec.ports if p.kind == "input" and p.transport == automix_port.transport), None)
                if near_end_in is not None:
                    new = route(new, automix_bus, near_end_in.id)

    return configure_automixer(new, processor.id, am)
