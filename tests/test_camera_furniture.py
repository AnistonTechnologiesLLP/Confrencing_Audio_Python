"""v4 model additions: conferencing cameras, loudspeaker aim, furniture geometry.

Covers serialization round-trips (camelCase, omit-when-absent), the v3→v4 version
gate + migration, the camera/speaker device profiles, the furniture catalog
resolvers, and the shared geometry helpers (point_in_sector / obb_corners).
"""
import json
import math

import pytest

import conf_pipeline as cp
from conf_pipeline import furniture as fz
from conf_pipeline.model import (
    ConferencingCamera,
    Loudspeaker,
    Point2D,
    RoomLayout,
    RoomObject,
    SeatAnchor,
)


def _rect_room():
    return RoomLayout(
        vertices=[Point2D(0, 0), Point2D(5, 0), Point2D(5, 4), Point2D(0, 4)],
        height=3.0,
    )


# --------------------------------------------------------------------------- #
# Serialization
# --------------------------------------------------------------------------- #
def test_camera_round_trips_with_camelcase_schema():
    c = cp.create_config("Room", "2026-06-13T00:00:00Z")
    cam = ConferencingCamera(
        id="cam1", label="Front cam", ports=[], position=Point2D(2.5, 0.2),
        bearing_deg=180.0, tilt_deg=10.0, profile_id="generic-ptz-camera",
    )
    c.devices = [cam]
    d = json.loads(cp.serialize(c))["devices"][0]
    assert d["type"] == "camera"
    assert d["bearingDeg"] == 180.0 and d["tiltDeg"] == 10.0
    assert d["profileId"] == "generic-ptz-camera"
    rt = cp.deserialize(cp.serialize(c))
    cam2 = rt.devices[0]
    assert cp.is_camera(cam2)
    assert (cam2.bearing_deg, cam2.tilt_deg) == (180.0, 10.0)
    assert cp.serialize(rt) == cp.serialize(c)


def test_loudspeaker_aim_round_trips_and_is_optional():
    c = cp.create_config("Room", "x")
    aimed = Loudspeaker(id="s1", label="LS", ports=[], position=Point2D(2, 3),
                        bearing_deg=45.0, tilt_deg=5.0, profile_id="generic-loudspeaker")
    plain = Loudspeaker(id="s2", label="LS2", ports=[], position=Point2D(3, 3),
                        profile_id="generic-loudspeaker")
    c.devices = [aimed, plain]
    docs = json.loads(cp.serialize(c))["devices"]
    assert docs[0]["bearingDeg"] == 45.0 and docs[0]["tiltDeg"] == 5.0
    # unaimed speaker omits the pose keys entirely (byte-stable legacy round-trip)
    assert "bearingDeg" not in docs[1] and "tiltDeg" not in docs[1]
    assert cp.serialize(cp.deserialize(cp.serialize(c))) == cp.serialize(c)


def test_enriched_room_object_round_trips():
    c = cp.create_config("Room", "x")
    fur = RoomObject(
        id="t1", kind="table", position=Point2D(2.5, 2.0),
        width=2.4, depth=1.0, height=0.74, rotation_deg=30.0,
        seats=[SeatAnchor(position=Point2D(2.5, 1.2), facing_deg=0.0)],
        blocks_camera=False,
    )
    c.room = RoomLayout(vertices=_rect_room().vertices, height=3.0, objects=[fur])
    o = json.loads(cp.serialize(c))["room"]["objects"][0]
    assert o["width"] == 2.4 and o["rotationDeg"] == 30.0 and o["blocksCamera"] is False
    assert o["seats"][0] == {"position": {"x": 2.5, "y": 1.2}, "facingDeg": 0.0}
    rt = cp.deserialize(cp.serialize(c))
    f2 = rt.room.objects[0]
    assert f2.width == 2.4 and f2.rotation_deg == 30.0 and f2.blocks_camera is False
    assert f2.seats[0].facing_deg == 0.0
    assert cp.serialize(rt) == cp.serialize(c)


def test_legacy_room_object_is_byte_identical():
    """A v1-shaped object (id/kind/position only) must omit every v4 field."""
    c = cp.create_config("Room", "x")
    c.room = RoomLayout(vertices=_rect_room().vertices, height=3.0,
                        objects=[RoomObject(id="o1", kind="table", position=Point2D(2, 2))])
    o = json.loads(cp.serialize(c))["room"]["objects"][0]
    assert sorted(o.keys()) == ["id", "kind", "position"]


# --------------------------------------------------------------------------- #
# Migration / version gate
# --------------------------------------------------------------------------- #
def test_v3_file_migrates_to_current():
    c = cp.create_config("Room", "x")
    doc = json.loads(cp.serialize(c))
    doc["version"] = 3                                    # a genuine v3 file
    restored = cp.deserialize(json.dumps(doc))
    assert restored.version == cp.CONFIG_VERSION         # migrates the whole chain to current (v5)
    # additive: a v3 doc gains nothing, so re-bumping to v3 reproduces the original
    up = json.loads(cp.serialize(restored))
    up["version"] = 3
    assert up == doc


def test_v3_with_legacy_furniture_migrates():
    c = cp.create_config("Room", "x")
    doc = json.loads(cp.serialize(c))
    doc["version"] = 3
    doc["room"] = {
        "vertices": [{"x": 0, "y": 0}, {"x": 5, "y": 0}, {"x": 5, "y": 4}, {"x": 0, "y": 4}],
        "height": 3.0, "units": "meters",
        "objects": [{"id": "o1", "kind": "table", "position": {"x": 2, "y": 2}}],
    }
    restored = cp.deserialize(json.dumps(doc))
    assert restored.version == cp.CONFIG_VERSION
    assert restored.room.objects[0].kind == "table"


# --------------------------------------------------------------------------- #
# Profiles
# --------------------------------------------------------------------------- #
def test_camera_and_speaker_specs_present():
    cam = cp.get_device_profile("generic-ptz-camera")
    assert cam is not None and cam.capabilities.camera is not None
    assert cam.capabilities.camera.fov_h_deg == 70.0
    assert cp.default_profile_id("camera") == "generic-ptz-camera"
    spk = cp.get_device_profile("generic-loudspeaker")
    assert spk.capabilities.speaker is not None
    assert spk.capabilities.speaker.dispersion_h_deg == 90.0


def test_existing_profiles_unregressed():
    # the original mic-array coverage angles are untouched
    assert cp.get_device_profile("generic-ceiling-array").capabilities.coverage_angle_deg == 120.0
    assert cp.get_device_profile("generic-table-array").capabilities.coverage_angle_deg == 130.0
    # three camera profiles added
    cams = [p for p in cp.DEVICE_PROFILES.values() if "camera" in p.applies_to]
    assert {p.id for p in cams} == {"generic-ptz-camera", "generic-wide-camera", "generic-soundbar-camera"}


# --------------------------------------------------------------------------- #
# Furniture catalog resolvers
# --------------------------------------------------------------------------- #
def test_furniture_resolvers_override_then_catalog():
    obj = RoomObject(id="t", kind="table", position=Point2D(0, 0))
    assert fz.resolved_dimensions(obj) == (2.4, 1.0, 0.74)          # catalog default
    obj2 = RoomObject(id="t", kind="table", position=Point2D(0, 0), width=3.0)
    assert fz.resolved_dimensions(obj2)[0] == 3.0                   # override wins
    # blocks_camera: catalog says a table doesn't, a screen does
    assert fz.blocks_camera(RoomObject(id="a", kind="table", position=Point2D(0, 0))) is False
    assert fz.blocks_camera(RoomObject(id="b", kind="screen", position=Point2D(0, 0))) is True
    # unknown kind / unset flag defaults to blocking the camera
    assert fz.blocks_camera(RoomObject(id="c", kind="mystery", position=Point2D(0, 0))) is True


# --------------------------------------------------------------------------- #
# Geometry helpers
# --------------------------------------------------------------------------- #
def test_point_in_sector_truth_table():
    apex = Point2D(0, 0)
    # facing +Y (0°), half-angle 30°, reach 5 m
    assert cp.point_in_sector(apex, Point2D(0, 2), 0.0, 30.0, 5.0) is True     # straight ahead
    assert cp.point_in_sector(apex, Point2D(0, -2), 0.0, 30.0, 5.0) is False   # behind
    assert cp.point_in_sector(apex, Point2D(2, 0), 0.0, 30.0, 5.0) is False    # 90° to the side
    assert cp.point_in_sector(apex, Point2D(0, 9), 0.0, 30.0, 5.0) is False    # out of range
    # within the cone but off-axis (< 30°)
    assert cp.point_in_sector(apex, Point2D(0.5, 2), 0.0, 30.0, 5.0) is True


def test_bearing_and_separation():
    assert cp.bearing_to_deg(Point2D(0, 0), Point2D(0, 1)) == 0.0     # +Y
    assert cp.bearing_to_deg(Point2D(0, 0), Point2D(1, 0)) == 90.0    # +X
    assert cp.angular_separation_deg(350.0, 10.0) == 20.0             # wraps across 0


def test_obb_corners_axis_aligned_and_rotated():
    corners = cp.obb_corners(Point2D(0, 0), 2.0, 1.0, 0.0)
    xs = sorted({round(p.x, 3) for p in corners})
    ys = sorted({round(p.y, 3) for p in corners})
    assert xs == [-1.0, 1.0] and ys == [-0.5, 0.5]
    # a 90° rotation swaps width/depth extents
    rot = cp.obb_corners(Point2D(0, 0), 2.0, 1.0, 90.0)
    assert max(abs(p.x) for p in rot) == pytest.approx(0.5)
    assert max(abs(p.y) for p in rot) == pytest.approx(1.0)
