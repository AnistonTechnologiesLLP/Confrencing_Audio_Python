"""Translate engine *coverage zones* into beamformer *look directions* (stdlib).

This is the bridge between the planning model and the DSP: the areas you draw in
the app (Records / dedicated = **pickup**, No-pickup = **exclusion**) become unit
direction vectors in the array's local frame, which the beamformer steers toward
(pickup) or nulls (exclusion). Geometry reuses :func:`conf_pipeline.steering_angles`
so a zone's bearing here is identical to the steering rays drawn on the canvas.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from conf_pipeline import (
    CoverageZone,
    MicrophoneArray,
    Point2D,
    Point3D,
    PolygonShape,
    RectShape,
    SystemConfig,
    default_elevation,
    find_device,
    is_pickup_zone,
    steering_angles,
)

from .model import DEFAULT_TARGET_ELEVATION_M


@dataclass(frozen=True)
class Direction:
    """A look direction in the array's local frame.

    ``unit`` is the unit vector pointing **from the array toward the area**
    (``z`` up, so a floor target has ``uz < 0``). ``azimuth_deg`` /
    ``off_nadir_deg`` mirror :class:`conf_pipeline.SteeringAngles`
    (azimuth: compass bearing from +Y clockwise; off-nadir: 0° = straight down).
    """

    unit: tuple[float, float, float]
    azimuth_deg: float
    off_nadir_deg: float
    distance_m: float
    label: str = ""


def zone_centroid(zone: CoverageZone) -> Point2D:
    """A representative interior point (floor plane) of a zone."""
    shape = zone.shape
    if isinstance(shape, RectShape):
        return Point2D(shape.origin.x + shape.width / 2.0, shape.origin.y + shape.height / 2.0)
    pts = shape.points if isinstance(shape, PolygonShape) else []
    if not pts:
        return Point2D(0.0, 0.0)
    return Point2D(sum(p.x for p in pts) / len(pts), sum(p.y for p in pts) / len(pts))


def _array(config: SystemConfig, array_id: str) -> MicrophoneArray:
    dev = find_device(config, array_id)
    if dev is None or not isinstance(dev, MicrophoneArray):
        raise ValueError(f"{array_id!r} is not a microphone array in this config")
    if dev.position is None:
        raise ValueError(f"array {array_id!r} has no position; place it in the room first")
    return dev


def look_direction(
    config: SystemConfig,
    array_id: str,
    target: Point2D,
    *,
    target_elevation_m: float = DEFAULT_TARGET_ELEVATION_M,
    label: str = "",
) -> Direction:
    """Look direction from the array (at its mount height) to a floor ``target``."""
    arr = _array(config, array_id)
    room_h = config.room.height if config.room is not None else 3.0
    mount = arr.elevation if arr.elevation is not None else default_elevation(arr, room_h)
    assert arr.position is not None  # narrowed by _array
    src = Point3D(arr.position.x, arr.position.y, mount)
    dst = Point3D(target.x, target.y, target_elevation_m)
    sa = steering_angles(src, dst)

    az = math.radians(sa.azimuth_deg)
    nadir = math.radians(sa.off_nadir_deg)
    sin_n = math.sin(nadir)
    unit = (sin_n * math.sin(az), sin_n * math.cos(az), -math.cos(nadir))
    return Direction(
        unit=unit,
        azimuth_deg=sa.azimuth_deg,
        off_nadir_deg=sa.off_nadir_deg,
        distance_m=sa.distance,
        label=label,
    )


def zone_look_direction(
    config: SystemConfig,
    array_id: str,
    zone: CoverageZone,
    *,
    target_elevation_m: float = DEFAULT_TARGET_ELEVATION_M,
) -> Direction:
    return look_direction(
        config, array_id, zone_centroid(zone), target_elevation_m=target_elevation_m, label=zone.label
    )


def pickup_directions(
    config: SystemConfig, array_id: str, *, target_elevation_m: float = DEFAULT_TARGET_ELEVATION_M
) -> list[tuple[CoverageZone, Direction]]:
    """One look direction per pickup zone (dynamic/dedicated) on the array."""
    arr = _array(config, array_id)
    return [
        (z, zone_look_direction(config, array_id, z, target_elevation_m=target_elevation_m))
        for z in arr.zones
        if is_pickup_zone(z)
    ]


def exclusion_directions(
    config: SystemConfig, array_id: str, *, target_elevation_m: float = DEFAULT_TARGET_ELEVATION_M
) -> list[tuple[CoverageZone, Direction]]:
    """One null direction per exclusion (No-pickup) zone on the array."""
    arr = _array(config, array_id)
    return [
        (z, zone_look_direction(config, array_id, z, target_elevation_m=target_elevation_m))
        for z in arr.zones
        if not is_pickup_zone(z)
    ]
