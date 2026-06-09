"""Geometric coverage-check engine tests (pure, numpy-free)."""
import math

import pytest

import conf_pipeline as cp
from conf_pipeline.model import Point2D, RectShape


def _cfg():
    c = cp.create_config("cov", "x")
    c = cp.set_room(c, cp.rectangular_room(10, 8, 3))
    c = cp.add_device(c, cp.create_microphone_array("A", "Array", "automatic"))
    c = cp.set_device_position(c, "A", Point2D(5, 4))
    c = cp.set_device_elevation(c, "A", 3.0)
    return c


def test_radius_math():
    assert cp.array_coverage_radius(3.0, 1.2, 120.0) == pytest.approx(1.8 * math.tan(math.radians(60)), abs=1e-6)


def test_radius_degenerate():
    assert cp.array_coverage_radius(3.0, 1.2, None) == 0.0          # no angle
    assert cp.array_coverage_radius(3.0, 1.2, 0.0) == 0.0           # zero angle
    assert cp.array_coverage_radius(1.0, 1.2, 120.0) == 0.0         # target above mount


def test_circle_for_ceiling_array():
    circ = cp.array_coverage_circle(_cfg(), "A")
    assert circ is not None
    center, radius = circ
    assert center == Point2D(5, 4)
    assert radius == pytest.approx(3.117, abs=0.01)


def test_circle_none_when_unplaced():
    c = cp.create_config("x", "y")
    c = cp.add_device(c, cp.create_microphone_array("A", "A", "automatic"))
    assert cp.array_coverage_circle(c, "A") is None


def test_circle_none_for_non_array():
    c = _cfg()
    c = cp.add_device(c, cp.create_wireless_mic("WM", "wm", "dante"))
    c = cp.set_device_position(c, "WM", Point2D(2, 2))
    assert cp.array_coverage_circle(c, "WM") is None


def test_covered_and_uncovered_by_distance():
    c = _cfg()
    c = cp.add_talker(c, cp.create_talker("T1", "near", Point2D(6, 4)))     # 1 m < 3.1
    c = cp.add_talker(c, cp.create_talker("T2", "far", Point2D(9.5, 7.5)))  # corner > 3.1
    rep = cp.coverage_report(c)
    assert "T1" in rep.covered and "T2" in rep.uncovered


def test_exclusion_overrides_coverage():
    c = _cfg()
    c = cp.add_coverage_zone(c, "A", cp.exclusion_zone("A-x", "dead", RectShape(origin=Point2D(5, 3), width=2, height=2)))
    c = cp.add_talker(c, cp.create_talker("T1", "in-excl", Point2D(5.5, 3.5)))  # inside circle but excluded
    assert "T1" in cp.coverage_report(c).uncovered


def test_overlap_detection():
    c = cp.create_config("ov", "x")
    c = cp.set_room(c, cp.rectangular_room(12, 6, 3))
    c = cp.add_device(c, cp.create_microphone_array("A1", "A1", "automatic"))
    c = cp.set_device_position(c, "A1", Point2D(4, 3))
    c = cp.add_device(c, cp.create_microphone_array("A2", "A2", "automatic"))
    c = cp.set_device_position(c, "A2", Point2D(6, 3))
    assert ("A1", "A2") in cp.coverage_report(c).overlaps
    c = cp.set_device_position(c, "A2", Point2D(11.5, 3))  # now far apart
    assert cp.coverage_report(c).overlaps == []


def test_profile_has_coverage_angle():
    from conf_pipeline import DEVICE_PROFILES
    assert DEVICE_PROFILES["generic-ceiling-array"].capabilities.coverage_angle_deg == 120.0
    assert DEVICE_PROFILES["generic-loudspeaker"].capabilities.coverage_angle_deg is None
