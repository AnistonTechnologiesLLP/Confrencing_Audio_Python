"""Domain model for the conferencing audio pipeline (Python port).

Strict-ish dataclasses mirroring the TypeScript model. This is a configuration
and signal-routing **control plane** — it models *what connects to what* and
validates correctness; it does **not** process real audio. See README scope.

JSON (de)serialization preserves the TS schema exactly (camelCase keys,
``version`` = :data:`CONFIG_VERSION`), so configs interoperate with the
TypeScript/browser version.
"""
from __future__ import annotations

from dataclasses import dataclass, field, fields, is_dataclass
from typing import Any, Literal, Optional, Union

# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #
CONFIG_VERSION = 2
MAX_ZONES_PER_ARRAY = 8
MAX_MANUAL_LOBES = 8
DEFAULT_DEDICATED_ZONE_SIZE_M = 1.8
GATING_SENSITIVITY_MIN = 0.0
GATING_SENSITIVITY_MAX = 1.0
NLP_LEVELS: tuple[str, ...] = ("off", "low", "medium", "high")
DEFAULT_TALKER_ELEVATION_M = 1.2

Transport = Literal["dante", "analog"]
PortKind = Literal["input", "output"]
DeviceType = Literal[
    "microphoneArray", "processor", "wirelessMic", "wiredMic", "loudspeaker", "codec"
]
CoverageMode = Literal["automatic", "manual"]
CoverageZoneType = Literal["dynamic", "dedicated", "exclusion"]
NlpLevel = Literal["off", "low", "medium", "high"]


# --------------------------------------------------------------------------- #
# Geometry
# --------------------------------------------------------------------------- #
@dataclass
class Point2D:
    x: float
    y: float


@dataclass
class RectShape:
    origin: Point2D
    width: float
    height: float
    kind: Literal["rect"] = "rect"


@dataclass
class PolygonShape:
    points: list[Point2D]
    kind: Literal["polygon"] = "polygon"


ZoneShape = Union[RectShape, PolygonShape]


def point_in_rect(p: Point2D, rect: RectShape) -> bool:
    return (
        rect.origin.x <= p.x <= rect.origin.x + rect.width
        and rect.origin.y <= p.y <= rect.origin.y + rect.height
    )


def point_in_polygon(p: Point2D, pts: list[Point2D]) -> bool:
    inside = False
    n = len(pts)
    j = n - 1
    for i in range(n):
        a, b = pts[i], pts[j]
        if (a.y > p.y) != (b.y > p.y) and p.x < (b.x - a.x) * (p.y - a.y) / (b.y - a.y) + a.x:
            inside = not inside
        j = i
    return inside


def point_in_shape(p: Point2D, shape: ZoneShape) -> bool:
    if isinstance(shape, RectShape):
        return point_in_rect(p, shape)
    return point_in_polygon(p, shape.points)


# --------------------------------------------------------------------------- #
# Ports / routes
# --------------------------------------------------------------------------- #
@dataclass
class Port:
    id: str
    device_id: str
    kind: PortKind
    transport: Transport
    label: str


@dataclass
class Route:
    id: str
    from_port_id: str
    to_port_id: str


DeploymentStatus = Literal["design", "online", "deployed"]


@dataclass
class DeploymentState:
    status: DeploymentStatus
    last_deployed_at: Optional[str] = None  # optional (omit when absent)


# --------------------------------------------------------------------------- #
# Coverage
# --------------------------------------------------------------------------- #
@dataclass
class CoverageZone:
    id: str
    type: CoverageZoneType
    shape: ZoneShape
    always_on: bool
    label: str


def is_pickup_zone(zone: CoverageZone) -> bool:
    return zone.type != "exclusion"


# --------------------------------------------------------------------------- #
# Matrix
# --------------------------------------------------------------------------- #
@dataclass
class Bus:
    id: str
    processor_id: str
    kind: Literal["input", "output"]
    port_id: str
    label: str


@dataclass
class Crosspoint:
    enabled: bool
    gain_db: float


@dataclass
class MatrixMixer:
    processor_id: str
    input_buses: list[Bus] = field(default_factory=list)
    output_buses: list[Bus] = field(default_factory=list)
    # cells[input_bus_id][output_bus_id] = Crosspoint
    cells: dict[str, dict[str, Crosspoint]] = field(default_factory=dict)


# --------------------------------------------------------------------------- #
# DSP config
# --------------------------------------------------------------------------- #
@dataclass
class AecConfig:
    enabled: bool
    reference_bus_id: Optional[str]  # nullable, not optional


@dataclass
class AutomixerChannel:
    input_bus_id: str
    always_on: bool
    gating_sensitivity: float


@dataclass
class AutomixerConfig:
    processor_id: str
    channels: list[AutomixerChannel] = field(default_factory=list)
    nlp: NlpLevel = "medium"
    output_bus_id: Optional[str] = None  # nullable


@dataclass
class MuteLink:
    id: str
    processor_output_bus_id: str
    linked_device_ids: list[str]
    sync_to_codec: bool
    muted: bool


# --------------------------------------------------------------------------- #
# DSP blocks (v1.7.0). params is a dict with the SAME camelCase keys as the TS
# version (e.g. {"gainDb": 0}); no audio is processed.
# --------------------------------------------------------------------------- #
DspBlockKind = Literal[
    "gain", "mute", "peq4", "agc", "compressor", "delay", "noiseReduction", "deverb"
]
DSP_BLOCK_KINDS: tuple[str, ...] = (
    "gain", "mute", "peq4", "agc", "compressor", "delay", "noiseReduction", "deverb"
)
PEQ_BAND_TYPES: tuple[str, ...] = ("bell", "lowShelf", "highShelf", "highpass", "lowpass")
PEQ_MAX_BANDS = 4
DSP_RANGES: dict[str, tuple[float, float]] = {
    "gainDb": (-60, 12),
    "peqFreqHz": (20, 20000),
    "peqGainDb": (-15, 15),
    "peqQ": (0.1, 10),
    "agcTargetDb": (-40, 0),
    "agcMaxGainDb": (0, 30),
    "compThresholdDb": (-60, 0),
    "compRatio": (1, 20),
    "compAttackMs": (0, 200),
    "compReleaseMs": (10, 2000),
    "compMakeupDb": (0, 24),
    "delayMs": (0, 500),
    "nrAmountDb": (0, 30),
    "deverbAmount": (0, 1),
}


@dataclass
class DspBlock:
    id: str
    kind: DspBlockKind
    enabled: bool
    params: dict[str, Any]
    target_bus_id: Optional[str] = None  # optional (omit when absent)


# --------------------------------------------------------------------------- #
# Room / talker
# --------------------------------------------------------------------------- #
@dataclass
class RoomObject:
    id: str
    kind: str
    position: Point2D
    meta: Optional[dict[str, Any]] = None


@dataclass
class RoomLayout:
    vertices: list[Point2D]
    height: float
    units: Literal["meters"] = "meters"
    objects: list[RoomObject] = field(default_factory=list)


@dataclass
class Talker:
    id: str
    label: str
    position: Point2D
    elevation: Optional[float] = None  # optional (omit when absent)


# --------------------------------------------------------------------------- #
# Devices
# --------------------------------------------------------------------------- #
@dataclass
class MicrophoneArray:
    id: str
    label: str
    ports: list[Port]
    coverage_mode: CoverageMode
    zones: list[CoverageZone]
    aec: AecConfig
    type: Literal["microphoneArray"] = "microphoneArray"
    position: Optional[Point2D] = None
    elevation: Optional[float] = None
    profile_id: Optional[str] = None
    dsp_blocks: list[DspBlock] = field(default_factory=list)


@dataclass
class WirelessMic:
    id: str
    label: str
    ports: list[Port]
    aec: AecConfig
    type: Literal["wirelessMic"] = "wirelessMic"
    position: Optional[Point2D] = None
    elevation: Optional[float] = None
    profile_id: Optional[str] = None
    dsp_blocks: list[DspBlock] = field(default_factory=list)


@dataclass
class WiredMic:
    id: str
    label: str
    ports: list[Port]
    aec: AecConfig
    type: Literal["wiredMic"] = "wiredMic"
    position: Optional[Point2D] = None
    elevation: Optional[float] = None
    profile_id: Optional[str] = None
    dsp_blocks: list[DspBlock] = field(default_factory=list)


@dataclass
class Loudspeaker:
    id: str
    label: str
    ports: list[Port]
    type: Literal["loudspeaker"] = "loudspeaker"
    position: Optional[Point2D] = None
    elevation: Optional[float] = None
    profile_id: Optional[str] = None
    dsp_blocks: list[DspBlock] = field(default_factory=list)


@dataclass
class Codec:
    id: str
    label: str
    ports: list[Port]
    type: Literal["codec"] = "codec"
    position: Optional[Point2D] = None
    elevation: Optional[float] = None
    profile_id: Optional[str] = None
    dsp_blocks: list[DspBlock] = field(default_factory=list)


@dataclass
class Processor:
    id: str
    label: str
    ports: list[Port]
    matrix: MatrixMixer
    buses: list[Bus]
    type: Literal["processor"] = "processor"
    position: Optional[Point2D] = None
    elevation: Optional[float] = None
    profile_id: Optional[str] = None
    dsp_blocks: list[DspBlock] = field(default_factory=list)


Device = Union[MicrophoneArray, WirelessMic, WiredMic, Loudspeaker, Codec, Processor]
MicDevice = Union[MicrophoneArray, WirelessMic, WiredMic]

_MIC_TYPES = {"microphoneArray", "wirelessMic", "wiredMic"}


def is_mic_device(d: Device) -> bool:
    return d.type in _MIC_TYPES


def is_processor(d: Device) -> bool:
    return d.type == "processor"


def default_elevation(device: Device, room_height: float = 3.0) -> float:
    """Default 3D elevation (metres) used when a device has no explicit value."""
    t = device.type
    if t == "microphoneArray":
        return room_height
    if t == "loudspeaker":
        return max(0.0, room_height - 0.3)
    if t == "codec":
        return 0.7
    if t == "processor":
        return 0.4
    if t in ("wirelessMic", "wiredMic"):
        return 1.1
    return 1.0


# --------------------------------------------------------------------------- #
# Config root
# --------------------------------------------------------------------------- #
@dataclass
class SystemConfig:
    version: int
    devices: list[Device]
    routes: list[Route]
    matrix: MatrixMixer
    automixer: AutomixerConfig
    mute_links: list[MuteLink]
    talkers: list[Talker]
    metadata: dict[str, str]
    room: Optional[RoomLayout] = None
    deployment: Optional[DeploymentState] = None


def find_port(config: SystemConfig, port_id: str) -> Optional[Port]:
    for d in config.devices:
        for p in d.ports:
            if p.id == port_id:
                return p
    return None


def find_device(config: SystemConfig, device_id: str) -> Optional[Device]:
    for d in config.devices:
        if d.id == device_id:
            return d
    return None


def find_talker(config: SystemConfig, talker_id: str) -> Optional[Talker]:
    for t in config.talkers:
        if t.id == talker_id:
            return t
    return None


# --------------------------------------------------------------------------- #
# JSON (de)serialization — preserves the TS camelCase schema
# --------------------------------------------------------------------------- #
# Keys whose ``None`` must serialize as JSON ``null`` (rather than being omitted).
_NULLABLE_KEYS = {"referenceBusId", "outputBusId"}


def _camel(snake: str) -> str:
    head, *rest = snake.split("_")
    return head + "".join(w[:1].upper() + w[1:] for w in rest)


def _snake(camel: str) -> str:
    out = []
    for ch in camel:
        if ch.isupper():
            out.append("_")
            out.append(ch.lower())
        else:
            out.append(ch)
    return "".join(out)


def to_jsonable(obj: Any) -> Any:
    """Recursively convert dataclasses/lists/primitives to JSON-ready values.

    Optional fields that are ``None`` are omitted, except :data:`_NULLABLE_KEYS`
    which emit ``null`` — matching ``JSON.stringify`` of the TS model.
    """
    if is_dataclass(obj) and not isinstance(obj, type):
        out: dict[str, Any] = {}
        for f in fields(obj):
            value = getattr(obj, f.name)
            key = _camel(f.name)
            if value is None:
                if key in _NULLABLE_KEYS:
                    out[key] = None
                continue
            out[key] = to_jsonable(value)
        return out
    if isinstance(obj, dict):
        return {k: to_jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [to_jsonable(v) for v in obj]
    return obj


# ---- from_dict reconstruction (camelCase dict -> dataclasses) ---- #
def _pt(d: dict[str, Any]) -> Point2D:
    return Point2D(x=d["x"], y=d["y"])


def _shape(d: dict[str, Any]) -> ZoneShape:
    if d["kind"] == "rect":
        return RectShape(origin=_pt(d["origin"]), width=d["width"], height=d["height"])
    return PolygonShape(points=[_pt(p) for p in d["points"]])


def _port(d: dict[str, Any]) -> Port:
    return Port(id=d["id"], device_id=d["deviceId"], kind=d["kind"], transport=d["transport"], label=d["label"])


def _bus(d: dict[str, Any]) -> Bus:
    return Bus(id=d["id"], processor_id=d["processorId"], kind=d["kind"], port_id=d["portId"], label=d["label"])


def _matrix(d: dict[str, Any]) -> MatrixMixer:
    cells: dict[str, dict[str, Crosspoint]] = {}
    for in_id, cols in d.get("cells", {}).items():
        cells[in_id] = {out_id: Crosspoint(enabled=cp["enabled"], gain_db=cp["gainDb"]) for out_id, cp in cols.items()}
    return MatrixMixer(
        processor_id=d["processorId"],
        input_buses=[_bus(b) for b in d.get("inputBuses", [])],
        output_buses=[_bus(b) for b in d.get("outputBuses", [])],
        cells=cells,
    )


def _aec(d: dict[str, Any]) -> AecConfig:
    return AecConfig(enabled=d["enabled"], reference_bus_id=d.get("referenceBusId"))


def _dsp_block(d: dict[str, Any]) -> DspBlock:
    return DspBlock(id=d["id"], kind=d["kind"], enabled=d["enabled"], params=dict(d.get("params", {})), target_bus_id=d.get("targetBusId"))


def _zone(d: dict[str, Any]) -> CoverageZone:
    return CoverageZone(id=d["id"], type=d["type"], shape=_shape(d["shape"]), always_on=d["alwaysOn"], label=d["label"])


def _device(d: dict[str, Any]) -> Device:
    t = d["type"]
    ports = [_port(p) for p in d["ports"]]
    pos = _pt(d["position"]) if d.get("position") is not None else None
    elev = d.get("elevation")
    common = dict(id=d["id"], label=d["label"], ports=ports)
    if t == "microphoneArray":
        dev: Device = MicrophoneArray(coverage_mode=d["coverageMode"], zones=[_zone(z) for z in d["zones"]], aec=_aec(d["aec"]), **common)
    elif t == "wirelessMic":
        dev = WirelessMic(aec=_aec(d["aec"]), **common)
    elif t == "wiredMic":
        dev = WiredMic(aec=_aec(d["aec"]), **common)
    elif t == "loudspeaker":
        dev = Loudspeaker(**common)
    elif t == "codec":
        dev = Codec(**common)
    elif t == "processor":
        dev = Processor(matrix=_matrix(d["matrix"]), buses=[_bus(b) for b in d["buses"]], **common)
    else:
        raise ValueError(f"Unknown device type: {t}")
    dev.position = pos
    dev.elevation = elev
    dev.profile_id = d.get("profileId")
    dev.dsp_blocks = [_dsp_block(b) for b in d.get("dspBlocks", [])]
    return dev


def _automixer(d: dict[str, Any]) -> AutomixerConfig:
    return AutomixerConfig(
        processor_id=d["processorId"],
        channels=[AutomixerChannel(input_bus_id=c["inputBusId"], always_on=c["alwaysOn"], gating_sensitivity=c["gatingSensitivity"]) for c in d.get("channels", [])],
        nlp=d.get("nlp", "medium"),
        output_bus_id=d.get("outputBusId"),
    )


def _mute(d: dict[str, Any]) -> MuteLink:
    return MuteLink(id=d["id"], processor_output_bus_id=d["processorOutputBusId"], linked_device_ids=list(d["linkedDeviceIds"]), sync_to_codec=d["syncToCodec"], muted=d["muted"])


def _room(d: dict[str, Any]) -> RoomLayout:
    return RoomLayout(
        vertices=[_pt(p) for p in d["vertices"]],
        height=d["height"],
        units=d.get("units", "meters"),
        objects=[RoomObject(id=o["id"], kind=o["kind"], position=_pt(o["position"]), meta=o.get("meta")) for o in d.get("objects", [])],
    )


def _talker(d: dict[str, Any]) -> Talker:
    return Talker(id=d["id"], label=d["label"], position=_pt(d["position"]), elevation=d.get("elevation"))


def config_from_dict(d: dict[str, Any]) -> SystemConfig:
    cfg = SystemConfig(
        version=d["version"],
        devices=[_device(x) for x in d["devices"]],
        routes=[Route(id=r["id"], from_port_id=r["fromPortId"], to_port_id=r["toPortId"]) for r in d["routes"]],
        matrix=_matrix(d["matrix"]),
        automixer=_automixer(d["automixer"]),
        mute_links=[_mute(m) for m in d.get("muteLinks", [])],
        talkers=[_talker(t) for t in d.get("talkers", [])],
        metadata=dict(d["metadata"]),
        room=_room(d["room"]) if d.get("room") is not None else None,
        deployment=DeploymentState(status=d["deployment"]["status"], last_deployed_at=d["deployment"].get("lastDeployedAt")) if d.get("deployment") is not None else None,
    )
    return cfg
