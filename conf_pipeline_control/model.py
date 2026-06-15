"""Shared constants for the host-side control / beamforming layer."""
from __future__ import annotations

# Talker mouth height used as the beamformer target plane (metres). Matches the
# engine's DEFAULT_TALKER_ELEVATION_M so canvas rays and beams agree.
DEFAULT_TARGET_ELEVATION_M = 1.2

# Reference design frequency (Hz). The beam design is verified per octave band
# across the speech band (see SPEECH_OCTAVE_CENTERS_HZ); this is the band the
# legacy single-frequency scalar fields are reported at. Override per call.
DEFAULT_DESIGN_FREQ_HZ = 1000.0

# Speech band covered by the wideband (subband) design verification.
SPEECH_BAND_LO_HZ = 250.0
SPEECH_BAND_HI_HZ = 8000.0

# Octave-band centers spanning the speech band — the default verification grid.
# The live runtime designs per FFT bin regardless; this grid sets the granularity
# of the *published* per-band numbers (pickup / DI / WNG / excluded leak).
SPEECH_OCTAVE_CENTERS_HZ: tuple[float, ...] = (250.0, 500.0, 1000.0, 2000.0, 4000.0, 8000.0)

# Third-octave centers (nominal) — the default grid for the DI/beamwidth-vs-
# frequency verification curves, where octave sampling is too coarse to draw.
SPEECH_THIRD_OCTAVE_CENTERS_HZ: tuple[float, ...] = (
    250.0, 315.0, 400.0, 500.0, 630.0, 800.0, 1000.0, 1250.0,
    1600.0, 2000.0, 2500.0, 3150.0, 4000.0, 5000.0, 6300.0, 8000.0,
)

# Beam-pattern response floor (dB) — values below this are reported as this.
RESPONSE_FLOOR_DB = -120.0
