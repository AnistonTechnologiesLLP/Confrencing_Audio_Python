"""Headless tests for the operator-workflow status model (Phase 6).

The full GUI MainWindow hangs headless on this box (see CLAUDE.md), so the operator surface is built as
a hardware-free **status model** (`OperatorStatus`) that gathers Phases 1–5 into 7 sections + a
diagnostics export. These tests cover that model (no Qt). The light GUI panel that renders it is probed
separately in `tests/test_gui_operator.py` (single-panel construct, never MainWindow).
"""
import json

import pytest

from conf_pipeline_control.operator import OperatorStatus


def _np():
    return pytest.importorskip("numpy")


def _engine(**kw):
    from conf_pipeline_control.polaris_beamformer import PolarisBeamformer
    return PolarisBeamformer(device=None, **kw)


# --------------------------------------------------------------------------- #
# Builds + sections present
# --------------------------------------------------------------------------- #
def test_operator_status_builds_without_hardware():
    _np()
    d = OperatorStatus.build(engine=_engine()).to_dict()
    for sec in ("device", "calibration", "placement", "pipeline", "egress", "transcription", "warnings"):
        assert sec in d


def test_operator_exported_from_root():
    import conf_pipeline_control as cc
    assert cc.OperatorStatus is OperatorStatus


def test_building_status_does_not_change_engine_defaults():
    _np()
    eng = _engine()
    OperatorStatus.build(engine=eng)
    assert eng.pre_nr is False and eng._calib is None and eng.post_nr is False


# --------------------------------------------------------------------------- #
# Calibration
# --------------------------------------------------------------------------- #
def test_calibration_section_disabled_by_default():
    _np()
    c = OperatorStatus.build(engine=_engine()).calibration_section()
    assert c["enabled"] is False
    assert "off" in c["status"].lower()


def test_calibration_section_enabled_with_profile():
    _np()
    from conf_pipeline_control.calibration import CalibrationProfile
    prof = CalibrationProfile(channels=8, sample_rate=44100.0, gain_db=(6.0,) + (0.0,) * 7,
                              polarity=(1, -1, 1, 1, 1, 1, 1, 1), reference_channel=2)
    c = OperatorStatus.build(engine=_engine(sample_rate=44100.0, calibration=prof)).calibration_section()
    assert c["enabled"] is True
    assert c["channels"] == 8 and c["referenceChannel"] == 2
    assert "dB" in c["gainDbSummary"]
    assert "invert" in c["polaritySummary"].lower()       # ch1 polarity flip is surfaced


def test_calibration_failure_is_surfaced_not_hidden(tmp_path):
    _np()
    bad = str(tmp_path / "nope.json")
    st = OperatorStatus.build(engine=_engine(calibration_path=bad), calibration_path=bad)
    c = st.calibration_section()
    assert c["enabled"] is False
    assert any("calibration" in w.lower() for w in st.warnings())     # not hidden


# --------------------------------------------------------------------------- #
# Placement
# --------------------------------------------------------------------------- #
def _placement(**kw):
    from conf_pipeline_control.placement import PlacementResult, STATUS_BAD
    base = dict(status=STATUS_BAD, score=42, reasons=("Strong tonal HVAC/fan noise near the array",),
                recommendations=("Move the array 0.5-1 m from airflow/vent/fan and re-check",),
                detected_tones_hz=(102.0, 140.0, 177.0), notch_suggestions_hz=(102.0, 140.0, 177.0),
                hpf_suggestion_hz=120.0)
    base.update(kw)
    return PlacementResult(**base)


def test_placement_section_maps_status():
    p = OperatorStatus.build(engine=_engine(), placement=_placement()).placement_section()
    assert p["available"] is True and p["status"] == "BAD" and p["score"] == 42
    assert p["reasons"] and p["recommendations"]


def test_placement_recommendations_not_auto_applied():
    _np()
    eng = _engine()
    p = OperatorStatus.build(engine=eng, placement=_placement()).placement_section()
    assert p["autoApplied"] is False
    assert p["suggestedPreNrBands"]                # shown
    assert eng.pre_nr is False                     # NOT applied to the live engine


def test_detected_tones_become_pre_nr_suggestions():
    p = OperatorStatus.build(engine=_engine(), placement=_placement()).placement_section()
    assert p["detectedTones"] == [102.0, 140.0, 177.0]
    bands = p["suggestedPreNrBands"]
    assert any(b["type"] == "highpass" for b in bands) and any(b["type"] == "bell" for b in bands)


def test_placement_section_absent_when_not_run():
    p = OperatorStatus.build(engine=_engine()).placement_section()
    assert p["available"] is False


# --------------------------------------------------------------------------- #
# Pipeline order + active stages
# --------------------------------------------------------------------------- #
def test_pipeline_order_has_calibration_and_pre_nr_before_post_nr():
    _np()
    stages = [s["stage"] for s in OperatorStatus.build(engine=_engine()).pipeline_section()["order"]]
    assert "calibration" in stages and stages.index("calibration") < stages.index("beam")
    assert "pre-NR HPF/notch" in stages
    assert stages.index("pre-NR HPF/notch") < stages.index("post-NR")


def test_pipeline_default_stages_are_off_and_marked_safe():
    _np()
    sec = OperatorStatus.build(engine=_engine()).pipeline_section()
    active = {s["stage"]: s["active"] for s in sec["order"]}
    assert active["calibration"] is False and active["pre-NR HPF/notch"] is False
    assert active["post-NR"] is False and active["AEC"] is False
    assert "default" in sec["note"].lower()        # default-off safety stated


def test_pipeline_active_includes_pre_nr_when_enabled():
    _np()
    from conf_pipeline_control.pre_nr import build_pre_nr_bands
    eng = _engine(pre_nr=True, pre_nr_bands=build_pre_nr_bands(hpf_hz=120.0))
    order = {s["stage"]: s for s in OperatorStatus.build(engine=eng).pipeline_section()["order"]}
    assert order["pre-NR HPF/notch"]["active"] is True
    eng._setup_runtime()
    assert "HPF/notch" in eng.active_cleaning_stages()     # surfaced in the cleaning-stages summary too


# --------------------------------------------------------------------------- #
# Egress
# --------------------------------------------------------------------------- #
def test_egress_section_shows_48k_and_16k_routes():
    _np()
    import conf_pipeline_control as cc
    router = cc.EgressRouter(48000.0, asr_rate=16000)
    e = OperatorStatus.build(engine=_engine(), egress=router).egress_section()
    assert e["available"] is True and e["sampleRate"] == 48000.0 and e["asrRate"] == 16000
    assert any("48" in r for r in e["routes"]) and any("16" in r for r in e["routes"])


def test_egress_section_notes_no_raw_8ch_and_is_enforced():
    np = _np()
    import conf_pipeline_control as cc
    router = cc.EgressRouter(48000.0)
    e = OperatorStatus.build(engine=_engine(), egress=router).egress_section()
    assert "raw" in e["note"].lower() or "8-channel" in e["note"].lower()
    with pytest.raises(cc.EgressError):
        router.push(np.zeros((480, 8), dtype=np.float32))     # the safeguard really holds


# --------------------------------------------------------------------------- #
# Transcription
# --------------------------------------------------------------------------- #
def test_transcription_section_reflects_mock_session():
    _np()
    import conf_pipeline_control as cc
    prov = cc.MockTranscriptionProvider()
    stream = cc.TranscriptionStream(prov)
    stream.start()
    t = OperatorStatus.build(engine=_engine(), transcription=stream).transcription_section()
    assert t["available"] is True and t["status"] == "running" and t["isMock"] is True
    stream.stop()
    t2 = OperatorStatus.build(engine=_engine(), transcription=stream).transcription_section()
    assert t2["status"] == "stopped"
    assert prov.network_calls == 0


def test_transcription_section_absent_when_not_configured():
    t = OperatorStatus.build(engine=_engine()).transcription_section()
    assert t["available"] is False


# --------------------------------------------------------------------------- #
# Diagnostics export
# --------------------------------------------------------------------------- #
def test_diagnostics_export_contains_all_sections(tmp_path):
    _np()
    st = OperatorStatus.build(engine=_engine(), placement=_placement())
    paths = st.save(str(tmp_path), stamp="20260101_000000")
    assert len(paths) == 2
    js = next(p for p in paths if p.endswith(".json"))
    with open(js, encoding="utf-8") as f:
        d = json.load(f)
    for sec in ("device", "calibration", "placement", "pipeline", "egress", "transcription", "warnings"):
        assert sec in d
    md = next(p for p in paths if p.endswith(".md"))
    with open(md, encoding="utf-8") as f:
        text = f.read()
    assert "Operator" in text and "Pipeline" in text and "Placement" in text
