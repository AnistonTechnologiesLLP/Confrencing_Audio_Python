"""Tests for the auto live placement check (Phase 3).

Two layers:
  * ``PlacementResult`` — the pure-stdlib (numpy-free) result record + camelCase JSON round-trip +
    survey comparison + the to-pre-NR-bands conversion (run in the default suite);
  * ``analyze_placement`` — the metric/scoring analyzer, exercised with deterministic **seeded-RNG**
    synthetic room captures (numpy).

The analyzer is a pure function: it never touches or changes the live DSP pipeline.
"""
import json

import pytest

from conf_pipeline_control.placement import (
    PlacementError,
    PlacementResult,
    STATUS_ACCEPTABLE,
    STATUS_BAD,
    STATUS_GOOD,
    analyze_placement,
    compare_placements,
)

SR = 48000.0
DUR = 2.0


def _np():
    return pytest.importorskip("numpy")


# --------------------------------------------------------------------------- #
# Synthetic room captures (deterministic, seeded)
# --------------------------------------------------------------------------- #
def _capture(*, seed=0, n=None, ch=8, noise=0.003, tones=(), tone_amp=0.05,
             low_freq=None, low_amp=0.0, hf_emphasis=0.0):
    np = _np()
    n = int(n or SR * DUR)
    rng = np.random.default_rng(seed)
    x = noise * rng.standard_normal((n, ch))
    t = np.arange(n) / SR
    for f in tones:
        x = x + (tone_amp * np.sin(2 * np.pi * f * t))[:, None]
    if low_freq and low_amp:
        x = x + (low_amp * np.sin(2 * np.pi * low_freq * t))[:, None]
    if hf_emphasis:
        w = rng.standard_normal((n, ch))
        x = x + hf_emphasis * np.diff(w, axis=0, prepend=0.0)      # +6 dB/oct ⇒ HF-heavy "hiss"
    return x.astype(np.float32)


# --------------------------------------------------------------------------- #
# PlacementResult — pure stdlib (no numpy), runs in the default suite
# --------------------------------------------------------------------------- #
def _result(**kw):
    base = dict(
        sample_rate=48000.0, channels=8, duration_seconds=10.0, label="A",
        status=STATUS_GOOD, score=91, noise_rms_dbfs=-58.2, speech_band_noise_dbfs=-61.4,
        low_frequency_rumble_dbfs=-46.8, broadband_hiss_dbfs=-62.5,
        detected_tones_hz=(102.0, 140.0, 177.0), notch_suggestions_hz=(102.0, 140.0, 177.0),
        hpf_suggestion_hz=120.0, clipping_risk=False,
        channel_imbalance_db=(0.0, 0.4, -0.2, 0.1, 0.3, -0.5, 0.2, -0.1),
        local_hotspot_suspected=False, reasons=("Tonal peaks at 102, 140, 177 Hz",),
        recommendations=("Move array away from airflow",),
    )
    base.update(kw)
    return PlacementResult(**base)


def test_status_constants():
    assert (STATUS_GOOD, STATUS_ACCEPTABLE, STATUS_BAD) == ("GOOD", "ACCEPTABLE", "BAD")


def test_placement_result_json_roundtrip_is_camelcase(tmp_path):
    r = _result()
    d = r.to_dict()
    for k in ("sampleRate", "durationSeconds", "noiseRmsDbfs", "speechBandNoiseDbfs",
              "lowFrequencyRumbleDbfs", "broadbandHissDbfs", "detectedTonesHz", "notchSuggestionsHz",
              "hpfSuggestionHz", "clippingRisk", "channelImbalanceDb", "localHotspotSuspected"):
        assert k in d
    assert PlacementResult.from_dict(d) == r
    assert PlacementResult.from_json(json.dumps(d)) == r
    fp = tmp_path / "p.json"
    r.save(fp)
    assert PlacementResult.load(fp) == r


def test_placement_load_missing_raises_controlled(tmp_path):
    with pytest.raises(PlacementError):
        PlacementResult.load(tmp_path / "nope.json")


def test_placement_from_malformed_json_raises_controlled():
    with pytest.raises(PlacementError):
        PlacementResult.from_json("{ not json")


def test_compare_placements_picks_highest_score():
    bad = _result(label="A", score=42, status=STATUS_BAD)
    ok = _result(label="B", score=76, status=STATUS_ACCEPTABLE)
    good = _result(label="C", score=91, status=STATUS_GOOD)
    best = compare_placements([bad, good, ok])
    assert best.label == "C" and best.score == 91


def test_compare_placements_empty_raises():
    with pytest.raises(PlacementError):
        compare_placements([])


def test_to_pre_nr_bands_from_suggestions():
    r = _result(notch_suggestions_hz=(102.0, 140.0, 177.0), hpf_suggestion_hz=120.0)
    bands = r.to_pre_nr_bands()
    assert bands[0]["type"] == "highpass" and bands[0]["freqHz"] == 120.0
    assert [b["freqHz"] for b in bands if b["type"] == "bell"] == [102.0, 140.0, 177.0]


def test_to_pre_nr_bands_no_suggestions_is_empty():
    assert _result(notch_suggestions_hz=(), hpf_suggestion_hz=None).to_pre_nr_bands() == []


def test_placement_exported_from_package_root():
    import conf_pipeline_control as cc
    assert cc.PlacementResult is PlacementResult
    assert cc.compare_placements is compare_placements
    assert cc.analyze_placement is analyze_placement


# --------------------------------------------------------------------------- #
# analyze_placement — metrics + scoring (numpy, seeded synthetic captures)
# --------------------------------------------------------------------------- #
def test_quiet_room_is_good():
    _np()
    r = analyze_placement(_capture(noise=0.003), sample_rate=SR)
    assert r.status == STATUS_GOOD
    assert r.score >= 85
    assert not r.detected_tones_hz
    assert not r.clipping_risk
    assert not r.local_hotspot_suspected


def test_band_level_matches_known_tone_power():
    np = _np()
    amp = 0.1
    r = analyze_placement(_capture(noise=1e-6, tones=(500.0,), tone_amp=amp), sample_rate=SR)
    expected = 10 * np.log10(amp ** 2 / 2)              # a 500 Hz tone sits in the speech band
    assert abs(r.speech_band_noise_dbfs - expected) < 1.5


def test_tonal_hvac_peak_is_detected():
    _np()
    r = analyze_placement(_capture(noise=0.003, tones=(250.0,), tone_amp=0.05), sample_rate=SR)
    assert any(abs(f - 250.0) <= 8.0 for f in r.detected_tones_hz)
    assert r.score < 100


def test_notch_suggestions_include_detected_tones():
    _np()
    r = analyze_placement(_capture(noise=0.003, tones=(102.0, 140.0, 177.0), tone_amp=0.05),
                          sample_rate=SR)
    for f in (102.0, 140.0, 177.0):
        assert any(abs(s - f) <= 8.0 for s in r.notch_suggestions_hz)
    assert any(b["type"] == "bell" for b in r.to_pre_nr_bands())


def test_strong_rumble_reduces_score_and_warns():
    _np()
    clean = analyze_placement(_capture(noise=0.003), sample_rate=SR)
    rumbly = analyze_placement(_capture(noise=0.003, low_freq=55.0, low_amp=0.06), sample_rate=SR)
    assert rumbly.score < clean.score
    assert any("rumble" in why.lower() for why in rumbly.reasons)


def test_moderate_rumble_is_acceptable_or_bad():
    _np()
    r = analyze_placement(_capture(noise=0.003, low_freq=55.0, low_amp=0.03), sample_rate=SR)
    assert r.status in (STATUS_ACCEPTABLE, STATUS_BAD)


def test_broadband_hiss_reduces_score():
    _np()
    clean = analyze_placement(_capture(noise=0.003), sample_rate=SR)
    hissy = analyze_placement(_capture(noise=0.003, hf_emphasis=0.03), sample_rate=SR)
    assert hissy.score < clean.score
    assert any(("hiss" in why.lower() or "high-frequency" in why.lower()) for why in hissy.reasons)


def test_clipping_risk_is_detected():
    np = _np()
    cap = _capture(noise=0.003)
    cap[1000:1300, 2] = 1.0                            # a run of full-scale samples on capsule 2
    r = analyze_placement(cap, sample_rate=SR)
    assert r.clipping_risk
    assert r.score < 100


def test_channel_imbalance_is_detected():
    np = _np()
    cap = _capture(noise=0.003)
    cap[:, 3] *= 5.0                                   # capsule 3 ~ +14 dB above the rest
    r = analyze_placement(cap, sample_rate=SR)
    assert max(r.channel_imbalance_db) >= 8.0
    assert any("channel" in why.lower() or "capsule" in why.lower() for why in r.reasons)


def test_local_hotspot_heuristic_triggers():
    np = _np()
    cap = _capture(noise=0.003, tones=(140.0,), tone_amp=0.04)
    cap[:, 3] *= 5.0
    r = analyze_placement(cap, sample_rate=SR)
    assert r.local_hotspot_suspected


def test_mono_input_is_rejected():
    np = _np()
    with pytest.raises(PlacementError):
        analyze_placement(np.zeros(48000, dtype=np.float32), sample_rate=SR)


def test_empty_input_is_rejected():
    np = _np()
    with pytest.raises(PlacementError):
        analyze_placement(np.zeros((0, 8), dtype=np.float32), sample_rate=SR)


def test_four_channel_capture_is_handled_safely():
    _np()
    r = analyze_placement(_capture(noise=0.003, ch=4), sample_rate=SR)
    assert r.channels == 4
    assert len(r.channel_imbalance_db) == 4


def test_low_sample_rate_is_handled_safely():
    _np()
    r = analyze_placement(_capture(noise=0.003, n=32000), sample_rate=16000.0)
    assert r.sample_rate == 16000.0                    # hiss band capped at Nyquist; no crash


def test_analysis_is_deterministic():
    _np()
    cap = _capture(noise=0.003, tones=(140.0,), tone_amp=0.05)
    r1 = analyze_placement(cap, sample_rate=SR)
    r2 = analyze_placement(cap, sample_rate=SR)
    assert r1.to_dict() == r2.to_dict()


def test_placement_does_not_change_pipeline_defaults():
    _np()
    analyze_placement(_capture(noise=0.003, tones=(140.0,)), sample_rate=SR)
    from conf_pipeline_control.polaris_beamformer import PolarisBeamformer
    bf = PolarisBeamformer(device=None)
    assert bf.pre_nr is False and bf._calib is None and bf.post_nr is False
