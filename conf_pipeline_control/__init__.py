"""Host-side **microphone control & beamforming** for the conferencing app.

The planning engine (:mod:`conf_pipeline`) decides *what connects to what*; this
package drives an **actual array microphone**. For arrays that expose only raw
multi-channel audio (e.g. a sensiBel 8-capsule array over USB), "coverage-area
selection" like a Shure MXA920 is done **on the host**: the pickup/exclusion
zones drawn in the app become beamformer weights that steer toward the areas you
want and null the areas you want left out / muted.

Two layers, deliberately split by dependency:

* **Design (pure stdlib, always importable):** :mod:`geometry`, :mod:`steering`,
  :mod:`beamformer` — array layout, zone→direction, delay-and-sum / LCMV
  null-steering, and beam-pattern verification. No numpy, no hardware.
* **Live (optional, behind the ``[control]`` extra):** :mod:`audio` + :mod:`live`
  — capture the array's channels and apply the weights in real time (numpy +
  sounddevice). Import-guarded: a clear error if the extra isn't installed.

Importing :mod:`conf_pipeline_control` never imports numpy/sounddevice.
"""
from __future__ import annotations

from .beamformer import (  # noqa: F401
    MODE_DELAYSUM,
    MODE_SUPERDIRECTIVE,
    BeamDesign,
    ZoneBeam,
    beam_pattern_azimuth,
    delay_and_sum_weights,
    design_zone_beams,
    diffuse_coherence,
    directivity_index_db,
    lcmv_weights,
    response,
    response_db,
    steering_vector,
    superdirective_weights,
    white_noise_gain_db,
)
from .control import MicController, MicState, SimulatedMicController  # noqa: F401
from .geometry import (  # noqa: F401
    SOUND_SPEED_MPS,
    ArrayGeometry,
    circular_array,
    sensibel_8,
    with_active_channels,
)
from .model import (  # noqa: F401
    DEFAULT_DESIGN_FREQ_HZ,
    DEFAULT_TARGET_ELEVATION_M,
    RESPONSE_FLOOR_DB,
)
from .steering import (  # noqa: F401
    Direction,
    exclusion_directions,
    look_direction,
    pickup_directions,
    zone_centroid,
    zone_look_direction,
)


def controls_available() -> bool:
    """True if the live-audio extra (numpy + sounddevice) is importable."""
    from .audio import controls_available as _ca

    return _ca()


__all__ = [
    "ArrayGeometry", "circular_array", "sensibel_8", "with_active_channels", "SOUND_SPEED_MPS",
    "Direction", "look_direction", "zone_centroid", "zone_look_direction",
    "pickup_directions", "exclusion_directions",
    "steering_vector", "delay_and_sum_weights", "lcmv_weights",
    "superdirective_weights", "diffuse_coherence", "directivity_index_db",
    "MODE_DELAYSUM", "MODE_SUPERDIRECTIVE", "response",
    "response_db", "white_noise_gain_db", "beam_pattern_azimuth",
    "design_zone_beams", "BeamDesign", "ZoneBeam",
    "MicController", "MicState", "SimulatedMicController",
    "DEFAULT_DESIGN_FREQ_HZ", "DEFAULT_TARGET_ELEVATION_M", "RESPONSE_FLOOR_DB",
    "controls_available",
]
