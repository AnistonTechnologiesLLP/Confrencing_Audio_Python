"""Room-aware seat mapping — turn a detected array-relative azimuth into the nearest room seat.

A pure-stdlib (no numpy) geometry layer that composes *over* the DOA, so it works unchanged with
any beam mode (delaysum / fracdelay / superdirective / mvdr). Given a detected azimuth in the
array's own frame (``0°`` = +Y, clockwise — exactly what
:attr:`conf_pipeline_control.polaris_beamformer.PolarisBeamformer.current_doa_deg` reports), the
array's room pose (``position`` + the v5 ``bearing_deg`` mounting heading), and the room's seats,
it returns the seat whose room-bearing-from-the-array is angularly closest to the detected
direction.

The only new piece of math is rotating the array-relative azimuth into room coordinates by the
array's mounting bearing (``room_az = azimuth_deg + bearing_deg``); everything else reuses the
engine's existing bearing helpers in :mod:`conf_pipeline.model`. Seats carry no explicit id, so
ids are synthesized as ``"{furniture_id}-seat{index}"`` with a **1-based** index — byte-identical to
:func:`conf_pipeline.coverage_sim.room_targets`, so a matched seat id correlates directly with a
coverage-simulation target.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Iterable, Optional

from .model import (
    MicrophoneArray,
    Point2D,
    SeatAnchor,
    SystemConfig,
    _norm_bearing,
    angular_separation_deg,
    bearing_to_deg,
)

# Beyond this angular gap the detected direction is treated as "between seats" → no match. The
# small POLARIS array resolves direction far more coarsely than this, so the gate is generous.
DEFAULT_MAX_SEPARATION_DEG = 30.0


@dataclass(frozen=True)
class SeatMatch:
    """The seat a detected direction maps to."""

    seat_id: str               # "{furniture_id}-seat{index}" (matches coverage_sim.room_targets)
    anchor: SeatAnchor         # the matched seat (world position + optional facing)
    separation_deg: float      # angular gap between the look bearing and the seat bearing (0..180)
    distance_m: float          # array → seat distance, metres


def room_seats(config: SystemConfig) -> list[tuple[str, SeatAnchor]]:
    """All ``(seat_id, anchor)`` in the config's room (empty if there is no room / no seats).

    Ids are synthesized as ``"{furniture_id}-seat{index}"`` — identical to
    :func:`conf_pipeline.coverage_sim.room_targets`."""
    room = config.room
    if room is None:
        return []
    out: list[tuple[str, SeatAnchor]] = []
    for obj in room.objects:
        for i, seat in enumerate(obj.seats or [], start=1):   # 1-based, exactly like room_targets
            out.append((f"{obj.id}-seat{i}", seat))
    return out


def nearest_seat(
    azimuth_deg: float,
    array_position: Point2D,
    array_bearing_deg: float,
    seats: Iterable[tuple[str, SeatAnchor]],
    *,
    max_separation_deg: float = DEFAULT_MAX_SEPARATION_DEG,
) -> Optional[SeatMatch]:
    """Nearest seat to a detected **array-relative** azimuth.

    ``azimuth_deg`` is in the array's own frame (``0°`` = +Y, clockwise); it is rotated into room
    coordinates by the array's mounting bearing (``room_az = azimuth_deg + array_bearing_deg``).
    Each candidate seat's room bearing from the array is
    ``bearing_to_deg(array_position, seat.position)``; the seat minimising the angular gap wins.
    Returns ``None`` if there are no seats, or the best gap exceeds ``max_separation_deg`` (the
    talker is between seats / outside the seated area). Distance is reported but does not affect the
    choice — a planar DOA resolves direction, not range.
    """
    room_az = _norm_bearing(azimuth_deg + array_bearing_deg)
    best: Optional[SeatMatch] = None
    for seat_id, anchor in seats:
        sep = angular_separation_deg(room_az, bearing_to_deg(array_position, anchor.position))
        if best is None or sep < best.separation_deg:
            dist = math.hypot(anchor.position.x - array_position.x, anchor.position.y - array_position.y)
            best = SeatMatch(seat_id, anchor, sep, dist)
    if best is None or best.separation_deg > max_separation_deg:
        return None
    return best


def nearest_seat_for_array(
    config: SystemConfig,
    array_id: str,
    azimuth_deg: float,
    *,
    max_separation_deg: float = DEFAULT_MAX_SEPARATION_DEG,
) -> Optional[SeatMatch]:
    """Config-level :func:`nearest_seat`: resolve the array's pose + the room's seats from ``config``.

    Returns ``None`` if ``array_id`` is unknown or not a microphone array, the array has no
    ``position`` or no ``bearing_deg`` (orientation unknown — set it with
    :func:`conf_pipeline.api.set_array_bearing`), or no seat is within ``max_separation_deg``.
    """
    array = next((d for d in config.devices if d.id == array_id), None)
    if not isinstance(array, MicrophoneArray):
        return None
    if array.position is None or array.bearing_deg is None:
        return None
    return nearest_seat(
        azimuth_deg, array.position, array.bearing_deg, room_seats(config),
        max_separation_deg=max_separation_deg,
    )


def _array_relative_azimuth(array_position: Point2D, array_bearing_deg: float, target: Point2D) -> float:
    """A room point's azimuth in the **array** frame (``0°`` = +Y clockwise — the DOA / ``set_steering`` /
    ``set_nulls`` frame): the inverse of the mapper's room rotation, ``azimuth =
    bearing_to_deg(array, target) − array_bearing_deg``."""
    return _norm_bearing(bearing_to_deg(array_position, target) - array_bearing_deg)


def seat_null_azimuths(
    config: SystemConfig,
    array_id: str,
    *,
    exclude_seat_id: Optional[str] = None,
) -> list[float]:
    """**Array-relative** azimuths (deg, ``0°`` = +Y clockwise — the DOA / ``set_nulls`` frame) of every
    room seat except ``exclude_seat_id`` — for nulling the non-target ("empty") seats while the beam
    listens to the matched one (see :func:`_array_relative_azimuth`).

    Returns ``[]`` if ``array_id`` is unknown / not a microphone array / has no ``position`` or
    ``bearing_deg``, or there are no other seats. De-duplication and the M−1 budget are the caller's
    concern (the steered beam's null-budget composer) — this only enumerates the bearings, in
    :func:`room_seats` order.
    """
    array = next((d for d in config.devices if d.id == array_id), None)
    if not isinstance(array, MicrophoneArray):
        return []
    if array.position is None or array.bearing_deg is None:
        return []
    return [
        _array_relative_azimuth(array.position, array.bearing_deg, anchor.position)
        for seat_id, anchor in room_seats(config)
        if seat_id != exclude_seat_id
    ]


def seat_azimuth_for_array(config: SystemConfig, array_id: str, seat_id: str) -> Optional[float]:
    """The **array-relative** azimuth (deg, ``0°`` = +Y clockwise — the ``set_steering`` / DOA frame) of
    a SPECIFIC room seat, for pinning the steered beam to that seat ("lock to seat"). The inverse of the
    mapper's room rotation (see :func:`_array_relative_azimuth`).

    Returns ``None`` if ``array_id`` is unknown / not a microphone array / has no ``position`` or
    ``bearing_deg``, or ``seat_id`` is not a seat in the room.
    """
    array = next((d for d in config.devices if d.id == array_id), None)
    if not isinstance(array, MicrophoneArray):
        return None
    if array.position is None or array.bearing_deg is None:
        return None
    for sid, anchor in room_seats(config):
        if sid == seat_id:
            return _array_relative_azimuth(array.position, array.bearing_deg, anchor.position)
    return None


def azimuth_for_array_point(config: SystemConfig, array_id: str, point: Point2D) -> Optional[float]:
    """The **array-relative** azimuth (deg, ``0°`` = +Y clockwise — the ``set_steering`` / DOA frame) of an
    ARBITRARY room point, for pinning the steered beam to a clicked spot ("lock to place"). The same
    rotation as :func:`seat_azimuth_for_array`, but for any point rather than a predefined seat.

    Returns ``None`` if ``array_id`` is unknown / not a microphone array / has no ``position`` or
    ``bearing_deg``.
    """
    array = next((d for d in config.devices if d.id == array_id), None)
    if not isinstance(array, MicrophoneArray):
        return None
    if array.position is None or array.bearing_deg is None:
        return None
    return _array_relative_azimuth(array.position, array.bearing_deg, point)
