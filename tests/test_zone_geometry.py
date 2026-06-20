"""Feature D foundation: exclusion-zone azimuths, 'is this azimuth in a pickup zone?', and the
compose_nulls exclusion precedence tier (all pure / hardware-free)."""
import pytest

import conf_pipeline as cp
from conf_pipeline.model import Point2D, RectShape


def _cfg(bearing=0.0):
    c = cp.create_config("Room", "2026-01-01T00:00:00Z")
    c = cp.set_room(c, cp.rectangular_room(8, 8, 3))
    c = cp.add_device(c, cp.create_microphone_array("A", "Array"))
    c = cp.set_device_position(c, "A", Point2D(4, 4))         # array at room centre
    c = cp.set_array_bearing(c, "A", bearing)
    arr = cp.find_device(c, "A")
    arr.zones = [
        cp.CoverageZone("pick", "dynamic", RectShape(Point2D(3.5, 6.0), 1, 1), False, "North seats"),  # +Y
        cp.CoverageZone("door", "exclusion", RectShape(Point2D(6.0, 3.5), 1, 1), False, "Door"),        # +X
    ]
    return c


# --------------------------------------------------------------------------- #
# exclusion_zone_azimuths
# --------------------------------------------------------------------------- #
def test_exclusion_zone_azimuth_points_at_the_door():
    az = cp.exclusion_zone_azimuths(_cfg(), "A")
    assert len(az) == 1
    assert abs(az[0] - 90.0) < 5.0                            # door is due east of the array → array az ≈ 90


def test_exclusion_empty_when_unknown_or_no_zones():
    assert cp.exclusion_zone_azimuths(_cfg(), "ZZ") == []     # unknown array
    c = _cfg()
    cp.find_device(c, "A").zones = []
    assert cp.exclusion_zone_azimuths(c, "A") == []           # no zones


def test_exclusion_empty_when_unposed():
    c = cp.create_config("R", "2026-01-01T00:00:00Z")
    c = cp.add_device(c, cp.create_microphone_array("A", "Array"))   # no position / bearing
    assert cp.exclusion_zone_azimuths(c, "A") == []


# --------------------------------------------------------------------------- #
# azimuth_in_pickup_zone
# --------------------------------------------------------------------------- #
def test_pickup_membership_north_in_door_out():
    c = _cfg()
    assert cp.azimuth_in_pickup_zone(c, "A", 0.0)             # north → the pickup zone
    assert not cp.azimuth_in_pickup_zone(c, "A", 90.0)        # east → the door (exclusion), NOT pickup
    assert not cp.azimuth_in_pickup_zone(c, "A", 180.0)       # south → nothing


def test_pickup_membership_follows_array_bearing():
    c = _cfg(bearing=90.0)                                    # re-mount: the north zone shifts in array frame
    assert cp.azimuth_in_pickup_zone(c, "A", 270.0)           # north zone now at array az 270
    assert not cp.azimuth_in_pickup_zone(c, "A", 0.0)


def test_pickup_false_when_unposed():
    c = cp.create_config("R", "2026-01-01T00:00:00Z")
    c = cp.add_device(c, cp.create_microphone_array("A", "Array"))
    assert not cp.azimuth_in_pickup_zone(c, "A", 0.0)


# --------------------------------------------------------------------------- #
# compose_nulls exclusion tier (Phase-0 #2): detected > exclusion > seats
# --------------------------------------------------------------------------- #
def test_compose_nulls_precedence_detected_exclusion_seats():
    from conf_pipeline_control.polaris_beamformer import compose_nulls
    out = compose_nulls(detected=[30.0], seats=[120.0, 150.0], target_az=0.0, budget=3, exclusion=[60.0])
    assert out == [30.0, 60.0, 120.0]                         # detected, then door, then one seat (budget 3)


def test_compose_nulls_exclusion_outranks_seats():
    from conf_pipeline_control.polaris_beamformer import compose_nulls
    assert compose_nulls(detected=[], seats=[120.0], target_az=0.0, budget=1, exclusion=[60.0]) == [60.0]


def test_compose_nulls_full_budget_drops_the_door():
    from conf_pipeline_control.polaris_beamformer import compose_nulls
    # one detected interferer + budget 1 → the door null can't fit (surfaced by the caller, not silent here)
    assert compose_nulls(detected=[30.0], seats=[], target_az=0.0, budget=1, exclusion=[60.0]) == [30.0]


def test_compose_nulls_backward_compatible_without_exclusion():
    from conf_pipeline_control.polaris_beamformer import compose_nulls
    assert compose_nulls([30.0], [120.0], 0.0, 2) == [30.0, 120.0]   # unchanged when no exclusion passed


# --------------------------------------------------------------------------- #
# auto-steer zone-cut policy (the pure helper the control loop uses)
# --------------------------------------------------------------------------- #
def test_apply_zone_cut_keeps_in_zone_nulls_rest_and_door():
    from conf_pipeline_control.autosteer import _apply_zone_cut
    c = _cfg()                                              # pickup north (az 0), door east (az ~90)
    keep, nulls = _apply_zone_cut(c, "A", in_az=[0.0, 180.0], out_az=[200.0])
    assert keep == [0.0]                                    # north (in pickup) kept; south dropped
    assert 180.0 in nulls and 200.0 in nulls               # south + the prior out-of-sector are nulled
    assert any(abs(n - 90.0) < 5.0 for n in nulls)         # the door (east exclusion) is nulled too
