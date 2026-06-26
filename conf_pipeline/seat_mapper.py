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
from typing import Iterable, Optional, Tuple

from .model import (
    MicrophoneArray,
    Point2D,
    SeatAnchor,
    SystemConfig,
    _norm_bearing,
    angular_separation_deg,
    bearing_to_deg,
    is_pickup_zone,
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


def seats_owned_by_array(
    config: SystemConfig,
    array_id: str,
    *,
    arrays: Optional[Iterable[MicrophoneArray]] = None,
) -> list[str]:
    """Room seats whose **nearest** microphone array (by Euclidean distance) is ``array_id``.

    In a multi-array room each seat is "owned" by the array physically closest to it (= best SNR), so two
    arrays don't both capture the same talker when their pickups are summed. Distance, **not** bearing.
    ``arrays`` defaults to every placed (``position`` set) :class:`MicrophoneArray` in the config. Ties go
    to the lowest array id (deterministic). Returns ``[]`` if ``array_id`` is unknown / has no position, or
    there are no seats — in which case the caller cannot enforce ownership and must fall back to best-effort.
    """
    candidates = arrays if arrays is not None else (
        d for d in config.devices if isinstance(d, MicrophoneArray))
    placed = [(a.id, a.position) for a in candidates if a.position is not None]   # (id, Point2D), narrowed
    if not any(aid == array_id for aid, _ in placed):
        return []
    owned: list[str] = []
    for seat_id, anchor in room_seats(config):
        sx, sy = anchor.position.x, anchor.position.y
        nearest_id = min(placed, key=lambda p: (math.hypot(sx - p[1].x, sy - p[1].y), p[0]))[0]
        if nearest_id == array_id:
            owned.append(seat_id)
    return owned


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


def learn_bearing(array_pos: Point2D, ref_point: Point2D, measured_az_deg: float) -> float:
    """Infer the array's ``bearing_deg`` (0°=+Y, CW) from a DOA measurement.

    A reference at ``ref_point`` is observed at ``measured_az_deg`` in the array's
    DOA / steering frame. Since ``measured_az = bearing_to_deg(array, ref) − bearing``
    (see :func:`_array_relative_azimuth`), the array bearing is the inverse::

        bearing = bearing_to_deg(array_pos, ref_point) − measured_az_deg   (mod 360)

    Pure geometry — no hardware. The caller supplies ``measured_az_deg`` from a live
    DOA capture (e.g. the calibrate-front worker)."""
    return _norm_bearing(bearing_to_deg(array_pos, ref_point) - measured_az_deg)


# --------------------------------------------------------------------------- #
# Zone geometry — exclusion (door) nulls + "is this detection inside a pickup zone?"
# These feed the live "cut the door / anyone outside the pickup area" behaviour in the auto-follow modes.
# --------------------------------------------------------------------------- #
def _posed_array(config: SystemConfig, array_id: str) -> Optional[Tuple[MicrophoneArray, Point2D, float]]:
    """The placed, bearing'd ``MicrophoneArray`` + its (position, bearing_deg), or ``None`` (unknown / not
    an array / unposed). Narrows the two Optionals for the callers."""
    array = next((d for d in config.devices if d.id == array_id), None)
    if not isinstance(array, MicrophoneArray) or array.position is None or array.bearing_deg is None:
        return None
    return array, array.position, array.bearing_deg


def _shape_corners(shape: object) -> list:
    """The corner points of a zone shape — a polygon's points, or a rect's four corners."""
    pts = getattr(shape, "points", None)
    if pts:
        return list(pts)
    o = getattr(shape, "origin", None)
    if o is None:
        return []
    w, h = float(getattr(shape, "width", 0.0)), float(getattr(shape, "height", 0.0))
    return [o, Point2D(o.x + w, o.y), Point2D(o.x + w, o.y + h), Point2D(o.x, o.y + h)]


def _centroid(pts: list) -> Point2D:
    n = max(1, len(pts))
    return Point2D(sum(p.x for p in pts) / n, sum(p.y for p in pts) / n)


def exclusion_zone_azimuths(config: SystemConfig, array_id: str) -> list:
    """The **array-relative** azimuths (deg, ``0°`` = +Y clockwise — the steered-beam / DOA frame) of the
    CENTRES of this array's no-pickup (exclusion) zones — e.g. a door drawn as a No-pickup area — for
    pushing to the steered beam as nulls (see
    :func:`conf_pipeline_control.polaris_beamformer.compose_nulls`). Empty if the array is unknown, unposed
    (no ``position`` / ``bearing_deg``), or has no exclusion zones."""
    posed = _posed_array(config, array_id)
    if posed is None:
        return []
    array, pos, bear = posed
    return [_array_relative_azimuth(pos, bear, _centroid(_shape_corners(z.shape)))
            for z in array.zones if not is_pickup_zone(z)]


def azimuth_in_pickup_zone(config: SystemConfig, array_id: str, azimuth_deg: float,
                           *, margin_deg: float = 8.0) -> bool:
    """True if a detected **array-relative** azimuth points into ANY pickup zone of the array. Each zone is
    treated as the angular sector its corners subtend from the array, widened by ``margin_deg`` (DOA jitter
    / hysteresis). False if the array is unknown / unposed or has no pickup zones — the caller decides what
    "no pickup zones" means (typically: capture everything)."""
    posed = _posed_array(config, array_id)
    if posed is None:
        return False
    array, pos, bear = posed
    az = float(azimuth_deg)
    for z in array.zones:
        if not is_pickup_zone(z):
            continue
        corners = _shape_corners(z.shape)
        if not corners:
            continue
        center = _array_relative_azimuth(pos, bear, _centroid(corners))
        half = max((angular_separation_deg(center, _array_relative_azimuth(pos, bear, c)) for c in corners),
                   default=0.0)
        if angular_separation_deg(az, center) <= half + margin_deg:
            return True
    return False


def active_zone_gain_db(
    config: SystemConfig,
    array_id: str,
    azimuth_deg: float,
    *,
    margin_deg: float = 8.0,
) -> Optional[float]:
    """The ``gain_db`` of the pickup zone the given **array-relative** azimuth points into, or ``None``.

    Uses the SAME angular-sector containment test as :func:`azimuth_in_pickup_zone`: each pickup zone
    is treated as the sector its corners subtend from the array, widened by ``margin_deg`` (DOA jitter
    / hysteresis). Returns the first matching zone's ``gain_db`` — which may itself be ``None`` if no
    gain was set. Returns ``None`` if the array is unknown / unposed or no pickup zone matches.

    Intended use: look up the zone-level trim (dB) so the live beam can apply it post-AGC.
    """
    posed = _posed_array(config, array_id)
    if posed is None:
        return None
    array, pos, bear = posed
    az = float(azimuth_deg)
    for z in array.zones:
        if not is_pickup_zone(z):
            continue
        corners = _shape_corners(z.shape)
        if not corners:
            continue
        center = _array_relative_azimuth(pos, bear, _centroid(corners))
        half = max(
            (angular_separation_deg(center, _array_relative_azimuth(pos, bear, c)) for c in corners),
            default=0.0,
        )
        if angular_separation_deg(az, center) <= half + margin_deg:
            return z.gain_db
    return None
