import conf_pipeline as cp
from conf_pipeline.coverage import add_coverage_zone, create_microphone_array, dynamic_zone
from conf_pipeline.model import AutomixerChannel, AutomixerConfig, Point2D, RectShape


def codes(issues):
    return [i.code for i in issues]


def test_transport_mismatch():
    c = cp.create_config("t", "x")
    c = cp.add_device(c, cp.create_processor("P", "P"))
    c = cp.add_device(c, cp.create_loudspeaker("S", "S", "analog"))
    c = cp.route(c, "P-out-dante-1", "S-in-analog-1")
    assert "ROUTE_TRANSPORT_MISMATCH" in codes(cp.validate(c).errors)


def test_direction_invalid():
    c = cp.create_config("t", "x")
    c = cp.add_device(c, cp.create_processor("P", "P"))
    c = cp.add_device(c, cp.create_wireless_mic("M", "M", "dante"))
    c = cp.route(c, "M-out-dante-1", "P-out-dante-1")
    assert "ROUTE_DIRECTION_INVALID" in codes(cp.validate(c).errors)


def test_orphaned_route_after_mode_switch():
    c = cp.create_config("t", "x")
    c = cp.add_device(c, cp.create_processor("P", "P"))
    arr = create_microphone_array("A", "Array", "manual")
    arr = add_coverage_zone(arr, dynamic_zone("z1", "z1", RectShape(origin=Point2D(0, 0), width=1, height=1)))
    c = cp.add_device(c, arr)
    c = cp.route(c, "A-out-lobe-1", "P-in-dante-1")
    assert cp.validate(c).ok
    c = cp.set_coverage_mode(c, "A", "automatic")
    r = cp.validate(c)
    assert not r.ok
    orphan = next((e for e in r.errors if e.code == "ORPHANED_ROUTE"), None)
    assert orphan is not None and "A-out-lobe-1" in orphan.refs
    assert any(rt.from_port_id == "A-out-lobe-1" for rt in c.routes)  # not dropped


def test_manual_lobe_limit():
    c = cp.create_config("t", "x")
    arr = create_microphone_array("A", "Array", "manual")
    arr.zones = [dynamic_zone(f"z{i}", f"z{i}", RectShape(origin=Point2D(0, 0), width=1, height=1)) for i in range(9)]
    c = cp.add_device(c, arr)
    cds = codes(cp.validate(c).errors)
    assert "COVERAGE_ZONE_LIMIT" in cds
    assert "MANUAL_LOBE_LIMIT" in cds


def test_automixer_range():
    c = cp.create_config("t", "x")
    c = cp.add_device(c, cp.create_processor("P", "P"))
    c = cp.configure_automixer(c, "P", AutomixerConfig("P", [AutomixerChannel("P-in-dante-1", False, 2)], "medium", None))
    assert "AUTOMIXER_INVALID" in codes(cp.validate(c).errors)


def test_empty_config_valid():
    assert cp.validate(cp.create_config("t", "x")).ok
