"""Multi-azimuth direction-of-arrival (DOA) for the array — SRP-PHAT.

A circular array is strong at **azimuth** (full 360°, no front/back ambiguity),
even on a small aperture where it can barely *separate* sources. This module
exploits that: it scans azimuth with a steered-response-power map (PHAT-weighted
for reverb robustness) and peak-picks the directions of the people currently
talking. Those bearings drive the live extractor (steer a beam at each in-area
talker, null the rest) in :mod:`conf_pipeline_control.autosteer`.

Inputs are a per-frequency spatial covariance ``R(f)`` (M×M, Hermitian) over a
speech sub-band and the :class:`~conf_pipeline_control.geometry.ArrayGeometry`.
The math is numpy, imported lazily (inside functions) so importing this module —
and the whole app — works without the ``[control]`` extra, matching the rest of
the package.

Honest limits (designed around, not hidden):
- **Azimuth only.** A planar array cannot measure range; the coverage boundary is
  an angular sector, not a metric radius.
- **Resolution ≈ beamwidth** (~λ/aperture): two talkers closer than the array's
  beamwidth merge into one peak. ``min_separation_deg`` reflects this.
- **Spatial aliasing** above ``c/(2·spacing)``: scan only the speech band
  (``f_lo``…``f_hi``) or phantom peaks appear.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Optional

from .geometry import SOUND_SPEED_MPS, ArrayGeometry

# Speech sub-band for the scan. Low end set by SNR/aperture, high end kept below
# the array's spatial-aliasing frequency for a few-cm capsule spacing.
DEFAULT_F_LO_HZ = 300.0
DEFAULT_F_HI_HZ = 3800.0


def _unit_vectors(grid_deg, off_nadir_deg: float):
    """Unit look vectors for a grid of azimuths at a fixed off-nadir.

    Azimuth is a compass bearing (0° = +Y, clockwise); off-nadir 0° = straight
    down, 90° = horizontal (desk array). Matches the convention in
    :mod:`conf_pipeline_control.steering`."""
    import numpy as np

    az = np.radians(grid_deg)
    nadir = math.radians(off_nadir_deg)
    sin_n = math.sin(nadir)
    ux = sin_n * np.sin(az)
    uy = sin_n * np.cos(az)
    uz = np.full_like(az, -math.cos(nadir))
    return np.stack([ux, uy, uz], axis=1)            # (G, 3)


def band_indices(freqs_full, f_lo: float = DEFAULT_F_LO_HZ, f_hi: float = DEFAULT_F_HI_HZ):
    """Indices of the rfft bins inside the speech scan band ``[f_lo, f_hi]``."""
    import numpy as np

    return np.where((freqs_full >= f_lo) & (freqs_full <= f_hi))[0]


def steering_cube(positions_active, units, freqs):
    """Plane-wave steering vectors ``a(az, f)`` for every grid azimuth and band
    frequency: shape ``(n_freq, G, na)``, ``a_m = exp(+j·2π f/c·(p_m·u))``."""
    import numpy as np

    proj = units @ positions_active.T                # (G, na) = p·u
    k = (2.0 * np.pi / SOUND_SPEED_MPS) * freqs      # (n_freq,)
    phase = k[:, None, None] * proj[None, :, :]      # (n_freq, G, na)
    return np.exp(1j * phase)


def srp_phat_map(r_band, freqs, positions_active, grid_deg, off_nadir_deg: float):
    """Steered-response-power (PHAT) over the azimuth grid.

    ``r_band`` is ``(n_freq, na, na)`` covariance over the **active** capsules.
    PHAT normalizes each cross-spectrum by its magnitude (whitening), which makes
    the peak location robust to spectral colour and reverberation. Returns a real
    power per grid azimuth, shape ``(G,)``."""
    import numpy as np

    rhat = r_band / (np.abs(r_band) + 1e-12)         # PHAT whitening, per entry
    units = _unit_vectors(grid_deg, off_nadir_deg)   # (G, 3)
    a = steering_cube(positions_active, units, freqs)  # (n_freq, G, na)
    # steered power per azimuth: aᴴ R̂ a, summed over the band
    ra = np.einsum("fij,fgj->fgi", rhat, a)          # R̂ a  → (n_freq, G, na)
    p = np.einsum("fgi,fgi->g", np.conj(a), ra).real  # Σ_f Σ_i conj(a_i)·(R̂a)_i
    return p


@dataclass
class Detection:
    """One detected talker direction."""

    azimuth_deg: float
    salience_db: float                # peak height over the map's median (VAD-ish)
    in_sector: bool = False           # set by sector_gate()


@dataclass
class DoaResult:
    detections: list                  # list[Detection], strongest first
    grid_deg: object = field(repr=False)        # numpy array of grid azimuths
    power_db: object = field(repr=False)        # SRP map in dB re median
    active: bool = False              # any source above the VAD floor?


def _circular_sep(a: float, b: float) -> float:
    """Smallest absolute angular separation between two bearings (deg)."""
    d = abs(a - b) % 360.0
    return min(d, 360.0 - d)


def _pick_peaks(grid_deg, power_db, *, max_talkers, min_separation_deg, min_salience_db) -> list:
    """Greedy multi-peak pick on a circular azimuth grid."""
    n = len(power_db)
    cand = [
        i for i in range(n)
        if power_db[i] >= power_db[(i - 1) % n] and power_db[i] > power_db[(i + 1) % n]
    ]
    cand.sort(key=lambda i: power_db[i], reverse=True)
    out: list = []
    for i in cand:
        if power_db[i] < min_salience_db:
            break                                    # sorted: nothing better remains
        az = float(grid_deg[i])
        if all(_circular_sep(az, d.azimuth_deg) >= min_separation_deg for d in out):
            out.append(Detection(azimuth_deg=az, salience_db=float(power_db[i])))
        if len(out) >= max_talkers:
            break
    return out


def detect(
    r_band,
    freqs,
    geom: ArrayGeometry,
    *,
    off_nadir_deg: float = 90.0,
    grid_step_deg: float = 2.0,
    max_talkers: int = 3,
    min_separation_deg: float = 40.0,
    min_salience_db: float = 3.0,
    vad_floor_db: float = 3.0,
) -> DoaResult:
    """Detect up to ``max_talkers`` talker azimuths from a band covariance.

    ``r_band`` is ``(n_freq, M, M)`` over **all** capsules (this slices to the
    active ones via the geometry mask). ``min_salience_db`` rejects weak peaks;
    ``min_separation_deg`` reflects the array's angular resolution. ``vad_floor_db``
    sets the overall "is anyone talking?" gate (peak-to-median of the map)."""
    import numpy as np

    idx = np.array(geom.active_indices(), dtype=int)
    positions = np.array([geom.elements[i] for i in idx], dtype=float)   # (na, 3)
    r_active = r_band[:, idx[:, None], idx[None, :]]                     # (n_freq, na, na)

    grid = np.arange(0.0, 360.0, grid_step_deg)
    p = srp_phat_map(r_active, freqs, positions, grid, off_nadir_deg)
    med = float(np.median(p)) if p.size else 0.0
    if med <= 0:
        med = float(np.max(p)) if p.size and np.max(p) > 0 else 1.0
    power_db = 10.0 * np.log10(np.maximum(p, 1e-12) / med)
    active = bool(power_db.size and float(np.max(power_db)) >= vad_floor_db)

    detections = (
        _pick_peaks(
            grid, power_db,
            max_talkers=max_talkers,
            min_separation_deg=min_separation_deg,
            min_salience_db=min_salience_db,
        )
        if active else []
    )
    return DoaResult(detections=detections, grid_deg=grid, power_db=power_db, active=active)


# --------------------------------------------------------------------------- #
# Sector ("radius") gate — keep only talkers inside the coverage arc
# --------------------------------------------------------------------------- #
def in_sector(azimuth_deg: float, center_deg: float, half_width_deg: float, *, front_offset_deg: float = 0.0) -> bool:
    """True if ``azimuth_deg`` lies within ``center ± half_width`` (wrap-aware).

    ``front_offset_deg`` rotates the array's azimuth-0 reference to the room's
    "front" so the sector is expressed in user terms."""
    rel = _circular_sep(azimuth_deg - front_offset_deg, center_deg)
    return rel <= half_width_deg


def sector_gate(detections: list, center_deg: float, half_width_deg: float, *, front_offset_deg: float = 0.0) -> list:
    """Mark each detection's ``in_sector`` flag (mutates and returns the list)."""
    for d in detections:
        d.in_sector = in_sector(
            d.azimuth_deg, center_deg, half_width_deg, front_offset_deg=front_offset_deg
        )
    return detections


# --------------------------------------------------------------------------- #
# Offline detection — scan a recorded 8-channel clip (for tuning)
# --------------------------------------------------------------------------- #
def covariance_from_clip(
    y8,
    sr: float,
    *,
    frame: int = 1024,
    hop: Optional[int] = None,
    f_lo: float = DEFAULT_F_LO_HZ,
    f_hi: float = DEFAULT_F_HI_HZ,
):
    """Band covariance ``R(f)`` averaged over a clip's frames.

    ``y8`` is ``(M, samples)`` float. Returns ``(r_band (n_freq, M, M), freqs)``
    restricted to the speech band. Hann-windowed STFT; same framing as the live
    runtime so offline tuning matches live behaviour."""
    import numpy as np

    if hop is None:
        hop = frame // 2
    m, nT = y8.shape
    win = np.hanning(frame)
    freqs_full = np.fft.rfftfreq(frame, d=1.0 / sr)
    bidx = band_indices(freqs_full, f_lo, f_hi)
    freqs = freqs_full[bidx]
    acc = np.zeros((len(bidx), m, m), dtype=complex)
    nframes = 0
    for start in range(0, max(1, nT - frame), hop):
        block = y8[:, start:start + frame] * win
        x = np.fft.rfft(block, axis=1)[:, bidx]      # (M, n_freq)
        xb = x.T                                      # (n_freq, M)
        acc += xb[:, :, None] * np.conj(xb[:, None, :])
        nframes += 1
    if nframes:
        acc /= nframes
    return acc, freqs


def detect_offline(
    y8,
    sr: float,
    geom: ArrayGeometry,
    *,
    f_lo: float = DEFAULT_F_LO_HZ,
    f_hi: float = DEFAULT_F_HI_HZ,
    **detect_kwargs,
) -> DoaResult:
    """Detect talker azimuths in a recorded ``(M, samples)`` clip — for tuning
    thresholds before going live. Extra kwargs pass through to :func:`detect`."""
    r_band, freqs = covariance_from_clip(y8, sr, f_lo=f_lo, f_hi=f_hi)
    return detect(r_band, freqs, geom, **detect_kwargs)
