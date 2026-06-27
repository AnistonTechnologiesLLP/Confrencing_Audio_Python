"""Tests for the Lobe Control model (Phase 11).

`LobeControl` is the operator's *pickup-pattern* state for the POLARIS beamformer (direction / focus /
suppress-direction / follow), distinct from capsule calibration. It is descriptive + validating: it bounds
angles and null count, produces an honest summary + warnings, and round-trips camelCase JSON. It never
applies DSP itself and makes no "perfect fencing" promise. Pure stdlib.
"""
import json

import pytest

from conf_pipeline_control.lobe_control import (
    LOBE_MODES,
    LOBE_WIDTHS,
    LobeControl,
    LobeControlError,
    LobeNull,
    default_lobe_for_mode,
    loading_for_width,
)


def test_validates_angle_range():
    LobeControl(mode="fixed", main_angle_deg=35.0).validate()        # in-range ok
    LobeControl(mode="fixed", main_angle_deg=-180.0).validate()
    LobeControl(mode="fixed", main_angle_deg=180.0).validate()
    for bad in (200.0, -181.0, 360.0):
        with pytest.raises(LobeControlError):
            LobeControl(mode="fixed", main_angle_deg=bad).validate()
    # the GUI wraps any input into range before setting
    assert LobeControl.clamp_angle(200.0) == -160.0
    assert LobeControl.clamp_angle(-181.0) == 179.0
    assert LobeControl.clamp_angle(35.0) == 35.0


def test_default_config_is_safe():
    p = LobeControl()
    assert p.validate() is p
    assert p.mode in LOBE_MODES and p.mode == "table"      # whole-table (today's default pickup)
    assert p.beam_width != "narrow"                         # never aggressively narrow by default
    assert p.auto_steer is False and p.nulls == []          # not following, no suppression
    assert p.safety.max_nulls == 2 and p.safety.requires_calibration_recommended is True


def test_null_count_is_bounded():
    p = LobeControl(mode="fixed", nulls=[LobeNull(angle_deg=180.0), LobeNull(angle_deg=90.0),
                                         LobeNull(angle_deg=-90.0)])
    with pytest.raises(LobeControlError):
        p.validate()                                        # 3 enabled nulls > max_nulls (2)
    assert len(p.effective_nulls()) == 2                    # the GUI only ever applies the first maxNulls
    # disabled nulls don't count toward the cap
    ok = LobeControl(mode="fixed", nulls=[LobeNull(angle_deg=180.0), LobeNull(angle_deg=90.0, enabled=False)])
    assert ok.validate() is ok and len(ok.effective_nulls()) == 1


def test_beam_width_presets_serialize_roundtrip():
    for w in LOBE_WIDTHS:
        p = LobeControl(mode="fixed", main_angle_deg=10.0, beam_width=w,
                        nulls=[LobeNull(angle_deg=180.0, label="Fan side")])
        d = p.to_dict()
        assert d["beamWidth"] == w and d["mainAngleDeg"] == 10.0
        assert d["nulls"][0] == {"angleDeg": 180.0, "enabled": True, "label": "Fan side"}
        assert LobeControl.from_dict(d) == p
        assert LobeControl.from_json(json.dumps(d)) == p
    with pytest.raises(LobeControlError):
        LobeControl(beam_width="ultra").validate()          # unknown preset rejected
    assert loading_for_width("wide") > loading_for_width("medium") > loading_for_width("narrow")


def test_manual_angle_summary_is_correct():
    s = LobeControl(mode="fixed", main_angle_deg=35.0, beam_width="medium",
                    nulls=[LobeNull(angle_deg=180.0)]).summary()
    assert "fixed 35" in s and "width medium" in s and "null 180" in s


def test_seat_lock_summary_is_correct():
    s = LobeControl(mode="seat", target_seat_id="Seat 3", beam_width="medium").summary()
    assert "Seat 3" in s and "seat" in s.lower()


def test_auto_steer_summary_is_correct():
    s = LobeControl(mode="follow", auto_steer=True).summary()
    assert "follow" in s.lower() and ("auto" in s.lower())


def test_calibration_off_warning_appears():
    p = LobeControl(mode="fixed", main_angle_deg=20.0)
    assert any("calibration" in w.lower() for w in p.warnings(calibration_on=False))
    assert not any("calibration" in w.lower() for w in p.warnings(calibration_on=True))
    assert "calibration OFF" in p.summary(calibration_on=False)
    assert "calibration ON" in p.summary(calibration_on=True)


def test_placement_bad_warning_appears():
    p = LobeControl(mode="fixed", main_angle_deg=20.0)
    assert any("placement" in w.lower() for w in p.warnings(calibration_on=True, placement_status="BAD"))
    assert not any("placement" in w.lower()
                   for w in p.warnings(calibration_on=True, placement_status="GOOD"))
    assert "placement BAD" in p.summary(calibration_on=True, placement_status="BAD")


def test_null_warning_is_honest_not_a_mute():
    p = LobeControl(mode="fixed", nulls=[LobeNull(angle_deg=180.0)])
    msg = " ".join(p.warnings(calibration_on=True)).lower()
    assert "reduce" in msg and "not" in msg and "mute" in msg     # honest: reduces, does not fully mute


def test_default_lobe_for_mode_whole_table_is_not_narrow():
    p = default_lobe_for_mode("table")
    assert p.mode == "table" and p.beam_width != "narrow" and p.auto_steer is False and p.nulls == []


def test_default_lobe_for_mode_follow_auto_steers():
    p = default_lobe_for_mode("follow")
    assert p.auto_steer is True and p.mode == "follow"


def test_lock_to_seat_handles_missing_seat_safely():
    p = default_lobe_for_mode("seat", target_seat_id=None)   # no seat selected yet
    assert p.validate() is p and p.mode == "seat"            # must not raise
    assert "seat" in p.summary().lower()                     # summary degrades gracefully


def test_exported_from_root():
    import conf_pipeline_control as cc
    assert cc.LobeControl is LobeControl
    assert cc.default_lobe_for_mode is default_lobe_for_mode
