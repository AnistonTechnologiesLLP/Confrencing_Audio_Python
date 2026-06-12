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
    SystemConfig,
    Talker,
    WiredMic,
    WirelessMic,
    ZoneChannelRef,
    config_from_dict,
    default_elevation,
    find_device,
    find_port,
    find_talker,
    is_mic_device,
    is_pickup_zone,
    is_processor,
    point_in_polygon,
    point_in_rect,
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
    DeviceCapabilities,
    DeviceProfile,
    default_profile_id,
    device_capabilities,
    get_device_profile,
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

# ---- design report export ----
from .report import design_report  # noqa: F401

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
    SimulatedTransport,
    TransportError,
    online_room_status,
)
# ---- project file management: recent files, autosave, crash recovery ----
from .files import (  # noqa: F401
    RECENT_MAX,
    OpenResult,
    ProjectFileManager,
    RecoveryInfo,
    default_state_dir,
)
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
    add_coverage_zone,
    add_device,
    add_dsp_block,
    add_mute_group,
    add_talker,
    assign_device_profile,
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
    create_config,
    create_mute_group,
    create_talker,
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
