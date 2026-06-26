# tests/test_active_zone_gain.py
import conf_pipeline as cp
from conf_pipeline.model import Point2D, RectShape
from conf_pipeline.seat_mapper import active_zone_gain_db, azimuth_for_array_point


def _posed():
    cfg = cp.create_config("T", "2026-01-01T00:00:00Z")
    arr = cp.create_microphone_array("a1", "Array", position=Point2D(0.0, 0.0))
    arr.bearing_deg = 0.0
    cfg = cp.add_device(cfg, arr)
    return cfg


def test_azimuth_into_zone_returns_its_gain():
    cfg = _posed()
    cfg = cp.add_coverage_zone(cfg, "a1", cp.dynamic_zone("z1", "Z", RectShape(Point2D(0.5, 1.5), 1.0, 1.0)))
    cfg = cp.set_zone_gain_db(cfg, "a1", "z1", -6.0)
    az = azimuth_for_array_point(cfg, "a1", Point2D(1.0, 2.0))   # a point inside the zone
    assert abs(active_zone_gain_db(cfg, "a1", az) - (-6.0)) < 1e-9


def test_azimuth_outside_any_zone_returns_none():
    cfg = _posed()
    cfg = cp.add_coverage_zone(cfg, "a1", cp.dynamic_zone("z1", "Z", RectShape(Point2D(0.5, 1.5), 1.0, 1.0)))
    cfg = cp.set_zone_gain_db(cfg, "a1", "z1", -6.0)
    assert active_zone_gain_db(cfg, "a1", 180.0) is None   # behind → not in the zone


def test_zone_without_gain_returns_none():
    cfg = _posed()
    cfg = cp.add_coverage_zone(cfg, "a1", cp.dynamic_zone("z1", "Z", RectShape(Point2D(0.5, 1.5), 1.0, 1.0)))
    az = azimuth_for_array_point(cfg, "a1", Point2D(1.0, 2.0))
    assert active_zone_gain_db(cfg, "a1", az) is None       # gain_db unset
