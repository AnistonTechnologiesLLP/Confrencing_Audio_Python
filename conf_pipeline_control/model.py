"""Shared constants for the host-side control / beamforming layer."""
from __future__ import annotations

# Talker mouth height used as the beamformer target plane (metres). Matches the
# engine's DEFAULT_TALKER_ELEVATION_M so canvas rays and beams agree.
DEFAULT_TARGET_ELEVATION_M = 1.2

# Single design frequency for the beam weights (Hz). Speech-band representative;
# directivity of a small array is frequency-dependent, so this is the point at
# which the published gains/nulls hold. Override per call where needed.
DEFAULT_DESIGN_FREQ_HZ = 1000.0

# Beam-pattern response floor (dB) — values below this are reported as this.
RESPONSE_FLOOR_DB = -120.0
