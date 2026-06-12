"""DeviceTransport interface + SimulatedTransport backend (no hardware, no I/O)."""
import pytest

import conf_pipeline as cp


def _room_devices():
    return [
        cp.create_processor("P1", "DSP"),
        cp.create_microphone_array("A1", "Ceiling Array"),
        cp.create_loudspeaker("L1", "Speaker", "analog"),
    ]


def _transport(**kw):
    return cp.SimulatedTransport(_room_devices(), **kw)


# --- discovery ---
def test_discover_lists_online_devices_deterministically():
    t = _transport()
    found = t.discover()
    assert [d.id for d in found] == ["A1", "L1", "P1"]          # sorted by id
    by_id = {d.id: d for d in found}
    assert by_id["A1"].device_type == "microphoneArray"
    assert by_id["A1"].label == "Ceiling Array"
    assert by_id["P1"].address == "sim://P1"
    assert by_id["P1"].firmware == "1.0.0-sim"


def test_offline_device_disappears_from_discovery():
    t = _transport()
    t.set_offline("A1")
    assert [d.id for d in t.discover()] == ["L1", "P1"]
    t.set_offline("A1", False)
    assert [d.id for d in t.discover()] == ["A1", "L1", "P1"]


# --- connection lifecycle ---
def test_connect_disconnect_bookkeeping():
    t = _transport()
    t.connect("P1")
    t.connect("A1")
    t.connect("P1")                                              # idempotent
    assert t.connected_ids == ("A1", "P1")
    assert t.is_connected("P1") and not t.is_connected("L1")
    t.disconnect("P1")
    assert t.connected_ids == ("A1",)
    t.disconnect("P1")                                           # idempotent
    t.disconnect_all()
    assert t.connected_ids == ()


def test_connect_unknown_or_offline_raises():
    t = _transport()
    with pytest.raises(cp.TransportError):
        t.connect("nope")
    t.set_offline("A1")
    with pytest.raises(cp.TransportError):
        t.connect("A1")


def test_going_offline_drops_an_open_connection():
    t = _transport()
    t.connect("A1")
    t.set_offline("A1")                                          # unplugged mid-session
    assert not t.is_connected("A1")
    with pytest.raises(cp.TransportError):
        t.read_config("A1")


def test_context_manager_disconnects_everything():
    t = _transport()
    with t:
        t.connect("P1")
        t.connect("L1")
        assert t.connected_ids == ("L1", "P1")
    assert t.connected_ids == ()


# --- config I/O ---
def test_read_config_requires_connection_and_isolates_copies():
    t = _transport()
    with pytest.raises(cp.TransportError):
        t.read_config("A1")
    t.connect("A1")
    dev = t.read_config("A1")
    assert dev.id == "A1" and dev.label == "Ceiling Array"
    dev.label = "Mutated"                                        # caller-side mutation…
    assert t.read_config("A1").label == "Ceiling Array"          # …never reaches the device


def test_push_config_round_trips():
    t = _transport()
    t.connect("A1")
    dev = t.read_config("A1")
    dev.label = "Boardroom North"
    t.push_config(dev)
    assert t.read_config("A1").label == "Boardroom North"
    assert t.push_count == 1


def test_push_requires_connection():
    t = _transport()
    with pytest.raises(cp.TransportError):
        t.push_config(cp.create_microphone_array("A1", "Ceiling Array"))


def test_seeded_drift_then_push_reconciles():
    """The A3 seed story: the device reports an older config than the design;
    pushing the designed device brings the device-side into line."""
    designed = cp.create_microphone_array("A1", "Ceiling Array (rev B)")
    drifted = cp.create_microphone_array("A1", "Ceiling Array (rev A)")
    t = cp.SimulatedTransport([drifted])
    t.connect("A1")
    assert t.read_config("A1").label != designed.label           # drift visible
    t.push_config(designed)
    assert t.read_config("A1").label == designed.label           # reconciled


# --- status ---
def test_read_status_works_without_connection():
    t = _transport()
    s = t.read_status("A1")
    assert s.online and not s.connected
    assert s.firmware == "1.0.0-sim" and s.detail == "simulated"
    t.connect("A1")
    assert t.read_status("A1").connected
    t.set_offline("A1")
    s = t.read_status("A1")
    assert not s.online and not s.connected
    unknown = t.read_status("ghost")
    assert not unknown.online and unknown.detail == "unknown device"


# --- simulation controls ---
def test_duplicate_or_unknown_simulated_ids_are_rejected():
    with pytest.raises(ValueError):
        cp.SimulatedTransport([cp.create_processor("P1", "a"), cp.create_processor("P1", "b")])
    t = _transport()
    with pytest.raises(ValueError):
        t.set_offline("ghost")
    with pytest.raises(ValueError):
        t.add_device(cp.create_processor("P1", "again"))


def test_add_device_appears_in_next_discovery():
    t = _transport()
    t.add_device(cp.create_codec("C1", "Codec"))
    assert "C1" in [d.id for d in t.discover()]
    assert t.has_device("C1") and not t.has_device("ghost")
    t.connect("C1")
    assert t.read_config("C1").label == "Codec"


# --- online-room status (A2): design vs last-deploy vs transport ---
def _design():
    c = cp.create_config("Room", "2026-06-12T00:00:00Z")
    c = cp.add_device(c, cp.create_processor("P1", "DSP"))
    c = cp.add_device(c, cp.create_microphone_array("A1", "Ceiling Array"))
    return c


def test_online_status_never_deployed_marks_all_new():
    c = _design()
    t = cp.SimulatedTransport(c.devices)
    rows = cp.online_room_status(c, None, t)
    assert [r.device_id for r in rows] == ["A1", "P1"]   # sorted
    assert all(r.new_since_deploy and not r.changed_since_deploy for r in rows)
    assert all(r.online and not r.connected for r in rows)
    assert not any(r.in_sync for r in rows)              # new ⇒ not in sync


def test_online_status_changed_and_new_since_deploy():
    deployed = _design()
    t = cp.SimulatedTransport(deployed.devices)
    c = cp.rename_device(deployed, "A1", "Ceiling Array (moved)")   # changed
    c = cp.add_device(c, cp.create_loudspeaker("L1", "Speaker", "analog"))  # new
    rows = {r.device_id: r for r in cp.online_room_status(c, deployed, t)}
    assert rows["A1"].changed_since_deploy and not rows["A1"].new_since_deploy
    assert rows["L1"].new_since_deploy and not rows["L1"].online    # not installed
    assert rows["P1"].in_sync                                       # untouched + online


def test_online_status_reflects_connection_and_offline():
    c = _design()
    t = cp.SimulatedTransport(c.devices)
    t.connect("P1")
    t.set_offline("A1")
    rows = {r.device_id: r for r in cp.online_room_status(c, c, t)}
    assert rows["P1"].connected and rows["P1"].online
    assert not rows["A1"].online and not rows["A1"].connected
    assert not rows["A1"].in_sync                                   # offline ⇒ not in sync
