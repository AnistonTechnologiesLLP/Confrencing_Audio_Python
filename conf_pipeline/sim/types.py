"""Dataclasses for the placement-simulation / recommendation engine.

Pure data — no acoustics or search logic here (those live in ``scoring`` and
``search``). Everything is a frozen dataclass so a :class:`Recommendation` is a
safe, hashable value object the GUI can stash on ``AppState`` and the engine
never mutates a caller's :class:`~conf_pipeline.model.SystemConfig`.

Coordinates follow the rest of the package: floor plane is ``(x, y)`` metres,
``z`` is height above the floor, and azimuth is the compass-style bearing from
:func:`conf_pipeline.angles.steering_angles` (``+Y = 0 deg``, ``+X = 90 deg``).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from ..model import DEFAULT_TALKER_ELEVATION_M, Point2D


@dataclass(frozen=True)
class SimParams:
    """Knobs for the heuristic sweep + (optional) physics validation.

    Weights need not sum to 1 — the combiner normalises by their sum.
    """

    # --- search grid (metres / degrees) ---
    grid_step_m: float = 0.5
    refine_step_m: float = 0.1
    refine_radius_m: float = 0.75
    max_cells: int = 60000  # hard guard: auto-coarsen the grid past this many candidate cells

    # --- geometry / heights ---
    array_height_m: Optional[float] = None  # None -> room height (ceiling mount)
    talker_height_m: float = DEFAULT_TALKER_ELEVATION_M  # 1.2 m mouth height
    min_talker_separation_m: float = 0.6    # don't recommend a seat on top of another talker

    # --- modelling scope ---
    # Score each talker by the BEST-covering array (the one being placed plus any
    # other placed arrays held fixed), instead of only the array under edit.
    consider_all_arrays: bool = True
    # Restrict recommended seats to the pickup/dedicated zones (the "table" /
    # seating area) when any are defined — so people are seated at the table, not
    # on open floor. Falls back to the whole room when no pickup zone exists.
    seat_in_pickup_zones: bool = True

    # --- acoustics ---
    rt60_s: Optional[float] = None  # None -> Sabine estimate from room volume
    absorption: float = 0.18        # average Sabine absorption coeff (used only when rt60 estimated)

    # --- capture / directivity model ---
    ref_distance_m: float = 1.0       # 0 dB reference distance for the level term
    lobe_halfwidth_deg: float = 35.0  # main-lobe half-angle of a ceiling array
    level_window_db: tuple[float, float] = (-24.0, 6.0)  # maps direct level -> [0,1]
    # DRR window tuned for ceiling pickup: a 3 m array onto a 1.2 m mouth sits
    # ~1.8 m away, which is already past the critical distance in most rooms, so
    # the window starts well below 0 dB to keep the objective discriminative.
    drr_window_db: tuple[float, float] = (-15.0, 3.0)    # maps DRR -> [0,1]

    # --- objective weights ---
    w_snr: float = 0.35
    w_drr: float = 0.25
    w_coverage: float = 0.25
    w_fairness: float = 0.15


@dataclass(frozen=True)
class PlacementScore:
    """A scored placement for one talker. Sub-scores are normalised to ``0..1``;
    the ``*_db`` / angle fields carry the raw physical quantities for display."""

    total: float          # combined 0..1 (incl. fairness when scored in context)
    snr: float            # 0..1 sub-scores
    drr: float
    coverage: float
    fairness: float
    # raw quantities (for tooltips / debugging)
    distance_m: float
    off_nadir_deg: float
    off_axis_deg: float
    direct_level_db: float
    drr_db: Optional[float]   # None when no room geometry is available
    in_pickup_zone: bool
    in_exclusion_zone: bool


@dataclass(frozen=True)
class Candidate:
    """One point in the joint search space: an array pose + (optional) seat."""

    array_pos: Point2D
    array_elev: float
    steer_off_nadir_deg: float  # 0 = straight down (nadir)
    steer_az_deg: float
    talker_pos: Optional[Point2D]  # None in array-only (multi-talker) mode
    talker_elev: float


@dataclass(frozen=True)
class Recommendation:
    """The engine's answer: where to mount/steer the array and where to seat."""

    array_id: str
    array_pos: Point2D
    array_elev: float
    steer_az_deg: float
    steer_off_nadir_deg: float
    talker_id: Optional[str]
    talker_pos: Optional[Point2D]
    score: PlacementScore
    # per-talker quality breakdown at the recommended array placement
    per_talker: dict[str, PlacementScore] = field(default_factory=dict)
    validated: Optional["ValidationResult"] = None
    note: str = ""  # human-readable caveat (e.g. "no talkers", "no room")


@dataclass(frozen=True)
class Heatmap:
    """Row-major grid of array-position scores (talkers held fixed).

    ``values[iy * nx + ix]`` is the score for the cell whose centre is at
    ``(origin.x + ix*step_m, origin.y + iy*step_m)``; ``None`` marks a cell
    outside the room or inside an exclusion zone (skipped when drawing).
    """

    origin: Point2D
    step_m: float
    nx: int
    ny: int
    values: list[Optional[float]]
    vmin: float
    vmax: float

    def at(self, ix: int, iy: int) -> Optional[float]:
        return self.values[iy * self.nx + ix]


@dataclass(frozen=True)
class ValidationResult:
    """Physics-backend check of a single recommended geometry."""

    backend: str             # "farfield" | "pyroomacoustics"
    method: str              # short description of the model used
    predicted_snr_db: float
    predicted_drr_db: Optional[float]
    beam_off_axis_db: float  # array gain at the talker direction, vs on-axis
    n_mics: int
