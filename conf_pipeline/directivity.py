# conf_pipeline/directivity.py
"""Aperture-aware beamwidth model (pure stdlib, numpy-free).

Honest directivity for the coverage simulation: a small array (e.g. the 40 mm
POLARIS) is near-omni at low speech frequencies and only mildly directive up
high, with a spatial-aliasing ceiling set by element spacing. The constants are
calibrated to the measured sensibel_8 beam (see tests/test_directivity_calibration.py).
"""
from __future__ import annotations

import math
from typing import Optional

SOUND_SPEED_MPS = 343.0
SIM_SPEECH_FREQ_HZ = 1500.0   # representative speech-band centre for the single-narrowband sim
NEAR_OMNI_HALF_DEG = 90.0     # a half-angle of 90 deg = no usable directivity in the look plane

# Calibration constants (refined in tests/test_directivity_calibration.py).
_BW_K = 0.55                  # 3 dB half-beamwidth ~= _BW_K * lambda / aperture (radians)
_ENDFIRE_WIDEN = 1.6          # extra widening as the look tilts toward endfire (off-nadir 90 deg)
_MIN_HALF_DEG = 30.0          # physical floor: a small circular array focuses no tighter than ~60 deg FWHM


def steered_beamwidth_deg(aperture_m: Optional[float], freq_hz: float, steer_deg: float) -> float:
    """3 dB main-lobe HALF-angle (deg) of the steered beam.

    ``steer_deg`` is the look angle off the array's broadside reference (0 = broadside,
    90 = endfire); the beam widens toward endfire. Returns ``NEAR_OMNI_HALF_DEG`` when the
    aperture/frequency are unknown or when the wavelength dwarfs the aperture (no directivity).
    """
    if not aperture_m or aperture_m <= 0.0 or freq_hz <= 0.0:
        return NEAR_OMNI_HALF_DEG
    lam = SOUND_SPEED_MPS / freq_hz
    half_deg = math.degrees(_BW_K * lam / aperture_m) / 2.0          # broadside half-angle
    widen = 1.0 + (_ENDFIRE_WIDEN - 1.0) * (min(abs(steer_deg), 90.0) / 90.0)
    return min(NEAR_OMNI_HALF_DEG, max(_MIN_HALF_DEG, half_deg * widen))


def alias_ceiling_hz(element_spacing_m: Optional[float]) -> float:
    """Spatial-aliasing ceiling (Hz) = c / (2 * spacing); ``inf`` when unknown."""
    if not element_spacing_m or element_spacing_m <= 0.0:
        return float("inf")
    return SOUND_SPEED_MPS / (2.0 * element_spacing_m)


def separable(sep_deg: float, beamwidth_half_deg: float, factor: float = 1.5) -> bool:
    """Two looks resolve when their angular separation exceeds ``factor`` x the half-beamwidth."""
    return sep_deg >= factor * beamwidth_half_deg
