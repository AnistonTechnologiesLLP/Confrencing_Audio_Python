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
