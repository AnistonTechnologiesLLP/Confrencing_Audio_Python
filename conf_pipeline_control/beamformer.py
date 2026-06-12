"""Narrowband beamformer design (pure stdlib complex math, no numpy).

Given an :class:`ArrayGeometry` and look/null :class:`Direction`\\ s, compute
complex capsule weights that steer the array's pickup toward chosen areas and
place spatial nulls toward excluded areas, then evaluate the resulting beam
pattern to *prove* it (pickup ≈ 0 dB, exclusions attenuated).

Conventions
-----------
- Plane-wave steering vector for direction unit ``u`` at frequency ``f``:
  ``a_m = exp(+j · 2π f / c · (p_m · u))`` for capsule position ``p_m``.
- Weights ``w`` give array response ``R(u) = wᴴ a(u)`` (Hermitian inner product).
- **Delay-and-sum** (matched) toward ``u0``: ``w = a(u0) / M`` → ``R(u0) = 1``.
- **LCMV** (linearly-constrained min-variance) for unit gain at ``u0`` and nulls
  at ``{u_k}``: minimum-norm ``w = C (Cᴴ C)⁻¹ g`` with ``C = [a(u0), a(u_1)…]``,
  ``g = [1, 0, …]``. This is the honest way to "mute an area": a real null whose
  depth and bandwidth are bounded by the array's size and capsule count.

A planar array discriminates mainly in azimuth/horizontal offset and forms at
most ``M − 1`` independent nulls; both limits are enforced/flagged below rather
than hidden.
"""
from __future__ import annotations

import cmath
import math
from dataclasses import dataclass

from .geometry import SOUND_SPEED_MPS, ArrayGeometry
from .model import DEFAULT_DESIGN_FREQ_HZ, RESPONSE_FLOOR_DB
from .steering import Direction

Complex = complex


# --------------------------------------------------------------------------- #
# Core narrowband operations
# --------------------------------------------------------------------------- #
def steering_vector(geom: ArrayGeometry, unit: tuple[float, float, float], freq_hz: float) -> list[Complex]:
    """Array manifold vector ``a(u, f)`` (one complex per capsule)."""
    k = 2.0 * math.pi * freq_hz / SOUND_SPEED_MPS
    ux, uy, uz = unit
    out: list[Complex] = []
    for (px, py, pz) in geom.elements:
        proj = px * ux + py * uy + pz * uz
        out.append(cmath.exp(1j * k * proj))
    return out


def diffuse_coherence(geom: ArrayGeometry, freq_hz: float) -> list[list[float]]:
    """Spatial-coherence matrix of an isotropic (diffuse) noise field over the
    **active** capsules: ``Γ_ij = sinc(k·d_ij)`` with ``sinc(x)=sin(x)/x``.

    This is the noise model a superdirective beamformer minimises against — it is
    what makes a small array reject room/background noise far better than plain
    delay-and-sum.
    """
    idx = geom.active_indices()
    pts = [geom.elements[i] for i in idx]
    na = len(pts)
    k = 2.0 * math.pi * freq_hz / SOUND_SPEED_MPS
    gamma = [[0.0] * na for _ in range(na)]
    for i in range(na):
        for j in range(na):
            if i == j:
                gamma[i][j] = 1.0
            else:
                x = k * math.dist(pts[i], pts[j])
                gamma[i][j] = math.sin(x) / x if abs(x) > 1e-9 else 1.0
    return gamma


def _solve_real(r: list[list[float]], b: list[Complex]) -> list[Complex]:
    """Solve ``R x = b`` for a real matrix ``R`` and complex ``b``."""
    return _solve([[c + 0j for c in row] for row in r], b)


def _weights_constrained(
    geom: ArrayGeometry,
    look: Direction,
    nulls: list[Direction],
    freq_hz: float,
    *,
    noise: list[list[float]] | None,
    loading: float,
) -> list[Complex]:
    """MVDR/LCMV weights against a noise covariance ``noise`` (None = identity =
    delay-and-sum / plain LCMV), with diagonal ``loading`` for robustness.

    ``noise = diffuse_coherence(...)`` gives a **superdirective** beam. Designs
    over active capsules and scatters into a full-length weight vector.
    """
    idx = geom.active_indices()
    na = len(idx)
    a_look = [steering_vector(geom, look.unit, freq_hz)[i] for i in idx]

    if noise is None:
        r = [[1.0 if i == j else 0.0 for j in range(na)] for i in range(na)]
    else:
        r = [row[:] for row in noise]
    if loading:
        for i in range(na):
            r[i][i] += loading

    if not nulls:
        # MVDR: w = R⁻¹a / (aᴴ R⁻¹ a)
        t = _solve_real(r, a_look)
        denom = sum(a_look[i].conjugate() * t[i] for i in range(na))
        w_active = [ti / denom for ti in t]
    else:
        if len(nulls) > na - 1:
            raise ValueError(
                f"{len(nulls)} nulls requested but {na} active capsule(s) can form at most {na - 1}"
            )
        cols = [a_look] + [[steering_vector(geom, n.unit, freq_hz)[i] for i in idx] for n in nulls]
        rinv_cols = [_solve_real(r, col) for col in cols]            # R⁻¹ C, columnwise
        k = len(cols)
        # CᴴR⁻¹C
        m = [[sum(cols[p][i].conjugate() * rinv_cols[q][i] for i in range(na)) for q in range(k)] for p in range(k)]
        g = [1.0 + 0j] + [0j] * len(nulls)
        try:
            y = _solve(m, g)
        except ZeroDivisionError as exc:
            raise ValueError("null direction coincides with look direction") from exc
        w_active = [sum(y[q] * rinv_cols[q][i] for q in range(k)) for i in range(na)]

    w = [0j] * geom.n_channels
    for slot, i in enumerate(idx):
        w[i] = w_active[slot]
    return w


def delay_and_sum_weights(geom: ArrayGeometry, look: Direction, freq_hz: float) -> list[Complex]:
    """Matched (delay-and-sum) weights steering toward ``look``.

    Only active capsules carry weight; inactive ones are left at 0 so the
    full-length vector still aligns with the device's channels.
    """
    return _weights_constrained(geom, look, [], freq_hz, noise=None, loading=0.0)


def superdirective_weights(
    geom: ArrayGeometry, look: Direction, nulls: list[Direction], freq_hz: float, *, loading: float = 0.05
) -> list[Complex]:
    """Superdirective (diffuse-noise MVDR) weights — maximise rejection of
    isotropic background while keeping unity gain toward ``look`` (and exact
    nulls toward ``nulls``). ``loading`` (diagonal loading) trades directivity
    for robustness to self-noise / capsule mismatch; raise it if the beam hisses."""
    noise = diffuse_coherence(geom, freq_hz)
    return _weights_constrained(geom, look, nulls, freq_hz, noise=noise, loading=loading)


def _hermitian_gram(cols: list[list[Complex]]) -> list[list[Complex]]:
    """``G = Cᴴ C`` where ``cols`` are the columns of C (each length M)."""
    k = len(cols)
    g = [[0j] * k for _ in range(k)]
    for i in range(k):
        for j in range(k):
            s = 0j
            ci, cj = cols[i], cols[j]
            for m in range(len(ci)):
                s += ci[m].conjugate() * cj[m]
            g[i][j] = s
    return g


def _solve(a: list[list[Complex]], b: list[Complex]) -> list[Complex]:
    """Solve ``A x = b`` for square complex ``A`` (Gauss-Jordan, partial pivot)."""
    n = len(b)
    aug = [list(a[i]) + [b[i]] for i in range(n)]
    for col in range(n):
        piv = max(range(col, n), key=lambda r: abs(aug[r][col]))
        if abs(aug[piv][col]) < 1e-12:
            raise ZeroDivisionError("singular constraint matrix")
        aug[col], aug[piv] = aug[piv], aug[col]
        inv = 1.0 / aug[col][col]
        for j in range(col, n + 1):
            aug[col][j] *= inv
        for r in range(n):
            if r != col:
                f = aug[r][col]
                if f != 0:
                    for j in range(col, n + 1):
                        aug[r][j] -= f * aug[col][j]
    return [aug[i][n] for i in range(n)]


def lcmv_weights(
    geom: ArrayGeometry, look: Direction, nulls: list[Direction], freq_hz: float
) -> list[Complex]:
    """Unit gain toward ``look``, exact nulls toward each of ``nulls``.

    Raises :class:`ValueError` if more nulls than the array can form
    (``> M − 1``) or if a null direction coincides with the look direction
    (singular constraints — you cannot null an area you are also steering at).
    """
    return _weights_constrained(geom, look, nulls, freq_hz, noise=None, loading=0.0)


def response(weights: list[Complex], geom: ArrayGeometry, unit: tuple[float, float, float], freq_hz: float) -> Complex:
    """Complex array response ``R(u) = wᴴ a(u)``."""
    a = steering_vector(geom, unit, freq_hz)
    s = 0j
    for w, av in zip(weights, a):
        s += w.conjugate() * av
    return s


def response_db(weights: list[Complex], geom: ArrayGeometry, unit: tuple[float, float, float], freq_hz: float) -> float:
    mag = abs(response(weights, geom, unit, freq_hz))
    if mag <= 0:
        return RESPONSE_FLOOR_DB
    return max(RESPONSE_FLOOR_DB, 20.0 * math.log10(mag))


def white_noise_gain_db(weights: list[Complex], geom: ArrayGeometry, look: Direction, freq_hz: float) -> float:
    """Array gain against spatially-white (self) noise, in dB.

    ``WNG = |wᴴ a(u0)|² / (wᴴ w)``. Aggressive nulling inflates ``wᴴ w`` and
    drives WNG down — the price of a deep null is amplified capsule noise. A
    healthy delay-and-sum beam sits near ``10·log10(M)``.
    """
    num = abs(response(weights, geom, look.unit, freq_hz)) ** 2
    den = sum((w.conjugate() * w).real for w in weights)
    if den <= 0 or num <= 0:
        return RESPONSE_FLOOR_DB
    return 10.0 * math.log10(num / den)


def directivity_index_db(weights: list[Complex], geom: ArrayGeometry, look: Direction, freq_hz: float) -> float:
    """Array gain against an isotropic (diffuse) noise field, in dB — i.e. how
    much better the beam captures the look direction than diffuse room/background
    noise. ``DI = |wᴴ a(u0)|² / (wᴴ Γ w)`` with Γ the diffuse coherence. This is
    the number that matters for "voice vs background"; superdirective beams push
    it well above a delay-and-sum beam's."""
    idx = geom.active_indices()
    wa = [weights[i] for i in idx]
    gamma = diffuse_coherence(geom, freq_hz)
    na = len(idx)
    den = 0j
    for i in range(na):
        for j in range(na):
            den += wa[i].conjugate() * gamma[i][j] * wa[j]
    num = abs(response(weights, geom, look.unit, freq_hz)) ** 2
    d = den.real
    if d <= 0 or num <= 0:
        return RESPONSE_FLOOR_DB
    return 10.0 * math.log10(num / d)


def _unit_from_az_offnadir(azimuth_deg: float, off_nadir_deg: float) -> tuple[float, float, float]:
    az = math.radians(azimuth_deg)
    nadir = math.radians(off_nadir_deg)
    sin_n = math.sin(nadir)
    return (sin_n * math.sin(az), sin_n * math.cos(az), -math.cos(nadir))


def beam_pattern_azimuth(
    weights: list[Complex],
    geom: ArrayGeometry,
    freq_hz: float,
    *,
    off_nadir_deg: float = 60.0,
    steps: int = 72,
) -> list[tuple[float, float]]:
    """Sweep azimuth at a fixed off-nadir; return ``(azimuth_deg, gain_db)``."""
    out: list[tuple[float, float]] = []
    for i in range(steps):
        az = 360.0 * i / steps
        u = _unit_from_az_offnadir(az, off_nadir_deg)
        out.append((az, response_db(weights, geom, u, freq_hz)))
    return out


# --------------------------------------------------------------------------- #
# Lobe analysis — where the beam picks up besides the target
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class LobeReport:
    """Structure of a beam's azimuth pattern at the target's elevation.

    A beam has one **main lobe** (toward the target) plus **side lobes** (smaller
    sensitivity peaks elsewhere). A side lobe within ``grating_threshold_db`` of
    the main lobe is a **grating lobe** — a person there is picked up almost as
    loudly as the target (spatial aliasing; happens on a sparse array at high
    frequency). ``side_lobes`` / ``grating_lobes`` are ``(azimuth_deg, level_db)``
    with level in dB **relative to the main lobe** (so ≤ 0)."""

    main_az_deg: float
    beamwidth_3db_deg: float          # −3 dB main-lobe width
    n_lobes: int                      # 1 main + side lobes
    side_lobes: tuple                 # ((az, level_db re main), …) strongest first
    peak_sidelobe_db: float           # worst off-target leak (re main)
    grating_lobes: tuple              # side lobes within grating_threshold of main
    off_nadir_deg: float
    freq_hz: float


def analyze_lobes(
    weights: list[Complex],
    geom: ArrayGeometry,
    freq_hz: float,
    *,
    off_nadir_deg: float = 60.0,
    steps: int = 720,
    grating_threshold_db: float = -3.0,
    sidelobe_floor_db: float = -25.0,
    min_sep_deg: float = 8.0,
) -> LobeReport:
    """Count and locate a beam's lobes (azimuth slice at ``off_nadir_deg``).

    ``sidelobe_floor_db`` ignores ripples weaker than this (re the main lobe);
    ``grating_threshold_db`` flags side lobes this close to the main as grating
    lobes (a real off-target pickup problem)."""
    pat = beam_pattern_azimuth(weights, geom, freq_hz, off_nadir_deg=off_nadir_deg, steps=steps)
    az = [p[0] for p in pat]
    g = [p[1] for p in pat]
    n = len(g)
    main_i = max(range(n), key=lambda i: g[i])
    main_db, main_az = g[main_i], az[main_i]
    rel = [x - main_db for x in g]                       # dB re main (main = 0)
    step_deg = 360.0 / steps

    # −3 dB main-lobe width
    left = 0
    while left < n and rel[(main_i - left) % n] > -3.0:
        left += 1
    right = 0
    while right < n and rel[(main_i + right) % n] > -3.0:
        right += 1
    beamwidth = (left + right) * step_deg

    # local maxima (circular) → side lobes away from the main lobe, above the floor
    peaks = [i for i in range(n) if rel[i] >= rel[(i - 1) % n] and rel[i] > rel[(i + 1) % n]]
    side = []
    for i in peaks:
        d = min(abs(az[i] - main_az), 360.0 - abs(az[i] - main_az))
        if d < max(min_sep_deg, beamwidth / 2.0):
            continue                                     # part of the main lobe
        if rel[i] < sidelobe_floor_db:
            continue                                     # negligible ripple
        side.append((az[i], rel[i]))
    side.sort(key=lambda s: -s[1])
    deduped: list[tuple[float, float]] = []
    for a, lv in side:
        if all(min(abs(a - a2), 360.0 - abs(a - a2)) >= min_sep_deg for a2, _ in deduped):
            deduped.append((a, lv))
    side = deduped
    grating = tuple((a, lv) for a, lv in side if lv >= grating_threshold_db)
    psl = max((lv for _a, lv in side), default=RESPONSE_FLOOR_DB)
    return LobeReport(
        main_az_deg=main_az,
        beamwidth_3db_deg=beamwidth,
        n_lobes=1 + len(side),
        side_lobes=tuple(side),
        peak_sidelobe_db=psl,
        grating_lobes=grating,
        off_nadir_deg=off_nadir_deg,
        freq_hz=freq_hz,
    )


def talker_leakage_db(config, array_id, geom, weights, freq_hz) -> list:
    """Per-placed-talker pickup level (dB, unity = 0) for a beam — i.e. how loudly
    each person is currently captured. The target ≈ 0 dB; everyone else should be
    well below. A high value for an out-of-area person means they leak through a
    side/grating lobe. Returns ``[(talker_id, label, gain_db, in_pickup), …]``."""
    from conf_pipeline import is_pickup_zone, point_in_shape  # noqa
    from .steering import look_direction

    dev = next((d for d in config.devices if d.id == array_id), None)
    pickup_shapes = [z.shape for z in getattr(dev, "zones", []) if is_pickup_zone(z)] if dev else []
    out = []
    for t in config.talkers:
        d = look_direction(config, array_id, t.position)
        gain = response_db(weights, geom, d.unit, freq_hz)
        in_pickup = any(point_in_shape(t.position, s) for s in pickup_shapes)
        out.append((t.id, t.label, gain, in_pickup))
    return out


# --------------------------------------------------------------------------- #
# Zone-driven design (the app-facing entry point)
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class ZoneBeam:
    """A beam designed for one pickup zone, with verification numbers."""

    zone_id: str
    label: str
    weights: tuple[Complex, ...]
    look: Direction
    pickup_gain_db: float            # response at the zone's own direction (~0 dB)
    wng_db: float                    # white-noise gain (robustness vs self-noise)
    di_db: float                     # directivity index (gain vs diffuse background)
    exclusion_atten_db: tuple[float, ...]  # gain at each exclusion direction (≤ 0 = good)
    nulled: bool                     # True if exclusion nulls were applied
    n_lobes: int = 1                 # main + side lobes
    peak_sidelobe_db: float = RESPONSE_FLOOR_DB   # worst off-target leak (re main)
    n_grating: int = 0               # grating lobes (near-full off-target pickup)
    n_nulls: int = 0                 # total nulls applied (exclusions + out-of-zone talkers)
    note: str = ""


# beamforming modes
MODE_DELAYSUM = "delaysum"
MODE_SUPERDIRECTIVE = "superdirective"


@dataclass(frozen=True)
class BeamDesign:
    array_id: str
    freq_hz: float
    geometry: ArrayGeometry
    beams: tuple[ZoneBeam, ...]
    exclusion_labels: tuple[str, ...]
    exclusion_dirs: tuple[Direction, ...] = ()   # exclusion-zone directions (for reporting)
    null_dirs: tuple[Direction, ...] = ()        # ALL nulls applied (exclusions + out-of-zone talkers)
    mode: str = MODE_SUPERDIRECTIVE
    loading: float = 0.05                        # diagonal loading (superdirective)

    def summary(self) -> str:
        mode_label = "superdirective" if self.mode == MODE_SUPERDIRECTIVE else "delay-and-sum"
        lines = [
            f"Beam design for {self.array_id} @ {self.freq_hz:.0f} Hz · {mode_label}"
            f" ({self.geometry.n_active}/{self.geometry.n_channels} capsules, "
            f"aperture {self.geometry.aperture_m()*100:.1f} cm)"
        ]
        if not self.beams:
            lines.append("  (no pickup zones — nothing to steer)")
        for b in self.beams:
            line = (
                f"  • {b.label or b.zone_id}: pickup {b.pickup_gain_db:+.1f} dB, "
                f"directivity {b.di_db:+.1f} dB, WNG {b.wng_db:+.1f} dB"
            )
            side = b.n_lobes - 1
            line += f"; lobes: 1 main + {side} side (peak {b.peak_sidelobe_db:+.0f} dB)"
            if b.n_nulls:
                line += f", {b.n_nulls} null(s)"
            if b.exclusion_atten_db:
                worst = max(b.exclusion_atten_db)  # closest to 0 = least suppressed
                line += f", worst excluded leak {worst:+.0f} dB"
            if b.n_grating:
                line += f"  ⚠ {b.n_grating} grating lobe(s) — off-target voices leak at near-full level"
            if b.note:
                line += f"  [{b.note}]"
            lines.append(line)
        return "\n".join(lines)


def bearing_direction(
    azimuth_deg: float,
    off_nadir_deg: float = 90.0,
    *,
    distance_m: float = 1.0,
    label: str = "",
) -> Direction:
    """A :class:`Direction` from a compass ``azimuth_deg`` (0° = +Y, clockwise)
    and ``off_nadir_deg`` (0° = straight down, **90° = horizontal**).

    The 90° default suits a **desk/table array** whose capsules sit in a
    horizontal plane and whose talkers are across the table at roughly the same
    height (a near-horizontal look). For a ceiling array looking down, use a
    smaller off-nadir. ``distance_m`` is informational (plane-wave design)."""
    return Direction(
        unit=_unit_from_az_offnadir(azimuth_deg, off_nadir_deg),
        azimuth_deg=azimuth_deg,
        off_nadir_deg=off_nadir_deg,
        distance_m=distance_m,
        label=label,
    )


def _coerce_direction(d) -> Direction:
    """Accept a :class:`Direction` or an ``(azimuth_deg, off_nadir_deg)`` tuple."""
    if isinstance(d, Direction):
        return d
    try:
        az, off = d
    except (TypeError, ValueError) as exc:
        raise TypeError(
            "expected a Direction or an (azimuth_deg, off_nadir_deg) tuple"
        ) from exc
    return bearing_direction(float(az), float(off))


def _beam_for_look(geom, look_dir, applied_nulls, freq_hz, mode, loading, *, label, base_note):
    """Build one verified :class:`ZoneBeam` toward ``look_dir`` nulling
    ``applied_nulls`` (shared across a multi-look design)."""
    def _weights(use_nulls):
        if mode == MODE_SUPERDIRECTIVE:
            return superdirective_weights(geom, look_dir, use_nulls, freq_hz, loading=loading)
        return (
            lcmv_weights(geom, look_dir, use_nulls, freq_hz)
            if use_nulls
            else delay_and_sum_weights(geom, look_dir, freq_hz)
        )

    note = base_note
    use_nulls = applied_nulls
    try:
        w = _weights(use_nulls)
    except ValueError as exc:
        use_nulls = []
        w = _weights([])
        note = f"no nulls: {exc}"

    atten = tuple(response_db(w, geom, d.unit, freq_hz) for d in applied_nulls)
    lobes = analyze_lobes(w, geom, freq_hz, off_nadir_deg=look_dir.off_nadir_deg)
    return ZoneBeam(
        zone_id="bearing",
        label=look_dir.label or label,
        weights=tuple(w),
        look=look_dir,
        pickup_gain_db=response_db(w, geom, look_dir.unit, freq_hz),
        wng_db=white_noise_gain_db(w, geom, look_dir, freq_hz),
        di_db=directivity_index_db(w, geom, look_dir, freq_hz),
        exclusion_atten_db=atten,
        nulled=bool(use_nulls),
        n_lobes=lobes.n_lobes,
        peak_sidelobe_db=lobes.peak_sidelobe_db,
        n_grating=len(lobes.grating_lobes),
        n_nulls=len(use_nulls),
        note=note,
    )


def _design_from_directions(geom, look_dirs, null_dirs, *, freq_hz, mode, loading, array_id):
    """Shared core: one shared null set, one beam per look direction."""
    budget = max(0, geom.n_active - 1)
    applied = list(null_dirs)[:budget]
    dropped = len(null_dirs) - len(applied)
    base_note = f"null budget {budget}: {dropped} dropped" if dropped else ""
    beams = tuple(
        _beam_for_look(geom, ld, applied, freq_hz, mode, loading, label="target", base_note=base_note)
        for ld in look_dirs
    )
    return BeamDesign(
        array_id=array_id,
        freq_hz=freq_hz,
        geometry=geom,
        beams=beams,
        exclusion_labels=tuple(d.label for d in applied),
        exclusion_dirs=tuple(applied),
        null_dirs=tuple(applied),
        mode=mode,
        loading=loading,
    )


def design_from_bearings(
    geom: ArrayGeometry,
    look,
    nulls=(),
    *,
    freq_hz: float = DEFAULT_DESIGN_FREQ_HZ,
    mode: str = MODE_SUPERDIRECTIVE,
    loading: float = 0.05,
    array_id: str = "array",
    label: str = "target",
) -> BeamDesign:
    """Design a single beam toward a **bearing** and null other bearings — the
    coverage-area feature without needing a room/zone :class:`SystemConfig`.

    Ideal for a **desk/table array**: say "listen toward this azimuth, reject
    those" directly. ``look`` and each entry of ``nulls`` is a :class:`Direction`
    or an ``(azimuth_deg, off_nadir_deg)`` tuple (see :func:`bearing_direction`,
    which defaults off-nadir to horizontal). ``mode`` is ``"superdirective"``
    (default) or ``"delaysum"``. Returns a one-beam :class:`BeamDesign` carrying
    the same verification numbers as :func:`design_zone_beams`, ready for
    :meth:`conf_pipeline_control.live.LiveBeamController.apply_design`.

    Nulls beyond the array's budget (``n_active − 1``) are dropped with a note; a
    null coinciding with the look direction falls back to no nulls (you cannot
    null an area you are also steering at)."""
    look_dir = _coerce_direction(look)
    null_dirs = [_coerce_direction(n) for n in nulls]
    return _design_from_directions(
        geom, [look_dir], null_dirs, freq_hz=freq_hz, mode=mode, loading=loading, array_id=array_id
    )


def design_multi_bearings(
    geom: ArrayGeometry,
    looks,
    nulls=(),
    *,
    freq_hz: float = DEFAULT_DESIGN_FREQ_HZ,
    mode: str = MODE_SUPERDIRECTIVE,
    loading: float = 0.05,
    array_id: str = "array",
) -> BeamDesign:
    """Multi-look version of :func:`design_from_bearings`: steer one beam at
    **each** of ``looks`` while nulling **each** of ``nulls`` (one shared null
    set). The live runtime sums the per-look beams into a single mixed output, so
    this captures several in-area talkers at once and rejects the out-of-area
    ones. Used by :mod:`conf_pipeline_control.autosteer` to turn the current DOA
    detections into a live beam. ``looks``/``nulls`` are :class:`Direction`\\ s or
    ``(azimuth_deg, off_nadir_deg)`` tuples. Empty ``looks`` ⇒ an empty design."""
    look_dirs = [_coerce_direction(d) for d in looks]
    null_dirs = [_coerce_direction(d) for d in nulls]
    return _design_from_directions(
        geom, look_dirs, null_dirs, freq_hz=freq_hz, mode=mode, loading=loading, array_id=array_id
    )


def design_zone_beams(
    config,
    array_id: str,
    geom: ArrayGeometry,
    *,
    freq_hz: float = DEFAULT_DESIGN_FREQ_HZ,
    null_exclusions: bool = True,
    mode: str = MODE_SUPERDIRECTIVE,
    loading: float = 0.05,
    suppress_outside_talkers: bool = False,
) -> BeamDesign:
    """Design one beam per pickup zone on ``array_id``, nulling exclusion zones.

    ``mode`` is ``"superdirective"`` (default — rejects diffuse background far
    better on a small array) or ``"delaysum"``. ``loading`` is the superdirective
    diagonal loading (robustness vs directivity). When ``suppress_outside_talkers``
    is set, every placed talker that is **not** inside a pickup zone is added as a
    null too — so people outside the pickup area are actively subtracted (up to the
    array's null budget, ``n_active − 1``). Pure: returns a :class:`BeamDesign`;
    falls back to fewer nulls (with a note) when the budget is exceeded.
    """
    from conf_pipeline import is_pickup_zone, point_in_shape  # noqa
    from .steering import exclusion_directions, look_direction, pickup_directions

    def _weights(look, use_nulls):
        if mode == MODE_SUPERDIRECTIVE:
            return superdirective_weights(geom, look, use_nulls, freq_hz, loading=loading)
        return lcmv_weights(geom, look, use_nulls, freq_hz) if use_nulls else delay_and_sum_weights(geom, look, freq_hz)

    pickups = pickup_directions(config, array_id)
    exclusions = exclusion_directions(config, array_id)
    excl_dirs = [d for _z, d in exclusions]
    excl_labels = tuple(z.label or z.id for z, _d in exclusions)

    # talkers outside every pickup zone → extra nulls ("subtract out-of-area voices")
    outside_dirs = []
    if suppress_outside_talkers:
        dev = next((d for d in config.devices if d.id == array_id), None)
        pickup_shapes = [z.shape for z in getattr(dev, "zones", []) if is_pickup_zone(z)] if dev else []
        for t in config.talkers:
            if not any(point_in_shape(t.position, s) for s in pickup_shapes):
                outside_dirs.append(look_direction(config, array_id, t.position))

    budget = max(0, geom.n_active - 1)
    wanted = (excl_dirs if null_exclusions else []) + outside_dirs
    applied_nulls = wanted[:budget]                 # same null set for every pickup beam
    dropped = len(wanted) - len(applied_nulls)

    beams: list[ZoneBeam] = []
    for zone, look in pickups:
        note = f"null budget {budget}: {dropped} dropped" if dropped else ""
        use_nulls = applied_nulls
        try:
            w = _weights(look, use_nulls)
        except ValueError as exc:
            use_nulls = []
            w = _weights(look, [])
            note = f"no nulls: {exc}"
        atten = tuple(response_db(w, geom, d.unit, freq_hz) for d in excl_dirs)
        lobes = analyze_lobes(w, geom, freq_hz, off_nadir_deg=look.off_nadir_deg)
        beams.append(
            ZoneBeam(
                zone_id=zone.id,
                label=zone.label,
                weights=tuple(w),
                look=look,
                pickup_gain_db=response_db(w, geom, look.unit, freq_hz),
                wng_db=white_noise_gain_db(w, geom, look, freq_hz),
                di_db=directivity_index_db(w, geom, look, freq_hz),
                exclusion_atten_db=atten,
                nulled=bool(use_nulls),
                n_lobes=lobes.n_lobes,
                peak_sidelobe_db=lobes.peak_sidelobe_db,
                n_grating=len(lobes.grating_lobes),
                n_nulls=len(use_nulls),
                note=note,
            )
        )
    return BeamDesign(
        array_id=array_id,
        freq_hz=freq_hz,
        geometry=geom,
        beams=tuple(beams),
        exclusion_labels=excl_labels,
        exclusion_dirs=tuple(excl_dirs),
        null_dirs=tuple(applied_nulls),
        mode=mode,
        loading=loading,
    )
