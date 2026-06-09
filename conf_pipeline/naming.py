"""Naming conventions (Designer's 'simplified naming')."""
from __future__ import annotations

import copy

from .model import DeviceType, SystemConfig

TYPE_LABEL: dict[str, str] = {
    "microphoneArray": "Array",
    "processor": "Processor",
    "wirelessMic": "Wireless Mic",
    "wiredMic": "Wired Mic",
    "loudspeaker": "Loudspeaker",
    "codec": "Codec",
}


def apply_naming_scheme(config: SystemConfig) -> SystemConfig:
    counters: dict[str, int] = {}
    devices = []
    for d in config.devices:
        n = counters.get(d.type, 0) + 1
        counters[d.type] = n
        nd = copy.copy(d)
        nd.label = f"{TYPE_LABEL[d.type]} {n}"
        devices.append(nd)
    new = copy.copy(config)
    new.devices = devices
    return new


def suggested_label(config: SystemConfig, device_type: DeviceType) -> str:
    used = sum(1 for d in config.devices if d.type == device_type)
    return f"{TYPE_LABEL[device_type]} {used + 1}"


def label_collisions(config: SystemConfig) -> dict[str, list[str]]:
    by_label: dict[str, list[str]] = {}
    for d in config.devices:
        by_label.setdefault(d.label, []).append(d.id)
    return {k: v for k, v in by_label.items() if len(v) > 1}
