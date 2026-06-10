"""Zone → look-direction geometry (pure, no hardware)."""
import math

import pytest

import conf_pipeline as cp
from conf_pipeline.model import Point2D, RectShape
import conf_pipeline_control as cc


def _scene():
    c = cp.create_config("Room", "2026-06-10T00:00:00Z")
    c = cp.set_room(c, cp.rectangular_room(8, 6, 3))
    c = cp.add_device(c, cp.create_microphone_array("A", "Ceiling Array"))
    c = cp.set_device_position(c, "A", Point2D(4, 3))  # array centred
    return c


def _rect_zone(zid, x, y, w, h, label, kind="dynamic"):
    return cp.CoverageZone(id=zid, type=kind, shape=RectShape(Point2D(x, y), w, h), always_on=False, label=label)


def test_centroid_of_rect():
    z = _rect_zone("z", 1, 1, 2, 4, "Z")
    ctr = cc.zone_centroid(z)
    assert ctr.x == pytest.approx(2.0) and ctr.y == pytest.approx(3.0)


def test_look_direction_straight_down_under_array():
    c = _scene()
    d = cc.look_direction(c, "A", Point2D(4, 3))  # directly below the array
    assert d.off_nadir_deg == pytest.approx(0.0, abs=1e-6)
    assert d.unit[2] < 0  # pointing down
    assert d.unit[0] == pytest.approx(0.0, abs=1e-6)
    assert d.unit[1] == pytest.approx(0.0, abs=1e-6)


def test_look_direction_to_east_has_positive_x():
    c = _scene()
    d = cc.look_direction(c, "A", Point2D(7, 3))  # east of the array
    assert d.unit[0] > 0.2
    assert d.azimuth_deg == pytest.approx(90.0, abs=1.0)  # +X = 90° bearing
    # unit vector is normalised
    assert math.isclose(math.sqrt(sum(v * v for v in d.unit)), 1.0, rel_tol=1e-9)


def test_pickup_and_exclusion_split():
    c = _scene()
    arr = cp.find_device(c, "A")
    arr.zones = [
        _rect_zone("p1", 5, 2, 1, 1, "Talk", "dynamic"),
        _rect_zone("x1", 1, 1, 1, 1, "Door", "exclusion"),
    ]
    pk = cc.pickup_directions(c, "A")
    ex = cc.exclusion_directions(c, "A")
    assert [z.id for z, _ in pk] == ["p1"]
    assert [z.id for z, _ in ex] == ["x1"]


def test_no_position_raises():
    c = cp.create_config("Room", "x")
    c = cp.set_room(c, cp.rectangular_room(8, 6, 3))
    c = cp.add_device(c, cp.create_microphone_array("A", "Array"))  # no position
    with pytest.raises(ValueError):
        cc.look_direction(c, "A", Point2D(1, 1))


def test_not_an_array_raises():
    c = cp.create_config("Room", "x")
    c = cp.add_device(c, cp.create_processor("P", "DSP"))
    with pytest.raises(ValueError):
        cc.look_direction(c, "P", Point2D(1, 1))
