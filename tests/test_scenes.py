"""Scenes (schema v3): round-trip, v2→v3 migration, capture/recall, validation."""
import json

import pytest

import conf_pipeline as cp
from conf_pipeline.model import Point2D, RectShape


def _scene_config():
    """A config with an array + pickup zone, a mute group, and one scene."""
    c = cp.create_config("Room", "2026-06-12T00:00:00Z")
    c = cp.add_device(c, cp.create_microphone_array("A", "Array"))
    c = cp.add_coverage_zone(c, "A", cp.CoverageZone("z1", "dynamic", RectShape(Point2D(1, 1), 2, 2), False, "Table"))
    c = cp.add_mute_group(c, cp.create_mute_group("mg1", "Room mute", device_ids=["A"]))
    scene = cp.create_scene(
        "s1", "Lecture",
        mute_states={"mg1": True},
        zone_states=[cp.SceneZoneState("A", "z1", gain_db=-3.0, active=True)],
        steer=[cp.SceneSteer("A", 90.0, 75.0)],
    )
    return cp.add_scene(c, scene)


# --- schema round-trip ---
def test_scene_round_trips_with_camelcase_schema():
    c = _scene_config()
    text = cp.serialize(c)
    d = json.loads(text)
    assert d["version"] == cp.CONFIG_VERSION
    s = d["control"]["scenes"][0]
    assert s["muteStates"] == {"mg1": True}
    assert s["zoneStates"][0] == {"arrayId": "A", "zoneId": "z1", "gainDb": -3.0, "active": True}
    assert s["steer"][0] == {"arrayId": "A", "azimuthDeg": 90.0, "offNadirDeg": 75.0}
    restored = cp.deserialize(text)
    assert cp.serialize(restored) == text                        # lossless
    rs = cp.get_scene(restored, "s1")
    assert rs is not None and rs.label == "Lecture"
    assert rs.zone_states[0].gain_db == -3.0 and rs.zone_states[0].active is True
    assert rs.steer[0].azimuth_deg == 90.0


def test_partial_scene_omits_unset_fields():
    c = cp.create_config("Room", "x")
    c = cp.add_device(c, cp.create_microphone_array("A", "Array"))
    c = cp.add_coverage_zone(c, "A", cp.CoverageZone("z1", "dynamic", RectShape(Point2D(1, 1), 2, 2), False, "T"))
    c = cp.add_scene(c, cp.create_scene("s1", "Gains only", zone_states=[cp.SceneZoneState("A", "z1", gain_db=-6.0)]))
    s = json.loads(cp.serialize(c))["control"]["scenes"][0]
    assert s["zoneStates"][0] == {"arrayId": "A", "zoneId": "z1", "gainDb": -6.0}   # no "active" key
    assert cp.serialize(cp.deserialize(cp.serialize(c))) == cp.serialize(c)


# --- migration ---
def test_v2_file_upgrades_losslessly():
    c = _scene_config()
    doc = json.loads(cp.serialize(c))
    doc["version"] = 2
    del doc["control"]["scenes"]                                 # a genuine v2 file
    v2_text = json.dumps(doc)
    restored = cp.deserialize(v2_text)
    assert restored.version == cp.CONFIG_VERSION
    # every v2 fact survived: devices, zones, mute groups
    assert cp.find_device(restored, "A").zones[0].id == "z1"
    assert restored.control.mute_groups[0].id == "mg1"
    assert restored.control.scenes == []                         # additive default
    # round-trip of the upgraded config differs from v2 only by version + scenes
    up = json.loads(cp.serialize(restored))
    assert up["version"] == cp.CONFIG_VERSION and up["control"]["scenes"] == []
    up["version"] = 2
    del up["control"]["scenes"]
    assert up == json.loads(v2_text)


def test_v1_chain_migrates_to_current():
    c = cp.create_config("m", "x")
    c = cp.add_device(c, cp.create_processor("P", "DSP"))
    doc = json.loads(cp.serialize(c))
    doc["version"] = 1
    for d in doc["devices"]:
        d.pop("profileId", None)
        d.pop("dspBlocks", None)
    restored = cp.deserialize(json.dumps(doc))
    assert restored.version == cp.CONFIG_VERSION
    assert cp.find_device(restored, "P").profile_id is not None  # v1→v2 step still ran


def test_unsupported_version_rejected():
    doc = json.loads(cp.serialize(cp.create_config("x", "y")))
    doc["version"] = 99
    with pytest.raises(cp.DeserializeError):
        cp.deserialize(json.dumps(doc))


# --- capture / recall ---
def test_capture_then_recall_round_trips_the_control_surface():
    c = _scene_config()
    c = cp.set_zone_gain_db(c, "A", "z1", -9.0)
    c = cp.set_mute_group_muted(c, "mg1", True)
    cap = cp.capture_scene(c, "snap", "As it is now")
    assert cap.mute_states == {"mg1": True}
    assert cap.zone_states[0].gain_db == -9.0 and cap.zone_states[0].active is None
    c = cp.add_scene(c, cap)
    # drift the surface, then recall the snapshot
    c = cp.set_zone_gain_db(c, "A", "z1", 0.0)
    c = cp.set_mute_group_muted(c, "mg1", False)
    back = cp.recall_scene(c, "snap")
    assert cp.find_device(back, "A").zones[0].gain_db == -9.0
    assert back.control.mute_groups[0].muted is True


def test_recall_applies_mutes_and_gains_and_skips_dangling():
    c = _scene_config()
    rec = cp.recall_scene(c, "s1")
    assert rec.control.mute_groups[0].muted is True
    assert cp.find_device(rec, "A").zones[0].gain_db == -3.0
    assert cp.find_device(rec, "A").zones[0].always_on is False  # type invariant untouched
    # a scene referencing vanished things recalls without error
    c2 = cp.add_scene(
        _scene_config(),
        cp.create_scene("ghost", "Ghost", mute_states={"gone": True},
                        zone_states=[cp.SceneZoneState("nope", "z9", gain_db=-1.0)]),
    )
    rec2 = cp.recall_scene(c2, "ghost")
    assert rec2.control.mute_groups[0].muted is False            # untouched


def test_recall_unknown_scene_raises():
    with pytest.raises(ValueError):
        cp.recall_scene(_scene_config(), "missing")


def test_recall_is_pure_and_idempotent():
    c = _scene_config()
    once = cp.recall_scene(c, "s1")
    assert cp.find_device(c, "A").zones[0].gain_db is None       # input untouched
    assert cp.serialize(cp.recall_scene(once, "s1")) == cp.serialize(once)


def test_scene_management_and_mute_group_coexistence():
    c = _scene_config()
    with pytest.raises(ValueError):
        cp.add_scene(c, cp.create_scene("s1", "dup"))
    # mute-group edits must not drop scenes (ControlConfig is rebuilt there)
    c = cp.add_mute_group(c, cp.create_mute_group("mg2", "Second", device_ids=["A"]))
    c = cp.set_mute_group_muted(c, "mg2", True)
    c = cp.remove_mute_group(c, "mg2")
    assert cp.get_scene(c, "s1") is not None
    # and scene removal keeps mute groups
    c = cp.remove_scene(c, "s1")
    assert cp.get_scene(c, "s1") is None
    assert c.control.mute_groups[0].id == "mg1"
    assert cp.remove_scene(c, "s1") is c                         # idempotent no-op


# --- validation ---
def test_scene_validation_flags_dangling_and_empty_and_dupes():
    c = _scene_config()
    assert not [e for e in cp.validate(c).errors if e.code == "SCENE_INVALID"]
    bad = cp.add_scene(c, cp.create_scene("empty", "Empty"))
    bad = cp.add_scene(bad, cp.create_scene(
        "dangling", "Dangling",
        mute_states={"gone": True},
        zone_states=[cp.SceneZoneState("ghostArray", "z9")],
        steer=[cp.SceneSteer("ghostArray", 0.0)],
    ))
    codes = [e for e in cp.validate(bad).errors if e.code == "SCENE_INVALID"]
    msgs = " | ".join(e.message for e in codes)
    assert len(codes) == 4                                       # empty + group + zone + steer
    assert "is empty" in msgs and 'missing mute group "gone"' in msgs
    assert 'missing array "ghostArray"' in msgs and "steers a missing array" in msgs


def test_duplicate_scene_ids_flagged_by_validation():
    c = _scene_config()
    ctrl = c.control
    ctrl.scenes = [*ctrl.scenes, cp.create_scene("s1", "dup", mute_states={"mg1": False})]
    codes = [e for e in cp.validate(c).errors if e.code == "SCENE_INVALID"]
    assert any("Duplicate scene id" in e.message for e in codes)
