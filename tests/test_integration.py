import conf_pipeline as cp
from conf_pipeline.coverage import create_microphone_array
from conf_pipeline.model import AecConfig


def build_scene():
    c = cp.create_config("boardroom", "2026-06-08T00:00:00Z")
    c = cp.add_device(c, cp.create_processor("P", "DSP"))
    c = cp.add_device(c, create_microphone_array("A1", "Ceiling Array 1", "automatic"))
    c = cp.add_device(c, create_microphone_array("A2", "Ceiling Array 2", "automatic"))
    c = cp.add_device(c, cp.create_wireless_mic("PM", "Presenter Mic", "dante"))
    c = cp.add_device(c, cp.create_loudspeaker("L1", "Speaker L", "analog"))
    c = cp.add_device(c, cp.create_loudspeaker("L2", "Speaker R", "analog"))
    c = cp.add_device(c, cp.create_codec("C", "Codec", "dante"))
    c = cp.route(c, "A1-out-mix", "P-in-dante-1")
    c = cp.route(c, "A2-out-mix", "P-in-dante-2")
    c = cp.route(c, "PM-out-dante-1", "P-in-dante-3")
    c = cp.route(c, "C-out-dante-1", "P-in-dante-4")
    c = cp.route(c, "P-out-analog-1", "L1-in-analog-1")
    c = cp.route(c, "P-out-analog-1", "L2-in-analog-1")
    c = cp.matrix_for(c, "P").route("P-in-dante-4", "P-out-analog-1")
    c = cp.matrix_for(c, "P").route("P-in-dante-3", "P-out-analog-1")
    c = cp.matrix_for(c, "P").route("P-in-dante-1", "P-out-dante-1")
    c = cp.matrix_for(c, "P").route("P-in-dante-2", "P-out-dante-1")
    c = cp.matrix_for(c, "P").route("P-in-dante-3", "P-out-dante-1")
    c = cp.route(c, "P-out-dante-1", "C-in-dante-1")
    c = cp.matrix_for(c, "P").route("P-in-dante-4", "P-out-dante-2")
    c = cp.set_aec(c, "A1", AecConfig(True, "P-out-analog-1"))
    c = cp.set_aec(c, "A2", AecConfig(True, "P-out-analog-1"))
    c = cp.set_aec(c, "PM", AecConfig(True, "P-out-dante-2"))
    return c


def test_correct_scene_validates():
    r = cp.validate(build_scene())
    assert r.errors == []
    assert r.ok


def test_remove_dedicated_reference_triggers_self_reference():
    c = build_scene()
    c = cp.set_aec(c, "PM", AecConfig(True, "P-out-dante-1"))
    r = cp.validate(c)
    assert not r.ok
    assert "AEC_SELF_REFERENCE" in [e.code for e in r.errors]


def test_pointing_reinforced_at_speaker_feed():
    c = build_scene()
    c = cp.set_aec(c, "PM", AecConfig(True, "P-out-analog-1"))
    r = cp.validate(c)
    assert not r.ok
    assert "AEC_REINFORCED_SHARED_REFERENCE" in [e.code for e in r.errors]


def test_auto_configure_no_errors():
    c = build_scene()
    c = cp.set_aec(c, "A1", AecConfig(False, None))
    c = cp.set_aec(c, "A2", AecConfig(False, None))
    c = cp.set_aec(c, "PM", AecConfig(False, None))
    configured = cp.auto_configure(c)
    assert cp.validate(configured).errors == []
