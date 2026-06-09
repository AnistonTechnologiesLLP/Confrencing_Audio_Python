"""Geometric coverage check: array pickup circles + a covered/uncovered/overlap report.

A planning aid mirroring Designer's on-floor coverage areas — distinct from the
hand-drawn coverage *zones* in :mod:`conf_pipeline.coverage`. An array's coverage
is modelled as a circle on the floor whose radius is ``(mount-height − target-height)
× tan(half cone angle)``, with the cone angle taken from the device profile.

Pure stdlib (``math`` only).
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Optional

from .model import (
    DEFAULT_TALKER_ELEVATION_M,
    Point2D,
    SystemConfig,
    default_elevation,
    point_in_shape,
)
from .profiles import device_capabilities


def array_coverage_radius(mount_height: float, target_height: float, coverage_angle_deg: Optional[float]) -> float:
    """Floor radius an array covers: ``max(mount-target,0) · tan(angle/2)``.

    Returns 0 for a missing/degenerate angle or when the target is at/above the
    mount (rather than raising)."""
    if coverage_angle_deg is None or not (0.0 < coverage_angle_deg < 180.0):
        return 0.0
    drop = max(mount_height - target_height, 0.0)
    return drop * math.tan(math.radians(coverage_angle_deg / 2.0))


def _mount_height(config: SystemConfig, array) -> float:
    if array.elevation is not None:
        return array.elevation
    room_height = config.room.height if config.room is not None else 3.0
    return default_elevation(array, room_height)


def array_coverage_circle(
    config: SystemConfig, array_id: str, target_height: float = DEFAULT_TALKER_ELEVATION_M
) -> Optional[tuple[Point2D, float]]:
    """``(center, radius)`` of an array's floor coverage, or ``None`` when the
    array is unplaced or its profile defines no coverage angle."""
    array = next((d for d in config.devices if d.id == array_id and d.type == "microphoneArray"), None)
    if array is None or array.position is None:
        return None
    angle = device_capabilities(array).coverage_angle_deg
    radius = array_coverage_radius(_mount_height(config, array), target_height, angle)
    if radius <= 0:
        return None
    return array.position, radius


@dataclass
class CoverageReport:
    covered: list[str] = field(default_factory=list)      # talker ids inside >=1 circle, not excluded
    uncovered: list[str] = field(default_factory=list)
    overlaps: list[tuple[str, str]] = field(default_factory=list)  # array-id pairs whose circles intersect


def _in_exclusion(array, point: Point2D) -> bool:
    return any(z.type == "exclusion" and point_in_shape(point, z.shape) for z in array.zones)


def coverage_report(config: SystemConfig, target_height: float = DEFAULT_TALKER_ELEVATION_M) -> CoverageReport:
    """Which talkers fall inside an array's coverage circle (and not in that
    array's exclusion zone), plus which array circles overlap."""
    arrays = [d for d in config.devices if d.type == "microphoneArray" and d.position is not None]
    circles: dict[str, tuple[Point2D, float]] = {}
    for a in arrays:
        circ = array_coverage_circle(config, a.id, target_height)
        if circ is not None:
            circles[a.id] = circ

    rep = CoverageReport()
    for t in config.talkers:
        covered = False
        for a in arrays:
            circ = circles.get(a.id)
            if circ is None:
                continue
            center, radius = circ
            if math.hypot(t.position.x - center.x, t.position.y - center.y) <= radius and not _in_exclusion(a, t.position):
                covered = True
                break
        (rep.covered if covered else rep.uncovered).append(t.id)

    ids = list(circles.keys())
    for i in range(len(ids)):
        for j in range(i + 1, len(ids)):
            (c1, r1), (c2, r2) = circles[ids[i]], circles[ids[j]]
            if math.hypot(c1.x - c2.x, c1.y - c2.y) < r1 + r2:
                rep.overlaps.append((ids[i], ids[j]))
    return rep
