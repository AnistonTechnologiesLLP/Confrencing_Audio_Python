"""Auto-Route tests (pure engine). The cardinal invariant: the result must
validate with zero errors (the AEC self-reference rule) and be idempotent."""
import conf_pipeline as cp
from conf_pipeline.dsp import analyze_aec_reference


def _scene():
    c = cp.create_config("ar", "2026-06-09T00:00:00Z")
    c = cp.add_device(c, cp.create_processor("P", "DSP"))
    c = cp.add_device(c, cp.create_microphone_array("A1", "Array", "automatic"))
    c = cp.add_device(c, cp.create_codec("C", "Codec", "dante"))
    c = cp.add_device(c, cp.create_loudspeaker("L1", "Speaker", "analog"))
    c = cp.route(c, "A1-out-mix", "P-in-dante-1")
    c = cp.route(c, "C-out-dante-1", "P-in-dante-2")
    return c


def test_auto_route_validates_clean():
    res = cp.auto_route(_scene())
    assert cp.validate(res.config).errors == []
    assert res.changes  # non-empty summary


def test_auto_route_aec_self_reference_safe():
    res = cp.auto_route(_scene())
    proc = cp.get_primary_processor(res.config)
    for m in [d for d in res.config.devices if cp.is_mic_device(d)]:
        analysis = analyze_aec_reference(res.config, proc, m.id, m.aec.reference_bus_id)
        assert not analysis.contains_own_signal


def test_auto_route_feeds_loudspeaker():
    res = cp.auto_route(_scene())
    assert any(r.to_port_id == "L1-in-analog-1" for r in res.config.routes)
    assert res.counts.get("routes", 0) >= 1


def test_auto_route_creates_mute_link():
    res = cp.auto_route(_scene())
    assert res.config.mute_links and res.counts.get("mute_links", 0) == 1


def test_auto_route_is_idempotent():
    once = cp.auto_route(_scene()).config
    twice = cp.auto_route(once)
    assert twice.changes == ["No changes — the design is already routed."]
    assert cp.serialize(twice.config) == cp.serialize(once)


def test_auto_route_no_processor():
    c = cp.create_config("np", "x")
    res = cp.auto_route(c)
    assert res.config is c
    assert "No processor" in res.changes[0]


def test_auto_route_no_codec_or_loudspeaker_still_valid():
    c = cp.create_config("min", "x")
    c = cp.add_device(c, cp.create_processor("P", "DSP"))
    c = cp.add_device(c, cp.create_microphone_array("A1", "A", "automatic"))
    c = cp.route(c, "A1-out-mix", "P-in-dante-1")
    res = cp.auto_route(c)
    assert cp.validate(res.config).errors == []


def test_auto_configure_unchanged_regression():
    # auto_route builds on auto_configure; the latter must still work standalone
    res = cp.auto_configure(_scene())
    assert cp.validate(res).errors == []
