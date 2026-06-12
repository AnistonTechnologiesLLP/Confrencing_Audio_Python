"""DSP configuration: AEC reference resolution, automixer, mute linking."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from . import matrix as mx
from .model import (
    GATING_SENSITIVITY_MAX,
    GATING_SENSITIVITY_MIN,
    NLP_LEVELS,
    AutomixerChannel,
    AutomixerConfig,
    Crosspoint,
    MuteLink,
    NlpLevel,
    Processor,
    SystemConfig,
    find_device,
    find_port,
    is_processor,
)

# --------------------------------------------------------------------------- #
# AEC reference resolution
# --------------------------------------------------------------------------- #
@dataclass
class SourceSignal:
    device_id: str
    port_id: str
    input_bus_id: str


@dataclass
class AecReferenceAnalysis:
    mic_id: str
    reference_bus_id: Optional[str]
    reference_sources: list[SourceSignal] = field(default_factory=list)
    contains_own_signal: bool = False
    reinforced: bool = False
    reference_is_speaker_feed: bool = False


def get_primary_processor(config: SystemConfig) -> Optional[Processor]:
    by_matrix = find_device(config, config.matrix.processor_id)
    if by_matrix is not None and is_processor(by_matrix):
        return by_matrix
    for d in config.devices:
        if is_processor(d):
            return d
    return None


def processor_input_buses_for_device(config: SystemConfig, processor: Processor, device_id: str) -> list[str]:
    buses: list[str] = []
    seen = set()
    for route in config.routes:
        frm = find_port(config, route.from_port_id)
        to = find_port(config, route.to_port_id)
        if frm is None or to is None:
            continue
        if frm.device_id == device_id and to.device_id == processor.id and to.kind == "input":
            if to.id not in seen:
                seen.add(to.id)
                buses.append(to.id)
    return buses


def sources_feeding_output_bus(config: SystemConfig, processor: Processor, output_bus_id: str) -> list[SourceSignal]:
    result: list[SourceSignal] = []
    seen = set()
    for input_bus_id in mx.inputs_for_output(processor.matrix, output_bus_id):
        for route in config.routes:
            to = find_port(config, route.to_port_id)
            if to is None or to.id != input_bus_id:
                continue
            frm = find_port(config, route.from_port_id)
            if frm is None:
                continue
            key = f"{frm.id}->{input_bus_id}"
            if key in seen:
                continue
            seen.add(key)
            result.append(SourceSignal(device_id=frm.device_id, port_id=frm.id, input_bus_id=input_bus_id))
    return result


def output_buses_feeding_loudspeakers(config: SystemConfig, processor: Processor) -> set[str]:
    feeds: set[str] = set()
    for route in config.routes:
        frm = find_port(config, route.from_port_id)
        to = find_port(config, route.to_port_id)
        if frm is None or to is None:
            continue
        if frm.device_id != processor.id or frm.kind != "output":
            continue
        sink = find_device(config, to.device_id)
        if sink is not None and sink.type == "loudspeaker":
            feeds.add(frm.id)
    return feeds


def is_mic_reinforced(config: SystemConfig, processor: Processor, mic_id: str) -> bool:
    speaker_feeds = output_buses_feeding_loudspeakers(config, processor)
    if not speaker_feeds:
        return False
    for input_bus_id in processor_input_buses_for_device(config, processor, mic_id):
        for output_bus_id in mx.outputs_for_input(processor.matrix, input_bus_id):
            if output_bus_id in speaker_feeds:
                return True
    return False


def analyze_aec_reference(config: SystemConfig, processor: Processor, mic_id: str, reference_bus_id: Optional[str]) -> AecReferenceAnalysis:
    mic_input_buses = set(processor_input_buses_for_device(config, processor, mic_id))
    reference_sources: list[SourceSignal] = []
    contains_own = False
    ref_is_speaker_feed = False
    if reference_bus_id is not None:
        reference_sources = sources_feeding_output_bus(config, processor, reference_bus_id)
        contains_own = any(s.device_id == mic_id or s.input_bus_id in mic_input_buses for s in reference_sources)
        ref_is_speaker_feed = reference_bus_id in output_buses_feeding_loudspeakers(config, processor)
    return AecReferenceAnalysis(
        mic_id=mic_id,
        reference_bus_id=reference_bus_id,
        reference_sources=reference_sources,
        contains_own_signal=contains_own,
        reinforced=is_mic_reinforced(config, processor, mic_id),
        reference_is_speaker_feed=ref_is_speaker_feed,
    )


# --------------------------------------------------------------------------- #
# Automixer
# --------------------------------------------------------------------------- #
def create_automixer(processor_id: str) -> AutomixerConfig:
    return AutomixerConfig(processor_id=processor_id, channels=[], nlp="medium", output_bus_id=None)


def is_valid_gating_sensitivity(value: float) -> bool:
    try:
        return GATING_SENSITIVITY_MIN <= float(value) <= GATING_SENSITIVITY_MAX
    except (TypeError, ValueError):
        return False


def automixer_channel(input_bus_id: str, always_on: bool = False, gating_sensitivity: float = 0.5) -> AutomixerChannel:
    if not is_valid_gating_sensitivity(gating_sensitivity):
        raise ValueError(f"gating_sensitivity must be within [{GATING_SENSITIVITY_MIN}, {GATING_SENSITIVITY_MAX}], got {gating_sensitivity}.")
    return AutomixerChannel(input_bus_id=input_bus_id, always_on=always_on, gating_sensitivity=gating_sensitivity)


def upsert_channel(config: AutomixerConfig, channel: AutomixerChannel) -> AutomixerConfig:
    channels = [c for c in config.channels if c.input_bus_id != channel.input_bus_id] + [channel]
    return AutomixerConfig(config.processor_id, channels, config.nlp, config.output_bus_id)


def set_automix_output(config: AutomixerConfig, output_bus_id: str) -> AutomixerConfig:
    return AutomixerConfig(config.processor_id, list(config.channels), config.nlp, output_bus_id)


def set_nlp(config: AutomixerConfig, nlp: NlpLevel) -> AutomixerConfig:
    if nlp not in NLP_LEVELS:
        raise ValueError(f"Invalid NLP level: {nlp}")
    return AutomixerConfig(config.processor_id, list(config.channels), nlp, config.output_bus_id)


# --------------------------------------------------------------------------- #
# Mute linking
# --------------------------------------------------------------------------- #
def create_mute_link(id: str, processor_output_bus_id: str, linked_device_ids: list[str], sync_to_codec: bool = False, muted: bool = False) -> MuteLink:
    return MuteLink(id=id, processor_output_bus_id=processor_output_bus_id, linked_device_ids=list(linked_device_ids), sync_to_codec=sync_to_codec, muted=muted)


def set_muted(link: MuteLink, muted: bool) -> MuteLink:
    return MuteLink(link.id, link.processor_output_bus_id, list(link.linked_device_ids), link.sync_to_codec, muted)


def muted_indicator_devices(config: SystemConfig) -> set[str]:
    out: set[str] = set()
    for link in config.mute_links:
        if link.muted:
            out.update(link.linked_device_ids)
    return out


def any_codec_sync_muted(config: SystemConfig) -> bool:
    return any(link.muted and link.sync_to_codec for link in config.mute_links)
