"""Tests for the Listening Processing Profile model (Phase 10).

A ListeningProfile *describes* the live processing recipe for each LIVE "Listening mode" (Whole table /
Follow the room / Lock to a seat / Clean audio / Manual / Two kits). The built-ins must match what each
mode actually does in the shipped GUI: the **recommended** cleanup is ON by default — AGC + dereverb on
every mode, plus the OM-LSA denoiser and tap-suppression on the steering paths (Follow / Lock-to-seat /
Clean / Two-kits). "Clean audio" uses **OM-LSA** (never silently DFN3). The base "Whole table" path has
no denoiser, so its denoise stays OFF. The model is descriptive: it produces a flow summary + warnings;
it does not apply anything. Pure stdlib.
"""
import json

import pytest

from conf_pipeline_control.listening_profile import (
    BUILTIN_LISTENING_PROFILES,
    ListeningProfile,
    listening_profile_for_mode,
)

MODES = ("table", "follow", "seat", "clean", "manual", "twokit")
EXPECTED_IDS = {"table": "whole_table", "follow": "follow_the_room", "seat": "lock_to_seat",
                "clean": "clean_audio", "manual": "manual", "twokit": "two_kits"}


def test_builtins_exist_for_every_mode():
    for m in MODES:
        assert m in BUILTIN_LISTENING_PROFILES
        assert isinstance(BUILTIN_LISTENING_PROFILES[m], ListeningProfile)


def test_profile_ids_are_stable():
    for m, pid in EXPECTED_IDS.items():
        assert BUILTIN_LISTENING_PROFILES[m].id == pid
        assert BUILTIN_LISTENING_PROFILES[m].is_built_in is True


def test_json_roundtrip_is_camelcase():
    p = BUILTIN_LISTENING_PROFILES["clean"]
    d = p.to_dict()
    for k in ("version", "id", "name", "isBuiltIn", "spatial", "calibration", "cleanup", "output", "safety"):
        assert k in d
    assert "postNr" in d["cleanup"] and "preNr" in d["cleanup"] and "agc" in d["cleanup"]
    assert ListeningProfile.from_dict(d) == p
    assert ListeningProfile.from_json(json.dumps(d)) == p


def test_flow_summary_contains_stages():
    f = BUILTIN_LISTENING_PROFILES["table"].flow_summary()
    for s in ("capture", "preamp", "beam:", "pre-NR", "denoise", "AGC", "output"):
        assert s in f


def test_flow_order_is_correct():
    f = BUILTIN_LISTENING_PROFILES["clean"].flow_summary()
    assert f.index("capture") < f.index("beam:") < f.index("denoise") < f.index("AGC") < f.index("output")
    assert f.index("pre-NR") < f.index("denoise")


def test_whole_table_is_default_safe():
    p = BUILTIN_LISTENING_PROFILES["table"]
    assert p.cleanup.post_nr.enabled is False          # base "Whole table" path has no denoiser
    assert p.cleanup.agc.enabled is True               # AGC is the recommended default (on)
    assert p.cleanup.dereverb is False                 # dereverb is NOT global — off for Whole table
    assert p.spatial.auto_steer is False and p.safety.default_safe is True


def test_recommended_cleanup_is_on_by_default():
    """The recommended cleanup ships ON: AGC everywhere, OM-LSA denoise + tap-suppression on the steering
    paths (Follow / Lock-to-seat / Clean). 'Whole table' has no denoiser path, so denoise stays off there;
    'Two kits' cleans per-kit (OM-LSA) with one AGC. Dereverb is NOT global (see the dereverb test)."""
    for m in ("follow", "seat", "clean"):
        c = BUILTIN_LISTENING_PROFILES[m].cleanup
        assert c.post_nr.enabled is True and c.post_nr.engine == "omlsa"
        assert c.transient_suppression is True
        assert c.agc.enabled is True
    tk = BUILTIN_LISTENING_PROFILES["twokit"].cleanup
    assert tk.post_nr.enabled is True and tk.post_nr.engine == "omlsa" and tk.agc.enabled is True
    tab = BUILTIN_LISTENING_PROFILES["table"].cleanup
    assert tab.post_nr.enabled is False and tab.agc.enabled is True


def test_dereverb_is_restricted_to_steering_modes():
    """Dereverb is recommended ONLY for the auto-steer modes (Follow / Clean), where it ships ON with a
    naturalness warning. Every other mode keeps it OFF — it is never a global default (it can colour a dry
    room). Lock-to-seat / Two kits / Manual / Whole table all stay off."""
    for m in ("follow", "clean"):
        p = BUILTIN_LISTENING_PROFILES[m]
        assert p.cleanup.dereverb is True
        assert any("dereverb" in w.lower() for w in p.warnings())        # ON → warned
    for m in ("table", "seat", "manual", "twokit"):
        assert BUILTIN_LISTENING_PROFILES[m].cleanup.dereverb is False
    # the manual fallback (no live toggles) never forces dereverb on either
    assert listening_profile_for_mode("manual").cleanup.dereverb is False


def test_follow_shows_auto_steer_on():
    p = BUILTIN_LISTENING_PROFILES["follow"]
    assert p.spatial.auto_steer is True
    assert "auto-steer ON" in p.flow_summary()


def test_lock_to_seat_warns_about_seat():
    assert any("seat" in w.lower() for w in BUILTIN_LISTENING_PROFILES["seat"].warnings())


def test_clean_audio_does_not_force_dfn3():
    p = BUILTIN_LISTENING_PROFILES["clean"]
    assert p.cleanup.post_nr.enabled is True
    assert p.cleanup.post_nr.engine == "omlsa" and p.cleanup.post_nr.engine != "dfn3"   # OM-LSA, never DFN3
    assert p.cleanup.aec is False                       # AEC needs a far-end ref → stays opt-in (off)
    assert any(("natural" in w.lower() or "latency" in w.lower()) for w in p.warnings())


def test_clean_audio_uses_recommended_agc():
    # Clean audio is the hands-off recommended mode: AGC is part of the recommended chain (on), not forced DFN3.
    assert BUILTIN_LISTENING_PROFILES["clean"].cleanup.agc.enabled is True


def test_manual_profile_reflects_flags_not_override():
    flags = {"post_nr": True, "post_nr_engine": "dfn3", "dereverb": True, "agc": True, "auto_steer": True}
    p = listening_profile_for_mode("manual", manual_flags=flags)
    assert p.id == "manual"
    assert p.cleanup.post_nr.enabled is True and p.cleanup.post_nr.engine == "dfn3"
    assert p.cleanup.dereverb is True and p.cleanup.agc.enabled is True
    assert any(("manual" in w.lower() or "toggle" in w.lower() or "source of truth" in w.lower())
               for w in p.warnings())


def test_manual_profile_without_flags_is_all_off():
    p = listening_profile_for_mode("manual")
    assert p.cleanup.post_nr.enabled is False and p.cleanup.agc.enabled is False


def test_room_specific_notches_are_not_global_defaults():
    for m in MODES:
        assert tuple(BUILTIN_LISTENING_PROFILES[m].cleanup.pre_nr.notches_hz) == ()
    assert any("global default" in w.lower() for w in BUILTIN_LISTENING_PROFILES["table"].warnings())


def test_two_kits_flow_is_automix():
    f = BUILTIN_LISTENING_PROFILES["twokit"].flow_summary()
    assert "combined output" in f and ("cross-fade" in f or "automix" in f.lower())
    assert "denoise OM-LSA" in f and "AGC ON" in f      # recommended per-kit cleaning + one combined AGC


def test_listening_profile_for_mode_returns_builtin_and_safe_default():
    assert listening_profile_for_mode("clean").id == "clean_audio"
    assert listening_profile_for_mode("twokit").id == "two_kits"
    assert listening_profile_for_mode("nope").id == "whole_table"   # unknown → safe default


def test_exported_from_root():
    import conf_pipeline_control as cc
    assert cc.ListeningProfile is ListeningProfile
    assert cc.listening_profile_for_mode is listening_profile_for_mode
