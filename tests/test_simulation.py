"""Placement-simulation engine tests — pure engine, numpy-free by default.

The numpy/pyroomacoustics validation path is exercised only when those optional
extras are installed (guarded by ``pytest.importorskip``), so the base suite
stays green with just PySide6 + pytest.
"""
import math

import pytest

import conf_pipeline as cp
from conf_pipeline.model import Point2D, RectShape, point_in_shape
from conf_pipeline.sim import scoring


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _config(width=8, depth=6, height=3.0):
    c = cp.create_config("sim", "2026-06-09T00:00:00Z")
    c = cp.set_room(c, cp.rectangular_room(width, depth, height))
    c = cp.add_device(c, cp.create_microphone_array("A", "Array", "automatic"))
    return c


# --------------------------------------------------------------------------- #
# 1. array sits ~above a single talker (array-only mode)
# --------------------------------------------------------------------------- #
def test_array_recommended_above_single_talker():
    c = _config()
    c = cp.add_talker(c, cp.create_talker("T1", "Presenter", Point2D(5, 3)))
    rec = cp.recommend_placement(c, "A", talker_id=None, params=cp.SimParams(grid_step_m=0.5))
    assert abs(rec.array_pos.x - 5) <= 0.6
    assert abs(rec.array_pos.y - 3) <= 0.6
    # array elevation defaults to the ceiling (room height)
    assert rec.array_elev == pytest.approx(3.0)


# --------------------------------------------------------------------------- #
# 2. recommended seat avoids an exclusion zone and stays in the room; the
#    jointly-placed array ends up directly above the seat
# --------------------------------------------------------------------------- #
def test_seat_recommended_avoids_exclusion_and_aligns_with_array():
    c = _config(width=10, depth=8)
    excl = cp.exclusion_zone("x1", "dead", RectShape(origin=Point2D(0, 0), width=4, height=8))
    c = cp.add_coverage_zone(c, "A", excl)
    c = cp.add_talker(c, cp.create_talker("T1", "Speaker", Point2D(7, 4)))
    rec = cp.recommend_placement(c, "A", talker_id="T1", params=cp.SimParams(grid_step_m=0.5))

    assert rec.talker_pos is not None
    assert not point_in_shape(rec.talker_pos, excl.shape)            # not seated in the dead zone
    from conf_pipeline.model import point_in_polygon
    assert point_in_polygon(rec.talker_pos, c.room.vertices)         # inside the room
    # the joint optimum mounts the array straight above the seat
    assert abs(rec.array_pos.x - rec.talker_pos.x) <= 0.2
    assert abs(rec.array_pos.y - rec.talker_pos.y) <= 0.2


# --------------------------------------------------------------------------- #
# 3. fairness pulls the array toward the midpoint of two opposed talkers
# --------------------------------------------------------------------------- #
def test_fairness_places_array_between_two_talkers():
    c = _config(width=10, depth=6)
    c = cp.add_talker(c, cp.create_talker("T1", "Left", Point2D(2, 3)))
    c = cp.add_talker(c, cp.create_talker("T2", "Right", Point2D(8, 3)))
    rec = cp.recommend_placement(c, "A", talker_id=None, params=cp.SimParams(grid_step_m=0.5))
    assert abs(rec.array_pos.x - 5) <= 0.6
    assert abs(rec.array_pos.y - 3) <= 0.6
    # both talkers end up similarly well served
    totals = [s.total for s in rec.per_talker.values()]
    assert max(totals) - min(totals) < 0.1


# --------------------------------------------------------------------------- #
# 4. RT60 / DRR monotonicity (pure arithmetic, no numpy)
# --------------------------------------------------------------------------- #
def test_rt60_grows_with_volume():
    small = cp.set_room(cp.create_config("s", "x"), cp.rectangular_room(4, 3, 2.5))
    big = cp.set_room(cp.create_config("b", "x"), cp.rectangular_room(12, 10, 4))
    assert cp.estimated_rt60(big) > cp.estimated_rt60(small)


def test_drr_decreases_with_distance():
    c = _config()
    dc = scoring._critical_distance(c, cp.SimParams())
    near = scoring.drr_db(1.5, dc)
    far = scoring.drr_db(5.0, dc)
    assert near is not None and far is not None and near > far


def test_estimated_rt60_uses_supplied_value():
    c = _config()
    assert cp.estimated_rt60(c, cp.SimParams(rt60_s=0.9)) == pytest.approx(0.9)


# --------------------------------------------------------------------------- #
# 5. purity + determinism
# --------------------------------------------------------------------------- #
def test_recommend_does_not_mutate_config():
    c = _config()
    c = cp.add_talker(c, cp.create_talker("T1", "P", Point2D(4, 3)))
    before = cp.serialize(c)
    cp.recommend_placement(c, "A", talker_id="T1")
    cp.score_heatmap(c, "A")
    assert cp.serialize(c) == before


def test_recommend_is_deterministic():
    c = _config()
    c = cp.add_talker(c, cp.create_talker("T1", "P", Point2D(4, 3)))
    r1 = cp.recommend_placement(c, "A", talker_id="T1")
    r2 = cp.recommend_placement(c, "A", talker_id="T1")
    assert r1 == r2


# --------------------------------------------------------------------------- #
# 6. edge cases
# --------------------------------------------------------------------------- #
def test_no_room_still_recommends():
    c = cp.create_config("nr", "x")
    c = cp.add_device(c, cp.create_microphone_array("A", "Array", "automatic"))
    c = cp.add_talker(c, cp.create_talker("T1", "P", Point2D(3, 2)))
    rec = cp.recommend_placement(c, "A", talker_id="T1")
    assert rec.array_pos is not None and rec.talker_pos is not None


def test_no_talkers_sets_note_and_does_not_crash():
    c = _config()
    rec = cp.recommend_placement(c, "A", talker_id=None)
    assert rec.note  # non-empty caveat
    hm = cp.score_heatmap(c, "A")
    assert hm.nx > 0 and hm.ny > 0


def test_talker_outside_room_is_seated_inside():
    c = _config(width=8, depth=6)
    c = cp.add_talker(c, cp.create_talker("T1", "Stray", Point2D(100, 100)))
    rec = cp.recommend_placement(c, "A", talker_id="T1")
    from conf_pipeline.model import point_in_polygon
    assert point_in_polygon(rec.talker_pos, c.room.vertices)


def test_no_array_raises():
    c = cp.create_config("na", "x")
    with pytest.raises(ValueError):
        cp.recommend_placement(c, None)


# --------------------------------------------------------------------------- #
# 6b. regression guards (from the adversarial review)
# --------------------------------------------------------------------------- #
def test_seat_never_in_exclusion_when_a_valid_seat_exists():
    # talker currently sits *inside* a large exclusion zone; the recommended seat
    # must move out of it (covers the _best_seat fallback + refinement-regression bugs)
    c = _config(width=10, depth=8)
    excl = cp.exclusion_zone("x1", "dead", RectShape(origin=Point2D(0, 0), width=5, height=8))
    c = cp.add_coverage_zone(c, "A", excl)
    c = cp.add_talker(c, cp.create_talker("T1", "Stuck", Point2D(2, 4)))  # inside exclusion
    rec = cp.recommend_placement(c, "A", talker_id="T1")
    assert not point_in_shape(rec.talker_pos, excl.shape)
    assert not rec.score.in_exclusion_zone


def test_zero_refine_step_does_not_crash():
    c = _config()
    c = cp.add_talker(c, cp.create_talker("T1", "P", Point2D(4, 3)))
    rec = cp.recommend_placement(c, "A", talker_id="T1", params=cp.SimParams(refine_step_m=0.0))
    assert rec.talker_pos is not None


def test_score_placement_matches_recommend_after_relocation():
    # the seated talker is relocated away from its stored position; score_placement
    # must reproduce recommend_placement's score when told which talker moved
    c = _config(width=10, depth=6)
    c = cp.add_talker(c, cp.create_talker("T1", "Mover", Point2D(8, 3)))
    c = cp.add_talker(c, cp.create_talker("T2", "Fixed", Point2D(2, 3)))
    rec = cp.recommend_placement(c, "A", talker_id="T1")
    cand = cp.Candidate(
        array_pos=rec.array_pos, array_elev=rec.array_elev,
        steer_off_nadir_deg=rec.steer_off_nadir_deg, steer_az_deg=rec.steer_az_deg,
        talker_pos=rec.talker_pos, talker_elev=1.2,
    )
    ps = cp.score_placement(c, "A", cand, talker_id="T1")
    assert ps.total == pytest.approx(rec.score.total, abs=1e-9)
    assert ps.fairness == pytest.approx(rec.score.fairness, abs=1e-9)


# --------------------------------------------------------------------------- #
# 9. table-aware seating + multi-array fairness
# --------------------------------------------------------------------------- #
def test_seat_lands_at_the_table_when_pickup_zone_defined():
    # a "table" is modelled as a pickup zone; the presenter should be seated in it
    c = _config(width=12, depth=8)
    table = cp.dynamic_zone("A-tbl", "Table", RectShape(origin=Point2D(4, 3), width=4, height=2))
    c = cp.add_coverage_zone(c, "A", table)
    c = cp.add_talker(c, cp.create_talker("T1", "Presenter", Point2D(1, 7)))  # currently off the table
    rec = cp.recommend_placement(c, "A", talker_id="T1")
    assert point_in_shape(rec.talker_pos, table.shape)


def test_no_pickup_zone_leaves_seating_unconstrained():
    # without any pickup zone, seating is the whole (non-excluded) room
    c = _config(width=10, depth=8)
    c = cp.add_talker(c, cp.create_talker("T1", "P", Point2D(8, 6)))
    rec = cp.recommend_placement(c, "A", talker_id="T1")
    from conf_pipeline.model import point_in_polygon
    assert point_in_polygon(rec.talker_pos, c.room.vertices)


def test_multi_array_fairness_uses_best_covering_array():
    c = cp.create_config("ma", "x")
    c = cp.set_room(c, cp.rectangular_room(12, 6, 3))
    c = cp.add_device(c, cp.create_microphone_array("A1", "A1", "automatic"))
    c = cp.add_device(c, cp.create_microphone_array("A2", "A2", "automatic"))
    c = cp.set_device_position(c, "A2", Point2D(10, 3))  # A2 fixed directly above the far talker
    c = cp.add_talker(c, cp.create_talker("T1", "near A1", Point2D(2, 3)))
    c = cp.add_talker(c, cp.create_talker("T2", "near A2", Point2D(10, 3)))
    # array-only mode (talkers fixed) so a single array can't just cluster everyone
    multi = cp.recommend_placement(c, "A1", talker_id=None)
    single = cp.recommend_placement(c, "A1", talker_id=None, params=cp.SimParams(consider_all_arrays=False))
    # A2 already covers T2 well, so considering all arrays rates T2 far better than
    # judging it by A1-only coverage (where one array must stretch to reach both)
    assert multi.per_talker["T2"].total > single.per_talker["T2"].total + 0.15


# --------------------------------------------------------------------------- #
# 7. heatmap shape
# --------------------------------------------------------------------------- #
def test_heatmap_covers_grid():
    c = _config()
    c = cp.add_talker(c, cp.create_talker("T1", "P", Point2D(4, 3)))
    hm = cp.score_heatmap(c, "A", params=cp.SimParams(grid_step_m=0.5))
    assert len(hm.values) == hm.nx * hm.ny
    assert hm.vmax >= hm.vmin
    # at least some interior cells scored (non-None)
    assert any(v is not None for v in hm.values)


# --------------------------------------------------------------------------- #
# 8. capability probing + optional numpy validator
# --------------------------------------------------------------------------- #
def test_backend_probing_is_safe():
    assert isinstance(cp.numpy_available(), bool)
    assert isinstance(cp.available_backends(), list)


def test_validator_requires_a_backend():
    c = _config()
    c = cp.add_talker(c, cp.create_talker("T1", "P", Point2D(4, 3)))
    rec = cp.recommend_placement(c, "A", talker_id="T1")
    if not cp.available_backends():
        with pytest.raises(RuntimeError):
            cp.validate_recommendation(c, rec)


def test_farfield_validation_when_numpy_present():
    pytest.importorskip("numpy")
    c = _config()
    c = cp.add_talker(c, cp.create_talker("T1", "P", Point2D(5, 3)))
    c = cp.add_talker(c, cp.create_talker("T2", "Q", Point2D(2, 2)))
    rec = cp.recommend_placement(c, "A", talker_id="T1")
    res = cp.validate_recommendation(c, rec, backend="farfield")
    assert res.backend == "farfield"
    assert math.isfinite(res.predicted_snr_db)
    assert res.n_mics == 8
