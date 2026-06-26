"""Generate microphone coverage zones from furniture seating (pure stdlib).

Sub-feature #2 of the POLARIS table-array coverage workflow. Derives seats from
furniture, clusters them by what the array can physically resolve (reusing the
aperture-aware beamwidth from directivity.py — sub-feature #1), and builds one
pickup zone per cluster. No numpy, no schema change.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from .furniture import furniture_type, resolved_dimensions
from .model import Point2D, SeatAnchor, SystemConfig


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
