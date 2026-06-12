"""Validation engine: pure, deterministic validate(config) -> ValidationResult."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Literal

from .blocks import dsp_block_param_issues
from .dsp import analyze_aec_reference, get_primary_processor, is_valid_gating_sensitivity
from .model import (
    GATING_SENSITIVITY_MAX,
    GATING_SENSITIVITY_MIN,
    MAX_MANUAL_LOBES,
    MAX_ZONES_PER_ARRAY,
    NLP_LEVELS,
    WEEKDAYS,
    ZONE_GAIN_DB_MAX,
    ZONE_GAIN_DB_MIN,
    RectShape,
    SystemConfig,
    find_device,
    find_port,
    is_mic_device,
    is_pickup_zone,
    is_processor,
    parse_hhmm,
)
from .profiles import device_capabilities, get_device_profile

Severity = Literal["error", "warning"]

# Catalog of codes (documented in README).
CODE_DESCRIPTIONS: dict[str, str] = {
    "ORPHANED_ROUTE": "Route references a port id that does not exist.",
    "ROUTE_TRANSPORT_MISMATCH": "Route connects mismatched transports (dante/analog).",
    "ROUTE_DIRECTION_INVALID": "Route is not output->input.",
    "AEC_SELF_REFERENCE": "Mic's AEC reference contains the mic's own signal.",
    "AEC_REINFORCED_SHARED_REFERENCE": "Reinforced mic's AEC reference is the speaker-feed bus that carries it.",
    "AEC_REFERENCE_MISSING": "AEC enabled but no reference bus assigned.",
    "AEC_REFERENCE_EMPTY": "AEC reference bus resolves to zero source signals.",
    "COVERAGE_ZONE_LIMIT": "More than 8 coverage zones on an array.",
    "COVERAGE_ZONE_INVALID": "Zone has invalid type/always_on pairing or degenerate geometry.",
    "COVERAGE_CHANNEL_INVALID": "Coverage-area output channel is out of range or assigned to an exclusion zone.",
    "COVERAGE_CHANNEL_DUPLICATE": "Two coverage areas on the same array share an output channel.",
    "COVERAGE_GAIN_INVALID": "Coverage-area gain trim is out of range.",
    "CONTROL_MUTE_GROUP_INVALID": "A mute group references a missing device or coverage area, or is empty.",
    "SCENE_INVALID": "A scene is empty, duplicates another scene's id, or references a missing mute group, array, or coverage area.",
    "SCHEDULE_INVALID": "A scene schedule has a bad time/day, duplicates another schedule's id, or recalls a missing scene.",
    "MANUAL_LOBE_LIMIT": "Manual mode with more than 8 pickup lobes.",
    "AUTOMIXER_INVALID": "Automixer value out of range or output bus unresolved.",
    "DEVICE_PROFILE_UNKNOWN": "Device references a profile id not in the catalog.",
    "DEVICE_CAPABILITY_MISMATCH": "Device's profile does not apply to its type.",
    "DSP_BLOCK_UNSUPPORTED": "DSP block kind not supported by the device's profile.",
    "DSP_BLOCK_INVALID": "DSP block has out-of-range/invalid parameters.",
    "DSP_TARGET_UNRESOLVED": "DSP block target bus does not resolve on the device.",
    "AEC_NO_FAR_END": "AEC enabled but no far-end (codec) source exists.",
    "AUTOMIX_OUTPUT_UNSET": "Mics exist but the automixer output bus is unset.",
    "MUTE_LINK_UNSUPPORTED": "Mute link targets a device with no mute capability.",
    "DSP_CHAIN_NO_LEVEL": "Device has DSP blocks but no gain/mute stage.",
    "NAMING_DUPLICATE_LABEL": "Two or more devices share the same label.",
    "NAMING_EMPTY_LABEL": "A device has an empty label.",
}


@dataclass
class ValidationIssue:
    severity: Severity
    code: str
    message: str
    refs: list[str] = field(default_factory=list)


@dataclass
class ValidationResult:
    ok: bool
    errors: list[ValidationIssue]
    warnings: list[ValidationIssue]


def validate(config: SystemConfig) -> ValidationResult:
    issues: list[ValidationIssue] = []

    def add(severity: Severity, code: str, message: str, refs: list[str]) -> None:
        issues.append(ValidationIssue(severity, code, message, refs))

    _validate_routes(config, add)
    _validate_coverage(config, add)
    _validate_aec(config, add)
    _validate_automixer(config, add)
    _validate_profiles_and_blocks(config, add)
    _validate_commissioning(config, add)
    _validate_naming(config, add)
    _validate_control(config, add)

    errors = [i for i in issues if i.severity == "error"]
    warnings = [i for i in issues if i.severity == "warning"]
    return ValidationResult(ok=len(errors) == 0, errors=errors, warnings=warnings)


AddIssue = Callable[[Severity, str, str, list[str]], None]


def _validate_routes(config: SystemConfig, add: AddIssue) -> None:
    for route in config.routes:
        frm = find_port(config, route.from_port_id)
        to = find_port(config, route.to_port_id)
        if frm is None or to is None:
            missing = [m for m in ([route.from_port_id] if frm is None else []) + ([route.to_port_id] if to is None else [])]
            add("error", "ORPHANED_ROUTE",
                f'Route "{route.id}" references missing port(s): {", ".join(missing)}. '
                f"This typically happens when a coverage mode switch removed the port.",
                [route.id, *missing])
            continue
        if not (frm.kind == "output" and to.kind == "input"):
            add("error", "ROUTE_DIRECTION_INVALID",
                f'Route "{route.id}" must connect an output port to an input port (got {frm.kind} -> {to.kind}).',
                [route.id, frm.id, to.id])
        if frm.transport != to.transport:
            add("error", "ROUTE_TRANSPORT_MISMATCH",
                f'Route "{route.id}" connects mismatched transports ({frm.transport} -> {to.transport}); transports must match.',
                [route.id, frm.id, to.id])


def _validate_coverage(config: SystemConfig, add: AddIssue) -> None:
    for device in config.devices:
        if device.type != "microphoneArray":
            continue
        if len(device.zones) > MAX_ZONES_PER_ARRAY:
            add("error", "COVERAGE_ZONE_LIMIT", f'Array "{device.id}" has {len(device.zones)} zones; max is {MAX_ZONES_PER_ARRAY}.', [device.id])
        lobe_count = sum(1 for z in device.zones if z.type != "exclusion")
        if device.coverage_mode == "manual" and lobe_count > MAX_MANUAL_LOBES:
            add("error", "MANUAL_LOBE_LIMIT", f'Array "{device.id}" in manual mode has {lobe_count} pickup lobes; max is {MAX_MANUAL_LOBES}.', [device.id])
        channels_seen: dict[int, str] = {}
        for zone in device.zones:
            expected = zone.type == "dedicated"
            if zone.always_on != expected:
                add("error", "COVERAGE_ZONE_INVALID", f'Zone "{zone.id}" ({zone.type}) on array "{device.id}" must have always_on={expected}.', [device.id, zone.id])
            if isinstance(zone.shape, RectShape):
                bad = not (zone.shape.width > 0 and zone.shape.height > 0)
            else:
                bad = len(zone.shape.points) < 3
            if bad:
                add("error", "COVERAGE_ZONE_INVALID", f'Zone "{zone.id}" on array "{device.id}" has degenerate geometry.', [device.id, zone.id])
            # Per-area output channel + gain (v1.12.0).
            ch = zone.output_channel
            if ch is not None:
                if not is_pickup_zone(zone):
                    add("error", "COVERAGE_CHANNEL_INVALID", f'Exclusion zone "{zone.id}" on array "{device.id}" cannot carry an output channel.', [device.id, zone.id])
                elif not (1 <= ch <= MAX_ZONES_PER_ARRAY):
                    add("error", "COVERAGE_CHANNEL_INVALID", f'Zone "{zone.id}" on array "{device.id}" has output channel {ch}, out of range 1..{MAX_ZONES_PER_ARRAY}.', [device.id, zone.id])
                elif ch in channels_seen:
                    add("error", "COVERAGE_CHANNEL_DUPLICATE", f'Array "{device.id}" assigns output channel {ch} to both "{channels_seen[ch]}" and "{zone.id}".', [device.id, zone.id, channels_seen[ch]])
                else:
                    channels_seen[ch] = zone.id
            if zone.gain_db is not None and not (ZONE_GAIN_DB_MIN <= zone.gain_db <= ZONE_GAIN_DB_MAX):
                add("error", "COVERAGE_GAIN_INVALID", f'Zone "{zone.id}" on array "{device.id}" gain {zone.gain_db} dB is out of range [{ZONE_GAIN_DB_MIN}, {ZONE_GAIN_DB_MAX}].', [device.id, zone.id])


def _validate_aec(config: SystemConfig, add: AddIssue) -> None:
    processor = get_primary_processor(config)
    if processor is None:
        return
    has_far_end = any(d.type == "codec" for d in config.devices)
    for device in config.devices:
        if not is_mic_device(device):
            continue
        aec = device.aec
        if not aec.enabled:
            continue
        if aec.reference_bus_id is None:
            msg = f'Mic "{device.id}" has AEC enabled but no reference bus assigned'
            msg += " while a far-end (codec) source exists - assign a far-end reference." if has_far_end else "."
            add("warning", "AEC_REFERENCE_MISSING", msg, [device.id])
            continue
        analysis = analyze_aec_reference(config, processor, device.id, aec.reference_bus_id)
        if analysis.contains_own_signal:
            if analysis.reinforced and analysis.reference_is_speaker_feed:
                add("error", "AEC_REINFORCED_SHARED_REFERENCE",
                    f'Mic "{device.id}" is reinforced to the loudspeakers and its AEC reference is the same speaker-feed '
                    f'bus "{aec.reference_bus_id}" that carries it. Build a dedicated far-end-only reference bus '
                    f"(exclude this mic) and point the mic's AEC at it.",
                    [device.id, aec.reference_bus_id])
            else:
                add("error", "AEC_SELF_REFERENCE",
                    f'Mic "{device.id}"\'s AEC reference (bus "{aec.reference_bus_id}") contains the mic\'s own signal. '
                    f"The AEC would cancel the mic against itself, destroying its audio. Use a reference built from far-end sources only.",
                    [device.id, aec.reference_bus_id])
            continue
        if len(analysis.reference_sources) == 0:
            add("warning", "AEC_REFERENCE_EMPTY",
                f'Mic "{device.id}"\'s AEC reference bus "{aec.reference_bus_id}" has no sources routed to it - the AEC has nothing to cancel against.',
                [device.id, aec.reference_bus_id])


def _validate_automixer(config: SystemConfig, add: AddIssue) -> None:
    am = config.automixer
    if am.nlp not in NLP_LEVELS:
        add("error", "AUTOMIXER_INVALID", f'Automixer NLP level "{am.nlp}" is invalid.', [am.processor_id])
    for ch in am.channels:
        if not is_valid_gating_sensitivity(ch.gating_sensitivity):
            add("error", "AUTOMIXER_INVALID",
                f'Automixer channel "{ch.input_bus_id}" gating_sensitivity {ch.gating_sensitivity} is out of range [{GATING_SENSITIVITY_MIN}, {GATING_SENSITIVITY_MAX}].',
                [am.processor_id, ch.input_bus_id])


def _validate_profiles_and_blocks(config: SystemConfig, add: AddIssue) -> None:
    for device in config.devices:
        pid = getattr(device, "profile_id", None)
        if pid is not None:
            profile = get_device_profile(pid)
            if profile is None:
                add("error", "DEVICE_PROFILE_UNKNOWN", f'Device "{device.id}" references unknown profile "{pid}".', [device.id])
            elif device.type not in profile.applies_to:
                add("error", "DEVICE_CAPABILITY_MISMATCH",
                    f'Profile "{pid}" (for {"/".join(profile.applies_to)}) cannot be assigned to {device.type} "{device.id}".', [device.id])
        caps = device_capabilities(device)
        for block in getattr(device, "dsp_blocks", []) or []:
            if block.kind not in caps.supported_blocks:
                add("error", "DSP_BLOCK_UNSUPPORTED", f'DSP block "{block.kind}" is not supported by device "{device.id}" (profile {pid or "none"}).', [device.id, block.id])
            param_issues = dsp_block_param_issues(block)
            if param_issues:
                add("error", "DSP_BLOCK_INVALID", f'DSP block "{block.id}" on "{device.id}": {"; ".join(param_issues)}.', [device.id, block.id])
            if block.target_bus_id is not None:
                resolved = is_processor(device) and any(b.id == block.target_bus_id for b in device.buses)
                if not resolved:
                    add("error", "DSP_TARGET_UNRESOLVED", f'DSP block "{block.id}" on "{device.id}" targets unknown bus "{block.target_bus_id}".', [device.id, block.id])


def _validate_commissioning(config: SystemConfig, add: AddIssue) -> None:
    has_far_end = any(d.type == "codec" for d in config.devices)
    mics = [d for d in config.devices if is_mic_device(d)]
    processor = get_primary_processor(config)
    if not has_far_end and any(m.aec.enabled for m in mics):
        add("warning", "AEC_NO_FAR_END", "One or more mics have AEC enabled but there is no far-end (codec) source in the configuration.", [m.id for m in mics if m.aec.enabled])
    if processor is not None and mics and config.automixer.output_bus_id is None:
        add("warning", "AUTOMIX_OUTPUT_UNSET", "Microphones exist but the automixer output bus is not set.", [processor.id])
    for link in config.mute_links:
        for device_id in link.linked_device_ids:
            device = next((d for d in config.devices if d.id == device_id), None)
            if device is not None and not device_capabilities(device).mute:
                add("warning", "MUTE_LINK_UNSUPPORTED", f'Mute link "{link.id}" targets device "{device_id}" which has no mute capability.', [link.id, device_id])
    for device in config.devices:
        blocks = getattr(device, "dsp_blocks", []) or []
        if blocks and not any(b.kind in ("gain", "mute") for b in blocks):
            add("warning", "DSP_CHAIN_NO_LEVEL", f'Device "{device.id}" has a DSP chain with no gain or mute stage.', [device.id])


def _validate_control(config: SystemConfig, add: AddIssue) -> None:
    if config.control is None:
        return
    for group in config.control.mute_groups:
        if not group.device_ids and not group.zone_refs:
            add("error", "CONTROL_MUTE_GROUP_INVALID", f'Mute group "{group.id}" is empty (no devices or coverage areas).', [group.id])
        for did in group.device_ids:
            dev = find_device(config, did)
            if dev is None:
                add("error", "CONTROL_MUTE_GROUP_INVALID", f'Mute group "{group.id}" references missing device "{did}".', [group.id, did])
            elif not device_capabilities(dev).mute:
                add("warning", "MUTE_LINK_UNSUPPORTED", f'Mute group "{group.id}" includes device "{did}" which has no mute capability.', [group.id, did])
        for ref in group.zone_refs:
            arr = find_device(config, ref.array_id)
            if arr is None or arr.type != "microphoneArray":
                add("error", "CONTROL_MUTE_GROUP_INVALID", f'Mute group "{group.id}" references missing array "{ref.array_id}".', [group.id, ref.array_id])
            elif not any(z.id == ref.zone_id for z in arr.zones):
                add("error", "CONTROL_MUTE_GROUP_INVALID", f'Mute group "{group.id}" references missing zone "{ref.zone_id}" on array "{ref.array_id}".', [group.id, ref.array_id, ref.zone_id])
    group_ids = {g.id for g in config.control.mute_groups}
    seen_scene_ids: set[str] = set()
    for scene in config.control.scenes:
        if scene.id in seen_scene_ids:
            add("error", "SCENE_INVALID", f'Duplicate scene id "{scene.id}".', [scene.id])
        seen_scene_ids.add(scene.id)
        if not scene.mute_states and not scene.zone_states and not scene.steer:
            add("error", "SCENE_INVALID", f'Scene "{scene.id}" is empty (no mute states, zone states, or steer).', [scene.id])
        for gid in scene.mute_states:
            if gid not in group_ids:
                add("error", "SCENE_INVALID", f'Scene "{scene.id}" references missing mute group "{gid}".', [scene.id, gid])
        for zs in scene.zone_states:
            arr = find_device(config, zs.array_id)
            if arr is None or arr.type != "microphoneArray":
                add("error", "SCENE_INVALID", f'Scene "{scene.id}" references missing array "{zs.array_id}".', [scene.id, zs.array_id])
            elif not any(z.id == zs.zone_id for z in arr.zones):
                add("error", "SCENE_INVALID", f'Scene "{scene.id}" references missing zone "{zs.zone_id}" on array "{zs.array_id}".', [scene.id, zs.array_id, zs.zone_id])
        for st in scene.steer:
            arr = find_device(config, st.array_id)
            if arr is None or arr.type != "microphoneArray":
                add("error", "SCENE_INVALID", f'Scene "{scene.id}" steers a missing array "{st.array_id}".', [scene.id, st.array_id])
    scene_ids = {s.id for s in config.control.scenes}
    seen_schedule_ids: set[str] = set()
    for sched in config.control.schedules:
        if sched.id in seen_schedule_ids:
            add("error", "SCHEDULE_INVALID", f'Duplicate schedule id "{sched.id}".', [sched.id])
        seen_schedule_ids.add(sched.id)
        if sched.scene_id not in scene_ids:
            add("error", "SCHEDULE_INVALID", f'Schedule "{sched.id}" recalls missing scene "{sched.scene_id}".', [sched.id, sched.scene_id])
        if parse_hhmm(sched.time) is None:
            add("error", "SCHEDULE_INVALID", f'Schedule "{sched.id}" has invalid time "{sched.time}" (expected "HH:MM").', [sched.id])
        if not sched.days:
            add("error", "SCHEDULE_INVALID", f'Schedule "{sched.id}" has no days.', [sched.id])
        for day in sched.days:
            if day not in WEEKDAYS:
                add("error", "SCHEDULE_INVALID", f'Schedule "{sched.id}" has unknown day "{day}" (expected one of {", ".join(WEEKDAYS)}).', [sched.id])


def _validate_naming(config: SystemConfig, add: AddIssue) -> None:
    by_label: dict[str, list[str]] = {}
    for device in config.devices:
        if device.label.strip() == "":
            add("warning", "NAMING_EMPTY_LABEL", f'Device "{device.id}" has an empty label.', [device.id])
        by_label.setdefault(device.label, []).append(device.id)
    for label, ids in by_label.items():
        if len(ids) > 1 and label.strip() != "":
            add("warning", "NAMING_DUPLICATE_LABEL", f'{len(ids)} devices share the label "{label}".', ids)
