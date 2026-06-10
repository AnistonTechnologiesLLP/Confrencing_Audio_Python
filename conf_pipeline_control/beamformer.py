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
    exclusion_dirs: tuple[Direction, ...] = ()   # null directions (for the live runtime)
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
            if b.exclusion_atten_db:
                worst = max(b.exclusion_atten_db)  # closest to 0 = least suppressed
                line += f", worst excluded-area leak {worst:+.1f} dB"
            if b.note:
                line += f"  [{b.note}]"
            lines.append(line)
        return "\n".join(lines)


def design_zone_beams(
    config,
    array_id: str,
    geom: ArrayGeometry,
    *,
    freq_hz: float = DEFAULT_DESIGN_FREQ_HZ,
    null_exclusions: bool = True,
    mode: str = MODE_SUPERDIRECTIVE,
    loading: float = 0.05,
) -> BeamDesign:
    """Design one beam per pickup zone on ``array_id``, nulling exclusion zones.

    ``mode`` is ``"superdirective"`` (default — rejects diffuse background far
    better on a small array) or ``"delaysum"``. ``loading`` is the superdirective
    diagonal loading (robustness vs directivity). Pure: reads the config's
    zones/positions, returns a :class:`BeamDesign`. Falls back to a no-null beam
    (with a note) for any zone whose nulls are infeasible.
    """
    from .steering import exclusion_directions, pickup_directions  # local: avoid cycle at import

    def _weights(look, use_nulls):
        if mode == MODE_SUPERDIRECTIVE:
            return superdirective_weights(geom, look, use_nulls, freq_hz, loading=loading)
        return lcmv_weights(geom, look, use_nulls, freq_hz) if use_nulls else delay_and_sum_weights(geom, look, freq_hz)

    pickups = pickup_directions(config, array_id)
    exclusions = exclusion_directions(config, array_id)
    excl_dirs = [d for _z, d in exclusions]
    excl_labels = tuple(z.label or z.id for z, _d in exclusions)

    beams: list[ZoneBeam] = []
    for zone, look in pickups:
        note = ""
        use_nulls = excl_dirs if (null_exclusions and excl_dirs) else []
        try:
            w = _weights(look, use_nulls)
            nulled = bool(use_nulls)
        except ValueError as exc:
            w = _weights(look, [])
            nulled = False
            note = f"no nulls: {exc}"
        atten = tuple(response_db(w, geom, d.unit, freq_hz) for d in excl_dirs)
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
                nulled=nulled,
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
        mode=mode,
        loading=loading,
    )
