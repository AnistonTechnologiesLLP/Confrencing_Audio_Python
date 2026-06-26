import pytest

import conf_pipeline as cp
from conf_pipeline.coverage import CoverageError, set_zone_type
from conf_pipeline.model import Point2D, RectShape, is_pickup_zone


def _array_with_zone(zone, mode="automatic"):
    arr = cp.create_microphone_array("a1", "Array", mode=mode, zones=[zone], position=Point2D(0.0, 0.0))
    return arr


def _dyn(zid="z1"):
    return cp.dynamic_zone(zid, "Zone", RectShape(Point2D(0.0, 0.0), 1.0, 1.0))


def test_dynamic_to_exclusion_sets_flags_and_clears_channel():
    from conf_pipeline.coverage import set_zone_output_channel
    arr = _array_with_zone(_dyn())
    arr = set_zone_output_channel(arr, "z1", 3)   # assign a channel, then flip to exclusion
    out = set_zone_type(arr, "z1", "exclusion")
    z = out.zones[0]
    assert z.type == "exclusion"
    assert z.always_on is False
    assert z.output_channel is None          # cleared (exclusion can't carry one)
    assert not is_pickup_zone(z)


def test_exclusion_to_dynamic_restores_pickup():
    arr = _array_with_zone(cp.exclusion_zone("z1", "Zone", RectShape(Point2D(0.0, 0.0), 1.0, 1.0)))
    out = set_zone_type(arr, "z1", "dynamic")
    z = out.zones[0]
    assert z.type == "dynamic"
    assert z.always_on is False
    assert is_pickup_zone(z)


def test_to_dedicated_sets_always_on():
    arr = _array_with_zone(_dyn())
    out = set_zone_type(arr, "z1", "dedicated")
    assert out.zones[0].type == "dedicated"
    assert out.zones[0].always_on is True


def test_unknown_zone_raises():
    arr = _array_with_zone(_dyn())
    with pytest.raises(CoverageError):
        set_zone_type(arr, "nope", "exclusion")


def test_manual_mode_uncut_past_cap_raises():
    # manual array: MAX_MANUAL_LOBES=8 pickup zones + 1 cut zone would be 9 total (above MAX_ZONES_PER_ARRAY=8),
    # so use 7 pickup + 1 exclusion = 8 total (within limit); un-cutting → 8 pickup = MAX_MANUAL_LOBES;
    # but to actually EXCEED the cap we need 7 pickup + "cut" and the flip takes us to 8 which equals the cap —
    # so instead use 8 pickups (all slots), replace one with exclusion via set_zone_type (that's tested separately),
    # then start with 7 pickup + 1 exclusion (8 total) BUT mark the array with a dummy 8th pickup to force
    # the cap: actually build 7 pickup + 1 exclusion, flip exclusion→dynamic = 8 = MAX_MANUAL_LOBES → OK.
    # To trigger the error we need the resulting count to EXCEED MAX_MANUAL_LOBES.
    # We must pre-flip one existing pickup to exclusion in-array, then add another and try to flip that too.
    # Simplest: build 8 pickups, flip p0→exclusion on the array object directly to bypass create_microphone_array,
    # then call set_zone_type(arr, "p0", "dynamic") which restores a 9th pickup — but MAX_MANUAL_LOBES is 8 so
    # 7+1=8 doesn't exceed. Real overflow: must have 8 pickups already + try to flip exclusion→pickup.
    # Since MAX_ZONES_PER_ARRAY==MAX_MANUAL_LOBES==8, the only way to overflow is to directly mutate the object.
    import copy
    from conf_pipeline.model import MAX_MANUAL_LOBES
    zones = [cp.dynamic_zone(f"p{i}", f"P{i}", RectShape(Point2D(float(i), 0.0), 0.5, 0.5)) for i in range(8)]
    arr = cp.create_microphone_array("a1", "Array", mode="manual", zones=zones, position=Point2D(0.0, 0.0))
    # Directly inject an extra exclusion zone beyond normal limits to simulate the overflow scenario
    cut = cp.exclusion_zone("cut", "Cut", RectShape(Point2D(9.0, 0.0), 0.5, 0.5))
    arr2 = copy.copy(arr)
    arr2.zones = list(arr.zones) + [cut]   # 9 zones: 8 pickup + 1 exclusion (bypass the constructor guard)
    with pytest.raises(CoverageError):
        set_zone_type(arr2, "cut", "dynamic")   # would be 9 pickup lobes > MAX_MANUAL_LOBES=8


def test_flip_to_exclusion_in_manual_mode_is_allowed():
    # flipping a pickup zone TO exclusion lowers the count → always safe even in manual mode
    zones = [cp.dynamic_zone(f"p{i}", f"P{i}", RectShape(Point2D(float(i), 0.0), 0.5, 0.5)) for i in range(7)]
    arr = cp.create_microphone_array("a1", "Array", mode="manual", zones=zones, position=Point2D(0.0, 0.0))
    out = set_zone_type(arr, "p0", "exclusion")
    assert out.zones[0].type == "exclusion"


def test_config_wrapper_and_validate_and_roundtrip():
    cfg = cp.create_config("Test", "2026-01-01T00:00:00Z")
    cfg = cp.add_device(cfg, cp.create_microphone_array("a1", "Array", position=Point2D(0.0, 0.0)))
    cfg = cp.add_coverage_zone(cfg, "a1", _dyn())
    cfg = cp.set_zone_type(cfg, "a1", "z1", "exclusion")
    arr = next(d for d in cfg.devices if d.id == "a1")
    assert arr.zones[0].type == "exclusion"
    assert cp.validate(cfg).ok
    # byte-identical round-trip
    assert cp.serialize(cp.deserialize(cp.serialize(cfg))) == cp.serialize(cfg)


def test_cut_zone_is_honored_by_exclusion_azimuths():
    # a cut zone shows up in the live null path's azimuth list (proves no live-DSP edit needed)
    from conf_pipeline.seat_mapper import exclusion_zone_azimuths
    cfg = cp.create_config("Test", "2026-01-01T00:00:00Z")
    arr = cp.create_microphone_array("a1", "Array", position=Point2D(0.0, 0.0))
    arr.bearing_deg = 0.0
    cfg = cp.add_device(cfg, arr)
    cfg = cp.add_coverage_zone(cfg, "a1", cp.dynamic_zone("z1", "Zone", RectShape(Point2D(1.0, 1.0), 0.5, 0.5)))
    assert exclusion_zone_azimuths(cfg, "a1") == []   # not cut yet → no nulls
    cfg = cp.set_zone_type(cfg, "a1", "z1", "exclusion")
    assert len(exclusion_zone_azimuths(cfg, "a1")) == 1   # now cut → nulled live
