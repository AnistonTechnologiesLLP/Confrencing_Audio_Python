"""Conferencing Audio Pipeline — configuration & signal-routing control plane.

A framework-agnostic Python port of the TypeScript engine. Models *what connects
to what* and validates correctness (above all the AEC self-reference rule); it
does NOT process real audio. JSON is interoperable with the TS/browser version.
"""
from __future__ import annotations

# ---- model ----
from .model import (  # noqa: F401
    CONFIG_VERSION,
    DEFAULT_DEDICATED_ZONE_SIZE_M,
    DEFAULT_TALKER_ELEVATION_M,
    GATING_SENSITIVITY_MAX,
    GATING_SENSITIVITY_MIN,
    MAX_MANUAL_LOBES,
    MAX_ZONES_PER_ARRAY,
    NLP_LEVELS,
    ZONE_GAIN_DB_MAX,
    ZONE_GAIN_DB_MIN,
    AecConfig,
    AutomixerChannel,
    AutomixerConfig,
    Bus,
    Codec,
    ConferencingCamera,
    ControlConfig,
    CoverageZone,
    Crosspoint,
    Device,
    Loudspeaker,
    MatrixMixer,
    MicrophoneArray,
    MuteGroup,
    MuteLink,
    Point2D,
    PolygonShape,
    Port,
    Processor,
    RectShape,
    RoomBackground,
    RoomLayout,
    RoomObject,
    Route,
    Scene,
    SceneSchedule,
    SceneSteer,
    SceneZoneState,
    SeatAnchor,
    SystemConfig,
    Talker,
    WEEKDAYS,
    WiredMic,
    WirelessMic,
    ZoneChannelRef,
    angular_separation_deg,
    bearing_to_deg,
    config_from_dict,
    default_elevation,
    find_device,
    find_port,
    find_talker,
    is_camera,
    is_mic_device,
    is_pickup_zone,
    is_processor,
    obb_corners,
    point_in_polygon,
    point_in_rect,
    point_in_sector,
    point_in_shape,
    to_jsonable,
)

# ---- DSP blocks ----
from .model import (  # noqa: F401
    DSP_BLOCK_KINDS,
    DSP_RANGES,
    PEQ_BAND_TYPES,
    PEQ_MAX_BANDS,
    DspBlock,
)

# ---- subsystems ----
from . import matrix  # noqa: F401
from .angles import Point3D, SteeringAngles, steering_angles  # noqa: F401
from .profiles import (  # noqa: F401
    DEVICE_PROFILES,
    FALLBACK_CAPABILITIES,
    CameraSpec,
    DeviceCapabilities,
    DeviceProfile,
    SpeakerSpec,
    default_profile_id,
    device_capabilities,
    get_device_profile,
)
from .furniture import (  # noqa: F401
    DEFAULT_FURNITURE_KIND,
    FURNITURE_CATALOG,
    FURNITURE_KINDS,
    FurnitureType,
    blocks_audio as furniture_blocks_audio,
    blocks_camera as furniture_blocks_camera,
    furniture_corners,
    furniture_type,
    resolved_absorption,
    resolved_dimensions,
)
from .blocks import create_dsp_block, default_peq_band, dsp_block_param_issues  # noqa: F401
from .coverage import (  # noqa: F401
    CoverageError,
    add_coverage_zone as array_add_coverage_zone,
    auto_assign_zone_channels as array_auto_assign_zone_channels,
    create_microphone_array,
    dedicated_zone,
    dynamic_zone,
    exclusion_zone,
    generate_array_output_ports,
    pickup_zone_count,
    set_zone_gain_db as array_set_zone_gain_db,
    set_zone_output_channel as array_set_zone_output_channel,
)
from .devices import (  # noqa: F401
    create_camera,
    create_codec,
    create_loudspeaker,
    create_processor,
    create_wired_mic,
    create_wireless_mic,
)
from .dsp import (  # noqa: F401
    AecReferenceAnalysis,
    SourceSignal,
    analyze_aec_reference,
    any_codec_sync_muted,
    automixer_channel,
    create_automixer,
    create_mute_link,
    get_primary_processor,
    is_mic_reinforced,
    is_valid_gating_sensitivity,
    muted_indicator_devices,
    output_buses_feeding_loudspeakers,
    processor_input_buses_for_device,
    set_automix_output,
    set_muted,
    set_nlp,
    sources_feeding_output_bus,
    upsert_channel,
)
from .validation import (  # noqa: F401
    CODE_DESCRIPTIONS,
    ValidationIssue,
    ValidationResult,
    validate,
)
from .persistence import DeserializeError, deserialize, serialize  # noqa: F401

# ---- geometric coverage check (array pickup circles) ----
from .coverage_check import (  # noqa: F401
    CoverageReport,
    ZoneCoverageReport,
    ZoneCoverageStatus,
    array_coverage_circle,
    array_coverage_radius,
    coverage_report,
    zone_coverage_report,
)

# ---- room/device coverage simulation (cameras, mics, speakers, occlusion) ----
from .coverage_sim import (  # noqa: F401
    CameraCoverage,
    CoverageWedge,
    MicCoverage,
    Occluder,
    RoomCoverage,
    SpeakerCoverage,
    Target,
    TargetHit,
    camera_coverage,
    camera_sees,
    camera_wedge,
    mic_coverage,
    room_occluders,
    room_targets,
    segment_intersects_obb,
    simulate_room_coverage,
    speaker_coverage,
    speaker_wedge,
)

# ---- room-aware seat mapping export ----
from .seat_mapper import (  # noqa: F401
    SeatMatch,
    azimuth_for_array_point,
    azimuth_in_pickup_zone,
    exclusion_zone_azimuths,
    nearest_seat,
    nearest_seat_for_array,
    room_seats,
    seat_azimuth_for_array,
    seat_null_azimuths,
    seats_owned_by_array,
)

# ---- design report export ----
from .report import CommissioningInfo, commissioning_report, design_report  # noqa: F401

# ---- placement simulation / recommendation ----
from .sim import (  # noqa: F401
    Candidate,
    Heatmap,
    PlacementScore,
    Recommendation,
    SimParams,
    ValidationResult as SimValidationResult,
    available_backends,
    estimated_rt60,
    numpy_available,
    recommend_placement,
    score_heatmap,
    score_placement,
    validate_recommendation,
)

# ---- 1.8.0: deployment, naming, routing, templates, projects ----
from .deployment import DeploymentDiff, deployment_diff, mark_deployed, set_deployment_status  # noqa: F401
# ---- commissioning: the device-facing transport seam (simulated backend) ----
from .transport import (  # noqa: F401
    DeviceStatus,
    DeviceTransport,
    DiscoveredDevice,
    OnlineDeviceState,
    PushReport,
    ReconcileEntry,
    SimulatedTransport,
    TransportError,
    online_room_status,
    push_to_online,
    reconcile_online,
)
# ---- project file management: recent files, autosave, crash recovery ----
from .files import (  # noqa: F401
    RECENT_MAX,
    OpenResult,
    ProjectFileManager,
    RecoveryInfo,
    default_state_dir,
)
# ---- local HTTP control API: scene recall / mute / status ----
from .control_api import ConfigHolder, ControlApiServer  # noqa: F401
# ---- scene scheduler: recall a scene at a time ----
from .scheduler import SceneScheduler  # noqa: F401
from .naming import TYPE_LABEL, apply_naming_scheme, label_collisions, suggested_label  # noqa: F401
from .routing import (  # noqa: F401
    Subscription,
    dante_subscriptions,
    routing_summary,
    signal_flow_report,
    subscriptions,
)
from .templates import DeviceTemplate, device_template, instantiate_template  # noqa: F401
from .project import (  # noqa: F401
    PROJECT_VERSION,
    Project,
    ProjectRoom,
    add_room,
    create_project,
    deserialize_project,
    get_active_room,
    get_room,
    remove_room,
    rename_room,
    serialize_project,
    set_active_room,
    update_room,
)

# ---- public builder API ----
from .api import (  # noqa: F401
    AutoRouteResult,
    MatrixAccessor,
    OptimizeRoomResult,
    TalkerCoverage,
    add_camera,
    add_coverage_zone,
    add_device,
    add_dsp_block,
    add_furniture,
    add_mute_group,
    add_talker,
    assign_device_profile,
    remove_furniture,
    set_array_bearing,
    set_camera_bearing,
    set_camera_tilt,
    set_furniture_dimensions,
    set_furniture_position,
    set_furniture_rotation,
    set_seat_anchors,
    set_speaker_bearing,
    set_speaker_tilt,
    auto_assign_zone_channels,
    remove_dsp_block,
    set_dsp_block_enabled,
    update_dsp_block,
    array_to_talker_angles,
    auto_configure,
    auto_route,
    clear_device_elevation,
    clear_device_position,
    clear_room,
    configure_automixer,
    add_scene,
    add_scene_schedule,
    capture_scene,
    create_config,
    create_mute_group,
    create_scene,
    create_scene_schedule,
    create_talker,
    get_scene,
    recall_scene,
    remove_scene,
    remove_scene_schedule,
    set_scene_schedule_enabled,
    matrix_for,
    optimize_room,
    rectangular_room,
    remove_coverage_zone,
    remove_device,
    remove_mute_group,
    remove_talker,
    rename_device,
    rename_talker,
    route,
    set_aec,
    set_coverage_mode,
    set_device_elevation,
    set_device_position,
    set_mute_group_muted,
    set_room,
    set_room_background,
    set_room_background_opacity,
    set_room_background_scale,
    clear_room_background,
    calibrated_scale,
    set_talker_elevation,
    set_talker_position,
    set_zone_gain_db,
    set_zone_output_channel,
    set_zone_shape,
    talker_coverage,
    talker_elevation,
    unroute,
)
