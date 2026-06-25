"""Geometric room/device coverage simulation (v4).

Answers "what does each device cover, to its spec?" for the GUI's simulation bar:
a microphone's pickup angles, a camera's field-of-view (and who is in frame after
furniture occlusion), and a loudspeaker's dispersion. It is **geometric / spec-
based** — fast, dependency-free, honest about its caveats — and returns a single
view-independent :class:`RoomCoverage` the 2D and 3D canvases both render.

Every device's draw geometry is a :class:`CoverageWedge` (apex + bearing/tilt +
horizontal/vertical half-angles + range), so 2D draws the horizontal slice and 3D
draws the full cone/frustum from the same data. The mic tier here is geometric; a
physics tier (true beam polar) can be injected via ``mic_coverage_fn`` without any
change to this contract or the canvas.

Pure stdlib (``math`` only). Reuses :func:`array_coverage_radius` /
:func:`steering_angles` / the furniture catalog rather than reimplementing them.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from . import furniture as fz
from .angles import Point3D, steering_angles
from .coverage_check import array_coverage_radius
from .model import (
    DEFAULT_TALKER_ELEVATION_M,
    CoverageZone,
    Point2D,
    RectShape,
    SystemConfig,
    angular_separation_deg,
    bearing_to_deg,
    default_elevation,
    is_pickup_zone,
    point_in_polygon,
    point_in_sector,
)
from .directivity import SIM_SPEECH_FREQ_HZ, steered_beamwidth_deg
from .profiles import device_capabilities

# Per-zone steered-beam half-angle for the geometric mic tier (deg). The profile's
# ``coverage_angle_deg`` is the *full unsteered* cone; a steered pickup beam toward
# a zone is much tighter. Matches the placement scorer's ``lobe_halfwidth_deg``.
DEFAULT_PICKUP_BEAM_HALF_DEG = 35.0
SEATED_HEAD_M = DEFAULT_TALKER_ELEVATION_M  # 1.2 m


# --------------------------------------------------------------------------- #
# Result structures (view-independent — 2D and 3D render the same data)
# --------------------------------------------------------------------------- #
@dataclass
class CoverageWedge:
    """A circular sector / cone anchored at ``apex`` (floor x,y) at height
    ``apex_elev_m``, centred on compass ``azimuth_deg`` (0° = +Y, CW) and tilted
    ``tilt_deg`` below horizontal (0 = level, 90 = straight down), with horizontal
    and vertical half-angles and a reach ``range_m``."""

    apex: Point2D
    apex_elev_m: float
    azimuth_deg: float
    tilt_deg: float
    h_half_deg: float
    v_half_deg: float
    range_m: float


@dataclass
class TargetHit:
    id: str
    label: str
    position: Point2D
    elev_m: float
    in_coverage: bool
    blocked: bool = False              # occluded by furniture (cameras)
    gain_db: Optional[float] = None    # relative on/off-axis gain (mics), 0 = on-axis
    distance_m: float = 0.0


@dataclass
class MicCoverage:
    device_id: str
    label: str
    wedges: list[CoverageWedge]
    center: Optional[Point2D]
    radius_m: float                    # floor coverage circle (downward cone)
    targets: list[TargetHit]
    covered_pct: float


@dataclass
class CameraCoverage:
    device_id: str
    label: str
    wedge: CoverageWedge
    targets: list[TargetHit]
    framed_pct: float


@dataclass
class SpeakerCoverage:
    device_id: str
    label: str
    wedge: CoverageWedge
    targets: list[TargetHit]
    covered_pct: float


@dataclass
class Occluder:
    object_id: str
    kind: str
    corners: list[Point2D]
    height_m: float
    blocks_camera: bool
    blocks_audio: bool


@dataclass
class Target:
    id: str
    label: str
    position: Point2D
    elev_m: float


@dataclass
class RoomCoverage:
    mics: list[MicCoverage] = field(default_factory=list)
    cameras: list[CameraCoverage] = field(default_factory=list)
    speakers: list[SpeakerCoverage] = field(default_factory=list)
    occluders: list[Occluder] = field(default_factory=list)
    targets: list[Target] = field(default_factory=list)
    fidelity: str = "geometric"
    caveats: list[str] = field(default_factory=list)
    summary: dict[str, Any] = field(default_factory=dict)


# --------------------------------------------------------------------------- #
# Scene gathering
# --------------------------------------------------------------------------- #
def _room_height(config: SystemConfig) -> float:
    return config.room.height if config.room is not None else 3.0


def _device_elev(config: SystemConfig, device) -> float:
    if device.elevation is not None:
        return device.elevation
    return default_elevation(device, _room_height(config))


def _zone_centroid(zone: CoverageZone) -> Point2D:
    if isinstance(zone.shape, RectShape):
        s = zone.shape
        return Point2D(s.origin.x + s.width / 2.0, s.origin.y + s.height / 2.0)
    pts = zone.shape.points
    return Point2D(sum(p.x for p in pts) / len(pts), sum(p.y for p in pts) / len(pts))


def room_targets(config: SystemConfig) -> list[Target]:
    """The points the room must cover: every talker, plus every furniture seat."""
    out: list[Target] = []
    for t in config.talkers:
        elev = t.elevation if t.elevation is not None else SEATED_HEAD_M
        out.append(Target(t.id, t.label, t.position, elev))
    if config.room is not None:
        for obj in config.room.objects:
            for i, seat in enumerate(obj.seats or [], start=1):
                out.append(Target(f"{obj.id}-seat{i}", f"{obj.kind} seat {i}", seat.position, SEATED_HEAD_M))
    return out


def room_occluders(config: SystemConfig) -> list[Occluder]:
    out: list[Occluder] = []
    if config.room is None:
        return out
    for obj in config.room.objects:
        _w, _d, h = fz.resolved_dimensions(obj)
        out.append(Occluder(
            object_id=obj.id, kind=obj.kind, corners=fz.furniture_corners(obj),
            height_m=h, blocks_camera=fz.blocks_camera(obj), blocks_audio=fz.blocks_audio(obj),
        ))
    return out


# --------------------------------------------------------------------------- #
# Line of sight / occlusion (height-aware)
# --------------------------------------------------------------------------- #
def _seg_intersection_t(a: Point2D, b: Point2D, c: Point2D, d: Point2D) -> Optional[float]:
    """Parameter ``t`` along ``a→b`` where it crosses segment ``c→d``, else None."""
    rx, ry = b.x - a.x, b.y - a.y
    sx, sy = d.x - c.x, d.y - c.y
    denom = rx * sy - ry * sx
    if abs(denom) < 1e-12:
        return None
    qx, qy = c.x - a.x, c.y - a.y
    t = (qx * sy - qy * sx) / denom
    u = (qx * ry - qy * rx) / denom
    if -1e-9 <= t <= 1.0 + 1e-9 and -1e-9 <= u <= 1.0 + 1e-9:
        return t
    return None


def segment_obb_entry(a: Point2D, b: Point2D, corners: list[Point2D]) -> Optional[float]:
    """Smallest ``t`` in ``(0,1)`` where segment ``a→b`` enters the polygon, or
    ``None``. Endpoints strictly inside count as a crossing at the midpoint."""
    ts: list[float] = []
    n = len(corners)
    for i in range(n):
        t = _seg_intersection_t(a, b, corners[i], corners[(i + 1) % n])
        if t is not None and 1e-6 < t < 1.0 - 1e-6:
            ts.append(t)
    if ts:
        return min(ts)
    if point_in_polygon(a, corners) or point_in_polygon(b, corners):
        return 0.5
    return None


def segment_intersects_obb(a: Point2D, b: Point2D, corners: list[Point2D]) -> bool:
    return segment_obb_entry(a, b, corners) is not None


def camera_sees(a: Point2D, a_elev: float, b: Point2D, b_elev: float, occluders: list[Occluder]) -> bool:
    """Height-aware line of sight from a camera at ``a`` (height ``a_elev``) to a
    target at ``b`` (height ``b_elev``). A camera-blocking occluder breaks LOS only
    when its top rises above the sight line where the ray crosses its footprint —
    so a ceiling camera sees over a low table/screen, a soundbar camera doesn't."""
    for oc in occluders:
        if not oc.blocks_camera:
            continue
        t = segment_obb_entry(a, b, oc.corners)
        if t is None:
            continue
        sight_h = a_elev + t * (b_elev - a_elev)
        if oc.height_m >= sight_h:
            return False
    return True


# --------------------------------------------------------------------------- #
# Per-device coverage
# --------------------------------------------------------------------------- #
def camera_wedge(config: SystemConfig, cam) -> Optional[CoverageWedge]:
    if cam.position is None:
        return None
    spec = device_capabilities(cam).camera
    if spec is None:
        return None
    return CoverageWedge(
        apex=cam.position, apex_elev_m=_device_elev(config, cam),
        azimuth_deg=float(cam.bearing_deg) % 360.0, tilt_deg=float(cam.tilt_deg),
        h_half_deg=spec.fov_h_deg / 2.0, v_half_deg=spec.fov_v_deg / 2.0, range_m=spec.max_range_m,
    )


def camera_coverage(config: SystemConfig, cam, targets: list[Target], occluders: list[Occluder]) -> Optional[CameraCoverage]:
    wedge = camera_wedge(config, cam)
    if wedge is None:
        return None
    hits: list[TargetHit] = []
    framed = 0
    for tg in targets:
        dist = math.hypot(tg.position.x - wedge.apex.x, tg.position.y - wedge.apex.y)
        in_fov = point_in_sector(wedge.apex, tg.position, wedge.azimuth_deg, wedge.h_half_deg, wedge.range_m)
        blocked = bool(in_fov) and not camera_sees(wedge.apex, wedge.apex_elev_m, tg.position, tg.elev_m, occluders)
        is_framed = in_fov and not blocked
        framed += 1 if is_framed else 0
        hits.append(TargetHit(tg.id, tg.label, tg.position, tg.elev_m, in_coverage=in_fov, blocked=blocked, distance_m=dist))
    pct = (framed / len(targets) * 100.0) if targets else 0.0
    return CameraCoverage(cam.id, cam.label, wedge, hits, pct)


def speaker_wedge(config: SystemConfig, spk) -> Optional[CoverageWedge]:
    if spk.position is None:
        return None
    spec = device_capabilities(spk).speaker
    if spec is None:
        return None
    # an unaimed speaker (no bearing) is treated as omnidirectional
    omni = spk.bearing_deg is None
    return CoverageWedge(
        apex=spk.position, apex_elev_m=_device_elev(config, spk),
        azimuth_deg=float(spk.bearing_deg or 0.0) % 360.0, tilt_deg=float(spk.tilt_deg or 0.0),
        h_half_deg=180.0 if omni else spec.dispersion_h_deg / 2.0,
        v_half_deg=spec.dispersion_v_deg / 2.0, range_m=spec.max_range_m,
    )


def speaker_coverage(config: SystemConfig, spk, targets: list[Target]) -> Optional[SpeakerCoverage]:
    wedge = speaker_wedge(config, spk)
    if wedge is None:
        return None
    hits: list[TargetHit] = []
    covered = 0
    for tg in targets:
        dist = math.hypot(tg.position.x - wedge.apex.x, tg.position.y - wedge.apex.y)
        inside = point_in_sector(wedge.apex, tg.position, wedge.azimuth_deg, wedge.h_half_deg, wedge.range_m)
        covered += 1 if inside else 0
        hits.append(TargetHit(tg.id, tg.label, tg.position, tg.elev_m, in_coverage=inside, distance_m=dist))
    pct = (covered / len(targets) * 100.0) if targets else 0.0
    return SpeakerCoverage(spk.id, spk.label, wedge, hits, pct)


def mic_coverage(config: SystemConfig, array, targets: list[Target]) -> Optional[MicCoverage]:
    """Geometric mic pickup: one steered beam toward each pickup zone (or a single
    downward cone when unzoned). Targets inside a beam sector and the floor coverage
    circle are 'picked up', with a coarse off-axis gain rolloff."""
    if array.position is None:
        return None
    cap = device_capabilities(array)
    angle = cap.coverage_angle_deg
    center = array.position
    elev = _device_elev(config, array)
    circ_radius = array_coverage_radius(elev, SEATED_HEAD_M, angle)

    pickup_zones = [z for z in array.zones if is_pickup_zone(z)]
    wedges: list[CoverageWedge] = []

    def _half_for(off_nadir_deg: float) -> float:
        if cap.aperture_m is None:
            return DEFAULT_PICKUP_BEAM_HALF_DEG
        return steered_beamwidth_deg(cap.aperture_m, SIM_SPEECH_FREQ_HZ, off_nadir_deg)

    if pickup_zones:
        src = Point3D(center.x, center.y, elev)
        for z in pickup_zones:
            cen = _zone_centroid(z)
            sa = steering_angles(src, Point3D(cen.x, cen.y, SEATED_HEAD_M))
            reach = circ_radius if circ_radius > 0 else max(sa.horizontal_distance, 1.0)
            half = _half_for(sa.downtilt_deg)
            wedges.append(CoverageWedge(
                apex=center, apex_elev_m=elev, azimuth_deg=sa.azimuth_deg, tilt_deg=sa.downtilt_deg,
                h_half_deg=half, v_half_deg=half,
                range_m=reach,
            ))
    else:
        half = (angle / 2.0) if angle else 60.0
        wedges.append(CoverageWedge(
            apex=center, apex_elev_m=elev, azimuth_deg=0.0, tilt_deg=90.0,
            h_half_deg=half, v_half_deg=half, range_m=circ_radius if circ_radius > 0 else 3.0,
        ))

    hits: list[TargetHit] = []
    covered = 0
    for tg in targets:
        dist = math.hypot(tg.position.x - center.x, tg.position.y - center.y)
        is_cov = False
        gain: Optional[float] = None
        if pickup_zones:
            bearing = bearing_to_deg(center, tg.position)
            for wd in wedges:
                reach = circ_radius if circ_radius > 0 else wd.range_m
                sep = angular_separation_deg(bearing, wd.azimuth_deg)
                if dist <= reach and sep <= wd.h_half_deg:
                    is_cov = True
                    gain = -6.0 * (sep / wd.h_half_deg) ** 2
                    break
        elif circ_radius > 0 and dist <= circ_radius:
            is_cov = True
            gain = 0.0
        covered += 1 if is_cov else 0
        hits.append(TargetHit(tg.id, tg.label, tg.position, tg.elev_m, in_coverage=is_cov, gain_db=gain, distance_m=dist))
    pct = (covered / len(targets) * 100.0) if targets else 0.0
    return MicCoverage(array.id, array.label, wedges, center, circ_radius, hits, pct)


# --------------------------------------------------------------------------- #
# Aperture-honesty caveats
# --------------------------------------------------------------------------- #
def coverage_caveats(config: SystemConfig) -> list[str]:
    """Honesty warnings for aperture-limited arrays.

    Returns one or more strings per array that has *aperture_m* set:

    * A separability warning for each pair of pickup zones whose angular
      separation is narrower than 1.5 × the steered beam half-width.
    * A grating-lobe note when the spatial-aliasing ceiling is below 8 kHz
      (i.e. lies within the speech band).

    Returns an empty list for arrays whose profile has no *aperture_m*
    (ceiling/table/legacy), so existing configs produce no new warnings.
    """
    from .directivity import alias_ceiling_hz, separable, steered_beamwidth_deg, SIM_SPEECH_FREQ_HZ

    out: list[str] = []
    for device in config.devices:
        if device.type != "microphoneArray":
            continue
        cap = device_capabilities(device)
        if cap.aperture_m is None or device.position is None:
            continue

        zones = [z for z in device.zones if is_pickup_zone(z)]
        elev = _device_elev(config, device)
        src = Point3D(device.position.x, device.position.y, elev)

        # Compute per-zone (label, azimuth_deg, beam_half_deg)
        looks: list[tuple[str, float, float]] = []
        for z in zones:
            cen = _zone_centroid(z)
            sa = steering_angles(src, Point3D(cen.x, cen.y, SEATED_HEAD_M))
            half = steered_beamwidth_deg(cap.aperture_m, SIM_SPEECH_FREQ_HZ, sa.downtilt_deg)
            looks.append((z.label, sa.azimuth_deg, half))

        # Pairwise separability check
        for i in range(len(looks)):
            for j in range(i + 1, len(looks)):
                sep = angular_separation_deg(looks[i][1], looks[j][1])
                worst_half = max(looks[i][2], looks[j][2])
                if not separable(sep, worst_half):
                    out.append(
                        f"{device.label}: zones '{looks[i][0]}' and '{looks[j][0]}' are "
                        f"{sep:.0f}° apart but this array's steered beam is "
                        f"~{worst_half:.0f}° half-width — it cannot separate them."
                    )

        # Grating-lobe / spatial-aliasing note
        if cap.element_spacing_m is not None:
            ceil_hz = alias_ceiling_hz(cap.element_spacing_m)
            if ceil_hz < 8000.0:
                out.append(
                    f"{device.label}: directivity degrades above "
                    f"~{ceil_hz / 1000.0:.1f} kHz "
                    "(spatial aliasing / grating lobes)."
                )

    return out


# --------------------------------------------------------------------------- #
# Aggregator
# --------------------------------------------------------------------------- #
MicCoverageFn = Callable[[SystemConfig, Any, list[Target]], Optional[MicCoverage]]

_GEOMETRIC_CAVEATS = [
    "Geometric, spec-based estimate (azimuth coverage from datasheet angles).",
    "Free-field: no acoustic reflections, reverberation, or directivity-vs-frequency.",
    "Camera occlusion is height-aware line-of-sight only (no transparency/partial framing).",
]


def simulate_room_coverage(
    config: SystemConfig, *, mic_coverage_fn: Optional[MicCoverageFn] = None
) -> RoomCoverage:
    """Aggregate coverage for every placed mic/camera/speaker into one
    :class:`RoomCoverage`. ``mic_coverage_fn`` overrides the geometric mic tier
    (e.g. a beamformer-based physics tier) behind the same contract."""
    targets = room_targets(config)
    occluders = room_occluders(config)
    mic_fn = mic_coverage_fn or mic_coverage

    mics, cameras, speakers = [], [], []
    for d in config.devices:
        if d.type == "microphoneArray":
            mc = mic_fn(config, d, targets)
            if mc is not None:
                mics.append(mc)
        elif d.type == "camera":
            cc = camera_coverage(config, d, targets, occluders)
            if cc is not None:
                cameras.append(cc)
        elif d.type == "loudspeaker":
            sc = speaker_coverage(config, d, targets)
            if sc is not None:
                speakers.append(sc)

    # overall mic coverage: targets picked up by ≥1 array
    mic_ids = {h.id for mc in mics for h in mc.targets if h.in_coverage}
    n = len(targets)
    uncovered = [t.id for t in targets if t.id not in mic_ids]
    mic_pct = (len(mic_ids) / n * 100.0) if n else 0.0
    framed_ids = {h.id for cc in cameras for h in cc.targets if h.in_coverage and not h.blocked}
    cam_pct = (len(framed_ids) / n * 100.0) if n else 0.0

    summary = {
        "target_count": n,
        "mic_covered": len(mic_ids),
        "mic_coverage_pct": mic_pct,
        "mic_gaps": uncovered,
        "camera_framed_pct": cam_pct,
        "has_camera": bool(cameras),
        "has_speaker": bool(speakers),
    }
    return RoomCoverage(
        mics=mics, cameras=cameras, speakers=speakers, occluders=occluders, targets=targets,
        fidelity="geometric",
        caveats=list(_GEOMETRIC_CAVEATS) + coverage_caveats(config),
        summary=summary,
    )
