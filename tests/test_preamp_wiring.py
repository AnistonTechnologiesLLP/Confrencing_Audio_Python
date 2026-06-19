"""The mic-input preamp is wired into every live capture path (track-independent software path).

Confirms each backend (a) builds no preamp by default (off ⇒ _preamp is None ⇒ byte-identical), (b)
applies a uniform input gain at the FRONT of its process method so the beamformed output scales with
the manual gain, and (c) exposes realtime-safe set_preamp_* reachable via the GUI's _active_ctl
surface — including the BeamEngine fan-out to both back-ends and the MultiKit per-kit fan-out.
"""
from types import SimpleNamespace

import pytest

np = pytest.importorskip("numpy")

import conf_pipeline_control as cc
from conf_pipeline_control.multikit import _default_engine_factory
from conf_pipeline_control.preamp import _db_to_lin

LIN6 = _db_to_lin(6.0)


def _block(blocksize, scale=0.1):
    t = np.arange(blocksize)[:, None]
    ch = np.arange(8)[None, :]
    return (scale * np.sin(2 * np.pi * (t + ch) / 16.0)).astype(np.float32)


# --------------------------------------------------------------------------- #
# PolarisBeamformer (steered)
# --------------------------------------------------------------------------- #
def test_polaris_default_preamp_is_off():
    bf = cc.PolarisBeamformer(device=None, mode="fracdelay")
    assert bf._preamp is None                       # off ⇒ no-op insert ⇒ pipeline byte-identical


def test_polaris_preamp_scales_the_beamformed_output():
    """A linear (fixed-weight) fracdelay beam: +6 dB input ⇒ output scaled by ~2x (the insert is at
    the FRONT of process_block, so it reaches both the beam and the covariance)."""
    def beam(gain_db):
        b = cc.PolarisBeamformer(device=None, mode="fracdelay", preamp_gain_db=gain_db)
        b._setup_runtime()
        return b
    b0, b6 = beam(0.0), beam(6.0)
    assert b0._preamp is None and b6._preamp is not None
    x = _block(b0.blocksize)
    out0 = b0.process_block(x)
    out6 = b6.process_block(x)
    assert out6.dtype == out0.dtype
    assert np.allclose(out6, out0 * LIN6, rtol=1e-4, atol=1e-6)


def test_polaris_setter_lazy_creates_and_auto_toggles():
    bf = cc.PolarisBeamformer(device=None, mode="fracdelay")
    assert bf._preamp is None
    bf.set_preamp_gain_db(0.0)                       # no-op set stays off (no allocation)
    assert bf._preamp is None
    bf.set_preamp_gain_db(6.0)                       # lazily creates
    assert bf._preamp is not None and bf._preamp.gain_db == 6.0
    bf.set_preamp_auto(True)
    assert bf._preamp.auto is True


# --------------------------------------------------------------------------- #
# VirtualMicGrid (grid)
# --------------------------------------------------------------------------- #
def test_virtual_mic_grid_builds_and_applies_preamp():
    g_off = cc.VirtualMicGrid(device=None)
    assert g_off._preamp is None
    g_on = cc.VirtualMicGrid(device=None, preamp_gain_db=6.0)
    assert g_on._preamp is not None and g_on._preamp.gain_db == 6.0
    # The mixin insert scales the raw block uniformly (the grid path then consumes the gained block).
    x = _block(64)
    assert np.allclose(g_on._apply_preamp(x), x * LIN6, rtol=1e-5, atol=1e-7)
    assert g_off._apply_preamp(x) is x


# --------------------------------------------------------------------------- #
# BeamEngine (A/B) — one preamp shared by both back-ends
# --------------------------------------------------------------------------- #
def test_beam_engine_forwards_preamp_to_both_backends():
    eng = cc.BeamEngine(device=None, mode="steered", preamp_gain_db=6.0)
    assert eng._steered._preamp is not None and eng._steered._preamp.gain_db == 6.0
    assert eng._grid._preamp is not None and eng._grid._preamp.gain_db == 6.0


def test_beam_engine_default_off_and_fanout_setters():
    eng = cc.BeamEngine(device=None, mode="steered")
    assert eng._steered._preamp is None and eng._grid._preamp is None
    eng.set_preamp_gain_db(3.0)                      # fan-out lazily creates on BOTH
    assert eng._steered._preamp.gain_db == 3.0 and eng._grid._preamp.gain_db == 3.0
    eng.set_preamp_auto(True)
    assert eng._steered._preamp.auto and eng._grid._preamp.auto


# --------------------------------------------------------------------------- #
# MultiKit — per-kit input staging (NOT the single combined-output AGC)
# --------------------------------------------------------------------------- #
def test_multikit_factory_keeps_preamp_in_kit_cfg():
    """_default_engine_factory strips agc_target_db (Invariant B) but must keep preamp_* — the preamp
    is per-kit INPUT staging, distinct from the one combined-output AGC."""
    ctrl = SimpleNamespace(sample_rate=44100.0, blocksize=1411)
    spec = cc.KitSpec(device=None, cfg={"mode": "fracdelay", "preamp_gain_db": 6.0,
                                        "agc_target_db": -20.0})
    eng = _default_engine_factory(spec, lambda _x: None, ctrl)
    assert eng._preamp is not None and eng._preamp.gain_db == 6.0
    assert eng._agc is None                          # agc_target_db WAS stripped (Invariant B intact)


def test_multikit_set_preamp_forwards_to_engines():
    class _FakeEngine:
        def __init__(self):
            self.preamp_gain = None
            self.preamp_auto = None

        def set_preamp_gain_db(self, db):
            self.preamp_gain = db

        def set_preamp_auto(self, on):
            self.preamp_auto = on

    ctrl = cc.MultiKitController(kits=[cc.KitSpec(device=None), cc.KitSpec(device=None)])
    fakes = [_FakeEngine(), _FakeEngine()]
    ctrl._engines = list(fakes)
    ctrl.set_preamp_gain_db(6.0)                     # all kits
    assert all(f.preamp_gain == 6.0 for f in fakes)
    ctrl.set_preamp_auto(True, kit=1)                # one kit
    assert fakes[1].preamp_auto is True and fakes[0].preamp_auto is None


# --------------------------------------------------------------------------- #
# AutoSteer — forwards to its wrapped LiveBeamController (reached via .ctrl)
# --------------------------------------------------------------------------- #
def test_autosteer_forwards_preamp_to_inner_controller():
    geom = cc.sensibel_8(radius_m=0.040)
    ctl = cc.AutoSteerController(geom, cc.SectorConfig(), preamp_gain_db=6.0)
    assert ctl.ctrl._preamp is not None and ctl.ctrl._preamp.gain_db == 6.0
    ctl.ctrl.set_preamp_auto(True)
    assert ctl.ctrl._preamp.auto is True
