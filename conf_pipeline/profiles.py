"""Vendor-neutral device capability profiles (v1.7.0). Mirrors the TS catalog."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from .model import DeviceType, DspBlockKind

_FULL = ["gain", "mute", "peq4", "agc", "compressor", "delay", "noiseReduction", "deverb"]
_MIC = ["gain", "mute", "peq4", "agc", "noiseReduction", "deverb"]
_SPK = ["gain", "mute", "peq4", "delay", "compressor"]


@dataclass
class DeviceCapabilities:
    aec: bool
    automix: bool
    mute: bool
    supported_blocks: list[DspBlockKind]
    max_coverage_zones: int
    coverage_angle_deg: Optional[float] = None  # full pickup cone (arrays only); None = no coverage geometry


@dataclass
class DeviceProfile:
    id: str
    label: str
    applies_to: list[DeviceType]
    capabilities: DeviceCapabilities
    port_defaults: dict = field(default_factory=dict)


def _cap(aec, automix, mute, blocks, zones, coverage_angle=None):
    return DeviceCapabilities(
        aec=aec, automix=automix, mute=mute, supported_blocks=list(blocks),
        max_coverage_zones=zones, coverage_angle_deg=coverage_angle,
    )


DEVICE_PROFILES: dict[str, DeviceProfile] = {
    "generic-ceiling-array": DeviceProfile("generic-ceiling-array", "Generic ceiling array", ["microphoneArray"], _cap(True, True, True, _MIC, 8, coverage_angle=120.0), {"danteOutputs": 1}),
    "generic-table-array": DeviceProfile("generic-table-array", "Generic table array", ["microphoneArray"], _cap(True, True, True, _MIC, 8, coverage_angle=130.0), {"danteOutputs": 1}),
    "generic-wireless-mic": DeviceProfile("generic-wireless-mic", "Generic wireless mic", ["wirelessMic"], _cap(True, True, True, ["gain", "mute", "peq4"], 0), {"danteOutputs": 1}),
    "generic-wired-mic": DeviceProfile("generic-wired-mic", "Generic wired mic", ["wiredMic"], _cap(True, True, True, ["gain", "mute", "peq4"], 0), {"analogOutputs": 1}),
    "generic-hardware-dsp": DeviceProfile("generic-hardware-dsp", "Generic hardware DSP", ["processor"], _cap(True, True, True, _FULL, 0), {"danteInputs": 8, "danteOutputs": 8, "analogInputs": 2, "analogOutputs": 2}),
    "generic-software-dsp": DeviceProfile("generic-software-dsp", "Generic software DSP", ["processor"], _cap(True, True, True, _FULL, 0), {"danteInputs": 16, "danteOutputs": 16}),
    "generic-loudspeaker": DeviceProfile("generic-loudspeaker", "Generic loudspeaker", ["loudspeaker"], _cap(False, False, True, _SPK, 0), {"analogInputs": 1}),
    "generic-codec": DeviceProfile("generic-codec", "Generic codec (far-end)", ["codec"], _cap(False, False, True, ["gain", "mute"], 0), {"danteInputs": 1, "danteOutputs": 1}),
    "generic-mute-control": DeviceProfile("generic-mute-control", "Generic mute/logic control", ["codec", "processor"], _cap(False, False, True, ["mute"], 0), {}),
}

FALLBACK_CAPABILITIES = _cap(True, True, True, _FULL, 8)


def default_profile_id(device_type: DeviceType) -> str:
    return {
        "microphoneArray": "generic-ceiling-array",
        "wirelessMic": "generic-wireless-mic",
        "wiredMic": "generic-wired-mic",
        "processor": "generic-hardware-dsp",
        "loudspeaker": "generic-loudspeaker",
        "codec": "generic-codec",
    }.get(device_type, "generic-hardware-dsp")


def get_device_profile(profile_id: Optional[str]) -> Optional[DeviceProfile]:
    return DEVICE_PROFILES.get(profile_id) if profile_id is not None else None


def device_capabilities(device) -> DeviceCapabilities:
    p = get_device_profile(getattr(device, "profile_id", None))
    return p.capabilities if p is not None else FALLBACK_CAPABILITIES
