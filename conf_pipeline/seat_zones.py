"""Generate microphone coverage zones from furniture seating (pure stdlib).

Sub-feature #2 of the POLARIS table-array coverage workflow. Derives seats from
furniture, clusters them by what the array can physically resolve (reusing the
aperture-aware beamwidth from directivity.py — sub-feature #1), and builds one
pickup zone per cluster. No numpy, no schema change.
"""
from __future__ import annotations

import copy
import math
from dataclasses import dataclass

from .angles import Point3D, steering_angles
from .coverage import add_coverage_zone, dynamic_zone, remove_coverage_zone
from .coverage_sim import DEFAULT_PICKUP_BEAM_HALF_DEG, SEATED_HEAD_M, _device_elev
from .directivity import SIM_SPEECH_FREQ_HZ, separable, steered_beamwidth_deg
from .furniture import furniture_type, resolved_dimensions
from .model import (
    MAX_ZONES_PER_ARRAY,
    CoverageZone,
    MicrophoneArray,
    Point2D,
    RectShape,
    SeatAnchor,
    SystemConfig,
    angular_separation_deg,
)
from .profiles import device_capabilities


def derived_room_seats(config: SystemConfig) -> list[tuple[str, SeatAnchor]]:
    """All ``(seat_id, anchor)`` for the room, deriving seats from seating furniture.

    Explicit ``RoomObject.seats`` win verbatim. Otherwise furniture whose catalog
    ``seat_capacity >= 1`` (chair/seat = 1, sofa = N) yields that many seats: one at
    the object's position for capacity 1, else N spread evenly across its width
    (local +X, rotated by ``rotation_deg``). Other furniture (tables, screens, …)
    yields nothing. Ids are ``"{obj.id}-seat{i}"`` (1-based), matching
    :func:`conf_pipeline.seat_mapper.room_seats`.
    """
    room = config.room
    if room is None:
        return []
    out: list[tuple[str, SeatAnchor]] = []
    for obj in room.objects:
        if obj.seats:
            for i, seat in enumerate(obj.seats, start=1):
                out.append((f"{obj.id}-seat{i}", seat))
            continue
        ft = furniture_type(obj.kind)
        capacity = ft.seat_capacity if ft else 0
        if capacity < 1:
            continue
        width, _depth, _h = resolved_dimensions(obj)
        rad = math.radians(obj.rotation_deg or 0.0)
        cos_r, sin_r = math.cos(rad), math.sin(rad)
        for i in range(capacity):
            frac = (i + 0.5) / capacity
            lx = -width / 2.0 + width * frac           # local +X offset; ly = 0 (centre depth)
            px = obj.position.x + lx * cos_r            # obb_corners rotation: x' = lx*cos + ly*sin
            py = obj.position.y - lx * sin_r            #                       y' = -lx*sin + ly*cos
            out.append((f"{obj.id}-seat{i + 1}", SeatAnchor(position=Point2D(px, py), facing_deg=obj.rotation_deg)))
    return out


@dataclass
class SeatLook:
    seat_id: str
    azimuth_deg: float   # room-frame bearing of the seat from the array (0° = +Y, CW)
    half_deg: float      # array's steered 3 dB half-beamwidth toward this seat


def cluster_seats(
    looks: list[SeatLook], *, max_zones: int = MAX_ZONES_PER_ARRAY, factor: float = 1.5
) -> tuple[list[list[str]], bool]:
    """Group seats the array cannot resolve apart.

    Sorts by azimuth and merges each seat into the running group when it is **not**
    ``separable`` from the previous seat (``angular_separation_deg`` vs the larger of
    the two half-beamwidths). If more than ``max_zones`` groups remain, repeatedly
    merges the closest-in-azimuth adjacent pair until ``max_zones`` (``forced_merge``
    True). Returns ``(groups_of_seat_ids, forced_merge)`` in azimuth order.
    """
    if not looks:
        return [], False
    ordered = sorted(looks, key=lambda L: L.azimuth_deg)
    groups: list[list[SeatLook]] = [[ordered[0]]]
    for cur in ordered[1:]:
        prev = groups[-1][-1]
        sep = angular_separation_deg(prev.azimuth_deg, cur.azimuth_deg)
        if separable(sep, max(prev.half_deg, cur.half_deg), factor):
            groups.append([cur])
        else:
            groups[-1].append(cur)

    forced = False
    while len(groups) > max_zones:
        forced = True
        # merge the adjacent pair with the smallest azimuth gap (group reps = last/first members)
        best_i, best_gap = 0, float("inf")
        for i in range(len(groups) - 1):
            gap = angular_separation_deg(groups[i][-1].azimuth_deg, groups[i + 1][0].azimuth_deg)
            if gap < best_gap:
                best_gap, best_i = gap, i
        groups[best_i] = groups[best_i] + groups[best_i + 1]
        del groups[best_i + 1]

    return [[L.seat_id for L in g] for g in groups], forced


# ---------------------------------------------------------------------------
# Task 3: zone generation
# ---------------------------------------------------------------------------

_ZONE_MARGIN_M = 0.35   # capture radius padding around the seat point(s)
_ZONE_MIN_SIDE_M = 0.6  # smallest zone side


@dataclass
class SeatZoneResult:
    config: SystemConfig
    created: list[str]        # generated zone labels
    merged: list[str]         # human-readable "merged because …" notes
    warnings: list[str]


def _owned_seats(
    config: SystemConfig,
    array_id: str,
    seats: list[tuple[str, SeatAnchor]],
) -> list[tuple[str, SeatAnchor]]:
    """The subset of ``seats`` whose nearest microphone array (Euclidean) is
    ``array_id`` — mirrors :func:`conf_pipeline.seat_mapper.seats_owned_by_array`
    but over derived seats. Ties go to the lowest array id. Arrays without a
    position are ignored as owners."""
    arrays = [
        d for d in config.devices
        if isinstance(d, MicrophoneArray) and d.position is not None
    ]
    if not arrays:
        return []
    out: list[tuple[str, SeatAnchor]] = []
    for sid, anchor in seats:
        best_id: str | None = None
        best_d = float("inf")
        for a in sorted(arrays, key=lambda a: a.id):
            assert a.position is not None  # filtered above
            d = math.hypot(
                a.position.x - anchor.position.x,
                a.position.y - anchor.position.y,
            )
            if d < best_d - 1e-9:
                best_d, best_id = d, a.id
        if best_id == array_id:
            out.append((sid, anchor))
    return out


def _zone_for_group(
    array_id: str,
    n: int,
    points: list[Point2D],
    seat_ids: list[str],
) -> CoverageZone:
    xs = [p.x for p in points]
    ys = [p.y for p in points]
    x0, x1 = min(xs) - _ZONE_MARGIN_M, max(xs) + _ZONE_MARGIN_M
    y0, y1 = min(ys) - _ZONE_MARGIN_M, max(ys) + _ZONE_MARGIN_M
    w = max(x1 - x0, _ZONE_MIN_SIDE_M)
    h = max(y1 - y0, _ZONE_MIN_SIDE_M)
    cx, cy = (x0 + x1) / 2.0, (y0 + y1) / 2.0
    shape = RectShape(origin=Point2D(cx - w / 2.0, cy - h / 2.0), width=w, height=h)
    label = (
        f"Seat {seat_ids[0].split('-seat')[-1]}"
        if len(seat_ids) == 1
        else f"Seats ({len(seat_ids)})"
    )
    return dynamic_zone(id=f"{array_id}-z{n}", label=label, shape=shape)


def generate_seat_zones(config: SystemConfig, array_id: str) -> SeatZoneResult:
    """Replace ``array_id``'s coverage zones with zones derived from room seating,
    clustered by what the array can physically resolve (sub-feature #1 beamwidth)."""
    array = next((d for d in config.devices if d.id == array_id), None)
    if not isinstance(array, MicrophoneArray):
        raise ValueError(f"No microphone array {array_id!r} in config")

    all_seats = derived_room_seats(config)
    warnings: list[str] = []

    if array.position is None:
        # no pose → can't compute nearest-array ownership or looks → per-seat, no merge
        owned = all_seats
        if not owned:
            return SeatZoneResult(
                config,
                [],
                [],
                ["No seats found for this array — place chairs/sofas or set seat anchors."],
            )
        anchors = {sid: anchor for sid, anchor in owned}
        seat_ids = [sid for sid, _ in owned]
        groups: list[list[str]] = [[sid] for sid in seat_ids][:8]
        if len(seat_ids) > 8:
            warnings.append(
                "More than 8 seats and no array position — only the first 8 got zones."
            )
        warnings.append("Set the array position for separability-aware grouping.")
        forced = False
    else:
        owned = _owned_seats(config, array_id, all_seats)
        if not owned:
            return SeatZoneResult(
                config,
                [],
                [],
                ["No seats found for this array — place chairs/sofas or set seat anchors."],
            )
        anchors = {sid: anchor for sid, anchor in owned}
        cap = device_capabilities(array)
        src = Point3D(array.position.x, array.position.y, _device_elev(config, array))
        looks: list[SeatLook] = []
        for sid, anchor in owned:
            sa = steering_angles(
                src, Point3D(anchor.position.x, anchor.position.y, SEATED_HEAD_M)
            )
            half = (
                steered_beamwidth_deg(cap.aperture_m, SIM_SPEECH_FREQ_HZ, sa.downtilt_deg)
                if cap.aperture_m is not None
                else DEFAULT_PICKUP_BEAM_HALF_DEG
            )
            looks.append(SeatLook(sid, sa.azimuth_deg, half))
        groups, forced = cluster_seats(looks)

    # build zones; replace-all on the array
    new_array = array
    for zid in [z.id for z in array.zones]:
        new_array = remove_coverage_zone(new_array, zid)
    created: list[str] = []
    merged: list[str] = []
    for n, group in enumerate(groups, start=1):
        pts = [anchors[sid].position for sid in group]
        zone = _zone_for_group(array_id, n, pts, group)
        new_array = add_coverage_zone(new_array, zone)
        created.append(zone.label)
        if len(group) > 1:
            merged.append(
                f"{zone.label}: merged {len(group)} seats this array cannot resolve apart."
            )
    if forced:
        warnings.append(
            "More seats than this array can address — merged the closest into shared zones."
        )

    new_devices = [new_array if d.id == array_id else d for d in config.devices]
    new_config = copy.copy(config)
    new_config.devices = new_devices
    return SeatZoneResult(new_config, created, merged, warnings)
