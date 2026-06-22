"""Engine integration of the per-stage meters + raw/processed bypass (PolarisBeamformer).

Wiring-level guards (the metric math itself is covered in test_stage_metrics.py): the snapshot
reflects which stages are on, the all-off path stays byte-identical (no metrics computed), the
bypass emits the loudness-matched raw beam, the chain keeps running while bypassed, and
reset_transient clears the snapshot while leaving the bypass choice alone.
"""
import pytest

np = pytest.importorskip("numpy")

from conf_pipeline_control._stage_metrics import ZERO_ACTIVITY
from conf_pipeline_control.polaris_beamformer import PolarisBeamformer


def _blocks(bf, n_blocks, *, scale=0.05, seed=0):
    rng = np.random.default_rng(seed)
    return [rng.standard_normal((bf.blocksize, bf.n_channels)).astype(float) * scale
            for _ in range(n_blocks)]


def _drive(bf, blocks):
    outs = [bf.process_block(b) for b in blocks]
    return outs


def test_stage_activity_reflects_enabled_stages():
    bf = PolarisBeamformer(device=None, aec=True, dereverb=True, post_nr=True, agc_target_db=-20.0)
    bf._setup_runtime()
    _drive(bf, _blocks(bf, 3))
    a = bf.stage_activity
    assert a.aec_on and a.dereverb_on and a.denoise_on and a.agc_on


def test_all_off_path_leaves_zero_activity_untouched():
    """No cleaning stages → the metric block is skipped entirely; the published snapshot is the
    exact preallocated ZERO_ACTIVITY (the byte-identity off-path guard)."""
    bf = PolarisBeamformer(device=None)            # no aec/dereverb/post_nr/agc
    bf._setup_runtime()
    _drive(bf, _blocks(bf, 3))
    assert bf.stage_activity is ZERO_ACTIVITY


def test_agc_gain_db_is_bipolar_in_engine():
    # quiet vs target but ABOVE the AGC silence floor (so it boosts rather than holding the floor)
    quiet = PolarisBeamformer(device=None, agc_target_db=-12.0)
    quiet._setup_runtime()
    _drive(quiet, _blocks(quiet, 12, scale=0.05))   # ~-35 dBFS beam → AGC boosts toward -12
    assert quiet.stage_activity.agc_on
    assert quiet.stage_activity.agc_gain_db > 0.0

    loud = PolarisBeamformer(device=None, agc_target_db=-40.0)
    loud._setup_runtime()
    _drive(loud, _blocks(loud, 12, scale=0.5))      # loud vs a low target → AGC cuts
    assert loud.stage_activity.agc_gain_db < 0.0


def test_bypass_emits_loudness_matched_raw_beam():
    blocks = None
    none = PolarisBeamformer(device=None)                       # beam only (+ band-limit), no cleaner
    none._setup_runtime()
    proc = PolarisBeamformer(device=None, post_nr=True)         # cleaner ON, processed
    proc._setup_runtime()
    byp = PolarisBeamformer(device=None, post_nr=True)          # cleaner ON but bypassed (AGC off → gain 1)
    byp._setup_runtime()
    byp.set_bypass(True)

    blocks = _blocks(none, 6, seed=7)
    out_none = _drive(none, [b.copy() for b in blocks])
    out_proc = _drive(proc, [b.copy() for b in blocks])
    out_byp = _drive(byp, [b.copy() for b in blocks])

    # Bypass emits the raw beam (band-limited, AGC gain 1) == the no-cleaner output.
    for a, b in zip(out_none, out_byp):
        assert np.allclose(a, b, atol=1e-5)
    # The processed (cleaner active) output genuinely differs from the raw beam.
    diff = max(float(np.max(np.abs(a - b))) for a, b in zip(out_none, out_proc))
    assert diff > 1e-4


def test_chain_runs_and_meters_update_while_bypassed():
    bf = PolarisBeamformer(device=None, post_nr=True, agc_target_db=-20.0)
    bf._setup_runtime()
    bf.set_bypass(True)
    _drive(bf, _blocks(bf, 4))
    a = bf.stage_activity
    assert a.denoise_on and a.agc_on        # chain still ran → meters populated even while monitoring raw


def test_toggle_bypass_midstream_does_not_raise():
    bf = PolarisBeamformer(device=None, post_nr=True)
    bf._setup_runtime()
    blocks = _blocks(bf, 6)
    bf.process_block(blocks[0])
    bf.set_bypass(True)
    bf.process_block(blocks[1])
    bf.set_bypass(False)
    bf.process_block(blocks[2])             # no exception, no reset required


def test_reset_transient_clears_snapshot_keeps_bypass():
    bf = PolarisBeamformer(device=None, post_nr=True)
    bf._setup_runtime()
    bf.set_bypass(True)
    _drive(bf, _blocks(bf, 3))
    assert bf.stage_activity is not ZERO_ACTIVITY
    bf.reset_transient()
    assert bf.stage_activity is ZERO_ACTIVITY     # snapshot wiped...
    assert bf._bypass_cleaning is True            # ...but the monitoring choice is preserved
