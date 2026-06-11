"""Coverage subsystem: zones + mode-driven output-port regeneration."""
from __future__ import annotations

import copy
from typing import Optional

from .model import (
    DEFAULT_DEDICATED_ZONE_SIZE_M,
    MAX_MANUAL_LOBES,
    MAX_ZONES_PER_ARRAY,
    ZONE_GAIN_DB_MAX,
    ZONE_GAIN_DB_MIN,
    AecConfig,
    CoverageMode,
    CoverageZone,
    MicrophoneArray,
    Point2D,
    Port,
    RectShape,
    ZoneShape,
    is_pickup_zone,
)


class CoverageError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _out_port(array_id: str, suffix: str, label: str) -> Port:
    return Port(id=f"{array_id}-out-{suffix}", device_id=array_id, kind="output", transport="dante", label=label)


def pickup_zone_count(zones: list[CoverageZone]) -> int:
    return sum(1 for z in zones if is_pickup_zone(z))


def _zone_channel_ports(array_id: str, zones: list[CoverageZone]) -> list[Port]:
    """One dedicated Dante output per pickup zone that carries an ``output_channel``
    (Designer steerable-coverage style). Channels are emitted in ascending order so
    the port list is stable regardless of zone draw order."""
    chans = sorted(
        (z for z in zones if is_pickup_zone(z) and z.output_channel is not None),
        key=lambda z: z.output_channel,  # type: ignore[arg-type, return-value]
    )
    return [_out_port(array_id, f"ch-{z.output_channel}", f"Coverage Ch {z.output_channel} ({z.label})") for z in chans]


def generate_array_output_ports(
    array_id: str, mode: CoverageMode, lobe_zone_count: int, zones: Optional[list[CoverageZone]] = None
) -> list[Port]:
    zone_ports = _zone_channel_ports(array_id, zones or [])
    if mode == "automatic":
        return [_out_port(array_id, "mix", "Mixed Dante Out"), *zone_ports]
    lobe_count = min(max(lobe_zone_count, 0), MAX_MANUAL_LOBES)
    ports = [_out_port(array_id, f"lobe-{i}", f"Lobe {i} Dante Out") for i in range(1, lobe_count + 1)]
    ports.append(_out_port(array_id, "automix", "Automix Dante Out"))
    ports.extend(zone_ports)
    return ports


def create_microphone_array(
    id: str,
    label: str,
    mode: CoverageMode = "automatic",
    zones: Optional[list[CoverageZone]] = None,
    position: Optional[Point2D] = None,
) -> MicrophoneArray:
    zones = list(zones or [])
    if len(zones) > MAX_ZONES_PER_ARRAY:
        raise CoverageError("COVERAGE_ZONE_LIMIT", f'Array "{id}" cannot have more than {MAX_ZONES_PER_ARRAY} zones (got {len(zones)}).')
    for z in zones:
        assert_zone_valid(z)
    arr = MicrophoneArray(
        id=id,
        label=label,
        ports=generate_array_output_ports(id, mode, pickup_zone_count(zones), zones),
        coverage_mode=mode,
        zones=zones,
        aec=AecConfig(enabled=False, reference_bus_id=None),
        profile_id="generic-ceiling-array",
    )
    if position is not None:
        arr.position = position
    return arr


def set_coverage_mode(array: MicrophoneArray, mode: CoverageMode) -> MicrophoneArray:
    new = copy.copy(array)
    new.coverage_mode = mode
    new.ports = generate_array_output_ports(array.id, mode, pickup_zone_count(array.zones), array.zones)
    return new


def add_coverage_zone(array: MicrophoneArray, zone: CoverageZone) -> MicrophoneArray:
    if len(array.zones) >= MAX_ZONES_PER_ARRAY:
        raise CoverageError("COVERAGE_ZONE_LIMIT", f'Array "{array.id}" already has {MAX_ZONES_PER_ARRAY} zones; cannot add another.')
    assert_zone_valid(zone)
    zones = list(array.zones) + [zone]
    new = copy.copy(array)
    new.zones = zones
    new.ports = generate_array_output_ports(array.id, array.coverage_mode, pickup_zone_count(zones), zones)
    return new


def update_zone_shape(array: MicrophoneArray, zone_id: str, shape: ZoneShape) -> MicrophoneArray:
    if not any(z.id == zone_id for z in array.zones):
        return array
    _assert_shape_valid(zone_id, shape)
    new = copy.copy(array)
    new.zones = [copy.copy(z) if z.id != zone_id else _with_shape(z, shape) for z in array.zones]
    return new


def _with_shape(zone: CoverageZone, shape: ZoneShape) -> CoverageZone:
    z = copy.copy(zone)
    z.shape = shape
    return z


def remove_coverage_zone(array: MicrophoneArray, zone_id: str) -> MicrophoneArray:
    zones = [z for z in array.zones if z.id != zone_id]
    if len(zones) == len(array.zones):
        return array
    new = copy.copy(array)
    new.zones = zones
    new.ports = generate_array_output_ports(array.id, array.coverage_mode, pickup_zone_count(zones), zones)
    return new


# --------------------------------------------------------------------------- #
# Per-coverage-area output channel + gain (v1.12.0, Designer steerable coverage)
# --------------------------------------------------------------------------- #
def set_zone_output_channel(array: MicrophoneArray, zone_id: str, channel: Optional[int]) -> MicrophoneArray:
    """Assign (or clear, with ``None``) a coverage area's own output channel. The
    channel must be a pickup zone, 1..MAX_ZONES_PER_ARRAY, and unique on the array.
    Regenerates the array's output ports so the per-area Dante out appears."""
    zone = next((z for z in array.zones if z.id == zone_id), None)
    if zone is None:
        raise CoverageError("COVERAGE_ZONE_INVALID", f'Array "{array.id}" has no zone "{zone_id}".')
    if channel is not None:
        if not is_pickup_zone(zone):
            raise CoverageError("COVERAGE_CHANNEL_INVALID", f'Exclusion zone "{zone_id}" cannot have an output channel.')
        if not (1 <= channel <= MAX_ZONES_PER_ARRAY):
            raise CoverageError("COVERAGE_CHANNEL_INVALID", f'Output channel {channel} out of range 1..{MAX_ZONES_PER_ARRAY}.')
        if any(z.id != zone_id and z.output_channel == channel for z in array.zones):
            raise CoverageError("COVERAGE_CHANNEL_DUPLICATE", f'Array "{array.id}" already uses output channel {channel}.')
    new = copy.copy(array)
    new.zones = [copy.copy(z) if z.id != zone_id else _with_channel(z, channel) for z in array.zones]
    new.ports = generate_array_output_ports(array.id, array.coverage_mode, pickup_zone_count(new.zones), new.zones)
    return new


def _with_channel(zone: CoverageZone, channel: Optional[int]) -> CoverageZone:
    z = copy.copy(zone)
    z.output_channel = channel
    return z


def set_zone_gain_db(array: MicrophoneArray, zone_id: str, gain_db: Optional[float]) -> MicrophoneArray:
    """Set (or clear, with ``None``) a coverage area's per-area gain trim."""
    zone = next((z for z in array.zones if z.id == zone_id), None)
    if zone is None:
        raise CoverageError("COVERAGE_ZONE_INVALID", f'Array "{array.id}" has no zone "{zone_id}".')
    if gain_db is not None and not (ZONE_GAIN_DB_MIN <= gain_db <= ZONE_GAIN_DB_MAX):
        raise CoverageError("COVERAGE_GAIN_INVALID", f'Zone gain {gain_db} dB out of range [{ZONE_GAIN_DB_MIN}, {ZONE_GAIN_DB_MAX}].')
    new = copy.copy(array)
    new.zones = [copy.copy(z) if z.id != zone_id else _with_gain(z, gain_db) for z in array.zones]
    return new


def _with_gain(zone: CoverageZone, gain_db: Optional[float]) -> CoverageZone:
    z = copy.copy(zone)
    z.gain_db = gain_db
    return z


def auto_assign_zone_channels(array: MicrophoneArray) -> MicrophoneArray:
    """Assign sequential output channels (1, 2, …) to every pickup zone that
    lacks one, preserving any already-assigned channels. Idempotent."""
    used = {z.output_channel for z in array.zones if z.output_channel is not None}
    next_ch = 1
    zones: list[CoverageZone] = []
    for z in array.zones:
        if is_pickup_zone(z) and z.output_channel is None:
            while next_ch in used and next_ch <= MAX_ZONES_PER_ARRAY:
                next_ch += 1
            if next_ch <= MAX_ZONES_PER_ARRAY:
                used.add(next_ch)
                zones.append(_with_channel(z, next_ch))
                continue
        zones.append(copy.copy(z))
    new = copy.copy(array)
    new.zones = zones
    new.ports = generate_array_output_ports(array.id, array.coverage_mode, pickup_zone_count(zones), zones)
    return new


def dynamic_zone(id: str, label: str, shape: ZoneShape) -> CoverageZone:
    return CoverageZone(id=id, type="dynamic", shape=shape, always_on=False, label=label)


def dedicated_zone(id: str, label: str, origin: Point2D, size_meters: float = DEFAULT_DEDICATED_ZONE_SIZE_M) -> CoverageZone:
    return CoverageZone(id=id, type="dedicated", shape=RectShape(origin=origin, width=size_meters, height=size_meters), always_on=True, label=label)


def exclusion_zone(id: str, label: str, shape: ZoneShape) -> CoverageZone:
    return CoverageZone(id=id, type="exclusion", shape=shape, always_on=False, label=label)


def assert_zone_valid(zone: CoverageZone) -> None:
    expected = zone.type == "dedicated"
    if zone.always_on != expected:
        raise CoverageError("COVERAGE_ZONE_INVALID", f'Zone "{zone.id}" ({zone.type}) must have always_on={expected}.')
    _assert_shape_valid(zone.id, zone.shape)


def _assert_shape_valid(zone_id: str, shape: ZoneShape) -> None:
    if isinstance(shape, RectShape):
        if not (shape.width > 0) or not (shape.height > 0):
            raise CoverageError("COVERAGE_ZONE_INVALID", f'Zone "{zone_id}" rect must have positive width and height.')
    else:
        if len(shape.points) < 3:
            raise CoverageError("COVERAGE_ZONE_INVALID", f'Zone "{zone_id}" polygon needs at least 3 vertices (got {len(shape.points)}).')
