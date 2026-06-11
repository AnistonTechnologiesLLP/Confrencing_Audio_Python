"""Tests for the v1.12.0 Designer-style features:

- per-coverage-area output channels + gain trim (A1/A5)
- zone-vs-coverage report (A2)
- optimize_room() orchestrator (A3)
- logic/mute-control mute groups (A4)

All stay inside the TS-interoperable JSON schema (additive optional fields).
"""
import pytest

import conf_pipeline as cp
from conf_pipeline.coverage import (
    CoverageError,
    auto_assign_zone_channels,
    create_microphone_array,
    add_coverage_zone,
    dynamic_zone,
    exclusion_zone,
    set_zone_gain_db,
    set_zone_output_channel,
)
from conf_pipeline.model import (
    MAX_ZONES_PER_ARRAY,
    ZONE_GAIN_DB_MAX,
    Point2D,
    RectShape,
    ZoneChannelRef,
)


def _rect(zid, x=0.0, y=0.0, w=1.0, h=1.0):
    return dynamic_zone(zid, zid, RectShape(origin=Point2D(x, y), width=w, height=h))


# --------------------------------------------------------------------------- #
# A1 / A5 — per-area output channel + gain (array level)
# --------------------------------------------------------------------------- #
def test_zone_channel_adds_output_port():
    a = create_microphone_array("arr", "Array", "automatic")
    a = add_coverage_zone(a, _rect("z1"))
    a = set_zone_output_channel(a, "z1", 1)
    assert "arr-out-ch-1" in [p.id for p in a.ports]
    assert next(z for z in a.zones if z.id == "z1").output_channel == 1


def test_zone_channel_clear_removes_port():
    a = create_microphone_array("arr", "Array")
    a = add_coverage_zone(a, _rect("z1"))
    a = set_zone_output_channel(a, "z1", 2)
    a = set_zone_output_channel(a, "z1", None)
    assert not any("ch-" in p.id for p in a.ports)
    assert next(z for z in a.zones if z.id == "z1").output_channel is None


def test_zone_channel_ports_sorted_by_channel():
    a = create_microphone_array("arr", "Array")
    a = add_coverage_zone(a, _rect("z1"))
    a = add_coverage_zone(a, _rect("z2"))
    a = set_zone_output_channel(a, "z2", 1)
    a = set_zone_output_channel(a, "z1", 2)
    chan_ports = [p.id for p in a.ports if "ch-" in p.id]
    assert chan_ports == ["arr-out-ch-1", "arr-out-ch-2"]


def test_zone_channel_duplicate_rejected():
    a = create_microphone_array("arr", "Array")
    a = add_coverage_zone(a, _rect("z1"))
    a = add_coverage_zone(a, _rect("z2"))
    a = set_zone_output_channel(a, "z1", 1)
    with pytest.raises(CoverageError) as e:
        set_zone_output_channel(a, "z2", 1)
    assert e.value.code == "COVERAGE_CHANNEL_DUPLICATE"


def test_zone_channel_out_of_range_rejected():
    a = create_microphone_array("arr", "Array")
    a = add_coverage_zone(a, _rect("z1"))
    with pytest.raises(CoverageError) as e:
        set_zone_output_channel(a, "z1", MAX_ZONES_PER_ARRAY + 1)
    assert e.value.code == "COVERAGE_CHANNEL_INVALID"


def test_exclusion_zone_cannot_have_channel():
    a = create_microphone_array("arr", "Array")
    a = add_coverage_zone(a, exclusion_zone("ex", "ex", RectShape(origin=Point2D(0, 0), width=1, height=1)))
    with pytest.raises(CoverageError) as e:
        set_zone_output_channel(a, "ex", 1)
    assert e.value.code == "COVERAGE_CHANNEL_INVALID"


def test_zone_gain_set_and_range():
    a = create_microphone_array("arr", "Array")
    a = add_coverage_zone(a, _rect("z1"))
    a = set_zone_gain_db(a, "z1", 3.0)
    assert next(z for z in a.zones if z.id == "z1").gain_db == 3.0
    with pytest.raises(CoverageError) as e:
        set_zone_gain_db(a, "z1", ZONE_GAIN_DB_MAX + 1)
    assert e.value.code == "COVERAGE_GAIN_INVALID"


def test_auto_assign_zone_channels_idempotent():
    a = create_microphone_array("arr", "Array")
    a = add_coverage_zone(a, _rect("z1"))
    a = add_coverage_zone(a, _rect("z2"))
    a = add_coverage_zone(a, exclusion_zone("ex", "ex", RectShape(origin=Point2D(0, 0), width=1, height=1)))
    a = auto_assign_zone_channels(a)
    chans = {z.id: z.output_channel for z in a.zones}
    assert chans["z1"] == 1 and chans["z2"] == 2 and chans["ex"] is None
    a2 = auto_assign_zone_channels(a)
    assert {z.id: z.output_channel for z in a2.zones} == chans


def test_auto_assign_preserves_existing_channel():
    a = create_microphone_array("arr", "Array")
    a = add_coverage_zone(a, _rect("z1"))
    a = add_coverage_zone(a, _rect("z2"))
    a = set_zone_output_channel(a, "z2", 1)
    a = auto_assign_zone_channels(a)
    chans = {z.id: z.output_channel for z in a.zones}
    assert chans["z2"] == 1 and chans["z1"] == 2


# --------------------------------------------------------------------------- #
# A1 — validation
# --------------------------------------------------------------------------- #
def _config_with_array():
    c = cp.create_config("T", "2026-06-11T00:00:00Z")
    c = cp.set_room(c, cp.rectangular_room(8, 6, 3))
    c = cp.add_device(c, cp.create_microphone_array("A", "Array"))
    c = cp.add_coverage_zone(c, "A", _rect("z1", 2, 2, 2, 2))
    c = cp.add_coverage_zone(c, "A", _rect("z2", 5, 2, 2, 2))
    return c


def test_validation_flags_duplicate_channel():
    c = _config_with_array()
    c = cp.set_zone_output_channel(c, "A", "z1", 1)
    # Force a duplicate by editing the deserialized form (bypassing the builder guard).
    d = cp.to_jsonable(c)
    d["devices"][0]["zones"][1]["outputChannel"] = 1
    c2 = cp.config_from_dict(d)
    res = cp.validate(c2)
    assert not res.ok
    assert "COVERAGE_CHANNEL_DUPLICATE" in [e.code for e in res.errors]


def test_validation_passes_with_valid_channels():
    c = _config_with_array()
    c = cp.set_zone_output_channel(c, "A", "z1", 1)
    c = cp.set_zone_output_channel(c, "A", "z2", 2)
    c = cp.set_zone_gain_db(c, "A", "z1", -3.0)
    res = cp.validate(c)
    assert res.ok, [e.code for e in res.errors]


def test_channel_gain_round_trip():
    c = _config_with_array()
    c = cp.set_zone_output_channel(c, "A", "z1", 3)
    c = cp.set_zone_gain_db(c, "A", "z1", 4.5)
    c2 = cp.deserialize(cp.serialize(c))
    assert cp.to_jsonable(c) == cp.to_jsonable(c2)
    z = next(z for z in cp.find_device(c2, "A").zones if z.id == "z1")
    assert z.output_channel == 3 and z.gain_db == 4.5


def test_plain_config_omits_new_fields():
    """A zone without channel/gain must not emit the keys (TS interop)."""
    c = _config_with_array()
    d = cp.to_jsonable(c)
    zone0 = d["devices"][0]["zones"][0]
    assert "outputChannel" not in zone0
    assert "gainDb" not in zone0


# --------------------------------------------------------------------------- #
# A2 — zone-vs-coverage report
# --------------------------------------------------------------------------- #
def test_zone_coverage_report_covered():
    c = cp.create_config("T", "2026-06-11T00:00:00Z")
    c = cp.set_room(c, cp.rectangular_room(8, 6, 3))
    c = cp.add_device(c, cp.create_microphone_array("A", "Array"))
    c = cp.set_device_position(c, "A", Point2D(4, 3))
    c = cp.add_coverage_zone(c, "A", _rect("z1", 3.5, 2.5, 1, 1))
    rep = cp.zone_coverage_report(c)
    assert len(rep.zones) == 1
    st = rep.zones[0]
    assert st.centroid_covered and st.fully_covered
    assert rep.uncovered == []


def test_zone_coverage_report_uncovered_far_zone():
    c = cp.create_config("T", "2026-06-11T00:00:00Z")
    c = cp.set_room(c, cp.rectangular_room(20, 20, 3))
    c = cp.add_device(c, cp.create_microphone_array("A", "Array"))
    c = cp.set_device_position(c, "A", Point2D(1, 1))
    c = cp.add_coverage_zone(c, "A", _rect("z1", 18, 18, 1, 1))
    rep = cp.zone_coverage_report(c)
    assert len(rep.uncovered) == 1


def test_zone_coverage_report_contention():
    c = cp.create_config("T", "2026-06-11T00:00:00Z")
    c = cp.set_room(c, cp.rectangular_room(8, 6, 3))
    c = cp.add_device(c, cp.create_microphone_array("A", "A"))
    c = cp.set_device_position(c, "A", Point2D(4, 3))
    c = cp.add_device(c, cp.create_microphone_array("B", "B"))
    c = cp.set_device_position(c, "B", Point2D(4.2, 3.2))
    c = cp.add_coverage_zone(c, "A", _rect("z1", 3.6, 2.6, 0.8, 0.8))
    rep = cp.zone_coverage_report(c)
    assert any(z.contended for z in rep.zones)
    assert rep.contended


# --------------------------------------------------------------------------- #
# A3 — optimize_room
# --------------------------------------------------------------------------- #
def _full_room():
    c = cp.create_config("T", "2026-06-11T00:00:00Z")
    c = cp.set_room(c, cp.rectangular_room(8, 6, 3))
    c = cp.add_device(c, cp.create_microphone_array("A", "Array"))
    c = cp.add_coverage_zone(c, "A", _rect("z1", 3, 2, 2, 2))
    c = cp.add_talker(c, cp.create_talker("t1", "P", Point2D(4, 3)))
    c = cp.add_device(c, cp.create_processor("P", "DSP"))
    c = cp.add_device(c, cp.create_codec("C", "Codec", "dante"))
    return c


def test_optimize_room_runs_all_stages():
    c = _full_room()
    res = cp.optimize_room(c)
    assert cp.find_device(res.config, "A").position is not None
    assert any(z.output_channel is not None for z in cp.find_device(res.config, "A").zones)
    assert res.auto_route is not None
    assert cp.validate(res.config).ok


def test_optimize_room_idempotent_routing():
    c = _full_room()
    once = cp.optimize_room(c).config
    twice = cp.optimize_room(once).config
    # routing/channels stable on a second pass (placement may re-confirm same pose)
    assert cp.to_jsonable(once) == cp.to_jsonable(twice)


def test_optimize_room_opt_out_stages():
    c = _full_room()
    res = cp.optimize_room(c, place_arrays=False, assign_channels=False, route=False)
    assert cp.find_device(res.config, "A").position is None


def test_optimize_room_no_processor():
    c = cp.create_config("T", "2026-06-11T00:00:00Z")
    res = cp.optimize_room(c)
    assert res.auto_route is not None  # auto_route still called; reports no processor


# --------------------------------------------------------------------------- #
# A4 — mute groups
# --------------------------------------------------------------------------- #
def test_add_mute_group_and_validate():
    c = _config_with_array()
    g = cp.create_mute_group("mg1", "Room mute", device_ids=["A"], zone_refs=[ZoneChannelRef("A", "z1")])
    c = cp.add_mute_group(c, g)
    res = cp.validate(c)
    assert res.ok, [e.code for e in res.errors]
    assert len(c.control.mute_groups) == 1


def test_mute_group_duplicate_id_rejected():
    c = _config_with_array()
    c = cp.add_mute_group(c, cp.create_mute_group("mg1", "x", device_ids=["A"]))
    with pytest.raises(ValueError):
        cp.add_mute_group(c, cp.create_mute_group("mg1", "y", device_ids=["A"]))


def test_mute_group_missing_device_flagged():
    c = _config_with_array()
    c = cp.add_mute_group(c, cp.create_mute_group("mg1", "x", device_ids=["NOPE"]))
    res = cp.validate(c)
    assert "CONTROL_MUTE_GROUP_INVALID" in [e.code for e in res.errors]


def test_mute_group_missing_zone_flagged():
    c = _config_with_array()
    c = cp.add_mute_group(c, cp.create_mute_group("mg1", "x", zone_refs=[ZoneChannelRef("A", "nope")]))
    res = cp.validate(c)
    assert "CONTROL_MUTE_GROUP_INVALID" in [e.code for e in res.errors]


def test_mute_group_empty_flagged():
    c = _config_with_array()
    c = cp.add_mute_group(c, cp.create_mute_group("mg1", "x"))
    res = cp.validate(c)
    assert "CONTROL_MUTE_GROUP_INVALID" in [e.code for e in res.errors]


def test_mute_group_toggle_and_remove():
    c = _config_with_array()
    c = cp.add_mute_group(c, cp.create_mute_group("mg1", "x", device_ids=["A"]))
    c = cp.set_mute_group_muted(c, "mg1", True)
    assert c.control.mute_groups[0].muted is True
    c = cp.remove_mute_group(c, "mg1")
    assert c.control.mute_groups == []


def test_control_round_trip():
    c = _config_with_array()
    c = cp.add_mute_group(c, cp.create_mute_group("mg1", "Room", device_ids=["A"],
                                                  zone_refs=[ZoneChannelRef("A", "z1")], trigger="button"))
    c2 = cp.deserialize(cp.serialize(c))
    assert cp.to_jsonable(c) == cp.to_jsonable(c2)


def test_no_control_omits_field():
    c = _config_with_array()
    assert "control" not in cp.to_jsonable(c)
