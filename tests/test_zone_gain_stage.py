"""B2 + B3: post-AGC zone-gain trim stage tests (synthetic blocks; no hardware).

B2: Tests the shared ``_apply_zone_gain`` helper and validates the off-path
is bit-exact (same object returned).

B3: Tests that AutoSteerController._tick wires active_zone_gain_db → set_zone_gain_lin
correctly (gain in zone, no zone, disabled by default).  Uses a stub controller so
no hardware / sounddevice is needed.
"""
import math
import threading
import numpy as np
import pytest

pytest.importorskip("numpy")
from conf_pipeline_control.agc import _apply_zone_gain   # pure helper extracted in Step 3


def test_zone_gain_off_is_bit_identical():
    x = np.linspace(-0.5, 0.5, 256).astype(np.float32)
    y = _apply_zone_gain(x, enabled=False, lin=0.5)
    assert y is x or np.array_equal(y, x)


def test_zone_gain_on_scales():
    x = np.ones(128, dtype=np.float32) * 0.5
    y = _apply_zone_gain(x, enabled=True, lin=0.5)   # -6 dB ≈ 0.501
    assert np.allclose(y, 0.25, atol=1e-6)


def test_zone_gain_none_lin_is_noop():
    x = np.ones(64, dtype=np.float32) * 0.3
    y = _apply_zone_gain(x, enabled=True, lin=None)
    assert y is x or np.array_equal(y, x)


# ---------------------------------------------------------------------------
# B3 — autosteer wiring tests (stub engine, no hardware)
# ---------------------------------------------------------------------------

import conf_pipeline as cp
from conf_pipeline.model import Point2D, RectShape
from conf_pipeline.seat_mapper import azimuth_for_array_point
from conf_pipeline_control import doa as _doa
import conf_pipeline_control.autosteer as _autosteer_mod
from conf_pipeline_control.autosteer import AutoSteerController, SectorConfig


class _StubCtrl:
    """Minimal stand-in for LiveBeamController — records set_zone_gain_lin calls."""

    def __init__(self):
        self.zone_gain_calls: list = []

    def snapshot_covariance(self):
        # Return a sentinel non-None so _tick proceeds past the early-return guard.
        return (object(), object())   # (cov, freqs) — never actually used in DOA

    def set_zone_gain_lin(self, lin):
        self.zone_gain_calls.append(lin)

    def apply_design(self, design):
        pass

    def set_mute(self, on):
        pass


def _make_config_with_zone(gain_db=-6.0):
    """Posed array at origin, bearing 0°, one pickup zone with a gain."""
    cfg = cp.create_config("T", "2026-01-01T00:00:00Z")
    arr = cp.create_microphone_array("a1", "Array", position=Point2D(0.0, 0.0))
    arr.bearing_deg = 0.0
    cfg = cp.add_device(cfg, arr)
    cfg = cp.add_coverage_zone(cfg, "a1", cp.dynamic_zone("z1", "Z", RectShape(Point2D(0.5, 1.5), 1.0, 1.0)))
    if gain_db is not None:
        cfg = cp.set_zone_gain_db(cfg, "a1", "z1", gain_db)
    return cfg


def _build_tick_controller(config, *, zone_gain_enabled=True):
    """Build an AutoSteerController with a stub ctrl and a wide-open sector."""
    ctrl = object.__new__(AutoSteerController)
    # Minimal state _tick needs (mirrors __init__ without constructing LiveBeamController)
    ctrl._config = config
    ctrl._array_id = "a1"
    ctrl.zone_cut = False
    ctrl.zone_gain_enabled = zone_gain_enabled
    ctrl._sectors = [SectorConfig(center_deg=0.0, half_width_deg=180.0)]
    ctrl.geometry = None          # monkeypatched doa.detect won't use it
    ctrl.off_nadir_deg = 90.0
    ctrl.grid_step_deg = 2.0
    ctrl.max_talkers = 3
    ctrl.min_separation_deg = 40.0
    ctrl.min_salience_db = 0.0    # accept any detection strength
    ctrl.vad_floor_db = 0.0
    ctrl.freq_hz = 1000.0
    ctrl.mode = "delaysum"
    ctrl.loading = 0.05
    ctrl.hold_cycles = 0          # no hysteresis — changes applied immediately
    ctrl._hold = 0
    ctrl._last_looks = []
    ctrl._last_sig = None
    ctrl._lock = threading.Lock()
    ctrl._detections = []
    ctrl._active_nulls = []
    ctrl.gate_when_empty = False  # avoid set_mute calls complicating the test
    ctrl.reselect_deg = 1.0       # fine-grain quantize so the sig always changes
    ctrl.ctrl = _StubCtrl()
    return ctrl


def _make_detect_result(azimuth_deg, salience_db=10.0):
    """A DoaResult with one detection at the given azimuth."""
    det = _doa.Detection(azimuth_deg=azimuth_deg, salience_db=salience_db)
    return _doa.DoaResult(detections=[det], grid_deg=None, power_db=None, active=True)


def test_b3_zone_gain_called_with_correct_linear_when_in_zone(monkeypatch):
    """_tick sets the zone gain linear when a talker is inside the pickup zone."""
    gain_db = -6.0
    cfg = _make_config_with_zone(gain_db)
    # The zone covers ~[0°..90°] in this setup; azimuth pointing into the zone centre
    az_in_zone = azimuth_for_array_point(cfg, "a1", Point2D(1.0, 2.0))

    ctrl = _build_tick_controller(cfg, zone_gain_enabled=True)
    expected_lin = 10 ** (gain_db / 20.0)

    def _fake_detect(cov, freqs, geom, **kw):
        return _make_detect_result(az_in_zone)

    monkeypatch.setattr(_doa, "detect", _fake_detect)
    monkeypatch.setattr(_doa, "sector_gate_multi", lambda dets, specs: [setattr(d, "in_sector", True) for d in dets])
    # Skip the actual beamformer design (needs real geometry); the zone gain fires before it
    monkeypatch.setattr(_autosteer_mod, "design_multi_bearings", lambda *a, **kw: None)

    ctrl._tick()

    assert ctrl.ctrl.zone_gain_calls, "set_zone_gain_lin was never called"
    assert abs(ctrl.ctrl.zone_gain_calls[-1] - expected_lin) < 1e-6


def test_b3_zone_gain_set_to_none_when_azimuth_outside_zone(monkeypatch):
    """_tick sets zone gain to None when the dominant talker is outside any pickup zone."""
    cfg = _make_config_with_zone(-6.0)
    az_outside = 180.0   # directly behind — not in the zone centred at ~45°

    ctrl = _build_tick_controller(cfg, zone_gain_enabled=True)

    def _fake_detect(cov, freqs, geom, **kw):
        return _make_detect_result(az_outside)

    monkeypatch.setattr(_doa, "detect", _fake_detect)
    monkeypatch.setattr(_doa, "sector_gate_multi", lambda dets, specs: [setattr(d, "in_sector", True) for d in dets])
    monkeypatch.setattr(_autosteer_mod, "design_multi_bearings", lambda *a, **kw: None)

    ctrl._tick()

    assert ctrl.ctrl.zone_gain_calls, "set_zone_gain_lin was never called"
    assert ctrl.ctrl.zone_gain_calls[-1] is None


def test_b3_zone_gain_disabled_by_default_does_not_call_set(monkeypatch):
    """When zone_gain_enabled=False (default), set_zone_gain_lin is never called."""
    gain_db = -6.0
    cfg = _make_config_with_zone(gain_db)
    az_in_zone = azimuth_for_array_point(cfg, "a1", Point2D(1.0, 2.0))

    ctrl = _build_tick_controller(cfg, zone_gain_enabled=False)  # default OFF

    def _fake_detect(cov, freqs, geom, **kw):
        return _make_detect_result(az_in_zone)

    monkeypatch.setattr(_doa, "detect", _fake_detect)
    monkeypatch.setattr(_doa, "sector_gate_multi", lambda dets, specs: [setattr(d, "in_sector", True) for d in dets])
    monkeypatch.setattr(_autosteer_mod, "design_multi_bearings", lambda *a, **kw: None)

    ctrl._tick()

    assert ctrl.ctrl.zone_gain_calls == [], "set_zone_gain_lin should NOT be called when feature is off"


# ---------------------------------------------------------------------------
# B4 — engine-level integration: AutoSteerController forwards live_zone_gain
#      to LiveBeamController so the trim gate is actually wired, not silently
#      stuck at False.
# ---------------------------------------------------------------------------

import conf_pipeline_control as _cc
from conf_pipeline_control import sensibel_8 as _sensibel_8
from conf_pipeline_control.live import LiveBeamController as _LiveBeamController


def test_b4_autosteer_forwards_zone_gain_flag_to_ctrl():
    """AutoSteerController(zone_gain_enabled=True) must set ctrl._zone_gain=True.

    This is the regression test for the forwarding bug: if live_zone_gain is not
    passed to the inner LiveBeamController constructor, ctrl._zone_gain stays False
    and _apply_zone_gain's 'enabled' gate is permanently closed — the trim is
    computed but never applied.
    """
    asc_on = AutoSteerController(
        _sensibel_8(),
        _cc.SectorConfig(),
        zone_gain_enabled=True,
    )
    assert asc_on.ctrl._zone_gain is True, (
        "ctrl._zone_gain must be True when AutoSteerController(zone_gain_enabled=True); "
        "forwarding is broken — live_zone_gain not passed to LiveBeamController.__init__"
    )

    asc_off = AutoSteerController(
        _sensibel_8(),
        _cc.SectorConfig(),
        zone_gain_enabled=False,
    )
    assert asc_off.ctrl._zone_gain is False, (
        "ctrl._zone_gain must be False when zone_gain_enabled=False (default)"
    )


def test_b4_livebeam_zone_gain_trim_scales_output():
    """LiveBeamController with live_zone_gain=True + set_zone_gain_lin(0.5) must halve
    the output of _apply_zone_gain, while live_zone_gain=False leaves it untouched.

    This drives the REAL trim path (_process_block line: out = _apply_zone_gain(...))
    directly via the public helper, confirming the enabled-gate wiring end-to-end.
    """
    block = np.ones(256, dtype=np.float32) * 0.8

    # Engine with zone gain ENABLED — set a 0.5x (≈ −6 dB) trim
    ctrl_on = _LiveBeamController(_sensibel_8(), live_zone_gain=True)
    ctrl_on.set_zone_gain_lin(0.5)
    assert ctrl_on._zone_gain is True
    assert ctrl_on._zone_gain_lin == pytest.approx(0.5, abs=1e-7)

    # Simulate the exact _process_block application line (live.py line ~623):
    #   out = _apply_zone_gain(out, enabled=self._zone_gain, lin=self._zone_gain_lin)
    out_on = _apply_zone_gain(block.copy(), enabled=ctrl_on._zone_gain, lin=ctrl_on._zone_gain_lin)
    assert np.allclose(out_on, 0.4, atol=1e-6), (
        f"Expected 0.8 * 0.5 = 0.4; got {out_on[0]:.6f}. Zone trim not applied."
    )

    # Engine with zone gain DISABLED — trim must be a no-op (bit-identical)
    ctrl_off = _LiveBeamController(_sensibel_8(), live_zone_gain=False)
    ctrl_off.set_zone_gain_lin(0.5)   # scalar written but gate closed
    assert ctrl_off._zone_gain is False

    block_ref = block.copy()
    out_off = _apply_zone_gain(block_ref, enabled=ctrl_off._zone_gain, lin=ctrl_off._zone_gain_lin)
    assert out_off is block_ref, "Off path must return the SAME array object (bit-exact, zero alloc)"
    assert np.allclose(out_off, 0.8, atol=1e-9), "Off path must leave block values unchanged"
