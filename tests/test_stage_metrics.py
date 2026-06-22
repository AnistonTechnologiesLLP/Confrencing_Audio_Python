"""Per-stage activity metrics — honest by construction.

These guard the metric *definitions* (not just that a number moves): a working denoiser
on speech must NOT read ~0 (broadband-RMS bug), AGC is bipolar gain not "reduction", and an
enabled-but-idle stage stays distinguishable from a disabled one.
"""
from __future__ import annotations

import numpy as np
import pytest

from conf_pipeline_control._stage_metrics import (
    StageActivity,
    StageMeter,
    ZERO_ACTIVITY,
    loudness_matched_raw,
)


def test_off_stages_report_zero_and_off() -> None:
    m = StageMeter(44100.0)
    a = m.update(
        aec_on=False, aec_erle_db=0.0, aec_farend=False,
        dereverb_on=False, dereverb_in_rms=0.0, dereverb_out_rms=0.0,
        denoise_on=False, denoise_in_rms=0.0, denoise_out_rms=0.0,
        agc_on=False, agc_gain_lin=1.0,
    )
    assert a == StageActivity(
        aec_erle_db=0.0, aec_on=False, aec_farend_active=False,
        dereverb_db=0.0, dereverb_on=False,
        denoise_db=0.0, denoise_on=False,
        agc_gain_db=0.0, agc_on=False,
    )
    assert ZERO_ACTIVITY.aec_on is False and ZERO_ACTIVITY.denoise_db == 0.0


def test_agc_gain_is_bipolar() -> None:
    m = StageMeter(44100.0)
    boost = m.update(
        aec_on=False, aec_erle_db=0.0, aec_farend=False,
        dereverb_on=False, dereverb_in_rms=0.0, dereverb_out_rms=0.0,
        denoise_on=False, denoise_in_rms=0.0, denoise_out_rms=0.0,
        agc_on=True, agc_gain_lin=2.0,
    )
    cut = m.update(
        aec_on=False, aec_erle_db=0.0, aec_farend=False,
        dereverb_on=False, dereverb_in_rms=0.0, dereverb_out_rms=0.0,
        denoise_on=False, denoise_in_rms=0.0, denoise_out_rms=0.0,
        agc_on=True, agc_gain_lin=0.5,
    )
    assert boost.agc_on is True
    assert boost.agc_gain_db == pytest.approx(6.0206, abs=1e-3)   # +6 dB boost
    assert cut.agc_gain_db == pytest.approx(-6.0206, abs=1e-3)    # -6 dB cut (bipolar, not "reduction")


def test_agc_gain_none_or_zero_reads_zero_db() -> None:
    m = StageMeter(44100.0)
    a = m.update(
        aec_on=False, aec_erle_db=0.0, aec_farend=False,
        dereverb_on=False, dereverb_in_rms=0.0, dereverb_out_rms=0.0,
        denoise_on=False, denoise_in_rms=0.0, denoise_out_rms=0.0,
        agc_on=True, agc_gain_lin=None,   # tracker not yet primed
    )
    assert a.agc_on is True and a.agc_gain_db == 0.0


def test_aec_idle_vs_active() -> None:
    m = StageMeter(44100.0)
    idle = m.update(
        aec_on=True, aec_erle_db=0.0, aec_farend=False,
        dereverb_on=False, dereverb_in_rms=0.0, dereverb_out_rms=0.0,
        denoise_on=False, denoise_in_rms=0.0, denoise_out_rms=0.0,
        agc_on=False, agc_gain_lin=1.0,
    )
    active = m.update(
        aec_on=True, aec_erle_db=12.0, aec_farend=True,
        dereverb_on=False, dereverb_in_rms=0.0, dereverb_out_rms=0.0,
        denoise_on=False, denoise_in_rms=0.0, denoise_out_rms=0.0,
        agc_on=False, agc_gain_lin=1.0,
    )
    assert idle.aec_on is True and idle.aec_farend_active is False
    assert active.aec_farend_active is True
    assert active.aec_erle_db == pytest.approx(12.0)


def test_dereverb_attenuation_positive_then_dry_zero() -> None:
    m = StageMeter(44100.0)
    wet = m.update(
        aec_on=False, aec_erle_db=0.0, aec_farend=False,
        dereverb_on=True, dereverb_in_rms=1.0, dereverb_out_rms=0.5,
        denoise_on=False, denoise_in_rms=0.0, denoise_out_rms=0.0,
        agc_on=False, agc_gain_lin=1.0,
    )
    dry = m.update(
        aec_on=False, aec_erle_db=0.0, aec_farend=False,
        dereverb_on=True, dereverb_in_rms=0.4, dereverb_out_rms=0.4,
        denoise_on=False, denoise_in_rms=0.0, denoise_out_rms=0.0,
        agc_on=False, agc_gain_lin=1.0,
    )
    assert wet.dereverb_on is True
    assert wet.dereverb_db == pytest.approx(6.0206, abs=1e-3)   # halved RMS = 6 dB attenuation
    assert dry.dereverb_db == pytest.approx(0.0, abs=1e-6)      # nothing to dereverb, still "on"


def test_denoise_reports_bed_reduction_not_broadband() -> None:
    """The key honest-metric gate: on speech-plus-noise the denoiser lowers the NOISE BED.
    A broadband-RMS implementation reads ~0 here (speech dominates) and fails this test."""
    m = StageMeter(44100.0)
    a = None
    # Speech (loud) interleaved with gaps; in the gaps the denoiser drops the floor 0.02 -> 0.004.
    for _ in range(8):
        m.update(  # speech frame: in≈out (level-preserving cleaner keeps voice)
            aec_on=False, aec_erle_db=0.0, aec_farend=False,
            dereverb_on=False, dereverb_in_rms=0.0, dereverb_out_rms=0.0,
            denoise_on=True, denoise_in_rms=0.30, denoise_out_rms=0.29,
            agc_on=False, agc_gain_lin=1.0,
        )
        a = m.update(  # gap frame: noise bed cut hard
            aec_on=False, aec_erle_db=0.0, aec_farend=False,
            dereverb_on=False, dereverb_in_rms=0.0, dereverb_out_rms=0.0,
            denoise_on=True, denoise_in_rms=0.020, denoise_out_rms=0.004,
            agc_on=False, agc_gain_lin=1.0,
        )
    assert a is not None
    assert a.denoise_on is True
    assert a.denoise_db == pytest.approx(20.0 * np.log10(0.020 / 0.004), abs=0.5)  # ~14 dB bed drop
    assert a.denoise_db > 6.0


def test_denoise_idle_zero_when_floor_unchanged() -> None:
    m = StageMeter(44100.0)
    a = None
    for _ in range(12):  # denoiser engaged but input is already clean: in == out every block
        a = m.update(
            aec_on=False, aec_erle_db=0.0, aec_farend=False,
            dereverb_on=False, dereverb_in_rms=0.0, dereverb_out_rms=0.0,
            denoise_on=True, denoise_in_rms=0.05, denoise_out_rms=0.05,
            agc_on=False, agc_gain_lin=1.0,
        )
    assert a is not None
    assert a.denoise_on is True               # ON, not greyed...
    assert a.denoise_db == pytest.approx(0.0, abs=1e-6)   # ...but honestly ~0 (nothing to remove)


def test_reset_clears_floor_history() -> None:
    m = StageMeter(44100.0)
    for _ in range(8):  # build a low output bed so the floor remembers a big reduction
        m.update(
            aec_on=False, aec_erle_db=0.0, aec_farend=False,
            dereverb_on=False, dereverb_in_rms=0.0, dereverb_out_rms=0.0,
            denoise_on=True, denoise_in_rms=0.02, denoise_out_rms=0.002,
            agc_on=False, agc_gain_lin=1.0,
        )
    m.reset()
    a = m.update(  # fresh history: a single equal-in/out block reads ~0, not the stale reduction
        aec_on=False, aec_erle_db=0.0, aec_farend=False,
        dereverb_on=False, dereverb_in_rms=0.0, dereverb_out_rms=0.0,
        denoise_on=True, denoise_in_rms=0.05, denoise_out_rms=0.05,
        agc_on=False, agc_gain_lin=1.0,
    )
    assert a.denoise_db == pytest.approx(0.0, abs=1e-6)


def test_loudness_matched_raw_scales_and_is_float32() -> None:
    pre = np.full(64, 0.1, dtype=np.float32)
    out = loudness_matched_raw(pre, 2.0)
    assert out.dtype == np.float32
    assert np.allclose(out, 0.2)
    # gain 1.0 (no AGC) is a pure pass-through value-wise
    assert np.allclose(loudness_matched_raw(pre, 1.0), pre)
