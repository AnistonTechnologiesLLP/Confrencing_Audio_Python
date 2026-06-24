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
    BandMetrics,
    BeamDesign,
    BeamFrequencyCurve,
    LobeReport,
    ZoneBeam,
    analyze_lobes,
    bearing_direction,
    beam_pattern_azimuth,
    delay_and_sum_weights,
    design_from_bearings,
    design_multi_bearings,
    design_zone_beams,
    diffuse_coherence,
    directivity_index_db,
    frequency_curves,
    lcmv_weights,
    response,
    response_db,
    steering_vector,
    superdirective_weights,
    talker_leakage_db,
    white_noise_gain_db,
)
from .control import MicController, MicState, SimulatedMicController  # noqa: F401
from .octovox_bridge import (  # noqa: F401
    OCTOVOX_DEFAULT_URL,
    CleanResult,
    OctovoxClient,
    ZoneAzimuths,
    octovox_deps_available,
    repair_dead_channels,
    to_octovox_azimuth,
    zone_azimuths,
)
from .octovox_monitor import CleanMonitor, MonitorState  # noqa: F401
from .ab_test import (  # noqa: F401
    ABReport,
    ABVariant,
    NullDepthReport,
    ab_compare,
    apply_design_offline,
    measure_null_depth,
    omni_reference,
    record_clip,
    save_ab_report,
)
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
    SPEECH_BAND_HI_HZ,
    SPEECH_BAND_LO_HZ,
    SPEECH_OCTAVE_CENTERS_HZ,
    SPEECH_THIRD_OCTAVE_CENTERS_HZ,
)
from .steering import (  # noqa: F401
    Direction,
    exclusion_directions,
    look_direction,
    pickup_directions,
    zone_centroid,
    zone_look_direction,
)
from .doa import (  # noqa: F401
    DEFAULT_F_HI_HZ,
    DEFAULT_F_LO_HZ,
    Detection,
    DoaResult,
    detect,
    detect_offline,
    in_any_sector,
    in_sector,
    sector_gate,
    sector_gate_multi,
)
from .tracking import AlphaBetaTracker, ExponentialTracker, Tracker, ValueSmoother  # noqa: F401
from .autosteer import AutoSteerController, SectorConfig  # noqa: F401
from .polaris_beamformer import (  # noqa: F401
    MODE_FRACDELAY,
    MODE_MVDR,
    MODE_RTF_MVDR,
    DeviceConfigError,
    DoaReading,
    PolarisBeamformer,
)
from .streaming_cleaner import StreamingCleaner, StreamingDereverb  # noqa: F401  (OCTOVOX-derived live cleaner + dereverb)
from .streaming_aec import StreamingAec  # noqa: F401  (streaming partitioned-block echo canceller)
from .reference_capture import ReferenceCapture  # noqa: F401  (far-end loopback reference for live AEC)
from .ab_capture import ABCapture, ABProofResult, write_ab_proof  # noqa: F401  (live raw-vs-cleaned proof tool)
from .virtual_mic_grid import VirtualMicGrid  # noqa: F401  (optional module — safe to delete)
from .beam_engine import BeamEngine, Location  # noqa: F401  (optional A/B wrapper — safe to delete)
from .agc import TargetLoudnessAgc  # noqa: F401  (shared target-loudness AGC)
from .preamp import HwGain, InputPreamp, PreampHost  # noqa: F401  (shared mic-input preamp / front-end gain)
from .multikit import KitSpec, KitStatus, MultiKitController  # noqa: F401  (dual-POLARIS cross-array automix)
from .multibeam import BeamStatus, MultiBeamController, MultiTrackRecorder  # noqa: F401  (single-array multi-talker "capture everyone")
from .multiroom import MultiRoomController, RoomKitSpec, RoomKitStatus  # noqa: F401  (combine N arrays → room-wide capture)
from .fence import (  # noqa: F401  (pure two-kit triangulation + soft fence decision)
    DEFAULT_FENCE_HOLD_TICKS,
    DEFAULT_FENCE_MARGIN_M,
    LEVEL_INSIDE_DB,
    FenceConfigError,
    FenceDecider,
    FenceDecision,
    FusedSource,
    KitPose,
    KitReading,
    Ray2D,
    closest_point_two_rays,
    crossing_confidence,
    fuse_position,
    level_cross_check,
    local_az_to_room_az,
    point_in_fence,
    ray_from_bearing,
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
    "MODE_DELAYSUM", "MODE_SUPERDIRECTIVE", "MODE_FRACDELAY", "MODE_MVDR", "MODE_RTF_MVDR", "response",
    "response_db", "white_noise_gain_db", "beam_pattern_azimuth",
    "analyze_lobes", "LobeReport", "talker_leakage_db",
    "design_zone_beams", "design_from_bearings", "design_multi_bearings",
    "bearing_direction", "BeamDesign", "ZoneBeam", "BandMetrics",
    "frequency_curves", "BeamFrequencyCurve",
    "detect", "detect_offline", "sector_gate", "sector_gate_multi", "in_sector", "in_any_sector",
    "Detection", "DoaResult",
    "DEFAULT_F_LO_HZ", "DEFAULT_F_HI_HZ",
    "AutoSteerController", "SectorConfig",
    "Tracker", "ValueSmoother", "ExponentialTracker", "AlphaBetaTracker",
    "PolarisBeamformer", "DoaReading", "DeviceConfigError", "StreamingCleaner", "StreamingDereverb",
    "StreamingAec", "ReferenceCapture", "ABCapture", "ABProofResult", "write_ab_proof",
    "VirtualMicGrid",
    "BeamEngine", "Location",
    "TargetLoudnessAgc", "InputPreamp", "HwGain", "PreampHost", "MultiKitController", "KitSpec", "KitStatus",
    "MultiBeamController", "BeamStatus", "MultiTrackRecorder",
    "MultiRoomController", "RoomKitSpec", "RoomKitStatus",
    "FenceConfigError", "FenceDecider", "FenceDecision", "FusedSource",
    "KitPose", "KitReading", "Ray2D",
    "DEFAULT_FENCE_MARGIN_M", "DEFAULT_FENCE_HOLD_TICKS", "LEVEL_INSIDE_DB",
    "ray_from_bearing", "local_az_to_room_az", "closest_point_two_rays",
    "crossing_confidence", "point_in_fence", "level_cross_check", "fuse_position",
    "MicController", "MicState", "SimulatedMicController",
    "DEFAULT_DESIGN_FREQ_HZ", "DEFAULT_TARGET_ELEVATION_M", "RESPONSE_FLOOR_DB",
    "SPEECH_BAND_LO_HZ", "SPEECH_BAND_HI_HZ", "SPEECH_OCTAVE_CENTERS_HZ",
    "SPEECH_THIRD_OCTAVE_CENTERS_HZ",
    "controls_available",
    "to_octovox_azimuth", "zone_azimuths", "ZoneAzimuths", "OctovoxClient",
    "CleanResult", "octovox_deps_available", "OCTOVOX_DEFAULT_URL",
    "repair_dead_channels", "CleanMonitor", "MonitorState",
    "ab_compare", "apply_design_offline", "omni_reference", "save_ab_report",
    "record_clip", "ABReport", "ABVariant",
    "measure_null_depth", "NullDepthReport",
]
