"""Joint array-placement + seat search (pure ``math`` — no numpy).

The search space is array ``(x, y)`` x array steer x seat ``(x, y)``. Two facts
collapse it from a 6-D grid to two cheap 2-D sweeps:

* **Steer is derived, not searched** — the optimal steer always points straight
  at the talker (off-axis is minimised at 0), so we compute it from
  :func:`steering_angles` instead of gridding over it.
* **Coarse-to-fine** — a coarse sweep over the room footprint finds the basin,
  then a small local grid refines array and seat independently.

Complexity: array-only mode is ``O(N_a * T)``; seat+relocate is ``O(N_a * N_s)``
(~tens of thousands of evals for a normal room at 0.5 m — well under 100 ms).
"""
from __future__ import annotations

import math
from dataclasses import replace
from typing import Optional

from ..angles import Point3D, steering_angles
from ..model import (
    MicrophoneArray,
    Point2D,
    SystemConfig,
    point_in_polygon,
)
from . import scoring
from .types import Candidate, Heatmap, PlacementScore, Recommendation, SimParams


# --------------------------------------------------------------------------- #
# geometry / candidate generation
# --------------------------------------------------------------------------- #
def _bbox(config: SystemConfig) -> tuple[float, float, float, float]:
    pts: list[Point2D] = []
    if config.room is not None and len(config.room.vertices) >= 2:
        pts += config.room.vertices
    else:
        for d in config.devices:
            if d.position is not None:
                pts.append(d.position)
        for t in config.talkers:
            pts.append(t.position)
    if not pts:
        return (0.0, 0.0, 10.0, 8.0)
    xs = [p.x for p in pts]
    ys = [p.y for p in pts]
    return (min(xs), min(ys), max(xs), max(ys))


def _inside_room(config: SystemConfig, p: Point2D) -> bool:
    room = config.room
    if room is None or len(room.vertices) < 3:
        return True
    return point_in_polygon(p, room.vertices)


def _effective_step(bbox: tuple[float, float, float, float], step: float, max_cells: int) -> float:
    """Grow the step so the bounding grid never exceeds ``max_cells`` cells."""
    minx, miny, maxx, maxy = bbox
    step = max(step, 1e-3)
    while True:
        nx = int((maxx - minx) / step) + 1
        ny = int((maxy - miny) / step) + 1
        if nx * ny <= max_cells or step > 1e6:
            return step
        step *= 1.5


def grid_points(config: SystemConfig, step: float, max_cells: int) -> tuple[list[Point2D], float]:
    """Grid over the room footprint (points inside the room polygon). Returns
    ``(points, effective_step)`` — the step may be coarsened to honour ``max_cells``."""
    bbox = _bbox(config)
    step = _effective_step(bbox, step, max_cells)
    minx, miny, maxx, maxy = bbox
    pts: list[Point2D] = []
    y = miny
    while y <= maxy + 1e-9:
        x = minx
        while x <= maxx + 1e-9:
            p = Point2D(round(x, 4), round(y, 4))
            if _inside_room(config, p):
                pts.append(p)
            x += step
        y += step
    if not pts:  # degenerate polygon — fall back to the bbox centre
        pts = [Point2D(round((minx + maxx) / 2, 4), round((miny + maxy) / 2, 4))]
    return pts, step


def _local_grid(center: Point2D, config: SystemConfig, step: float, radius: float) -> list[Point2D]:
    pts: list[Point2D] = []
    step = max(step, 1e-6)  # guard against a zero refine step (ZeroDivisionError)
    # cap the per-axis count so a tiny step can't explode the grid (keep full radius)
    max_n = 48
    if radius / step > max_n:
        step = radius / max_n
    n = max(1, int(round(radius / step)))
    for iy in range(-n, n + 1):
        for ix in range(-n, n + 1):
            p = Point2D(round(center.x + ix * step, 4), round(center.y + iy * step, 4))
            if _inside_room(config, p):
                pts.append(p)
    return pts or [center]


# --------------------------------------------------------------------------- #
# resolution helpers
# --------------------------------------------------------------------------- #
def _resolve_array(config: SystemConfig, array_id: Optional[str]) -> MicrophoneArray:
    arrays = [d for d in config.devices if d.type == "microphoneArray"]
    if not arrays:
        raise ValueError("Configuration has no microphone array to place.")
    if array_id is None:
        return arrays[0]
    for a in arrays:
        if a.id == array_id:
            return a
    raise ValueError(f"No microphone array with id {array_id!r}")


def _talker_elev(talker, params: SimParams) -> float:
    return talker.elevation if talker.elevation is not None else params.talker_height_m


def _derive_steer(array_pos: Point2D, array_elev: float, pos: Point2D, elev: float):
    ang = steering_angles(
        Point3D(array_pos.x, array_pos.y, array_elev), Point3D(pos.x, pos.y, elev)
    )
    return ang.off_nadir_deg, ang.azimuth_deg


# --------------------------------------------------------------------------- #
# evaluation at a fixed (array, seat)
# --------------------------------------------------------------------------- #
def _eval(
    config: SystemConfig,
    array: MicrophoneArray,
    array_pos: Point2D,
    array_elev: float,
    seat_pos: Optional[Point2D],
    seat_elev: float,
    fixed_talkers: list,
    dc: Optional[float],
    params: SimParams,
):
    """Return ``(total, fairness, per_talker, seat_score, steer_off_nadir, steer_az)``."""
    per_talker: dict[str, PlacementScore] = {}
    fair_inputs: list[float] = []
    for t in fixed_talkers:
        elev = _talker_elev(t, params)
        # each talker is served by whichever array (this one or the others) covers best
        ps = scoring.talker_best_quality(config, array, array_pos, array_elev, t.position, elev, dc, params)
        per_talker[t.id] = ps
        fair_inputs.append(ps.total)
    fairness = scoring.fairness_aggregate(fair_inputs)
    # surface the aggregate on each per-talker entry for display
    per_talker = {k: replace(v, fairness=fairness) for k, v in per_talker.items()}

    if seat_pos is not None:
        on, az = _derive_steer(array_pos, array_elev, seat_pos, seat_elev)
        seat = scoring.talker_quality(array_pos, array_elev, on, az, seat_pos, seat_elev, array, dc, params)
        total = scoring.combine(seat.snr, seat.drr, seat.coverage, fairness, params)
        seat = replace(seat, total=total, fairness=fairness)
        return total, fairness, per_talker, seat, on, az

    # array-only: aim the displayed steer at the centroid of the fixed talkers
    total = scoring.combine(fairness, fairness, fairness, fairness, params)
    if fixed_talkers:
        cx = sum(t.position.x for t in fixed_talkers) / len(fixed_talkers)
        cy = sum(t.position.y for t in fixed_talkers) / len(fixed_talkers)
        on, az = _derive_steer(array_pos, array_elev, Point2D(cx, cy), params.talker_height_m)
    else:
        on, az = 0.0, 0.0
    return total, fairness, per_talker, None, on, az


def _best_seat(
    config: SystemConfig,
    array: MicrophoneArray,
    array_pos: Point2D,
    array_elev: float,
    candidates: list[Point2D],
    talker,
    dc: Optional[float],
    params: SimParams,
) -> tuple[Point2D, float, PlacementScore]:
    """Best seat for ``talker`` given a fixed array pose.

    Never returns a seat inside an exclusion zone *if any non-excluded candidate
    exists* (crowding separation is relaxed before exclusion is). Only when every
    candidate is in a no-pickup zone does it fall back to the talker's current
    position — a degenerate, fully-excluded layout.
    """
    elev = _talker_elev(talker, params)
    others = [t for t in config.talkers if t.id != talker.id]
    sep = params.min_talker_separation_m

    def _scan(require_separation: bool) -> Optional[tuple[Point2D, float, PlacementScore]]:
        best: Optional[tuple[Point2D, float, PlacementScore]] = None
        for sp in candidates:
            if scoring.point_in_any_exclusion(config, sp):
                continue  # never seat inside any array's no-pickup zone
            if require_separation and sep > 0 and any(
                math.hypot(sp.x - o.position.x, sp.y - o.position.y) < sep for o in others
            ):
                continue  # don't seat someone on top of another talker
            on, az = _derive_steer(array_pos, array_elev, sp, elev)
            ps = scoring.talker_quality(array_pos, array_elev, on, az, sp, elev, array, dc, params)
            if best is None or ps.total > best[2].total:
                best = (sp, elev, ps)
        return best

    # prefer a non-excluded, uncrowded seat; relax crowding before exclusion
    best = _scan(require_separation=True) or _scan(require_separation=False)
    if best is None:  # every candidate is in an exclusion zone — degenerate layout
        on, az = _derive_steer(array_pos, array_elev, talker.position, elev)
        ps = scoring.talker_quality(array_pos, array_elev, on, az, talker.position, elev, array, dc, params)
        best = (talker.position, elev, ps)
    return best


def _seat_candidates(config: SystemConfig, candidates: list[Point2D], params: SimParams) -> list[Point2D]:
    """Where a person may be seated: drop exclusion zones, and — when any pickup
    zone (a "table"/seating area) is defined — keep only seats inside one, so the
    recommendation sits people at the table rather than on open floor. Relaxes to
    the whole (non-excluded) floor if no candidate lands in a pickup zone."""
    non_excluded = [p for p in candidates if not scoring.point_in_any_exclusion(config, p)]
    if params.seat_in_pickup_zones and scoring.has_any_pickup_zone(config):
        at_table = [p for p in non_excluded if scoring.point_in_any_pickup(config, p)]
        if at_table:
            return at_table
    return non_excluded


# --------------------------------------------------------------------------- #
# public: recommend_placement
# --------------------------------------------------------------------------- #
def recommend_placement(
    config: SystemConfig,
    array_id: Optional[str] = None,
    talker_id: Optional[str] = None,
    params: SimParams = SimParams(),
) -> Recommendation:
    """Recommend the best array pose (and, when ``talker_id`` is given, the best
    seat for that talker), optimising direct level, DRR, coverage and fairness."""
    array = _resolve_array(config, array_id)
    array_elev = scoring.array_elevation(config, array, params)
    dc = scoring._critical_distance(config, params)

    selected = next((t for t in config.talkers if t.id == talker_id), None) if talker_id else None
    fixed = [t for t in config.talkers if t.id != talker_id]

    grid, _step = grid_points(config, params.grid_step_m, params.max_cells)
    seat_grid = _seat_candidates(config, grid, params)  # seats at the table / off exclusion zones

    note = ""
    if not config.talkers:
        note = "No talkers placed — add a talker to optimise placement."
    elif selected is None and talker_id is not None:
        note = f"Talker {talker_id!r} not found; optimised for all talkers."

    # ---- Stage A: coarse array sweep --------------------------------------
    best = None  # (total, array_pos, seat_pos, seat_elev, per_talker, seat_score, steer_on, steer_az)
    for ap in grid:
        if selected is not None:
            seat_pos, seat_elev, _ = _best_seat(config, array, ap, array_elev, seat_grid, selected, dc, params)
        else:
            seat_pos, seat_elev = None, params.talker_height_m
        total, _fair, per_talker, seat_ps, on, az = _eval(
            config, array, ap, array_elev, seat_pos, seat_elev, fixed, dc, params
        )
        if best is None or total > best[0]:
            best = (total, ap, seat_pos, seat_elev, per_talker, seat_ps, on, az)

    assert best is not None
    _, array_pos, seat_pos, seat_elev, per_talker, seat_ps, on, az = best

    # ---- Stage C: coarse-to-fine refinement -------------------------------
    # refine array position with the seat held at the coarse optimum
    for ap in _local_grid(array_pos, config, params.refine_step_m, params.refine_radius_m):
        total, _f, pt, sps, son, saz = _eval(
            config, array, ap, array_elev, seat_pos, seat_elev, fixed, dc, params
        )
        if total > best[0]:
            best = (total, ap, seat_pos, seat_elev, pt, sps, son, saz)
    _, array_pos, seat_pos, seat_elev, per_talker, seat_ps, on, az = best

    # refine the seat with the refined array held fixed — keep it only if it
    # does not regress the coarse/array-refined optimum (e.g. a fallback seat)
    if selected is not None and seat_pos is not None:
        local = _seat_candidates(
            config, _local_grid(seat_pos, config, params.refine_step_m, params.refine_radius_m), params
        )
        cand_seat, cand_elev, _ = _best_seat(config, array, array_pos, array_elev, local, selected, dc, params)
        cand_total, _f, cand_pt, cand_sps, cand_on, cand_az = _eval(
            config, array, array_pos, array_elev, cand_seat, cand_elev, fixed, dc, params
        )
        if cand_total > best[0]:
            best = (cand_total, array_pos, cand_seat, cand_elev, cand_pt, cand_sps, cand_on, cand_az)

    total, array_pos, seat_pos, seat_elev, per_talker, seat_ps, on, az = best

    score = seat_ps if seat_ps is not None else _array_only_score(total, best, params)
    return Recommendation(
        array_id=array.id,
        array_pos=array_pos,
        array_elev=array_elev,
        steer_az_deg=az,
        steer_off_nadir_deg=on,
        talker_id=selected.id if selected is not None else None,
        talker_pos=seat_pos,
        score=score,
        per_talker=per_talker,
        note=note,
    )


def _array_only_score(total: float, best, params: SimParams) -> PlacementScore:
    fairness = best[0]  # in array-only mode total == fairness aggregate
    return PlacementScore(
        total=total, snr=fairness, drr=fairness, coverage=fairness, fairness=fairness,
        distance_m=float("nan"), off_nadir_deg=float("nan"), off_axis_deg=float("nan"),
        direct_level_db=float("nan"), drr_db=None, in_pickup_zone=False, in_exclusion_zone=False,
    )


# --------------------------------------------------------------------------- #
# public: score_heatmap (array-position sweep, talkers fixed)
# --------------------------------------------------------------------------- #
def score_heatmap(
    config: SystemConfig,
    array_id: Optional[str] = None,
    params: SimParams = SimParams(),
    talker_id: Optional[str] = None,
) -> Heatmap:
    """A grid of *where to mount the array* scores (talkers held fixed).

    ``talker_id`` is accepted for API symmetry but does not change the heatmap —
    the heatmap always sweeps array position with every talker fixed (no nested
    seat sweep), which is what makes it cheap enough to recompute interactively.
    """
    array = _resolve_array(config, array_id)
    array_elev = scoring.array_elevation(config, array, params)
    dc = scoring._critical_distance(config, params)
    fixed = list(config.talkers)

    bbox = _bbox(config)
    step = _effective_step(bbox, params.grid_step_m, params.max_cells)
    minx, miny, maxx, maxy = bbox
    nx = int((maxx - minx) / step) + 1
    ny = int((maxy - miny) / step) + 1

    values: list[Optional[float]] = []
    finite: list[float] = []
    for iy in range(ny):
        for ix in range(nx):
            p = Point2D(round(minx + ix * step, 4), round(miny + iy * step, 4))
            if not _inside_room(config, p):
                values.append(None)
                continue
            total, *_ = _eval(config, array, p, array_elev, None, params.talker_height_m, fixed, dc, params)
            values.append(total)
            finite.append(total)

    vmin = min(finite) if finite else 0.0
    vmax = max(finite) if finite else 1.0
    return Heatmap(origin=Point2D(minx, miny), step_m=step, nx=nx, ny=ny, values=values, vmin=vmin, vmax=vmax)
