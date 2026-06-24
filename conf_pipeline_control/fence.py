"""Pure two-kit audio fence — stdlib-only, numpy-free, hardware-free.

Given two POLARIS kits at known room positions with known mounting bearings,
this module fuses their independent DOA readings into a 2D estimated source
position and decides whether that source lies inside a user-drawn polygon
(the "fence").  The fence is *soft*: a margin band + hysteresis guard against
chattering on the boundary.

Convention (repo-wide): 0° = +Y, clockwise, ``atan2(x, y)``.  A bearing's
unit direction is ``dx = sin(rad)``, ``dy = cos(rad)``.

Imports only :mod:`conf_pipeline.model`.  Never imports numpy, Qt, or
sounddevice — importing this module pulls nothing heavy.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional

from conf_pipeline.model import (
    Point2D,
    _norm_bearing,
    bearing_to_deg,  # noqa: F401 — re-exported for test helpers that import from here
    point_in_polygon,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DEFAULT_FENCE_MARGIN_M: float = 0.20
"""Soft-margin band (metres) added outside the polygon edge — sources within
this band are treated as inside the fence.  Accounts for bearing coarseness."""

DEFAULT_FENCE_HOLD_TICKS: int = 3
"""Number of consecutive agreeing raw decisions required before
:class:`FenceDecider` flips its committed ``keep`` state (hysteresis)."""

LEVEL_INSIDE_DB: float = -45.0
"""Minimum peak level (dBFS) for a source to count as inside when the
geometric fusion is degenerate or the rays are near-parallel."""


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class FenceConfigError(Exception):
    """Raised at *setup* time when the fence cannot be configured.

    Examples: a kit's array has no position or bearing, the fence was
    requested with a number of kits other than 2, etc.  These are operator
    errors that should be surfaced visibly — never silently ignored.
    """


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class KitPose:
    """Room-space pose of one POLARIS kit's microphone array."""
    position: Point2D
    """Array position in room coordinates (metres)."""
    bearing_deg: float
    """Mounting heading: the direction the array's 0° local axis points in
    room space (0° = +Y / North, clockwise)."""


@dataclass(frozen=True)
class KitReading:
    """Snapshot of one kit's DOA + level at a single control tick."""
    azimuth_deg: Optional[float]
    """Array-relative azimuth of the detected source (None if DOA failed)."""
    salience_db: float
    """DOA salience / confidence in dB (negative = weak)."""
    level: float
    """RMS amplitude of the kit's output (linear, ≥ 0)."""
    active: bool
    """True if this kit is the currently selected talker in the automix."""


@dataclass(frozen=True)
class Ray2D:
    """A 2D ray: origin + *unit* direction."""
    origin: Point2D
    dx: float
    """X-component of the unit direction vector."""
    dy: float
    """Y-component of the unit direction vector."""


@dataclass(frozen=True)
class FusedSource:
    """Result of fusing two kit readings into a 2D position estimate."""
    point: Optional[Point2D]
    """Estimated 2D source position (None when degenerate)."""
    confidence: float
    """Crossing confidence ∈ [0, 1] (0 = near-parallel rays, 1 = orthogonal)."""
    inside: bool
    """True if the fused point is inside the fence polygon (or margin band)."""
    degenerate: bool
    """True when the geometry could not produce a reliable position estimate
    (missing azimuth, near-parallel rays, etc.)."""
    loud_kit: Optional[int]
    """Index (0 or 1) of the kit with the higher level, or None if both silent."""
    miss_distance_m: float
    """Distance (metres) between the two closest-approach points on the rays.
    0.0 for a perfect crossing, ``inf`` when degenerate."""


@dataclass(frozen=True)
class FenceDecision:
    """Output of one :class:`FenceDecider` tick."""
    keep: bool
    """True → let the source through; False → gate / veto it."""
    veto_kit: Optional[int]
    """When ``keep`` is False, the kit index to silence in the automix
    selection (the louder kit); None when keeping."""
    source: FusedSource
    """The underlying fused-position estimate for this tick."""


# ---------------------------------------------------------------------------
# Geometry primitives
# ---------------------------------------------------------------------------

def ray_from_bearing(origin: Point2D, room_az_deg: float) -> Ray2D:
    """Build a :class:`Ray2D` from a room-space azimuth.

    Uses the repo-wide convention: 0° = +Y, clockwise →
    ``dx = sin(rad)``, ``dy = cos(rad)``.
    """
    rad = math.radians(room_az_deg)
    return Ray2D(origin=origin, dx=math.sin(rad), dy=math.cos(rad))


def local_az_to_room_az(local_az_deg: float, bearing_deg: float) -> float:
    """Convert an array-relative azimuth to a room azimuth.

    Identical to the transform used in :mod:`conf_pipeline.seat_mapper`:
    ``room_az = _norm_bearing(local_az + bearing_deg)``.
    """
    return _norm_bearing(local_az_deg + bearing_deg)


def closest_point_two_rays(
    a: Ray2D,
    b: Ray2D,
    *,
    parallel_eps: float = 1e-3,
) -> tuple[Optional[Point2D], float, bool]:
    """Least-squares nearest approach of two 2D *rays* (not lines).

    Standard closed-form solve for the parameters ``sa`` (along ray *a*)
    and ``sb`` (along ray *b*) that minimise the squared distance between
    the two rays.  Both parameters are clamped to ≥ 0 (rays, not lines).

    Returns:
        ``(midpoint, miss_distance_m, degenerate)``

        * ``midpoint``  — midpoint of the two closest-approach points, or
          ``None`` when degenerate.
        * ``miss_distance_m`` — distance between the two closest-approach
          points (0 for a perfect crossing).
        * ``degenerate`` — ``True`` when ``|denom| < parallel_eps`` (rays
          are nearly parallel).
    """
    da = (a.dx, a.dy)
    db = (b.dx, b.dy)

    # w = a.origin - b.origin
    wx = a.origin.x - b.origin.x
    wy = a.origin.y - b.origin.y

    # b_ = da · db
    b_ = da[0] * db[0] + da[1] * db[1]

    denom = 1.0 - b_ * b_
    if abs(denom) < parallel_eps:
        return (None, math.inf, True)

    # da · w and db · w
    daw = da[0] * wx + da[1] * wy
    dbw = db[0] * wx + db[1] * wy

    sa = (b_ * dbw - daw) / denom
    sb = (dbw - b_ * daw) / denom

    # Clamp to rays (not lines)
    sa = max(0.0, sa)
    sb = max(0.0, sb)

    # Closest approach points
    pax = a.origin.x + sa * a.dx
    pay = a.origin.y + sa * a.dy
    pbx = b.origin.x + sb * b.dx
    pby = b.origin.y + sb * b.dy

    miss = math.hypot(pax - pbx, pay - pby)
    midpoint = Point2D((pax + pbx) * 0.5, (pay + pby) * 0.5)
    return (midpoint, miss, False)


def crossing_confidence(a: Ray2D, b: Ray2D) -> float:
    """2D cross-product magnitude of two ray directions.

    Returns ``|dx1*dy2 - dy1*dx2|`` ∈ [0, 1].

    * ≈ 0  → near-parallel (bearings from both kits point the same way,
      position estimate is unreliable).
    * ≈ 1  → orthogonal (best geometry for triangulation).
    """
    return abs(a.dx * b.dy - a.dy * b.dx)


def _dist_point_to_segment(p: Point2D, seg_a: Point2D, seg_b: Point2D) -> float:
    """Minimum distance from point *p* to the line segment [seg_a, seg_b]."""
    abx = seg_b.x - seg_a.x
    aby = seg_b.y - seg_a.y
    len_sq = abx * abx + aby * aby
    if len_sq < 1e-18:
        # Degenerate segment — return distance to the single point
        return math.hypot(p.x - seg_a.x, p.y - seg_a.y)
    t = ((p.x - seg_a.x) * abx + (p.y - seg_a.y) * aby) / len_sq
    t = max(0.0, min(1.0, t))
    cx = seg_a.x + t * abx
    cy = seg_a.y + t * aby
    return math.hypot(p.x - cx, p.y - cy)


def point_in_fence(p: Point2D, polygon: list[Point2D], margin_m: float) -> bool:
    """True if *p* is inside the fence polygon or within ``margin_m`` of an edge.

    When ``margin_m == 0`` the result is **exactly** :func:`~conf_pipeline.model.point_in_polygon`.
    An empty polygon always returns ``False``.
    """
    if not polygon:
        return False

    if point_in_polygon(p, polygon):
        return True

    if margin_m <= 0.0:
        return False

    # Check distance to each polygon edge
    n = len(polygon)
    for i in range(n):
        seg_a = polygon[i]
        seg_b = polygon[(i + 1) % n]
        if _dist_point_to_segment(p, seg_a, seg_b) <= margin_m:
            return True
    return False


def level_cross_check(
    reading_a: KitReading,
    reading_b: KitReading,
    *,
    inside_db: float = LEVEL_INSIDE_DB,
) -> bool:
    """True if the peak level across both kits exceeds ``inside_db``.

    A source *inside* the fence is close to at least one kit and therefore
    registers a meaningful level; a far source (same bearing, greater range)
    is typically much quieter.

    ``20 * log10(max(level_a, level_b, 1e-9)) >= inside_db``
    """
    peak = max(reading_a.level, reading_b.level, 1e-9)
    return 20.0 * math.log10(peak) >= inside_db


# ---------------------------------------------------------------------------
# Fusion
# ---------------------------------------------------------------------------

def fuse_position(
    reading_a: KitReading,
    reading_b: KitReading,
    pose_a: KitPose,
    pose_b: KitPose,
    polygon: list[Point2D],
    *,
    margin_m: float,
    parallel_eps: float = 1e-3,
) -> FusedSource:
    """Fuse two kit readings into a :class:`FusedSource`.

    Steps:

    1. Convert each kit's local azimuth to room azimuth.
    2. Build a :class:`Ray2D` from each kit's position.
    3. Find the nearest approach of the two rays.
    4. Classify the estimated point as inside/outside the fence.
    5. Determine the loud kit.
    """
    # Missing azimuth → can't triangulate
    if reading_a.azimuth_deg is None or reading_b.azimuth_deg is None:
        loud_kit: Optional[int]
        if reading_a.level > 1e-9 or reading_b.level > 1e-9:
            loud_kit = 0 if reading_a.level >= reading_b.level else 1
        else:
            loud_kit = None
        return FusedSource(
            point=None,
            confidence=0.0,
            inside=False,
            degenerate=True,
            loud_kit=loud_kit,
            miss_distance_m=math.inf,
        )

    # Local → room azimuths
    room_az_a = local_az_to_room_az(reading_a.azimuth_deg, pose_a.bearing_deg)
    room_az_b = local_az_to_room_az(reading_b.azimuth_deg, pose_b.bearing_deg)

    # Build rays from kit positions
    ray_a = ray_from_bearing(pose_a.position, room_az_a)
    ray_b = ray_from_bearing(pose_b.position, room_az_b)

    # Nearest approach
    point, miss_m, degen = closest_point_two_rays(ray_a, ray_b, parallel_eps=parallel_eps)

    conf = crossing_confidence(ray_a, ray_b)

    # Loud kit
    if reading_a.level > 1e-9 or reading_b.level > 1e-9:
        loud_kit = 0 if reading_a.level >= reading_b.level else 1
    else:
        loud_kit = None

    # Inside classification
    if not polygon:
        # Inert — no polygon drawn
        inside = True
    elif point is None or degen:
        inside = False
    else:
        inside = point_in_fence(point, polygon, margin_m)

    return FusedSource(
        point=point,
        confidence=conf,
        inside=inside,
        degenerate=degen,
        loud_kit=loud_kit,
        miss_distance_m=miss_m,
    )


# ---------------------------------------------------------------------------
# Stateful fence decision with hysteresis
# ---------------------------------------------------------------------------

class FenceDecider:
    """Stateful fence decision-maker with hysteresis (run-length counter).

    ``update()`` is called once per control tick (e.g. every 60 ms by the
    GUI's ``_tick_twokit``).  It computes a *raw* keep/reject decision, then
    only **commits** a flip after ``hold_ticks`` consecutive agreeing raw
    decisions — preventing chatter when a source wanders across the boundary.

    Thread safety: not thread-safe; caller must hold the appropriate lock.
    """

    def __init__(
        self,
        *,
        hold_ticks: int = DEFAULT_FENCE_HOLD_TICKS,
        margin_m: float = DEFAULT_FENCE_MARGIN_M,
        inside_db: float = LEVEL_INSIDE_DB,
        parallel_eps: float = 1e-3,
    ) -> None:
        self._hold_ticks = hold_ticks
        self._margin_m = margin_m
        self._inside_db = inside_db
        self._parallel_eps = parallel_eps

        self._committed_keep: bool = True
        """Last committed decision (starts as keep=True / pass-through)."""
        self._run_len: int = 0
        """Consecutive raw decisions that agree on the *opposite* of committed."""

    def reset(self) -> None:
        """Reset to the initial pass-through state."""
        self._committed_keep = True
        self._run_len = 0

    def update(
        self,
        ra: KitReading,
        rb: KitReading,
        pose_a: KitPose,
        pose_b: KitPose,
        polygon: list[Point2D],
        t: float,
    ) -> FenceDecision:
        """Compute one fence decision tick.

        Parameters:
            ra, rb: current readings from kits A and B.
            pose_a, pose_b: room poses of kits A and B.
            polygon: the fence polygon in room coordinates.
            t: current time (seconds, for future use / logging).

        Returns:
            A :class:`FenceDecision` with the committed keep/reject state.
        """
        fused = fuse_position(
            ra, rb, pose_a, pose_b, polygon,
            margin_m=self._margin_m,
            parallel_eps=self._parallel_eps,
        )

        # --- Raw decision ---
        if not polygon:
            # No fence drawn → always keep
            raw_keep = True
        elif fused.degenerate or fused.point is None:
            # Can't triangulate → fall back to level
            raw_keep = level_cross_check(ra, rb, inside_db=self._inside_db)
        else:
            # Full geometry available
            level_ok = level_cross_check(ra, rb, inside_db=self._inside_db)
            salience_strong = (ra.salience_db > -10.0) or (rb.salience_db > -10.0)
            raw_keep = fused.inside and (level_ok or salience_strong)

        # --- Hysteresis ---
        if raw_keep == self._committed_keep:
            # Agrees with current commitment → reset the run counter
            self._run_len = 0
        else:
            self._run_len += 1
            if self._run_len >= self._hold_ticks:
                # Sustained disagreement → flip commitment
                self._committed_keep = raw_keep
                self._run_len = 0

        veto_kit: Optional[int] = None
        if not self._committed_keep:
            veto_kit = fused.loud_kit

        return FenceDecision(keep=self._committed_keep, veto_kit=veto_kit, source=fused)
