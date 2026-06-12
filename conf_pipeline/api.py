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
    ControlConfig,
    CoverageMode,
    CoverageZone,
    Crosspoint,
    Device,
    MatrixMixer,
    MuteGroup,
    MuteTrigger,
    Point2D,
    Processor,
    Route,
    RoomBackground,
    RoomLayout,
    Scene,
    SceneSteer,
    SceneZoneState,
    SystemConfig,
    Talker,
    ZoneChannelRef,
    ZoneShape,
    default_elevation,
    find_device,
    is_mic_device,
    is_pickup_zone,
    is_processor,
    point_in_shape,
    to_jsonable,
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
        new.matrix = device.matrix
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


# ---- floor-plan background image ----
def set_room_background(config: SystemConfig, path: str, image_width_px: int, image_height_px: int,
                        scale_m_per_px: Optional[float] = None, origin: Optional[Point2D] = None,
                        opacity: Optional[float] = None) -> SystemConfig:
    if config.room is None:
        raise ValueError("No room to attach a floor-plan background to.")
    room = copy.copy(config.room)
    room.background = RoomBackground(
        path=path, image_width_px=image_width_px, image_height_px=image_height_px,
        scale_m_per_px=scale_m_per_px,
        origin=origin if origin is not None else Point2D(0.0, 0.0),
        opacity=0.5 if opacity is None else max(0.0, min(1.0, opacity)),
    )
    return _clone(config, room=room)


def set_room_background_scale(config: SystemConfig, scale_m_per_px: float) -> SystemConfig:
    if config.room is None or config.room.background is None:
        raise ValueError("No floor-plan background to scale.")
    room = copy.copy(config.room)
    bg = copy.copy(config.room.background)
    bg.scale_m_per_px = scale_m_per_px
    room.background = bg
    return _clone(config, room=room)


def set_room_background_opacity(config: SystemConfig, opacity: float) -> SystemConfig:
    if config.room is None or config.room.background is None:
        raise ValueError("No floor-plan background.")
    room = copy.copy(config.room)
    bg = copy.copy(config.room.background)
    bg.opacity = max(0.0, min(1.0, opacity))
    room.background = bg
    return _clone(config, room=room)


def clear_room_background(config: SystemConfig) -> SystemConfig:
    if config.room is None or config.room.background is None:
        return config
    room = copy.copy(config.room)
    room.background = None
    return _clone(config, room=room)


def calibrated_scale(scale_old: float, world_dist: float, real_len: float) -> float:
    """New metres-per-pixel from a calibration drag: ``scale_old · real_len / world_dist``.

    ``world_dist`` is the on-floor length the drawn line currently spans at
    ``scale_old``; ``real_len`` is the true distance the user entered."""
    if world_dist <= 1e-6:
        raise ValueError("Calibration distance is too small.")
    return scale_old * real_len / world_dist


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
        return cov.update_zone_shape(d, zone_id, shape)
    return _map_device(config, array_id, fn)


# --------------------------------------------------------------------------- #
# Coverage (config-level)
# --------------------------------------------------------------------------- #
def set_coverage_mode(config: SystemConfig, array_id: str, mode: CoverageMode) -> SystemConfig:
    def fn(d: Device) -> Device:
        if d.type != "microphoneArray":
            raise ValueError(f"Device {array_id} is not a microphone array.")
        return cov.set_coverage_mode(d, mode)
    return _map_device(config, array_id, fn)


def add_coverage_zone(config: SystemConfig, array_id: str, zone: CoverageZone) -> SystemConfig:
    def fn(d: Device) -> Device:
        if d.type != "microphoneArray":
            raise ValueError(f"Device {array_id} is not a microphone array.")
        return cov.add_coverage_zone(d, zone)
    return _map_device(config, array_id, fn)


def remove_coverage_zone(config: SystemConfig, array_id: str, zone_id: str) -> SystemConfig:
    def fn(d: Device) -> Device:
        if d.type != "microphoneArray":
            raise ValueError(f"Device {array_id} is not a microphone array.")
        return cov.remove_coverage_zone(d, zone_id)
    return _map_device(config, array_id, fn)


def _array_fn(config: SystemConfig, array_id: str, fn: Callable[[Device], Device]) -> SystemConfig:
    def wrap(d: Device) -> Device:
        if d.type != "microphoneArray":
            raise ValueError(f"Device {array_id} is not a microphone array.")
        return fn(d)
    return _map_device(config, array_id, wrap)


def set_zone_output_channel(config: SystemConfig, array_id: str, zone_id: str, channel: Optional[int]) -> SystemConfig:
    """Assign (or clear with ``None``) a coverage area's own numbered output channel
    (Designer steerable-coverage style). Regenerates the array's output ports."""
    return _array_fn(config, array_id, lambda d: cov.set_zone_output_channel(d, zone_id, channel))  # type: ignore[arg-type]


def set_zone_gain_db(config: SystemConfig, array_id: str, zone_id: str, gain_db: Optional[float]) -> SystemConfig:
    """Set (or clear with ``None``) a coverage area's per-area gain trim (dB)."""
    return _array_fn(config, array_id, lambda d: cov.set_zone_gain_db(d, zone_id, gain_db))  # type: ignore[arg-type]


def auto_assign_zone_channels(config: SystemConfig, array_id: str) -> SystemConfig:
    """Give every pickup area on the array a sequential output channel (idempotent)."""
    return _array_fn(config, array_id, lambda d: cov.auto_assign_zone_channels(d))  # type: ignore[arg-type]


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
        self._proc: Processor = proc

    def _apply(self, fn: Callable[[MatrixMixer], MatrixMixer]) -> SystemConfig:
        updated: dict[str, MatrixMixer] = {}

        def map_fn(d: Device) -> Device:
            if not is_processor(d):
                raise ValueError(f"Device {self._pid} is not a processor.")
            m = fn(d.matrix)
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
        for zone in device.zones:
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

    # Reuse already-assigned reference / automix buses so re-running is idempotent
    # (a fresh design has neither, so first-run behaviour is unchanged).
    existing_ref = next((m.aec.reference_bus_id for m in mics if m.aec.enabled and m.aec.reference_bus_id), None)
    existing_automix = config.automixer.output_bus_id
    pick_iter = iter(_pick_unused_dante_output_buses(new, processor, 2))
    ref_bus = existing_ref or next(pick_iter, None)
    automix_bus = existing_automix or next((b for b in pick_iter if b != ref_bus), None)

    if ref_bus and far_end_input_buses:
        for in_bus in far_end_input_buses:
            new = matrix_for(new, processor.id).route(in_bus, ref_bus)
        for mic in mics:
            new = set_aec(new, mic.id, AecConfig(enabled=True, reference_bus_id=ref_bus))

    am = dsp.create_automixer(processor.id)
    speaker_feeds = dsp.output_buses_feeding_loudspeakers(new, processor)
    proc_now = dsp.get_primary_processor(new)
    assert proc_now is not None  # the primary processor never disappears mid-configure
    for mic in mics:
        for in_bus in dsp.processor_input_buses_for_device(new, proc_now, mic.id):
            is_reinforced = any(o in speaker_feeds for o in mx.outputs_for_input(proc_now.matrix, in_bus))
            am = dsp.upsert_channel(am, dsp.automixer_channel(in_bus, always_on=is_reinforced, gating_sensitivity=0.5))

    if automix_bus:
        am = dsp.set_automix_output(am, automix_bus)
        for ch in am.channels:
            new = matrix_for(new, processor.id).route(ch.input_bus_id, automix_bus)
        proc_now = dsp.get_primary_processor(new)
        assert proc_now is not None
        automix_port = next((p for p in proc_now.ports if p.id == automix_bus), None)
        if automix_port is not None:
            for codec in codecs:
                near_end_in = next((p for p in codec.ports if p.kind == "input" and p.transport == automix_port.transport), None)
                if near_end_in is not None:
                    new = route(new, automix_bus, near_end_in.id)

    return configure_automixer(new, processor.id, am)


# --------------------------------------------------------------------------- #
# Auto-route (one-click optimize) + change summary
# --------------------------------------------------------------------------- #
@dataclass
class AutoRouteResult:
    config: SystemConfig
    changes: list[str] = field(default_factory=list)
    counts: dict = field(default_factory=dict)


def _pick_program_bus(processor: Processor, transport: str, forbidden: set[str]):
    """First processor output bus whose port matches ``transport`` and is not a
    forbidden column (any AEC reference / the automix bus). Returns the Bus or None."""
    for bus in processor.matrix.output_buses:
        port = next((p for p in processor.ports if p.id == bus.port_id), None)
        if port is None or port.transport != transport:
            continue
        if bus.id in forbidden:
            continue
        return bus
    return None


def auto_route(config: SystemConfig) -> AutoRouteResult:
    """One-click optimize. Runs :func:`auto_configure` (AEC references, automixer,
    near-end send), then feeds the far-end to the loudspeakers and links mic mutes,
    returning the new config plus a human-readable summary.

    Invariant: never routes a mic into an AEC reference bus, so
    ``validate(result.config).errors`` stays empty (the AEC self-reference rule).
    """
    processor = dsp.get_primary_processor(config)
    if processor is None:
        return AutoRouteResult(config, ["No processor in the design — nothing to route."], {})

    changes: list[str] = []
    counts = {"crosspoints": 0, "routes": 0, "mute_links": 0}

    new = auto_configure(config)
    proc = dsp.get_primary_processor(new)
    assert proc is not None  # checked above; auto_configure never removes it
    pid = proc.id
    mics = [d for d in new.devices if is_mic_device(d)]
    codecs = [d for d in new.devices if d.type == "codec"]
    speakers = [d for d in new.devices if d.type == "loudspeaker"]

    aec_mics = [m.id for m in mics if m.aec.enabled]
    if aec_mics:
        changes.append(f"AEC enabled on {len(aec_mics)} mic(s), referencing the far-end bus")
    if new.automixer.channels:
        changes.append(f"Automixer configured with {len(new.automixer.channels)} channel(s)")
    if new.automixer.output_bus_id and codecs:
        changes.append("Mic mix routed to the codec (near-end send)")
    if not codecs:
        changes.append("No codec in the design — far-end / AEC routing skipped")

    # Never reinforce into an AEC reference bus or the near-end mix bus.
    forbidden = {m.aec.reference_bus_id for m in mics if m.aec.enabled and m.aec.reference_bus_id}
    if new.automixer.output_bus_id:
        forbidden.add(new.automixer.output_bus_id)

    far_end_in: set[str] = set()
    for codec in codecs:
        for b in dsp.processor_input_buses_for_device(new, proc, codec.id):
            far_end_in.add(b)

    # Far-end audio -> loudspeakers (so remote participants are heard in the room).
    if speakers and far_end_in:
        for spk in speakers:
            in_port = next((p for p in spk.ports if p.kind == "input"), None)
            if in_port is None:
                continue
            proc_latest = dsp.get_primary_processor(new)  # matrix changes as routes land
            assert proc_latest is not None
            bus = _pick_program_bus(proc_latest, in_port.transport, forbidden)
            if bus is None:
                continue
            for fe in far_end_in:
                if not matrix_for(new, pid).is_active(fe, bus.id):
                    new = matrix_for(new, pid).route(fe, bus.id)
                    counts["crosspoints"] += 1
            had = any(r.id == _route_id(bus.port_id, in_port.id) for r in new.routes)
            new = route(new, bus.port_id, in_port.id)
            if not had:
                counts["routes"] += 1
                changes.append(f"Fed far-end audio to {spk.label}")
    elif speakers and not far_end_in:
        changes.append("Loudspeakers present but no codec/far-end source to feed them")

    # Link mic mutes to the near-end mix so a room mute syncs (mics are mute-capable).
    if new.automixer.output_bus_id and mics:
        link_id = f"ml:auto:{new.automixer.output_bus_id}"
        if not any(lk.id == link_id for lk in new.mute_links):
            link = dsp.create_mute_link(link_id, new.automixer.output_bus_id, [m.id for m in mics], sync_to_codec=bool(codecs))
            new = _clone(new, mute_links=[*new.mute_links, link])
            counts["mute_links"] += 1
            changes.append(f"Linked mute across {len(mics)} mic(s)" + (" (synced to codec)" if codecs else ""))

    if to_jsonable(new) == to_jsonable(config):
        return AutoRouteResult(config, ["No changes — the design is already routed."], {"crosspoints": 0, "routes": 0, "mute_links": 0})
    return AutoRouteResult(new, changes, counts)


# --------------------------------------------------------------------------- #
# Logic / control — mute groups (v1.12.0)
# --------------------------------------------------------------------------- #
def _ensure_control(config: SystemConfig) -> ControlConfig:
    return config.control if config.control is not None else ControlConfig()


def create_mute_group(
    id: str,
    label: str,
    device_ids: Optional[list[str]] = None,
    zone_refs: Optional[list[ZoneChannelRef]] = None,
    trigger: MuteTrigger = "software",
    muted: bool = False,
) -> MuteGroup:
    return MuteGroup(
        id=id, label=label,
        device_ids=list(device_ids or []),
        zone_refs=list(zone_refs or []),
        trigger=trigger, muted=muted,
    )


def add_mute_group(config: SystemConfig, group: MuteGroup) -> SystemConfig:
    ctrl = _ensure_control(config)
    if any(g.id == group.id for g in ctrl.mute_groups):
        raise ValueError(f"Duplicate mute-group id: {group.id}")
    return _clone(config, control=_with(ctrl, mute_groups=[*ctrl.mute_groups, group]))


def remove_mute_group(config: SystemConfig, group_id: str) -> SystemConfig:
    if config.control is None:
        return config
    groups = [g for g in config.control.mute_groups if g.id != group_id]
    if len(groups) == len(config.control.mute_groups):
        return config
    return _clone(config, control=_with(config.control, mute_groups=groups))


def set_mute_group_muted(config: SystemConfig, group_id: str, muted: bool) -> SystemConfig:
    if config.control is None:
        raise ValueError("No control config.")
    groups = [_with(g, muted=muted) if g.id == group_id else g for g in config.control.mute_groups]
    return _clone(config, control=_with(config.control, mute_groups=groups))


# --------------------------------------------------------------------------- #
# Scenes (v3) — named, recallable snapshots of the control surface
# --------------------------------------------------------------------------- #
def create_scene(
    id: str,
    label: str,
    mute_states: Optional[dict[str, bool]] = None,
    zone_states: Optional[list[SceneZoneState]] = None,
    steer: Optional[list[SceneSteer]] = None,
) -> Scene:
    return Scene(
        id=id, label=label,
        mute_states=dict(mute_states or {}),
        zone_states=list(zone_states or []),
        steer=list(steer or []),
    )


def add_scene(config: SystemConfig, scene: Scene) -> SystemConfig:
    ctrl = _ensure_control(config)
    if any(s.id == scene.id for s in ctrl.scenes):
        raise ValueError(f"Duplicate scene id: {scene.id}")
    return _clone(config, control=_with(ctrl, scenes=[*ctrl.scenes, scene]))


def remove_scene(config: SystemConfig, scene_id: str) -> SystemConfig:
    if config.control is None:
        return config
    scenes = [s for s in config.control.scenes if s.id != scene_id]
    if len(scenes) == len(config.control.scenes):
        return config
    return _clone(config, control=_with(config.control, scenes=scenes))


def get_scene(config: SystemConfig, scene_id: str) -> Optional[Scene]:
    if config.control is None:
        return None
    return next((s for s in config.control.scenes if s.id == scene_id), None)


def capture_scene(config: SystemConfig, id: str, label: str) -> Scene:
    """Snapshot the current control surface: every mute group's muted state and
    every pickup area's gain trim. ``active`` flags and steer hints are
    live-layer state, not config, so a captured scene leaves them unset. A zone
    whose trim is unset is captured as ``None`` ("leave as-is" on recall)."""
    ctrl = config.control
    mute_states = {g.id: g.muted for g in (ctrl.mute_groups if ctrl is not None else [])}
    zone_states = []
    for d in config.devices:
        if d.type != "microphoneArray":
            continue
        for z in d.zones:
            if is_pickup_zone(z):
                zone_states.append(SceneZoneState(array_id=d.id, zone_id=z.id, gain_db=z.gain_db))
    return Scene(id=id, label=label, mute_states=mute_states, zone_states=zone_states)


def recall_scene(config: SystemConfig, scene_id: str) -> SystemConfig:
    """Apply a scene: mute-group states and per-area gain trims. Pure —
    returns a new config. Entries referencing things that no longer exist are
    skipped (validation flags them as ``SCENE_INVALID``); ``None`` fields mean
    "leave as-is". A scene's ``active`` flags and steer hints are config-inert —
    the live layer reads them (``get_scene(...)``) to choose which areas to
    beamform and where to aim."""
    scene = get_scene(config, scene_id)
    if scene is None:
        raise ValueError(f"Unknown scene: {scene_id}")
    new = config
    ctrl = new.control
    assert ctrl is not None  # the scene was found in it
    groups = [
        _with(g, muted=scene.mute_states[g.id]) if g.id in scene.mute_states else g
        for g in ctrl.mute_groups
    ]
    new = _clone(new, control=_with(ctrl, mute_groups=groups))
    for zs in scene.zone_states:
        if zs.gain_db is None:
            continue
        arr = find_device(new, zs.array_id)
        if arr is None or arr.type != "microphoneArray" or not any(z.id == zs.zone_id for z in arr.zones):
            continue
        new = set_zone_gain_db(new, zs.array_id, zs.zone_id, zs.gain_db)
    return new


# --------------------------------------------------------------------------- #
# Optimize room — one-click "do everything" (v1.12.0). Orchestrates the existing
# placement-recommendation, per-area channel assignment, and auto-route into a
# single Designer-style action, returning the new config + a change summary.
# --------------------------------------------------------------------------- #
@dataclass
class OptimizeRoomResult:
    config: SystemConfig
    changes: list[str] = field(default_factory=list)
    auto_route: Optional[AutoRouteResult] = None


def optimize_room(
    config: SystemConfig,
    *,
    place_arrays: bool = True,
    assign_channels: bool = True,
    route: bool = True,
    params=None,
) -> OptimizeRoomResult:
    """One-click optimize: (1) recommend + apply each array's best placement/steer
    (when a room + talkers exist), (2) give every pickup area its own output channel,
    (3) run :func:`auto_route`. Each stage is opt-out and idempotent.

    Pure: returns a new config; never mutates the input.
    """
    from .sim import recommend_placement  # local import: sim is dependency-light but optional in spirit

    new = config
    changes: list[str] = []

    arrays = [d for d in new.devices if d.type == "microphoneArray"]

    if place_arrays and new.room is not None and new.talkers and arrays:
        for arr in arrays:
            try:
                rec = recommend_placement(new, arr.id) if params is None else recommend_placement(new, arr.id, params=params)
            except Exception as exc:  # noqa: BLE001 — never let a single array break the whole run
                changes.append(f'Placement skipped for "{arr.label}" ({exc}).')
                continue
            if rec is None or rec.array_pos is None:
                continue
            new = set_device_position(new, arr.id, rec.array_pos)
            new = set_device_elevation(new, arr.id, rec.array_elev)
            changes.append(f'Placed "{arr.label}" at ({rec.array_pos.x:.2f}, {rec.array_pos.y:.2f}) m, '
                           f'elev {rec.array_elev:.2f} m (score {rec.score.total:.2f}).')

    if assign_channels:
        for arr in [d for d in new.devices if d.type == "microphoneArray"]:
            pickups = [z for z in arr.zones if z.type != "exclusion"]
            unassigned = [z for z in pickups if z.output_channel is None]
            if unassigned:
                new = auto_assign_zone_channels(new, arr.id)
                changes.append(f'Assigned {len(unassigned)} output channel(s) on "{arr.label}".')

    ar: Optional[AutoRouteResult] = None
    if route:
        ar = auto_route(new)
        new = ar.config
        changes.extend(ar.changes)

    if not changes:
        changes.append("Nothing to optimize — room already placed, channelled, and routed.")
    return OptimizeRoomResult(config=new, changes=changes, auto_route=ar)
