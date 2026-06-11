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
    CoverageZone,
    Point2D,
    RectShape,
    SystemConfig,
    default_elevation,
    is_pickup_zone,
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


# --------------------------------------------------------------------------- #
# Zone-vs-coverage report (v1.12.0) — closer to Designer than the circle report:
# instead of "do array circles overlap", it answers "is each *drawn coverage area*
# actually inside a mic's usable pickup, and is any area covered by 2+ arrays
# (lobe contention)?".
# --------------------------------------------------------------------------- #
def _zone_centroid(zone: CoverageZone) -> Point2D:
    if isinstance(zone.shape, RectShape):
        s = zone.shape
        return Point2D(s.origin.x + s.width / 2.0, s.origin.y + s.height / 2.0)
    pts = zone.shape.points
    return Point2D(sum(p.x for p in pts) / len(pts), sum(p.y for p in pts) / len(pts))


def _zone_corners(zone: CoverageZone) -> list[Point2D]:
    if isinstance(zone.shape, RectShape):
        s = zone.shape
        return [
            Point2D(s.origin.x, s.origin.y),
            Point2D(s.origin.x + s.width, s.origin.y),
            Point2D(s.origin.x + s.width, s.origin.y + s.height),
            Point2D(s.origin.x, s.origin.y + s.height),
        ]
    return list(zone.shape.points)


@dataclass
class ZoneCoverageStatus:
    array_id: str
    zone_id: str
    zone_label: str
    fully_covered: bool          # every corner inside the covering array's circle
    centroid_covered: bool       # centroid inside its own array's circle
    covering_arrays: list[str] = field(default_factory=list)  # arrays (incl. own) whose circle holds the centroid
    contended: bool = False      # centroid covered by >1 array → automix lobe contention


@dataclass
class ZoneCoverageReport:
    zones: list[ZoneCoverageStatus] = field(default_factory=list)

    @property
    def uncovered(self) -> list["ZoneCoverageStatus"]:
        return [z for z in self.zones if not z.centroid_covered]

    @property
    def partial(self) -> list["ZoneCoverageStatus"]:
        return [z for z in self.zones if z.centroid_covered and not z.fully_covered]

    @property
    def contended(self) -> list["ZoneCoverageStatus"]:
        return [z for z in self.zones if z.contended]


def _circle_holds(circle: Optional[tuple[Point2D, float]], p: Point2D) -> bool:
    if circle is None:
        return False
    center, radius = circle
    return math.hypot(p.x - center.x, p.y - center.y) <= radius


def zone_coverage_report(config: SystemConfig, target_height: float = DEFAULT_TALKER_ELEVATION_M) -> ZoneCoverageReport:
    """For each pickup coverage area on each placed array, report whether it falls
    inside that array's floor coverage circle (centroid + every corner), and which
    arrays cover its centroid (more than one ⇒ lobe contention)."""
    arrays = [d for d in config.devices if d.type == "microphoneArray" and d.position is not None]
    circles: dict[str, Optional[tuple[Point2D, float]]] = {
        a.id: array_coverage_circle(config, a.id, target_height) for a in arrays
    }
    rep = ZoneCoverageReport()
    for a in arrays:
        own = circles.get(a.id)
        for zone in a.zones:  # type: ignore[attr-defined]
            if not is_pickup_zone(zone):
                continue
            centroid = _zone_centroid(zone)
            centroid_covered = _circle_holds(own, centroid)
            fully = all(_circle_holds(own, c) for c in _zone_corners(zone))
            covering = [oid for oid, circ in circles.items() if _circle_holds(circ, centroid)]
            rep.zones.append(ZoneCoverageStatus(
                array_id=a.id, zone_id=zone.id, zone_label=zone.label,
                fully_covered=centroid_covered and fully,
                centroid_covered=centroid_covered,
                covering_arrays=covering,
                contended=len(covering) > 1,
            ))
    return rep
