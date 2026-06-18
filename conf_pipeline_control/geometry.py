"""Physical microphone-array geometry (pure stdlib).

The conferencing engine (:mod:`conf_pipeline`) models *zones* and *routing*; it
has no notion of the individual mic capsules inside an array. Host-side
beamforming (steering the pickup toward chosen areas, nulling excluded areas)
needs the actual capsule layout, so we describe it here.

Coordinates are a right-handed local frame centred on the array, in metres:
``x`` → room +X (east), ``y`` → room +Y (north), ``z`` → up. The capsules of a
ceiling array lie in the horizontal plane ``z = 0``; a talker on the floor below
is in a direction with a downward (−z) component. A purely planar array therefore
discriminates sources mainly by **azimuth / horizontal offset** — it cannot tell
two sources apart by range alone when they share a bearing. That is real array
physics, not a limitation of this code.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

# Speed of sound in air at ~20 °C, m/s. The beam geometry scales with c/f.
SOUND_SPEED_MPS = 343.0


@dataclass(frozen=True)
class ArrayGeometry:
    """Capsule layout of a real microphone array.

    ``elements`` are capsule positions ``(x, y, z)`` in metres in the array's
    local frame (origin = array centre). ``label`` is informational. ``active``
    is an optional per-capsule on/off mask (a dead or non-audio channel can be
    switched off); an empty tuple means **all capsules active**. Beamforming
    designs over the active capsules only and gives the rest zero weight, so the
    full-length weight vector still lines up with the device's channel count.
    """

    elements: tuple[tuple[float, float, float], ...]
    label: str = "array"
    active: tuple[bool, ...] = ()

    @property
    def n_channels(self) -> int:
        return len(self.elements)

    def active_indices(self) -> tuple[int, ...]:
        """Indices of the capsules currently in use (all, if no mask is set)."""
        if not self.active:
            return tuple(range(len(self.elements)))
        return tuple(i for i, on in enumerate(self.active) if on and i < len(self.elements))

    @property
    def n_active(self) -> int:
        return len(self.active_indices())

    def aperture_m(self) -> float:
        """Largest centre-to-centre spacing between any two active capsules (m)."""
        idx = self.active_indices()
        pts = [self.elements[i] for i in idx]
        best = 0.0
        for i in range(len(pts)):
            for j in range(i + 1, len(pts)):
                d = math.dist(pts[i], pts[j])
                if d > best:
                    best = d
        return best


def with_active_channels(geom: ArrayGeometry, active) -> ArrayGeometry:
    """Return a copy of ``geom`` with a per-capsule active mask.

    ``active`` is an iterable of bools, one per capsule. Raises if its length
    doesn't match the capsule count, or if it would leave no capsule active.
    """
    mask = tuple(bool(x) for x in active)
    if len(mask) != geom.n_channels:
        raise ValueError(f"active mask has {len(mask)} entries but array has {geom.n_channels} capsules")
    if not any(mask):
        raise ValueError("at least one capsule must stay active")
    return ArrayGeometry(elements=geom.elements, label=geom.label, active=mask)


def circular_array(
    n: int,
    radius_m: float,
    *,
    center_element: bool = False,
    label: str = "array",
    phase0_deg: float = 0.0,
) -> ArrayGeometry:
    """``n`` capsules equally spaced on a horizontal circle of ``radius_m``.

    ``center_element`` adds one capsule at the origin (some arrays have a centre
    mic). ``phase0_deg`` rotates the ring. Capsule 0 is at bearing ``phase0_deg``.
    """
    if n < 1:
        raise ValueError("n must be >= 1")
    if radius_m <= 0:
        raise ValueError("radius_m must be > 0")
    elems: list[tuple[float, float, float]] = []
    if center_element:
        elems.append((0.0, 0.0, 0.0))
    for m in range(n):
        ang = math.radians(phase0_deg) + 2.0 * math.pi * m / n
        elems.append((radius_m * math.cos(ang), radius_m * math.sin(ang), 0.0))
    return ArrayGeometry(elements=tuple(elems), label=label)


def sensibel_8(radius_m: float = 0.05) -> ArrayGeometry:
    """The sensiBel-style 8-capsule circular array.

    ``radius_m`` is the capsule-circle radius. **Set this to your array's actual
    radius** — the default (0.05 m) is a documented placeholder; the absolute
    beamwidth scales inversely with the radius, so the number matters for any
    quantitative reading. The relative behaviour (which way the beam steers,
    where nulls land) is correct for whatever radius you pass.
    """
    return circular_array(8, radius_m, label="sensiBel-8")
