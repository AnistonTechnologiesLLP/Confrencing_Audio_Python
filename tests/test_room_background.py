"""Floor-plan background (engine) tests: round-trip, builders, calibration math."""
import json

import pytest

import conf_pipeline as cp
from conf_pipeline.model import Point2D


def _room_cfg():
    return cp.set_room(cp.create_config("bg", "x"), cp.rectangular_room(10, 8, 3))


def test_set_and_roundtrip():
    c = _room_cfg()
    c = cp.set_room_background(c, "plan.png", 1000, 800, scale_m_per_px=0.01, origin=Point2D(0, 0), opacity=0.6)
    assert cp.serialize(cp.deserialize(cp.serialize(c))) == cp.serialize(c)
    bg = c.room.background
    assert bg.path == "plan.png" and bg.image_width_px == 1000 and bg.scale_m_per_px == 0.01 and bg.opacity == 0.6


def test_none_background_omitted_from_json():
    obj = json.loads(cp.serialize(_room_cfg()))
    assert "background" not in obj["room"]


def test_uncalibrated_scale_omitted_and_roundtrips():
    c = cp.set_room_background(_room_cfg(), "plan.png", 800, 600)  # no scale
    obj = json.loads(cp.serialize(c))
    assert "scaleMPerPx" not in obj["room"]["background"]
    assert cp.deserialize(cp.serialize(c)).room.background.scale_m_per_px is None


def test_set_scale_preserves_other_fields():
    c = cp.set_room_background(_room_cfg(), "p.png", 100, 100, scale_m_per_px=0.02, origin=Point2D(1, 2), opacity=0.4)
    c = cp.set_room_background_scale(c, 0.05)
    bg = c.room.background
    assert bg.scale_m_per_px == 0.05 and bg.origin == Point2D(1, 2) and bg.opacity == 0.4 and bg.path == "p.png"


def test_clear_background():
    c = cp.set_room_background(_room_cfg(), "p.png", 10, 10)
    assert cp.clear_room_background(c).room.background is None


def test_calibrated_scale_math():
    # a line spanning 2 m on the floor that's really 4 m -> scale doubles
    assert cp.calibrated_scale(0.01, 2.0, 4.0) == pytest.approx(0.02)
    with pytest.raises(ValueError):
        cp.calibrated_scale(0.01, 0.0, 4.0)


def test_opacity_clamped():
    c = cp.set_room_background(_room_cfg(), "p.png", 10, 10, opacity=5.0)
    assert c.room.background.opacity == 1.0


def test_no_room_raises():
    with pytest.raises(ValueError):
        cp.set_room_background(cp.create_config("x", "y"), "p.png", 10, 10)
