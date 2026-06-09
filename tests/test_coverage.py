import pytest

from conf_pipeline.coverage import (
    CoverageError,
    add_coverage_zone,
    create_microphone_array,
    dedicated_zone,
    dynamic_zone,
    exclusion_zone,
    generate_array_output_ports,
    remove_coverage_zone,
    set_coverage_mode,
)
from conf_pipeline.model import MAX_ZONES_PER_ARRAY, Point2D, RectShape


def rect(zid):
    return dynamic_zone(zid, zid, RectShape(origin=Point2D(0, 0), width=1, height=1))


def test_automatic_single_output():
    a = create_microphone_array("arr", "Array", "automatic")
    assert [p.id for p in a.ports] == ["arr-out-mix"]


def test_manual_outputs_per_zone_plus_automix():
    a = create_microphone_array("arr", "Array", "manual")
    a = add_coverage_zone(a, rect("z1"))
    a = add_coverage_zone(a, rect("z2"))
    assert [p.id for p in a.ports] == ["arr-out-lobe-1", "arr-out-lobe-2", "arr-out-automix"]


def test_lobe_cap_8():
    ports = generate_array_output_ports("arr", "manual", 8)
    assert len([p for p in ports if "lobe" in p.id]) == 8
    assert len([p for p in ports if "automix" in p.id]) == 1


def test_mode_switch_regenerates_ports():
    a = create_microphone_array("arr", "Array", "manual")
    a = add_coverage_zone(a, rect("z1"))
    assert [p.id for p in a.ports] == ["arr-out-lobe-1", "arr-out-automix"]
    a = set_coverage_mode(a, "automatic")
    assert [p.id for p in a.ports] == ["arr-out-mix"]


def test_immutable():
    a0 = create_microphone_array("arr", "Array", "automatic")
    a1 = add_coverage_zone(a0, rect("z1"))
    assert len(a0.zones) == 0 and len(a1.zones) == 1 and a0 is not a1


def test_reject_ninth_zone():
    a = create_microphone_array("arr", "Array", "automatic")
    for i in range(MAX_ZONES_PER_ARRAY):
        a = add_coverage_zone(a, rect(f"z{i}"))
    with pytest.raises(CoverageError):
        add_coverage_zone(a, rect("overflow"))


def test_dedicated_always_on():
    ded = dedicated_zone("d", "Podium", Point2D(2, 2))
    assert ded.always_on and ded.type == "dedicated"
    assert rect("y").always_on is False


def test_reject_always_on_mismatch():
    bad = rect("b")
    bad.always_on = True
    with pytest.raises(CoverageError, match="always_on"):
        add_coverage_zone(create_microphone_array("arr", "A"), bad)


def test_reject_degenerate_geometry():
    bad = dynamic_zone("b", "b", RectShape(origin=Point2D(0, 0), width=0, height=1))
    with pytest.raises(CoverageError, match="positive width"):
        add_coverage_zone(create_microphone_array("arr", "A"), bad)


def test_exclusion_no_lobe():
    ex = exclusion_zone("x", "Doorway", RectShape(origin=Point2D(0, 0), width=1, height=1))
    assert ex.type == "exclusion" and ex.always_on is False
    a = create_microphone_array("arr", "Array", "manual")
    a = add_coverage_zone(a, rect("z1"))
    a = add_coverage_zone(a, ex)
    assert len(a.zones) == 2
    assert [p.id for p in a.ports] == ["arr-out-lobe-1", "arr-out-automix"]


def test_exclusion_counts_toward_max():
    a = create_microphone_array("arr", "Array", "automatic")
    for i in range(4):
        a = add_coverage_zone(a, rect(f"z{i}"))
    for i in range(4):
        a = add_coverage_zone(a, exclusion_zone(f"x{i}", f"x{i}", RectShape(origin=Point2D(0, 0), width=1, height=1)))
    assert len(a.zones) == 8
    with pytest.raises(CoverageError):
        add_coverage_zone(a, rect("overflow"))


def test_remove_zone_regenerates():
    a = create_microphone_array("arr", "Array", "manual")
    a = add_coverage_zone(a, rect("z1"))
    a = add_coverage_zone(a, rect("z2"))
    a = remove_coverage_zone(a, "z1")
    assert [p.id for p in a.ports] == ["arr-out-lobe-1", "arr-out-automix"]
