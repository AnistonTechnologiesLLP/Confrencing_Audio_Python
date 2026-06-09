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
    AecConfig,
    AutomixerChannel,
    AutomixerConfig,
    Bus,
    Codec,
    CoverageZone,
    Crosspoint,
    Device,
    Loudspeaker,
    MatrixMixer,
    MicrophoneArray,
    MuteLink,
    Point2D,
    PolygonShape,
    Port,
    Processor,
    RectShape,
    RoomLayout,
    RoomObject,
    Route,
    SystemConfig,
    Talker,
    WiredMic,
    WirelessMic,
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
    create_microphone_array,
    dedicated_zone,
    dynamic_zone,
    exclusion_zone,
    generate_array_output_ports,
    pickup_zone_count,
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

# ---- 1.8.0: deployment, naming, routing, templates, projects ----
from .deployment import DeploymentDiff, deployment_diff, mark_deployed, set_deployment_status  # noqa: F401
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
    MatrixAccessor,
    TalkerCoverage,
    add_coverage_zone,
    add_device,
    add_dsp_block,
    add_talker,
    assign_device_profile,
    remove_dsp_block,
    set_dsp_block_enabled,
    update_dsp_block,
    array_to_talker_angles,
    auto_configure,
    clear_device_elevation,
    clear_device_position,
    clear_room,
    configure_automixer,
    create_config,
    create_talker,
    matrix_for,
    rectangular_room,
    remove_coverage_zone,
    remove_device,
    remove_talker,
    rename_device,
    rename_talker,
    route,
    set_aec,
    set_coverage_mode,
    set_device_elevation,
    set_device_position,
    set_room,
    set_talker_elevation,
    set_talker_position,
    set_zone_shape,
    talker_coverage,
    talker_elevation,
    unroute,
)
