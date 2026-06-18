"""Tests for the live A/B proof & measurement tool (raw-beam vs cleaned).

Covers the metrics (broadband + noise-bed dB reduction, ERLE in the report), the
bounded armed capture (`done` at the sample cap; no growth after), and the WAV +
numbers export. numpy is required and skipped if absent.
"""
import os

import pytest

np = pytest.importorskip("numpy")

from conf_pipeline_control.ab_capture import ABCapture, ABProofResult, _quiet_rms_db, _rms_db, write_ab_proof


def test_rms_db_empty_and_value():
    assert _rms_db(np.zeros(0, dtype=np.float32)) == -120.0
    x = np.full(200, 0.5, dtype=np.float32)
    assert abs(_rms_db(x) - 20.0 * np.log10(0.5)) < 0.1


def test_quiet_rms_db_tracks_the_background_floor():
    rng = np.random.default_rng(0)
    bed = (0.01 * rng.standard_normal(16000)).astype(np.float32)     # quiet bed everywhere
    speech = bed.copy()
    speech[4000:6000] += (0.5 * rng.standard_normal(2000)).astype(np.float32)  # a loud burst
    # the noise-bed (p20 of short-window RMS) ignores the burst → both read ~the bed level
    assert abs(_quiet_rms_db(bed, 16000.0) - _quiet_rms_db(speech, 16000.0)) < 3.0


def test_capture_done_at_cap_and_measures_reduction():
    cap = ABCapture(8000.0, seconds=0.5)                            # cap = 4000 samples
    rng = np.random.default_rng(1)
    for _ in range(5):
        raw = (0.1 * rng.standard_normal(1000)).astype(np.float32)
        cap.feed(raw, raw * 0.1)                                    # cleaned is 20 dB quieter
    assert cap.done
    res = cap.finalize(erle_db=0.0, stages="AI cleaner")
    assert isinstance(res, ABProofResult)
    assert res.raw.shape == res.clean.shape and res.raw.shape[0] >= 4000
    assert 15.0 < res.rms_reduction_db < 25.0
    assert 12.0 < res.bed_reduction_db < 28.0
    assert "AI cleaner" in res.summary() and "background" in res.headline()


def test_capture_is_bounded_after_done():
    cap = ABCapture(8000.0, seconds=0.5)
    rng = np.random.default_rng(2)
    for _ in range(40):                                             # keep feeding well past the cap
        cap.feed(rng.standard_normal(1000).astype(np.float32), rng.standard_normal(1000).astype(np.float32))
    res = cap.finalize()
    assert res.raw.shape[0] <= 4000 + 1000                         # stops at the cap (+ at most one block)


def test_erle_only_in_summary_when_nonzero():
    cap = ABCapture(8000.0, seconds=0.5)
    rng = np.random.default_rng(3)
    for _ in range(5):
        r = (0.1 * rng.standard_normal(1000)).astype(np.float32)
        cap.feed(r, r)
    assert "ERLE" not in cap.finalize(erle_db=0.0).summary()
    assert "ERLE" in cap.finalize(erle_db=8.0).summary()


def test_write_ab_proof(tmp_path):
    cap = ABCapture(8000.0, seconds=0.5)
    rng = np.random.default_rng(4)
    for _ in range(5):
        r = (0.1 * rng.standard_normal(1000)).astype(np.float32)
        cap.feed(r, r * 0.2)
    res = cap.finalize(erle_db=8.0, stages="AEC + AI cleaner")
    paths = write_ab_proof(res, str(tmp_path))
    assert sorted(os.path.basename(p) for p in paths) == ["ab_clean.wav", "ab_proof.txt", "ab_raw.wav"]
    for p in paths:
        assert os.path.getsize(p) > 0
    txt = open(os.path.join(str(tmp_path), "ab_proof.txt"), encoding="utf-8").read()
    assert "AEC + AI cleaner" in txt and "ERLE" in txt
