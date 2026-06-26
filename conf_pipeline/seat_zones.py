"""Generate microphone coverage zones from furniture seating (pure stdlib).

Sub-feature #2 of the POLARIS table-array coverage workflow. Derives seats from
furniture, clusters them by what the array can physically resolve (reusing the
aperture-aware beamwidth from directivity.py — sub-feature #1), and builds one
pickup zone per cluster. No numpy, no schema change.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from .directivity import separable
from .furniture import furniture_type, resolved_dimensions
from .model import MAX_ZONES_PER_ARRAY, Point2D, SeatAnchor, SystemConfig, angular_separation_deg


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
