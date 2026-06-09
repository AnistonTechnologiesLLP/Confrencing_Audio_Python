import math

import pytest

import conf_pipeline as cp
from conf_pipeline.angles import Point3D, steering_angles
from conf_pipeline.coverage import add_coverage_zone, create_microphone_array, dynamic_zone, exclusion_zone
from conf_pipeline.model import Point2D, RectShape


# ---- room & device placement / elevation ----
def test_room_attach_clear():
    c = cp.create_config("r", "x")
    c = cp.set_room(c, cp.rectangular_room(8, 6, 3))
    assert len(c.room.vertices) == 4 and c.room.height == 3 and c.room.units == "meters"
    c = cp.clear_room(c)
    assert c.room is None


def test_device_position_and_elevation():
    c = cp.create_config("r", "x")
    c = cp.add_device(c, cp.create_microphone_array("A", "Array", "automatic"))
    assert cp.find_device(c, "A").elevation is None
    assert cp.default_elevation(cp.find_device(c, "A"), 3) == 3
    c = cp.set_device_elevation(c, "A", 2.6)
    assert cp.find_device(c, "A").elevation == 2.6
    c = cp.set_device_position(c, "A", Point2D(5, 3))
    assert cp.find_device(c, "A").position == Point2D(5, 3)


def test_round_trip_room_position_elevation():
    c = cp.create_config("r", "x")
    c = cp.add_device(c, cp.create_processor("P", "P"))
    c = cp.set_room(c, cp.rectangular_room(10, 7))
    c = cp.set_device_position(c, "P", Point2D(5, 3))
    c = cp.set_device_elevation(c, "P", 0.5)
    assert cp.serialize(cp.deserialize(cp.serialize(c))) == cp.serialize(c)


# ---- steering angles ----
def test_steering_angles_known_geometry():
    a = steering_angles(Point3D(0, 0, 3), Point3D(4, 0, 1))
    assert a.horizontal_distance == pytest.approx(4)
    assert a.distance == pytest.approx(math.hypot(4, 2))
    assert a.azimuth_deg == pytest.approx(90)
    assert a.downtilt_deg == pytest.approx(26.565, abs=0.01)
    assert a.off_nadir_deg == pytest.approx(63.435, abs=0.01)


def test_directly_below_is_zero_off_nadir():
    a = steering_angles(Point3D(2, 2, 3), Point3D(2, 2, 1))
    assert a.off_nadir_deg == pytest.approx(0)
    assert a.downtilt_deg == pytest.approx(90)


# ---- talkers ----
def base_with_array():
    c = cp.create_config("t", "x")
    c = cp.add_device(c, create_microphone_array("A", "Array", "automatic"))
    c = cp.set_device_position(c, "A", Point2D(0, 0))
    c = cp.set_device_elevation(c, "A", 3)
    return c


def test_talker_crud():
    c = base_with_array()
    c = cp.add_talker(c, cp.create_talker("T1", "Presenter", Point2D(4, 0), 1))
    assert len(c.talkers) == 1
    c = cp.rename_talker(c, "T1", "Speaker A")
    assert c.talkers[0].label == "Speaker A"
    c = cp.set_talker_position(c, "T1", Point2D(2, 2))
    c = cp.set_talker_elevation(c, "T1", 1.4)
    assert c.talkers[0].position == Point2D(2, 2) and c.talkers[0].elevation == 1.4
    c = cp.remove_talker(c, "T1")
    assert c.talkers == []


def test_duplicate_talker():
    c = base_with_array()
    c = cp.add_talker(c, cp.create_talker("T1", "A", Point2D(1, 1)))
    with pytest.raises(ValueError, match="Duplicate"):
        cp.add_talker(c, cp.create_talker("T1", "B", Point2D(2, 2)))


def test_array_to_talker_angles():
    c = base_with_array()
    c = cp.add_talker(c, cp.create_talker("T1", "Presenter", Point2D(4, 0), 1))
    ang = cp.array_to_talker_angles(c, "A", "T1")
    assert ang is not None
    assert ang.off_nadir_deg == pytest.approx(63.435, abs=0.01)
    assert ang.azimuth_deg == pytest.approx(90)


def test_angles_none_when_unplaced():
    c = cp.create_config("t", "x")
    c = cp.add_device(c, create_microphone_array("A", "Array", "automatic"))  # no position
    c = cp.add_talker(c, cp.create_talker("T1", "P", Point2D(1, 1)))
    assert cp.array_to_talker_angles(c, "A", "T1") is None
    assert cp.array_to_talker_angles(c, "A", "nope") is None


def test_talker_coverage_and_exclusion():
    c = cp.create_config("t", "x")
    c = cp.add_device(c, create_microphone_array("A", "Array", "automatic"))
    c = cp.add_coverage_zone(c, "A", dynamic_zone("z1", "pickup", RectShape(origin=Point2D(0, 0), width=4, height=4)))
    c = cp.add_talker(c, cp.create_talker("T1", "Inside", Point2D(2, 2)))
    c = cp.add_talker(c, cp.create_talker("T2", "Outside", Point2D(9, 9)))
    assert cp.talker_coverage(c, "T1").captured
    assert not cp.talker_coverage(c, "T2").captured
    c = cp.add_coverage_zone(c, "A", exclusion_zone("x1", "dead", RectShape(origin=Point2D(1, 1), width=2, height=2)))
    cov = cp.talker_coverage(c, "T1")
    assert not cov.captured and "A" in cov.excluded_by


def test_round_trip_talkers():
    c = cp.create_config("t", "x")
    c = cp.add_talker(c, cp.create_talker("T1", "Presenter", Point2D(4, 2), 1.3))
    assert cp.serialize(cp.deserialize(cp.serialize(c))) == cp.serialize(c)


def test_legacy_json_without_talkers():
    import json
    c = cp.create_config("t", "x")
    obj = json.loads(cp.serialize(c))
    del obj["talkers"]
    restored = cp.deserialize(json.dumps(obj))
    assert restored.talkers == []
