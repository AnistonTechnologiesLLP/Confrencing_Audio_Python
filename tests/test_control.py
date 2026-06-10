"""Controller abstraction + simulated backend (pure stdlib, no hardware)."""
import pytest

import conf_pipeline as cp
from conf_pipeline.model import Point2D, RectShape
import conf_pipeline_control as cc


def _design():
    c = cp.create_config("Room", "x")
    c = cp.set_room(c, cp.rectangular_room(8, 6, 3))
    c = cp.add_device(c, cp.create_microphone_array("A", "Array"))
    c = cp.set_device_position(c, "A", Point2D(4, 3))
    arr = cp.find_device(c, "A")
    arr.zones = [cp.CoverageZone("p1", "dynamic", RectShape(Point2D(6.5, 2.5), 1, 1), False, "Talk")]
    geom = cc.sensibel_8(0.05)
    return cc.design_zone_beams(c, "A", geom, freq_hz=2000.0), geom


def test_connect_disconnect_and_state():
    geom = cc.sensibel_8(0.05)
    ctl = cc.SimulatedMicController(geom)
    assert not ctl.connected
    assert ctl.read_level() == 0.0  # disconnected → no level
    ctl.connect()
    assert ctl.connected
    assert ctl.read_level() > 0.0
    st = ctl.state()
    assert st.backend == "simulated" and st.n_channels == 8 and st.connected
    ctl.disconnect()
    assert not ctl.connected and ctl.read_level() == 0.0


def test_mute_zeros_level():
    ctl = cc.SimulatedMicController(cc.sensibel_8(0.05))
    ctl.connect()
    assert ctl.read_level() > 0.0
    ctl.set_mute(True)
    assert ctl.muted and ctl.read_level() == 0.0
    assert ctl.toggle_mute() is False  # unmuted
    assert ctl.read_level() > 0.0


def test_gain_scales_and_clamps():
    ctl = cc.SimulatedMicController(cc.sensibel_8(0.05))
    ctl.connect()
    ctl.set_gain_db(100.0)        # clamps to the +24 dB max
    assert ctl.gain_db == 24.0
    peak = max(ctl.read_level() for _ in range(50))  # loud part of the envelope pegs at 1.0
    assert peak == pytest.approx(1.0)
    ctl.set_gain_db(-200.0)       # clamps to the -60 dB floor; near-silent
    assert ctl.gain_db == -60.0
    assert max(ctl.read_level() for _ in range(50)) < 0.01


def test_apply_design_records_zone_count():
    design, geom = _design()
    ctl = cc.SimulatedMicController(geom)
    ctl.connect()
    ctl.apply_design(design)
    assert ctl.state().design_zones == 1


def test_apply_design_channel_mismatch_raises():
    design, _ = _design()
    ctl = cc.SimulatedMicController(cc.circular_array(6, 0.05))  # 6 ≠ 8
    with pytest.raises(ValueError):
        ctl.apply_design(design)


def test_context_manager_connects():
    with cc.SimulatedMicController(cc.sensibel_8(0.05)) as ctl:
        assert ctl.connected
    assert not ctl.connected


def test_controls_available_is_bool():
    assert isinstance(cc.controls_available(), bool)
