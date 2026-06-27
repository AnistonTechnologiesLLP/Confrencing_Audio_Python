"""Tests for the Audio Room Profile model (Phase 9).

An `AudioRoomProfile` is an editable, room-specific **document** — it stores references + preferences
(calibration profile path, placement result + suggestions, pre-NR HPF/notch, egress/transcription prefs,
safety flags) and round-trips to camelCase JSON. It is inert: it never touches the DSP engine, never
forces a feature on, and never auto-applies placement suggestions. Pure stdlib (no numpy).
"""
import json

import pytest

from conf_pipeline_control.room_profile import AudioRoomProfile, RoomProfileError


def test_default_profile_validates_clean():
    p = AudioRoomProfile()
    assert p.version == 1 and p.device == "POLARIS_8MEMS" and p.channels == 8
    assert p.validate() == []                          # a fresh profile has no warnings


def test_safety_flags_default_safe():
    s = AudioRoomProfile().safety
    assert not any([s.dfn3_forced_on, s.dereverb_forced_on, s.placement_suggestions_auto_applied,
                    s.real_asr_network_call, s.virtual_mic_driver_bundled])


def test_json_roundtrip_is_camelcase(tmp_path):
    p = AudioRoomProfile(name="Conference Room A - AC On", sample_rate=44100.0, notes="bench")
    p.pre_nr_cleanup.hpf_hz = 120.0
    p.pre_nr_cleanup.notches_hz = [102.0, 140.0]
    p.egress.asr_16k = True
    p.transcription.enabled = True
    d = p.to_dict()
    for k in ("version", "name", "sampleRate", "createdAt", "updatedAt", "calibration", "placement",
              "preNrCleanup", "egress", "transcription", "safety"):
        assert k in d
    assert d["preNrCleanup"]["hpfHz"] == 120.0 and d["preNrCleanup"]["notchesHz"] == [102.0, 140.0]
    assert d["egress"]["asr16k"] is True
    assert AudioRoomProfile.from_dict(d) == p
    assert AudioRoomProfile.from_json(json.dumps(d)) == p
    fp = tmp_path / "room.json"
    p.save(fp)
    assert AudioRoomProfile.load(fp) == p


def test_invalid_version_is_warned_not_crashed():
    p = AudioRoomProfile.from_dict({"version": 99, "name": "x"})
    assert p.version == 99
    assert any("version" in w.lower() for w in p.validate())


def test_malformed_json_raises_controlled():
    with pytest.raises(RoomProfileError):
        AudioRoomProfile.from_json("{ not json")


def test_load_missing_file_raises_controlled(tmp_path):
    with pytest.raises(RoomProfileError):
        AudioRoomProfile.load(tmp_path / "nope.json")


def test_device_rate_channel_mismatch_warns():
    w = AudioRoomProfile().validate(expected_device="OTHER", expected_rate=44100, expected_channels=4)
    assert any("device" in x.lower() for x in w)
    assert any(("rate" in x.lower() or "samplerate" in x.lower()) for x in w)
    assert any("channel" in x.lower() for x in w)


def test_missing_calibration_path_warns():
    p = AudioRoomProfile()
    p.calibration.profile_path = "C:/nope/cal.json"
    assert any("calibration" in w.lower() for w in p.validate())


def test_missing_placement_path_warns():
    p = AudioRoomProfile()
    p.placement.result_path = "C:/nope/placement.json"
    assert any("placement" in w.lower() for w in p.validate())


def _placement():
    from conf_pipeline_control.placement import PlacementResult, STATUS_BAD
    return PlacementResult(status=STATUS_BAD, score=42, detected_tones_hz=(102.0, 140.0, 177.0),
                           notch_suggestions_hz=(102.0, 140.0, 177.0), hpf_suggestion_hz=120.0)


def test_copy_placement_suggestions_into_draft():
    p = AudioRoomProfile()
    p.copy_placement_suggestions(_placement(), result_path="placement_a.json")
    assert p.placement.last_status == "BAD" and p.placement.last_score == 42
    assert p.placement.detected_tones_hz == [102.0, 140.0, 177.0]
    assert p.placement.result_path == "placement_a.json"
    assert p.pre_nr_cleanup.notches_hz == [102.0, 140.0, 177.0]
    assert p.pre_nr_cleanup.hpf_hz == 120.0


def test_copy_placement_suggestions_does_not_enable_or_autoapply():
    p = AudioRoomProfile()
    p.copy_placement_suggestions(_placement())
    assert p.pre_nr_cleanup.enabled is False                  # NOT forced on
    assert p.placement.auto_apply_suggestions is False
    assert p.safety.placement_suggestions_auto_applied is False


def test_pre_nr_settings_stored_but_not_forced():
    p = AudioRoomProfile()
    p.pre_nr_cleanup.hpf_hz = 120.0
    p.pre_nr_cleanup.notches_hz = [140.0]
    assert p.pre_nr_cleanup.hpf_hz == 120.0 and p.pre_nr_cleanup.enabled is False
    assert not hasattr(p, "apply_to_engine")                  # the model cannot touch a live engine


def test_transcription_settings_stored_no_network():
    p = AudioRoomProfile()
    p.transcription.enabled = True
    p.transcription.provider = "mock"
    assert p.transcription.enabled is True and p.transcription.provider == "mock"
    assert p.transcription.sample_rate == 16000
    assert p.safety.real_asr_network_call is False


def test_unsafe_safety_flag_warns():
    p = AudioRoomProfile()
    p.safety.dfn3_forced_on = True
    assert any(("dfn3" in w.lower() or "unsafe" in w.lower()) for w in p.validate())


def test_autoapply_flag_warns():
    p = AudioRoomProfile()
    p.placement.auto_apply_suggestions = True
    assert any("auto" in w.lower() for w in p.validate())


def test_attach_calibration_sets_path_not_enabled():
    p = AudioRoomProfile()
    p.attach_calibration("polaris_cal.json", summary="8ch ref0")
    assert p.calibration.profile_path == "polaris_cal.json" and p.calibration.summary == "8ch ref0"
    assert p.calibration.enabled is False                     # attaching is not enabling


def test_preferred_listening_profile_id_roundtrips_and_is_backwards_compatible():
    p = AudioRoomProfile()
    assert p.preferred_listening_profile_id == ""           # default; never auto-applied
    p.preferred_listening_profile_id = "clean_audio"
    d = p.to_dict()
    assert d["preferredListeningProfileId"] == "clean_audio"
    assert AudioRoomProfile.from_dict(d) == p
    old = {k: v for k, v in p.to_dict().items() if k != "preferredListeningProfileId"}
    assert AudioRoomProfile.from_dict(old).preferred_listening_profile_id == ""   # old JSON still loads


def test_room_profile_exported_from_root():
    import conf_pipeline_control as cc
    assert cc.AudioRoomProfile is AudioRoomProfile
