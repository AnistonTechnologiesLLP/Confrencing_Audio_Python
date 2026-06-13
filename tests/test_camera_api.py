"""v4 builders (cameras, device aim, furniture) + the new validation codes."""
import pytest

import conf_pipeline as cp
from conf_pipeline.model import Loudspeaker, Point2D, RoomObject, SeatAnchor


def _room_config():
    c = cp.create_config("Room", "2026-06-13T00:00:00Z")
    return cp.set_room(c, cp.rectangular_room(6.0, 5.0, 3.0))


def _codes(c):
    res = cp.validate(c)
    return {i.code for i in res.errors} | {i.code for i in res.warnings}


# --------------------------------------------------------------------------- #
# Builders are pure (return new config, never mutate input)
# --------------------------------------------------------------------------- #
def test_add_camera_is_pure():
    c = _room_config()
    c2 = cp.add_camera(c, cp.create_camera("cam", "Front cam"))
    assert len(c.devices) == 0 and len(c2.devices) == 1
    assert cp.is_camera(cp.find_device(c2, "cam"))
    assert cp.find_device(c2, "cam").profile_id == "generic-ptz-camera"


def test_set_camera_and_speaker_aim():
    c = _room_config()
    c = cp.add_camera(c, cp.create_camera("cam", "Cam"))
    c = cp.add_device(c, Loudspeaker(id="s", label="LS", ports=[], profile_id="generic-loudspeaker"))
    c = cp.set_camera_bearing(c, "cam", 450.0)        # wraps to 90
    c = cp.set_camera_tilt(c, "cam", 12.0)
    c = cp.set_speaker_bearing(c, "s", 180.0)
    cam = cp.find_device(c, "cam")
    assert cam.bearing_deg == 90.0 and cam.tilt_deg == 12.0
    assert cp.find_device(c, "s").bearing_deg == 180.0


def test_aim_rejects_non_aimable_device():
    c = _room_config()
    c = cp.add_device(c, cp.create_microphone_array("A", "Array"))
    with pytest.raises(ValueError):
        cp.set_camera_bearing(c, "A", 90.0)


# --------------------------------------------------------------------------- #
# Furniture builders
# --------------------------------------------------------------------------- #
def test_add_furniture_pulls_catalog_defaults():
    c = _room_config()
    c = cp.add_furniture(c, "t1", "table", Point2D(3, 2.5))
    o = c.room.objects[0]
    assert (o.width, o.depth, o.height) == (2.4, 1.0, 0.74)   # catalog
    # occlusion/absorption stay catalog-resolved (unset on the instance)
    assert o.blocks_camera is None and cp.furniture_blocks_camera(o) is False


def test_furniture_edit_and_remove():
    c = _room_config()
    c = cp.add_furniture(c, "t1", "table", Point2D(3, 2.5))
    c = cp.set_furniture_position(c, "t1", Point2D(4, 3))
    c = cp.set_furniture_rotation(c, "t1", 390.0)             # wraps to 30
    c = cp.set_furniture_dimensions(c, "t1", width=3.0)
    c = cp.set_seat_anchors(c, "t1", [SeatAnchor(position=Point2D(4, 2))])
    o = c.room.objects[0]
    assert o.position.x == 4 and o.rotation_deg == 30.0 and o.width == 3.0
    assert o.seats[0].position.y == 2
    c = cp.remove_furniture(c, "t1")
    assert c.room.objects == []


def test_add_furniture_requires_room_and_unique_id():
    no_room = cp.create_config("x", "y")
    with pytest.raises(ValueError):
        cp.add_furniture(no_room, "t1", "table", Point2D(0, 0))
    c = cp.add_furniture(_room_config(), "t1", "chair", Point2D(1, 1))
    with pytest.raises(ValueError):
        cp.add_furniture(c, "t1", "chair", Point2D(2, 2))


# --------------------------------------------------------------------------- #
# Validation codes
# --------------------------------------------------------------------------- #
def test_camera_unplaced_warns():
    c = cp.add_camera(_room_config(), cp.create_camera("cam", "Cam"))  # no position
    assert "CAMERA_UNPLACED" in _codes(c)


def test_camera_no_subject_warns_then_clears():
    c = _room_config()
    c = cp.add_talker(c, cp.create_talker("t", "P", Point2D(3.0, 1.0)))
    cam = cp.create_camera("cam", "Cam")
    cam.position = Point2D(3.0, 4.5)     # at the back wall, facing +Y → subject is behind it
    c = cp.add_camera(c, cam)
    assert "CAMERA_NO_SUBJECT" in _codes(c)
    # move the subject in front of the camera → warning clears
    c = cp.set_talker_position(c, "t", Point2D(3.0, 4.0))
    c = cp.set_camera_bearing(c, "cam", 180.0)   # face -Y, toward the subject
    assert "CAMERA_NO_SUBJECT" not in _codes(c)


def test_furniture_outside_room_warns():
    c = cp.add_furniture(_room_config(), "t1", "table", Point2D(20, 20))
    assert "FURNITURE_OUTSIDE_ROOM" in _codes(c)


def test_device_inside_furniture_warns():
    c = _room_config()
    c = cp.add_furniture(c, "t1", "table", Point2D(3, 2.5))      # table top at 0.74 m
    c = cp.add_device(c, Loudspeaker(id="s", label="LS", ports=[], profile_id="generic-loudspeaker"))
    c = cp.set_device_position(c, "s", Point2D(3, 2.5))
    c = cp.set_device_elevation(c, "s", 0.4)                     # below the table top, inside footprint
    assert "DEVICE_INSIDE_FURNITURE" in _codes(c)


def test_ceiling_array_over_furniture_not_flagged():
    c = _room_config()
    c = cp.add_furniture(c, "t1", "table", Point2D(3, 2.5))
    c = cp.add_device(c, cp.create_microphone_array("A", "Array"))
    c = cp.set_device_position(c, "A", Point2D(3, 2.5))          # ceiling height → above the table
    assert "DEVICE_INSIDE_FURNITURE" not in _codes(c)


def test_degenerate_furniture_is_error():
    c = _room_config()
    c.room.objects = [RoomObject(id="bad", kind="table", position=Point2D(3, 2.5), width=0.0, depth=1.0)]
    res = cp.validate(c)
    assert not res.ok and any(i.code == "FURNITURE_GEOMETRY_INVALID" for i in res.errors)
