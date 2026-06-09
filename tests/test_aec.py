import conf_pipeline as cp
from conf_pipeline.model import AecConfig


def scene():
    c = cp.create_config("aec", "2026-01-01T00:00:00Z")
    c = cp.add_device(c, cp.create_processor("P", "Processor"))
    c = cp.add_device(c, cp.create_wireless_mic("M", "Presenter", "dante"))
    c = cp.add_device(c, cp.create_loudspeaker("S", "Speaker", "analog"))
    c = cp.add_device(c, cp.create_codec("C", "Codec", "dante"))
    c = cp.route(c, "M-out-dante-1", "P-in-dante-1")
    c = cp.route(c, "C-out-dante-1", "P-in-dante-2")
    c = cp.route(c, "P-out-analog-1", "S-in-analog-1")
    c = cp.matrix_for(c, "P").route("P-in-dante-1", "P-out-analog-1")
    c = cp.matrix_for(c, "P").route("P-in-dante-2", "P-out-analog-1")
    return c


def codes(issues):
    return [i.code for i in issues]


def test_reinforced_shared_reference():
    c = scene()
    c = cp.set_aec(c, "M", AecConfig(True, "P-out-analog-1"))
    r = cp.validate(c)
    assert not r.ok
    assert "AEC_REINFORCED_SHARED_REFERENCE" in codes(r.errors)


def test_plain_self_reference():
    c = scene()
    c = cp.matrix_for(c, "P").route("P-in-dante-1", "P-out-dante-1")
    c = cp.matrix_for(c, "P").route("P-in-dante-2", "P-out-dante-1")
    c = cp.set_aec(c, "M", AecConfig(True, "P-out-dante-1"))
    r = cp.validate(c)
    assert not r.ok
    assert "AEC_SELF_REFERENCE" in codes(r.errors)
    assert "AEC_REINFORCED_SHARED_REFERENCE" not in codes(r.errors)


def test_negative_dedicated_far_end_reference():
    c = scene()
    c = cp.matrix_for(c, "P").route("P-in-dante-2", "P-out-dante-2")
    c = cp.set_aec(c, "M", AecConfig(True, "P-out-dante-2"))
    r = cp.validate(c)
    assert r.ok
    assert codes(r.errors) == []


def test_reference_missing_warning():
    c = scene()
    c = cp.set_aec(c, "M", AecConfig(True, None))
    r = cp.validate(c)
    assert r.ok
    assert "AEC_REFERENCE_MISSING" in codes(r.warnings)


def test_reference_empty_warning():
    c = scene()
    c = cp.set_aec(c, "M", AecConfig(True, "P-out-dante-3"))
    r = cp.validate(c)
    assert r.ok
    assert "AEC_REFERENCE_EMPTY" in codes(r.warnings)


def test_disabled_aec_no_diagnostics():
    c = scene()
    c = cp.set_aec(c, "M", AecConfig(False, "P-out-analog-1"))
    r = cp.validate(c)
    aec_codes = [i.code for i in [*r.errors, *r.warnings] if i.code.startswith("AEC_")]
    assert aec_codes == []
