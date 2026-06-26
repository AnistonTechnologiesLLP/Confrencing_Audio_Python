"""Vendor-neutral device capability profiles (v1.7.0). Mirrors the TS catalog."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from .model import DeviceType, DspBlockKind

_FULL = ["gain", "mute", "peq4", "agc", "compressor", "delay", "noiseReduction", "deverb"]
_MIC = ["gain", "mute", "peq4", "agc", "noiseReduction", "deverb"]
_SPK = ["gain", "mute", "peq4", "delay", "compressor"]


@dataclass
class CameraSpec:
    """Field-of-view / reach for a conferencing camera (v4). Lives on the device
    profile, mirroring how a mic array's ``coverage_angle_deg`` lives here. The
    coverage simulator draws a horizontal FOV wedge of half-angle ``fov_h_deg/2``
    and a 3D frustum using ``fov_v_deg``; ``zoom_min_fov_deg`` (if set) is the
    tightest framing a PTZ can reach."""

    fov_h_deg: float
    fov_v_deg: float
    max_range_m: float
    zoom_min_fov_deg: Optional[float] = None


@dataclass
class SpeakerSpec:
    """Dispersion / reach for a loudspeaker (v4). Drives the dispersion cone and a
    coarse inverse-distance SPL falloff in the coverage simulator."""

    dispersion_h_deg: float
    dispersion_v_deg: float
    max_range_m: float
    spl_1m_db: Optional[float] = None


@dataclass
class DeviceCapabilities:
    aec: bool
    automix: bool
    mute: bool
    supported_blocks: list[DspBlockKind]
    max_coverage_zones: int
    coverage_angle_deg: Optional[float] = None  # full pickup cone (arrays only); None = no coverage geometry
    aperture_m: Optional[float] = None          # physical array aperture (m); enables honest beamwidth. None = legacy 35 deg
    element_spacing_m: Optional[float] = None    # adjacent-capsule spacing (m); sets the spatial-aliasing ceiling
    camera: Optional[CameraSpec] = None         # FOV/range (camera profiles only)
    speaker: Optional[SpeakerSpec] = None       # dispersion/range (loudspeaker profiles)


@dataclass
class DeviceProfile:
    id: str
    label: str
    applies_to: list[DeviceType]
    capabilities: DeviceCapabilities
    port_defaults: dict = field(default_factory=dict)


def _cap(aec, automix, mute, blocks, zones, coverage_angle=None, aperture_m=None,
         element_spacing_m=None, camera=None, speaker=None):
    return DeviceCapabilities(
        aec=aec, automix=automix, mute=mute, supported_blocks=list(blocks),
        max_coverage_zones=zones, coverage_angle_deg=coverage_angle,
        aperture_m=aperture_m, element_spacing_m=element_spacing_m, camera=camera, speaker=speaker,
    )


DEVICE_PROFILES: dict[str, DeviceProfile] = {
    "generic-ceiling-array": DeviceProfile("generic-ceiling-array", "Generic ceiling array", ["microphoneArray"], _cap(True, True, True, _MIC, 8, coverage_angle=120.0), {"danteOutputs": 1}),
    "generic-table-array": DeviceProfile("generic-table-array", "Generic table array", ["microphoneArray"], _cap(True, True, True, _MIC, 8, coverage_angle=130.0), {"danteOutputs": 1}),
    "polaris-8": DeviceProfile("polaris-8", "sensiBel POLARIS (8-capsule, 40 mm)", ["microphoneArray"], _cap(True, True, True, _MIC, 8, coverage_angle=150.0, aperture_m=0.08, element_spacing_m=0.0306), {"danteOutputs": 1}),
    "generic-wireless-mic": DeviceProfile("generic-wireless-mic", "Generic wireless mic", ["wirelessMic"], _cap(True, True, True, ["gain", "mute", "peq4"], 0), {"danteOutputs": 1}),
    "generic-wired-mic": DeviceProfile("generic-wired-mic", "Generic wired mic", ["wiredMic"], _cap(True, True, True, ["gain", "mute", "peq4"], 0), {"analogOutputs": 1}),
    "generic-hardware-dsp": DeviceProfile("generic-hardware-dsp", "Generic hardware DSP", ["processor"], _cap(True, True, True, _FULL, 0), {"danteInputs": 8, "danteOutputs": 8, "analogInputs": 2, "analogOutputs": 2}),
    "generic-software-dsp": DeviceProfile("generic-software-dsp", "Generic software DSP", ["processor"], _cap(True, True, True, _FULL, 0), {"danteInputs": 16, "danteOutputs": 16}),
    "generic-loudspeaker": DeviceProfile("generic-loudspeaker", "Generic loudspeaker", ["loudspeaker"], _cap(False, False, True, _SPK, 0, speaker=SpeakerSpec(dispersion_h_deg=90.0, dispersion_v_deg=60.0, max_range_m=8.0, spl_1m_db=90.0)), {"analogInputs": 1}),
    "generic-codec": DeviceProfile("generic-codec", "Generic codec (far-end)", ["codec"], _cap(False, False, True, ["gain", "mute"], 0), {"danteInputs": 1, "danteOutputs": 1}),
    "generic-mute-control": DeviceProfile("generic-mute-control", "Generic mute/logic control", ["codec", "processor"], _cap(False, False, True, ["mute"], 0), {}),
    # v4 — conferencing cameras (coverage-only; no DSP, no audio routing required).
    "generic-ptz-camera": DeviceProfile("generic-ptz-camera", "Generic PTZ camera", ["camera"], _cap(False, False, False, [], 0, camera=CameraSpec(fov_h_deg=70.0, fov_v_deg=40.0, max_range_m=10.0, zoom_min_fov_deg=6.0)), {}),
    "generic-wide-camera": DeviceProfile("generic-wide-camera", "Generic wide-angle camera", ["camera"], _cap(False, False, False, [], 0, camera=CameraSpec(fov_h_deg=120.0, fov_v_deg=70.0, max_range_m=6.0)), {}),
    "generic-soundbar-camera": DeviceProfile("generic-soundbar-camera", "Generic soundbar camera", ["camera"], _cap(False, False, False, [], 0, camera=CameraSpec(fov_h_deg=110.0, fov_v_deg=60.0, max_range_m=5.0)), {}),
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
        "camera": "generic-ptz-camera",
    }.get(device_type, "generic-hardware-dsp")


def get_device_profile(profile_id: Optional[str]) -> Optional[DeviceProfile]:
    return DEVICE_PROFILES.get(profile_id) if profile_id is not None else None


def device_capabilities(device) -> DeviceCapabilities:
    p = get_device_profile(getattr(device, "profile_id", None))
    return p.capabilities if p is not None else FALLBACK_CAPABILITIES
