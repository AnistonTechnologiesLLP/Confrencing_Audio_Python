"""Heuristic acoustic scoring for placement simulation (pure ``math`` — no numpy).

Every objective is derived from geometry that the package already computes via
:func:`conf_pipeline.angles.steering_angles` (distance, off-nadir, azimuth) plus
the room volume. The array is modelled as a *point* with a downward (nadir)
reference axis, optionally tilted by a steer direction — so **no per-element mic
geometry is needed**. The four objectives are:

1. ``snr``      direct-path level: inverse-distance spreading + main-lobe rolloff
2. ``drr``      direct-to-reverberant ratio from a Sabine RT60 / critical distance
3. ``coverage`` gaussian main-lobe weight gated by pickup / exclusion zones
4. ``fairness`` an *aggregate* over per-talker quality (balances all talkers)

Sub-scores are normalised to ``0..1`` and blended by the weights in
:class:`SimParams`.
"""
from __future__ import annotations

import math
from typing import Optional

from ..angles import Point3D, SteeringAngles, steering_angles
from ..model import (
    MicrophoneArray,
    Point2D,
    SystemConfig,
    default_elevation,
    point_in_shape,
)
from .types import Candidate, PlacementScore, SimParams


# --------------------------------------------------------------------------- #
# small helpers
# --------------------------------------------------------------------------- #
def _clamp01(x: float) -> float:
    return 0.0 if x < 0.0 else 1.0 if x > 1.0 else x


def _norm(value: float, window: tuple[float, float]) -> float:
    lo, hi = window
    return _clamp01((value - lo) / (hi - lo)) if hi > lo else 0.0


def _unit_from_nadir(off_nadir_deg: float, az_deg: float) -> tuple[float, float, float]:
    """Unit vector pointing from the array toward a ray at ``(off_nadir, az)``.

    Matches the azimuth convention of :func:`steering_angles` (``+Y = 0 deg``,
    ``+X = 90 deg``); ``off_nadir = 0`` is straight down (``z = -1``).
    """
    onr = math.radians(off_nadir_deg)
    azr = math.radians(az_deg)
    s = math.sin(onr)
    return (s * math.sin(azr), s * math.cos(azr), -math.cos(onr))


def off_axis_deg(ang: SteeringAngles, steer_off_nadir_deg: float, steer_az_deg: float) -> float:
    """Angle between the array→talker ray and the steer ray (both from the array).

    With the default (un-steered, ``steer_off_nadir = 0``) this collapses to
    ``ang.off_nadir_deg`` — i.e. the same number the rest of the app reports.
    """
    t = _unit_from_nadir(ang.off_nadir_deg, ang.azimuth_deg)
    s = _unit_from_nadir(steer_off_nadir_deg, steer_az_deg)
    dot = t[0] * s[0] + t[1] * s[1] + t[2] * s[2]
    return math.degrees(math.acos(max(-1.0, min(1.0, dot))))


# --------------------------------------------------------------------------- #
# room acoustics
# --------------------------------------------------------------------------- #
def _polygon_area(verts: list[Point2D]) -> float:
    a = 0.0
    n = len(verts)
    for i in range(n):
        j = (i + 1) % n
        a += verts[i].x * verts[j].y - verts[j].x * verts[i].y
    return abs(a) / 2.0


def _perimeter(verts: list[Point2D]) -> float:
    n = len(verts)
    p = 0.0
    for i in range(n):
        j = (i + 1) % n
        p += math.hypot(verts[j].x - verts[i].x, verts[j].y - verts[i].y)
    return p


def room_volume_and_surface(config: SystemConfig) -> Optional[tuple[float, float]]:
    """``(volume_m3, total_surface_m2)`` from the room polygon, or ``None``."""
    room = config.room
    if room is None or len(room.vertices) < 3:
        return None
    area = _polygon_area(room.vertices)
    if area <= 0:
        return None
    h = room.height
    volume = area * h
    surface = 2.0 * area + _perimeter(room.vertices) * h
    return volume, surface


def estimated_rt60(config: SystemConfig, params: SimParams = SimParams()) -> float:
    """Sabine RT60 (seconds): ``0.161 * V / (absorption * S)``.

    Returns ``params.rt60_s`` when the user supplied one, and a neutral 0.5 s
    when there is no room geometry to estimate from.
    """
    if params.rt60_s is not None:
        return params.rt60_s
    vs = room_volume_and_surface(config)
    if vs is None:
        return 0.5
    volume, surface = vs
    absorbing_area = max(params.absorption * surface, 1e-6)
    return 0.161 * volume / absorbing_area


def _critical_distance(config: SystemConfig, params: SimParams) -> Optional[float]:
    vs = room_volume_and_surface(config)
    if vs is None:
        return None
    volume, _ = vs
    rt60 = estimated_rt60(config, params)
    return 0.057 * math.sqrt(max(volume, 1.0) / max(rt60, 0.1))


def drr_db(distance_m: float, critical_distance_m: Optional[float]) -> Optional[float]:
    """Direct-to-reverberant ratio in dB; ``None`` when no room geometry."""
    if critical_distance_m is None:
        return None
    return -20.0 * math.log10(max(distance_m, 0.25) / max(critical_distance_m, 0.25))


# --------------------------------------------------------------------------- #
# per-objective sub-scores
# --------------------------------------------------------------------------- #
def direct_level_db(distance_m: float, off_axis_angle_deg: float, params: SimParams) -> float:
    """Relative direct-path level (dB) vs ``ref_distance``: spreading + directivity."""
    d = max(distance_m, 0.25)
    spread_db = -20.0 * math.log10(d / params.ref_distance_m)
    x = off_axis_angle_deg / params.lobe_halfwidth_deg
    dir_db = -3.0 * (x * x)
    return spread_db + dir_db


def snr_score(level_db: float, params: SimParams) -> float:
    return _norm(level_db, params.level_window_db)


def drr_score(drr_db_value: Optional[float], params: SimParams) -> float:
    if drr_db_value is None:
        return 0.5  # neutral when room geometry is unknown
    return _norm(drr_db_value, params.drr_window_db)


def coverage_score(off_axis_angle_deg: float, in_pickup: bool, in_exclusion: bool, params: SimParams) -> float:
    if in_exclusion:
        return 0.0
    lobe = math.exp(-0.5 * (off_axis_angle_deg / params.lobe_halfwidth_deg) ** 2)
    zone_factor = 1.0 if in_pickup else 0.6  # soft penalty when no pickup zone is defined
    return _clamp01(lobe * zone_factor)


def fairness_aggregate(qualities: list[float]) -> float:
    """Reward a high *mean* and a high *worst-case*, penalise *spread*.

    Empty input (no fixed talkers) returns a neutral 1.0 so the term drops out.
    """
    if not qualities:
        return 1.0
    n = len(qualities)
    mean = sum(qualities) / n
    var = sum((q - mean) ** 2 for q in qualities) / n
    worst = min(qualities)
    return _clamp01(0.5 * mean + 0.5 * worst - 0.5 * var)


# --------------------------------------------------------------------------- #
# zone membership (mirrors api.talker_coverage exactly)
# --------------------------------------------------------------------------- #
def zone_membership(array: MicrophoneArray, point: Point2D) -> tuple[bool, bool]:
    """``(in_pickup, in_exclusion)`` for ``point`` against one array's zones."""
    in_pickup = False
    in_exclusion = False
    for zone in array.zones:
        if not point_in_shape(point, zone.shape):
            continue
        if zone.type == "exclusion":
            in_exclusion = True
        else:
            in_pickup = True
    return in_pickup, in_exclusion


# --------------------------------------------------------------------------- #
# combiners
# --------------------------------------------------------------------------- #
def geom_quality(snr: float, drr: float, coverage: float, params: SimParams) -> float:
    """Weighted blend of the three single-talker objectives (no fairness)."""
    w = (params.w_snr, params.w_drr, params.w_coverage)
    wsum = sum(w) or 1.0
    return (params.w_snr * snr + params.w_drr * drr + params.w_coverage * coverage) / wsum


def combine(snr: float, drr: float, coverage: float, fairness: float, params: SimParams) -> float:
    """Final score: the three objectives plus the fairness aggregate."""
    w = (params.w_snr, params.w_drr, params.w_coverage, params.w_fairness)
    wsum = sum(w) or 1.0
    return (
        params.w_snr * snr
        + params.w_drr * drr
        + params.w_coverage * coverage
        + params.w_fairness * fairness
    ) / wsum


# --------------------------------------------------------------------------- #
# the core scoring primitive
# --------------------------------------------------------------------------- #
def array_elevation(config: SystemConfig, array: MicrophoneArray, params: SimParams) -> float:
    if params.array_height_m is not None:
        return params.array_height_m
    if array.elevation is not None:
        return array.elevation
    room_height = config.room.height if config.room is not None else 3.0
    return default_elevation(array, room_height)


def talker_quality(
    array_pos: Point2D,
    array_elev: float,
    steer_off_nadir_deg: float,
    steer_az_deg: float,
    talker_pos: Point2D,
    talker_elev: float,
    array: MicrophoneArray,
    critical_distance_m: Optional[float],
    params: SimParams,
) -> PlacementScore:
    """Score how well ``array`` (at this pose) captures a talker at ``talker_pos``.

    ``total`` here is the geometry-only quality (snr/drr/coverage); the fairness
    term is folded in by the search layer, not by this primitive.
    """
    ang = steering_angles(
        Point3D(array_pos.x, array_pos.y, array_elev),
        Point3D(talker_pos.x, talker_pos.y, talker_elev),
    )
    oa = off_axis_deg(ang, steer_off_nadir_deg, steer_az_deg)
    level_db = direct_level_db(ang.distance, oa, params)
    drr_value = drr_db(ang.distance, critical_distance_m)
    in_pickup, in_exclusion = zone_membership(array, talker_pos)

    snr = snr_score(level_db, params)
    drr = drr_score(drr_value, params)
    cov = coverage_score(oa, in_pickup, in_exclusion, params)
    return PlacementScore(
        total=geom_quality(snr, drr, cov, params),
        snr=snr,
        drr=drr,
        coverage=cov,
        fairness=float("nan"),  # filled in by the search layer
        distance_m=ang.distance,
        off_nadir_deg=ang.off_nadir_deg,
        off_axis_deg=oa,
        direct_level_db=level_db,
        drr_db=drr_value,
        in_pickup_zone=in_pickup,
        in_exclusion_zone=in_exclusion,
    )


def _find_array(config: SystemConfig, array_id: str) -> MicrophoneArray:
    for d in config.devices:
        if d.id == array_id and d.type == "microphoneArray":
            return d  # type: ignore[return-value]
    raise ValueError(f"No microphone array with id {array_id!r}")


def score_placement(
    config: SystemConfig,
    array_id: str,
    candidate: Candidate,
    params: SimParams = SimParams(),
    talker_id: Optional[str] = None,
) -> PlacementScore:
    """Public: full score (incl. fairness) for one candidate placement.

    Pass ``talker_id`` to say which existing talker is being relocated to
    ``candidate.talker_pos`` so it is excluded from the fairness aggregate — this
    makes the result match :func:`~conf_pipeline.sim.search.recommend_placement`
    even after the seat has moved away from the talker's stored position. Without
    it, the seated talker is matched by coordinate (only reliable before moving).

    ``snr/drr/coverage`` reflect ``candidate.talker_pos`` (the seated talker);
    ``fairness`` aggregates every *other* existing talker's quality at the
    candidate array position. In array-only mode (``talker_pos is None``) the
    score reduces to the fairness aggregate over all talkers.
    """
    array = _find_array(config, array_id)
    dc = _critical_distance(config, params)

    def quality_at(pos: Point2D, elev: float) -> PlacementScore:
        # each talker is captured with the array steered straight at it
        ang = steering_angles(
            Point3D(candidate.array_pos.x, candidate.array_pos.y, candidate.array_elev),
            Point3D(pos.x, pos.y, elev),
        )
        return talker_quality(
            candidate.array_pos, candidate.array_elev,
            ang.off_nadir_deg, ang.azimuth_deg,
            pos, elev, array, dc, params,
        )

    seated_id = talker_id
    if seated_id is None and candidate.talker_pos is not None:
        # fall back to matching an existing talker at this position (pre-move only)
        for t in config.talkers:
            if t.position.x == candidate.talker_pos.x and t.position.y == candidate.talker_pos.y:
                seated_id = t.id
                break

    others = [
        t for t in config.talkers
        if t.id != seated_id
    ]
    fair_inputs = []
    for t in others:
        elev = t.elevation if t.elevation is not None else params.talker_height_m
        fair_inputs.append(quality_at(t.position, elev).total)
    fairness = fairness_aggregate(fair_inputs)

    if candidate.talker_pos is not None:
        seat = talker_quality(
            candidate.array_pos, candidate.array_elev,
            candidate.steer_off_nadir_deg, candidate.steer_az_deg,
            candidate.talker_pos, candidate.talker_elev, array, dc, params,
        )
        total = combine(seat.snr, seat.drr, seat.coverage, fairness, params)
        return PlacementScore(
            total=total, snr=seat.snr, drr=seat.drr, coverage=seat.coverage, fairness=fairness,
            distance_m=seat.distance_m, off_nadir_deg=seat.off_nadir_deg, off_axis_deg=seat.off_axis_deg,
            direct_level_db=seat.direct_level_db, drr_db=seat.drr_db,
            in_pickup_zone=seat.in_pickup_zone, in_exclusion_zone=seat.in_exclusion_zone,
        )

    # array-only mode: nothing seated -> score is fairness over all talkers
    total = combine(fairness, fairness, fairness, fairness, params)
    return PlacementScore(
        total=total, snr=fairness, drr=fairness, coverage=fairness, fairness=fairness,
        distance_m=float("nan"), off_nadir_deg=float("nan"), off_axis_deg=float("nan"),
        direct_level_db=float("nan"), drr_db=None, in_pickup_zone=False, in_exclusion_zone=False,
    )
