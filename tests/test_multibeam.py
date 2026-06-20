"""Multi-talker "capture everyone" planner — pure (no numpy / no audio) tests.

Covers the two novel control pieces: the hybrid seat-snap (`snap_targets`) and the persistent
beam-slot assignment + hold (`BeamSlotTracker`). The realtime mixer / controller get their own tests.
"""
import conf_pipeline as cp
from conf_pipeline.model import Point2D, RoomLayout, RoomObject, SeatAnchor
from conf_pipeline_control.multibeam import (
    BeamSlotTracker,
    BeamTarget,
    snap_targets,
)


def _config_with_seats(bearing=0.0):
    """Array at origin (bearing settable) + two seats: seat1 due +Y (bearing 0), seat2 due +X (90)."""
    c = cp.create_config("rt", "2026-01-01T00:00:00Z")
    c = cp.add_device(c, cp.create_microphone_array("A", "Array", position=Point2D(0.0, 0.0)))
    if bearing is not None:
        c = cp.set_array_bearing(c, "A", bearing)
    c.room = RoomLayout(
        vertices=[Point2D(-3, -3), Point2D(3, -3), Point2D(3, 3), Point2D(-3, 3)],
        height=3.0, units="meters",
        objects=[RoomObject(id="sofa", kind="sofa", position=Point2D(0.0, 3.0),
                            seats=[SeatAnchor(position=Point2D(0.0, 3.0)),    # sofa-seat1 -> bearing 0
                                   SeatAnchor(position=Point2D(3.0, 0.0))])], # sofa-seat2 -> bearing 90
    )
    return c


# --------------------------------------------------------------------------- snap_targets
def test_snap_targets_maps_detections_to_seats():
    c = _config_with_seats()
    out = snap_targets(c, "A", [(0.0, 10.0), (90.0, 8.0)])
    assert [(t.seat_id, round(t.azimuth_deg), t.salience_db) for t in out] == [
        ("sofa-seat1", 0, 10.0), ("sofa-seat2", 90, 8.0)]


def test_snap_targets_falls_back_to_free_doa_when_far_from_seats():
    c = _config_with_seats()
    out = snap_targets(c, "A", [(200.0, 5.0)])               # 110-160 deg from either seat -> no snap
    assert out == [BeamTarget(200.0, None, 5.0)]


def test_snap_targets_off_keeps_raw_doa():
    c = _config_with_seats()
    out = snap_targets(c, "A", [(0.0, 10.0)], snap=False)
    assert out == [BeamTarget(0.0, None, 10.0)]


def test_snap_targets_free_when_no_bearing_or_no_array():
    assert snap_targets(_config_with_seats(bearing=None), "A", [(0.0, 9.0)]) == [BeamTarget(0.0, None, 9.0)]
    assert snap_targets(_config_with_seats(), None, [(0.0, 9.0)]) == [BeamTarget(0.0, None, 9.0)]


def test_snap_targets_skips_none_azimuth():
    assert snap_targets(_config_with_seats(), "A", [(None, 9.0), (90.0, 8.0)]) == \
        [BeamTarget(90.0, "sofa-seat2", 8.0)]


# --------------------------------------------------------------------------- BeamSlotTracker
def _active(slots):
    return [(s.azimuth_deg, s.seat_id) for s in slots if s.active]


def test_two_separated_targets_get_two_slots():
    tr = BeamSlotTracker(n_slots=3)
    slots = tr.update([BeamTarget(0.0, "s1", 10.0), BeamTarget(90.0, "s2", 8.0)], t=0.0)
    assert sorted(_active(slots)) == [(0.0, "s1"), (90.0, "s2")]
    assert sum(s.active for s in slots) == 2 and sum(s.azimuth_deg is None for s in slots) == 1


def test_slot_persists_by_seat_identity_across_ticks():
    tr = BeamSlotTracker(n_slots=3)
    s0 = tr.update([BeamTarget(0.0, "s1", 10.0)], t=0.0)
    idx = next(s.index for s in s0 if s.active)
    s1 = tr.update([BeamTarget(7.0, "s1", 10.0)], t=0.1)     # same seat, drifted bearing
    same = next(s for s in s1 if s.index == idx)
    assert same.active and same.seat_id == "s1" and same.azimuth_deg == 7.0


def test_free_doa_matches_by_bearing_then_splits_when_far():
    tr = BeamSlotTracker(n_slots=3, match_radius_deg=25.0)
    idx = next(s.index for s in tr.update([BeamTarget(50.0, None, 10.0)], t=0.0) if s.active)
    s1 = tr.update([BeamTarget(60.0, None, 10.0)], t=0.1)    # within 25 deg -> same slot
    assert next(s for s in s1 if s.index == idx).azimuth_deg == 60.0
    s2 = tr.update([BeamTarget(120.0, None, 10.0)], t=0.2)   # >25 deg from 60 -> a different slot
    assert next(s for s in s2 if s.active).index != idx


def test_hold_then_release():
    tr = BeamSlotTracker(n_slots=2, hold_seconds=0.6)
    tr.update([BeamTarget(0.0, "s1", 10.0)], t=0.0)
    held = tr.update([], t=0.3)                              # within hold -> coasting
    h = next(s for s in held if s.azimuth_deg is not None)
    assert h.held and not h.active and h.azimuth_deg == 0.0
    rel = tr.update([], t=1.0)                               # past hold -> released
    assert all(s.azimuth_deg is None for s in rel)


def test_capacity_keeps_the_loudest():
    tr = BeamSlotTracker(n_slots=2)
    slots = tr.update([BeamTarget(0.0, None, 5.0), BeamTarget(90.0, None, 9.0),
                       BeamTarget(180.0, None, 7.0)], t=0.0)
    assert sorted(round(a) for a, _ in _active(slots)) == [90, 180]   # the 5 dB talker is dropped


def test_new_target_fills_an_idle_slot_keeping_the_existing_one():
    tr = BeamSlotTracker(n_slots=3)
    s0 = tr.update([BeamTarget(0.0, "s1", 10.0)], t=0.0)
    idx = next(s.index for s in s0 if s.active)
    s1 = tr.update([BeamTarget(0.0, "s1", 10.0), BeamTarget(90.0, "s2", 8.0)], t=0.1)
    assert next(s for s in s1 if s.index == idx).seat_id == "s1"      # original slot kept
    assert sorted(_active(s1)) == [(0.0, "s1"), (90.0, "s2")]
