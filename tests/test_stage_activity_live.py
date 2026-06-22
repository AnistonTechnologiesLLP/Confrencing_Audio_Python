"""Per-stage meters + raw/processed bypass on the zone/auto-steer path (LiveBeamController).

Mirrors test_stage_activity.py (the steered PolarisBeamformer path) for the SECOND live DSP
implementation, via the same shared StageMeter helper. The zone path has no AGC, so agc is always off.
"""
import pytest

np = pytest.importorskip("numpy")

from conf_pipeline_control._stage_metrics import ZERO_ACTIVITY
from conf_pipeline_control.autosteer import AutoSteerController
from conf_pipeline_control.live import LiveBeamController, _FRAME, _HOP
from conf_pipeline_control.polaris_beamformer import PolarisBeamformer
import conf_pipeline_control as cc


def _geom():
    return PolarisBeamformer(device=None).geometry


def _wire(bf):
    bf._np = np
    bf._win = np.hanning(_FRAME).astype(float)
    bf._inbuf = np.zeros((_FRAME, bf.n_channels), dtype=float)
    bf._ola = np.zeros(_FRAME, dtype=float)
    bf._weights = None                # passthrough beam (averaging) — beam math isn't under test
    bf._build_post_nr()
    return bf


def _blocks(bf, n, *, scale=0.05, seed=0):
    rng = np.random.default_rng(seed)
    return [rng.standard_normal((_HOP, bf.n_channels)).astype(float) * scale for _ in range(n)]


def test_live_stage_activity_reflects_enabled_stages():
    bf = _wire(LiveBeamController(_geom(), post_nr=True, dereverb=True, aec=True))
    for b in _blocks(bf, 4):
        bf._process_block(b)
    a = bf.stage_activity
    assert a.aec_on and a.dereverb_on and a.denoise_on
    assert a.agc_on is False           # no AGC on the zone/auto-steer path


def test_live_all_off_leaves_zero_activity():
    bf = _wire(LiveBeamController(_geom()))   # no cleaners
    for b in _blocks(bf, 4):
        bf._process_block(b)
    assert bf.stage_activity is ZERO_ACTIVITY


def test_live_bypass_emits_raw_beam():
    none = _wire(LiveBeamController(_geom()))                 # beam only
    byp = _wire(LiveBeamController(_geom(), post_nr=True, post_nr_warmup_frames=4))   # cleaner on, bypassed
    byp.set_bypass(True)
    proc = _wire(LiveBeamController(_geom(), post_nr=True, post_nr_warmup_frames=4))  # cleaner on, processed

    blocks = _blocks(none, 40, seed=5)                        # enough HOPs for the gate to engage
    out_none = [none._process_block(b.copy()) for b in blocks]
    out_byp = [byp._process_block(b.copy()) for b in blocks]
    out_proc = [proc._process_block(b.copy()) for b in blocks]

    for a, b in zip(out_none, out_byp):
        assert np.allclose(a, b, atol=1e-6)                  # bypass == the raw (no-cleaner) beam
    diff = max(float(np.max(np.abs(a - b))) for a, b in zip(out_none, out_proc))
    assert diff > 1e-5                                       # the processed output really differs


def test_live_set_bypass_and_property_defaults():
    bf = _wire(LiveBeamController(_geom(), post_nr=True))
    assert bf.stage_activity is ZERO_ACTIVITY    # before any block
    bf.set_bypass(True)
    assert bf._bypass_cleaning is True


def test_autosteer_forwards_stage_activity_and_bypass():
    sector = cc.SectorConfig(center_deg=0.0, half_width_deg=60.0)
    a = AutoSteerController(_geom(), sector, samplerate=44100.0, post_nr=True)
    assert a.stage_activity is ZERO_ACTIVITY      # delegates to the wrapped LiveBeamController
    a.set_bypass(True)
    assert a.ctrl._bypass_cleaning is True
