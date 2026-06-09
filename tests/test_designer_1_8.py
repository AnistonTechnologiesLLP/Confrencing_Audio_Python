import json

import conf_pipeline as cp


def codes(issues):
    return [i.code for i in issues]


def test_deployment_status_and_diff():
    c = cp.create_config("d", "x")
    c = cp.set_deployment_status(c, "online")
    assert c.deployment.status == "online"
    c = cp.mark_deployed(c, "2026-06-09T00:00:00Z")
    assert c.deployment.status == "deployed" and c.deployment.last_deployed_at == "2026-06-09T00:00:00Z"
    assert cp.serialize(cp.deserialize(cp.serialize(c))) == cp.serialize(c)


def test_deployment_diff():
    base = cp.create_config("d", "x")
    base = cp.add_device(base, cp.create_processor("P", "DSP"))
    base = cp.add_device(base, cp.create_wireless_mic("M", "Mic"))
    target = cp.rename_device(base, "M", "Mic Renamed")
    target = cp.add_device(target, cp.create_loudspeaker("L", "Spk"))
    target = cp.route(target, "P-out-analog-1", "L-in-analog-1")
    diff = cp.deployment_diff(base, target)
    assert diff.devices_added == ["L"]
    assert diff.devices_changed == ["M"]
    assert len(diff.routes_added) == 1 and not diff.identical
    assert cp.deployment_diff(base, base).identical


def test_naming_scheme_and_warnings():
    c = cp.create_config("n", "x")
    c = cp.add_device(c, cp.create_wireless_mic("M1", "foo"))
    c = cp.add_device(c, cp.create_wireless_mic("M2", "bar"))
    c = cp.add_device(c, cp.create_loudspeaker("L1", "baz"))
    c = cp.apply_naming_scheme(c)
    assert [d.label for d in c.devices] == ["Wireless Mic 1", "Wireless Mic 2", "Loudspeaker 1"]
    assert cp.suggested_label(c, "wirelessMic") == "Wireless Mic 3"
    c2 = cp.create_config("n", "x")
    c2 = cp.add_device(c2, cp.create_wireless_mic("A", "Dup"))
    c2 = cp.add_device(c2, cp.create_wireless_mic("B", "Dup"))
    c2 = cp.add_device(c2, cp.create_wireless_mic("C", "  "))
    w = codes(cp.validate(c2).warnings)
    assert "NAMING_DUPLICATE_LABEL" in w and "NAMING_EMPTY_LABEL" in w


def test_routing_summary():
    c = cp.create_config("r", "x")
    c = cp.add_device(c, cp.create_processor("P", "DSP"))
    c = cp.add_device(c, cp.create_wireless_mic("M", "Mic"))
    c = cp.add_device(c, cp.create_loudspeaker("L", "Spk"))
    c = cp.route(c, "M-out-dante-1", "P-in-dante-1")
    c = cp.route(c, "P-out-analog-1", "L-in-analog-1")
    dante = cp.dante_subscriptions(c)
    assert len(dante) == 1 and dante[0].from_device_label == "Mic"
    assert cp.routing_summary(c) == {"dante": 1, "analog": 1, "total": 2}
    assert len(cp.signal_flow_report(c).split("\n")) == 2


def test_device_templates():
    c = cp.create_config("t", "x")
    c = cp.add_device(c, cp.create_processor("P", "DSP"))
    c = cp.add_dsp_block(c, "P", cp.create_dsp_block("gain", "g1"))
    c = cp.add_dsp_block(c, "P", cp.create_dsp_block("peq4", "q1"))
    tpl = cp.device_template("My DSP", cp.find_device(c, "P"))
    assert tpl.device_type == "processor" and len(tpl.dsp_blocks) == 2
    dev = cp.instantiate_template(tpl, "P2", "DSP 2")
    assert dev.id == "P2" and dev.profile_id == "generic-hardware-dsp"
    assert [b.id for b in dev.dsp_blocks] == ["P2-gain-1", "P2-peq4-2"]
    c2 = cp.add_device(cp.create_config("t2", "x"), dev)
    assert cp.validate(c2).ok


def test_projects():
    p = cp.create_project("Campus", "x")
    assert len(p.rooms) == 1 and cp.get_active_room(p).id == "room-1"
    p = cp.add_room(p, "Boardroom")
    assert len(p.rooms) == 2 and p.active_room_id == "room-2"
    p = cp.rename_room(p, "room-2", "Big Boardroom")
    assert cp.get_active_room(p).config.metadata["name"] == "Big Boardroom"
    p = cp.set_active_room(p, "room-1")
    p = cp.remove_room(p, "room-1")
    assert [r.id for r in p.rooms] == ["room-2"] and p.active_room_id == "room-2"


def test_project_round_trip_and_migration():
    p = cp.create_project("Campus", "x")
    room = cp.get_active_room(p).config
    room = cp.add_device(room, cp.create_microphone_array("A", "Array"))
    p = cp.update_room(p, "room-1", room)
    restored = cp.deserialize_project(cp.serialize_project(p))
    assert restored.rooms[0].config.devices[0].id == "A"
    assert restored.rooms[0].config.version == 2
    # v1 room migration inside project
    doc = json.loads(cp.serialize_project(p))
    doc["rooms"][0]["config"]["version"] = 1
    for d in doc["rooms"][0]["config"]["devices"]:
        d.pop("profileId", None)
    restored2 = cp.deserialize_project(json.dumps(doc))
    assert restored2.rooms[0].config.version == 2
