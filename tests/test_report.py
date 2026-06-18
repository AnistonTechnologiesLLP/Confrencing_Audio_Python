"""Design-report export tests (pure engine)."""
import pytest

import conf_pipeline as cp
from conf_pipeline.model import Point2D


def _scene():
    c = cp.create_config("Boardroom", "2026-06-09T00:00:00Z")
    c = cp.set_room(c, cp.rectangular_room(9, 7, 3))
    c = cp.add_device(c, cp.create_processor("P", "DSP"))
    c = cp.add_device(c, cp.create_microphone_array("A1", "Ceiling Array", "automatic"))
    c = cp.set_device_position(c, "A1", Point2D(4.5, 3.5))
    c = cp.add_talker(c, cp.create_talker("T1", "Presenter", Point2D(4.5, 3.5)))
    return c


def test_markdown_has_sections_and_content():
    md = cp.design_report(_scene(), "markdown")
    assert isinstance(md, str) and md
    for heading in ["# Design report", "## Devices", "## Routing", "## AEC references", "## Coverage", "## Validation"]:
        assert heading in md
    assert "Ceiling Array" in md and "Presenter" in md


def test_html_is_html_and_escapes_labels():
    c = cp.rename_device(_scene(), "A1", "A<b>x")  # injection attempt
    out = cp.design_report(c, "html")
    assert out.startswith("<!doctype html>") and "<table" in out
    assert "&lt;b&gt;" in out and "<b>x" not in out


def test_empty_config_does_not_crash():
    md = cp.design_report(cp.create_config("empty", "x"), "markdown")
    assert "Room: not defined" in md


def test_bad_format_raises():
    with pytest.raises(ValueError):
        cp.design_report(_scene(), "pdf")


def test_report_is_deterministic():
    c = _scene()
    assert cp.design_report(c) == cp.design_report(c)
    assert cp.design_report(c, "html") == cp.design_report(c, "html")


# --------------------------------------------------------------------------- #
# Commissioning / as-built report
# --------------------------------------------------------------------------- #
def test_commissioning_layers_measurements_and_signoff():
    info = cp.CommissioningInfo(
        site="HQ Boardroom", commissioned_by="A. Tech", date="2026-06-18",
        listening_mode="Whole table", estimated_latency_ms=56.0,
        active_cleaning_stages="AI cleaner + dereverb", aec_ref_source="WASAPI loopback",
        aec_erle_db=12.3, bed_reduction_db=21.7, rms_reduction_db=8.0,
        front_offset_deg=15.0, silent_capsules=(),
    )
    md = cp.commissioning_report(_scene(), info)
    assert md.startswith("# Commissioning report — Boardroom")
    for heading in ["## Room", "## Devices", "## Live measurements", "## Health & calibration",
                    "## Validation", "## Commissioning sign-off"]:
        assert heading in md
    assert "HQ Boardroom" in md and "A. Tech" in md
    assert "~56 ms" in md and "within the" in md          # latency framed as estimated + target judged
    assert "12.3 dB" in md and "21.7 dB quieter" in md
    assert "all capsules active" in md
    assert "Checks passing:" in md


def test_commissioning_default_info_is_config_only_plus_signoff():
    md = cp.commissioning_report(_scene())                  # default CommissioningInfo()
    assert "## Live measurements" not in md                 # nothing measured → section omitted
    assert "## Health & calibration" not in md
    assert "## Devices" in md and "## Commissioning sign-off" in md
    assert "________________" in md                         # blank hand-sign form


def test_commissioning_signoff_reflects_validation_errors():
    # a mic array routed to a non-existent bus → a validation error → unticked check
    c = cp.add_device(cp.create_config("Bad", "x"), cp.create_microphone_array("A1", "Arr", "automatic"))
    md = cp.commissioning_report(c)
    assert "[ ] No configuration errors" in md or "error(s)" in md
    # room never defined → that check is unticked
    assert "[ ] Room geometry defined" in md


def test_commissioning_latency_above_target_is_flagged():
    over = cp.CommissioningInfo(estimated_latency_ms=300.0)
    md = cp.commissioning_report(_scene(), over)
    assert "ABOVE the" in md
    assert "[ ] Estimated latency within target" in md


def test_commissioning_silent_capsules_listed():
    info = cp.CommissioningInfo(silent_capsules=(5, 6))
    md = cp.commissioning_report(_scene(), info)
    assert "Silent / disabled capsules: 5, 6" in md
    assert "[ ] All capsules active (no silent capsules)" in md


def test_commissioning_html_escapes_labels():
    c = cp.rename_device(_scene(), "A1", "A<script>x")
    out = cp.commissioning_report(c, cp.CommissioningInfo(estimated_latency_ms=56.0), "html")
    assert out.startswith("<!doctype html>") and "<table" in out
    assert "&lt;script&gt;" in out and "<script>x" not in out


def test_commissioning_bad_format_raises():
    with pytest.raises(ValueError):
        cp.commissioning_report(_scene(), None, "pdf")


def test_commissioning_is_deterministic_given_info():
    info = cp.CommissioningInfo(date="2026-06-18", estimated_latency_ms=56.0, bed_reduction_db=21.7)
    c = _scene()
    assert cp.commissioning_report(c, info) == cp.commissioning_report(c, info)
