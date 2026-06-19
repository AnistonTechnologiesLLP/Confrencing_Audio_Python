"""Hardware-free tests for the mic-input preamp software core (:mod:`conf_pipeline_control.preamp`).

Covers the track-independent manual gain stage: bit-exact no-op when off (Invariant D), float32
preservation (Invariant C — the NEP-50 upcast trap), correct manual scaling, gain clamping to the
controller range, and the inert-auto hook. The auto headroom stager and the hardware backend land on
the analog track and get their own tests then.
"""
import pytest

np = pytest.importorskip("numpy")

import conf_pipeline_control as cc
from conf_pipeline_control.control import GAIN_MAX_DB, GAIN_MIN_DB
from conf_pipeline_control.preamp import InputPreamp, _db_to_lin


def _block(scale=0.1):
    """A small deterministic (N, 8) float32 block (no RNG — keeps the test reproducible)."""
    n = 64
    t = np.arange(n)[:, None]
    ch = np.arange(8)[None, :]
    return (scale * np.sin(2 * np.pi * (t + ch) / 16.0)).astype(np.float32)


def test_default_off_is_identity_noop():
    """0 dB + auto off ⇒ process_block returns the SAME array object (byte-identical, no allocation)."""
    pre = InputPreamp()
    x = _block()
    out = pre.process_block(x)
    assert out is x                       # identity: the off preamp must not perturb the pipeline
    assert pre.gain_db == 0.0
    assert pre.auto is False


def test_explicit_zero_db_is_noop():
    pre = InputPreamp(gain_db=0.0)
    x = _block()
    assert pre.process_block(x) is x


def test_manual_gain_scales_block():
    pre = InputPreamp(gain_db=6.0)
    x = _block()
    out = pre.process_block(x)
    assert out is not x                   # gained path allocates a fresh array
    assert np.allclose(out, x * _db_to_lin(6.0), rtol=1e-5, atol=1e-7)
    # +6 dB is ~2x amplitude
    assert abs(_db_to_lin(6.0) - 1.9952623) < 1e-6


def test_float32_is_preserved_under_gain():
    """Invariant C: a float32 block stays float32 after the multiply (no NEP-50 float64 upcast)."""
    pre = InputPreamp(gain_db=3.0)
    x = _block().astype(np.float32)
    out = pre.process_block(x)
    assert out.dtype == np.float32
    # The scalar must be a plain Python float, not a numpy scalar that would upcast.
    assert type(pre._manual_lin) is float


def test_does_not_mutate_input():
    pre = InputPreamp(gain_db=6.0)
    x = _block()
    before = x.copy()
    pre.process_block(x)
    assert np.array_equal(x, before)      # the multiply allocates; indata is never mutated in place


def test_gain_clamps_to_controller_range():
    pre = InputPreamp()
    pre.set_gain_db(200.0)
    assert pre.gain_db == GAIN_MAX_DB
    pre.set_gain_db(-200.0)
    assert pre.gain_db == GAIN_MIN_DB
    # Construction clamps too.
    assert InputPreamp(gain_db=999.0).gain_db == GAIN_MAX_DB


def test_set_gain_db_updates_applied_scale():
    pre = InputPreamp()
    x = _block()
    pre.set_gain_db(-6.0)
    out = pre.process_block(x)
    assert np.allclose(out, x * _db_to_lin(-6.0), rtol=1e-5, atol=1e-7)


def test_auto_is_inert_without_a_stager():
    """`auto=True` with no stager wired ⇒ only the manual gain applies (auto is a forward-compat hook
    until the analog track injects an envelope follower)."""
    pre = InputPreamp(gain_db=0.0, auto=True)
    assert pre.auto is True
    x = _block()
    assert pre.process_block(x) is x      # 0 dB manual + inert auto ⇒ still a no-op
    pre.set_auto(False)
    assert pre.auto is False


def test_reset_is_safe_without_a_stager():
    InputPreamp(gain_db=6.0).reset()      # must not raise when no stager is wired


def test_exported_from_package_root():
    assert cc.InputPreamp is InputPreamp
    assert hasattr(cc, "HwGain")
