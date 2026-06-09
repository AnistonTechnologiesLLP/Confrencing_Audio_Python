"""Device templates (Designer-style presets)."""
from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Optional

from .coverage import create_microphone_array
from .devices import (
    create_codec,
    create_loudspeaker,
    create_processor,
    create_wired_mic,
    create_wireless_mic,
)
from .model import DspBlock


@dataclass
class DeviceTemplate:
    name: str
    device_type: str
    profile_id: Optional[str] = None
    dsp_blocks: list[DspBlock] = field(default_factory=list)
    transport: Optional[str] = None
    coverage_mode: Optional[str] = None


def device_template(name: str, device) -> DeviceTemplate:
    t = DeviceTemplate(name=name, device_type=device.type)
    t.profile_id = getattr(device, "profile_id", None)
    t.dsp_blocks = [DspBlock(b.id, b.kind, b.enabled, dict(b.params), b.target_bus_id) for b in (getattr(device, "dsp_blocks", []) or [])]
    if device.ports:
        t.transport = device.ports[0].transport
    if device.type == "microphoneArray":
        t.coverage_mode = device.coverage_mode
    return t


def instantiate_template(template: DeviceTemplate, id: str, label: str):
    transport = template.transport or "dante"
    t = template.device_type
    if t == "processor":
        dev = create_processor(id, label)
    elif t == "microphoneArray":
        dev = create_microphone_array(id, label, template.coverage_mode or "automatic")
    elif t == "wirelessMic":
        dev = create_wireless_mic(id, label, transport)
    elif t == "wiredMic":
        dev = create_wired_mic(id, label, transport)
    elif t == "loudspeaker":
        dev = create_loudspeaker(id, label, transport)
    else:
        dev = create_codec(id, label, transport)
    dev = copy.copy(dev)
    if template.profile_id is not None:
        dev.profile_id = template.profile_id
    dev.dsp_blocks = [DspBlock(f"{id}-{b.kind}-{i + 1}", b.kind, b.enabled, dict(b.params), b.target_bus_id) for i, b in enumerate(template.dsp_blocks)]
    return dev
