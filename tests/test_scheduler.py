"""Scene scheduler: schedule round-trip + validation, and deterministic
clock-driven firing (no sleeps — the scheduler takes an injectable now_fn)."""
import json
from datetime import datetime

import pytest

import conf_pipeline as cp
from conf_pipeline.model import Point2D, RectShape
from conf_pipeline.scheduler import next_fire

TUE_0900 = datetime(2026, 6, 16, 9, 0, 12)     # a Tuesday, mid-minute


def _config():
    c = cp.create_config("Room", "2026-06-12T00:00:00Z")
    c = cp.add_device(c, cp.create_microphone_array("A", "Array"))
    c = cp.add_coverage_zone(c, "A", cp.CoverageZone("z1", "dynamic", RectShape(Point2D(1, 1), 2, 2), False, "T"))
    c = cp.add_mute_group(c, cp.create_mute_group("mg1", "Room", device_ids=["A"]))
    c = cp.add_scene(c, cp.create_scene("meeting", "Meeting", mute_states={"mg1": False},
                                        zone_states=[cp.SceneZoneState("A", "z1", gain_db=-2.0)]))
    c = cp.add_scene(c, cp.create_scene("lecture", "Lecture", mute_states={"mg1": True}))
    return c


def _scheduled(time="09:00", days=("tue",), enabled=True, scene="lecture"):
    return cp.add_scene_schedule(_config(), cp.create_scene_schedule("t1", scene, time, list(days), enabled))


# --- schema (additive on v3) ---
def test_schedule_round_trips_and_is_additive():
    c = _scheduled()
    d = json.loads(cp.serialize(c))
    assert d["version"] == cp.CONFIG_VERSION                       # schedules are additive
    assert d["control"]["schedules"] == [
        {"id": "t1", "sceneId": "lecture", "time": "09:00", "days": ["tue"], "enabled": True}
    ]
    restored = cp.deserialize(cp.serialize(c))
    assert cp.serialize(restored) == cp.serialize(c)
    s = restored.control.schedules[0]
    assert s.scene_id == "lecture" and s.days == ["tue"] and s.enabled
    # a v3 file written before schedules existed still loads
    del d["control"]["schedules"]
    older = cp.deserialize(json.dumps(d))
    assert older.control.schedules == []


def test_schedule_builders_and_dup_guard():
    c = _scheduled()
    with pytest.raises(ValueError):
        cp.add_scene_schedule(c, cp.create_scene_schedule("t1", "meeting", "10:00"))
    c = cp.set_scene_schedule_enabled(c, "t1", False)
    assert c.control.schedules[0].enabled is False
    assert cp.get_scene(c, "lecture") is not None                  # scenes untouched
    c = cp.remove_scene_schedule(c, "t1")
    assert c.control.schedules == []
    assert cp.remove_scene_schedule(c, "t1") is c                  # idempotent no-op


def test_default_days_are_every_day():
    s = cp.create_scene_schedule("d", "meeting", "08:00")
    assert s.days == list(cp.WEEKDAYS)


# --- validation ---
def test_schedule_validation_matrix():
    c = _scheduled()
    assert not [e for e in cp.validate(c).errors if e.code == "SCHEDULE_INVALID"]
    bad = c
    bad = cp.add_scene_schedule(bad, cp.create_scene_schedule("ghost", "nope", "09:00"))
    bad = cp.add_scene_schedule(bad, cp.create_scene_schedule("badtime", "meeting", "24:00"))
    bad = cp.add_scene_schedule(bad, cp.create_scene_schedule("badtime2", "meeting", "9:5"))
    bad = cp.add_scene_schedule(bad, cp.create_scene_schedule("nodays", "meeting", "09:00", days=[]))
    bad = cp.add_scene_schedule(bad, cp.create_scene_schedule("badday", "meeting", "09:00", days=["funday"]))
    msgs = " | ".join(e.message for e in cp.validate(bad).errors if e.code == "SCHEDULE_INVALID")
    assert 'missing scene "nope"' in msgs
    assert 'invalid time "24:00"' in msgs and 'invalid time "9:5"' in msgs
    assert "has no days" in msgs and 'unknown day "funday"' in msgs


def test_duplicate_schedule_ids_flagged():
    c = _scheduled()
    c.control.schedules = [*c.control.schedules, cp.create_scene_schedule("t1", "meeting", "10:00")]
    msgs = [e.message for e in cp.validate(c).errors if e.code == "SCHEDULE_INVALID"]
    assert any("Duplicate schedule id" in m for m in msgs)


# --- firing (clock-driven, no sleeps) ---
def _scheduler(config, now):
    holder = cp.ConfigHolder(config)
    clock = {"now": now}
    sched = cp.SceneScheduler(holder.get, holder.apply, now_fn=lambda: clock["now"])
    return sched, holder, clock


def test_fires_at_matching_minute_and_day():
    sched, holder, _ = _scheduler(_scheduled(), TUE_0900)
    assert sched.run_pending() == ["lecture"]
    assert holder.get().control.mute_groups[0].muted is True       # scene applied


def test_fires_once_per_scheduled_minute_then_rearms():
    sched, holder, clock = _scheduler(_scheduled(), TUE_0900)
    assert sched.run_pending() == ["lecture"]
    clock["now"] = TUE_0900.replace(second=45)
    assert sched.run_pending() == []                               # same minute: deduped
    clock["now"] = datetime(2026, 6, 23, 9, 0, 3)                  # next Tuesday
    assert sched.run_pending() == ["lecture"]                      # re-armed


def test_respects_day_filter_disabled_flag_and_wrong_minute():
    sched, _, clock = _scheduler(_scheduled(days=("wed",)), TUE_0900)
    assert sched.run_pending() == []                               # Tuesday ≠ wed
    sched2, _, _ = _scheduler(_scheduled(enabled=False), TUE_0900)
    assert sched2.run_pending() == []                              # disabled
    sched3, _, clock3 = _scheduler(_scheduled(), TUE_0900.replace(minute=1))
    assert sched3.run_pending() == []                              # 09:01 ≠ 09:00


def test_two_entries_same_minute_both_fire_in_order():
    c = _scheduled()                                                # lecture @ 09:00 tue
    c = cp.add_scene_schedule(c, cp.create_scene_schedule("t2", "meeting", "09:00", ["tue"]))
    sched, holder, _ = _scheduler(c, TUE_0900)
    assert sched.run_pending() == ["lecture", "meeting"]
    assert cp.find_device(holder.get(), "A").zones[0].gain_db == -2.0   # meeting's gain landed


def test_dangling_scene_is_skipped_without_aborting_the_tick():
    c = _scheduled()
    c = cp.add_scene_schedule(c, cp.create_scene_schedule("t0", "vanished", "09:00", ["tue"]))
    c.control.schedules.reverse()                                   # dangling first
    sched, holder, _ = _scheduler(c, TUE_0900)
    assert sched.run_pending() == ["lecture"]                       # the good one still fired
    assert holder.get().control.mute_groups[0].muted is True


def test_next_fire_same_day_and_week_wrap():
    schedules = [cp.create_scene_schedule("a", "s", "10:30", ["tue"]),
                 cp.create_scene_schedule("b", "s", "08:00", ["mon"])]
    nxt = next_fire(schedules, TUE_0900)
    assert nxt == datetime(2026, 6, 16, 10, 30)                     # later today beats next Monday
    nxt2 = next_fire([cp.create_scene_schedule("c", "s", "08:00", ["tue"])], TUE_0900)
    assert nxt2 == datetime(2026, 6, 23, 8, 0)                      # already past → next week
    assert next_fire([cp.create_scene_schedule("d", "s", "08:00", ["tue"], enabled=False)], TUE_0900) is None


def test_scheduler_thread_lifecycle():
    sched, _, _ = _scheduler(_scheduled(), TUE_0900)
    with sched:
        assert sched.running
    assert not sched.running
    sched.stop()                                                    # idempotent


def test_status_endpoint_reports_schedules():
    holder = cp.ConfigHolder(_scheduled())
    with cp.ControlApiServer(holder.get, holder.apply) as srv:
        import urllib.request
        with urllib.request.urlopen(srv.url + "/api/status", timeout=10) as r:
            d = json.loads(r.read().decode())
    assert d["schedules"] == [
        {"id": "t1", "sceneId": "lecture", "time": "09:00", "days": ["tue"], "enabled": True}
    ]
