import pytest

from conf_pipeline import matrix as mx
from conf_pipeline.model import Crosspoint, Port


def port(pid, kind):
    return Port(id=pid, device_id="proc", kind=kind, transport="dante", label=pid)


INPUTS = [port("in1", "input"), port("in2", "input")]
OUTPUTS = [port("out1", "output"), port("out2", "output")]


def test_builds_buses_from_ports():
    m = mx.create_matrix("proc", INPUTS, OUTPUTS)
    assert [b.id for b in m.input_buses] == ["in1", "in2"]
    assert [b.id for b in m.output_buses] == ["out1", "out2"]
    assert mx.active_crosspoints(m) == []


def test_route_and_read_back():
    m = mx.route(mx.create_matrix("proc", INPUTS, OUTPUTS), "in1", "out1", -6)
    cp = mx.get(m, "in1", "out1")
    assert cp == Crosspoint(enabled=True, gain_db=-6)
    assert mx.is_active(m, "in1", "out1")
    assert not mx.is_active(m, "in2", "out1")


def test_immutable():
    m0 = mx.create_matrix("proc", INPUTS, OUTPUTS)
    m1 = mx.route(m0, "in1", "out1")
    assert m0 is not m1
    assert mx.active_crosspoints(m0) == []
    assert len(mx.active_crosspoints(m1)) == 1


def test_disabled_not_active():
    m = mx.set_crosspoint(mx.create_matrix("proc", INPUTS, OUTPUTS), "in1", "out1", Crosspoint(False, 0))
    assert not mx.is_active(m, "in1", "out1")


def test_clear_prunes_rows():
    m = mx.route(mx.create_matrix("proc", INPUTS, OUTPUTS), "in1", "out1")
    m = mx.clear(m, "in1", "out1")
    assert mx.get(m, "in1", "out1") is None
    assert "in1" not in m.cells


def test_queries():
    m = mx.create_matrix("proc", INPUTS, OUTPUTS)
    m = mx.route(m, "in1", "out1")
    m = mx.route(m, "in2", "out1")
    m = mx.route(m, "in1", "out2")
    assert sorted(mx.inputs_for_output(m, "out1")) == ["in1", "in2"]
    assert sorted(mx.outputs_for_input(m, "in1")) == ["out1", "out2"]


def test_unknown_buses_raise():
    m = mx.create_matrix("proc", INPUTS, OUTPUTS)
    with pytest.raises(ValueError, match="input bus"):
        mx.route(m, "nope", "out1")
    with pytest.raises(ValueError, match="output bus"):
        mx.route(m, "in1", "nope")
