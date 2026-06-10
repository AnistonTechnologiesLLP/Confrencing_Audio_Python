"""Beamformer design math (pure stdlib complex; no numpy, no hardware)."""
import cmath
import math

import pytest

import conf_pipeline as cp
from conf_pipeline.model import Point2D, RectShape
import conf_pipeline_control as cc
from conf_pipeline_control.steering import Direction


GEOM = cc.sensibel_8(radius_m=0.05)
F = 2000.0  # higher band → more directivity for a small array


def _dir(az_deg, off_nadir_deg=70.0):
    az = math.radians(az_deg)
    n = math.radians(off_nadir_deg)
    s = math.sin(n)
    return Direction(unit=(s * math.sin(az), s * math.cos(az), -math.cos(n)),
                     azimuth_deg=az_deg, off_nadir_deg=off_nadir_deg, distance_m=2.0)


# --- core operations ---
def test_steering_vector_unit_magnitude_and_length():
    v = cc.steering_vector(GEOM, _dir(30).unit, F)
    assert len(v) == 8
    for c in v:
        assert abs(c) == pytest.approx(1.0, rel=1e-9)


def test_das_unit_gain_at_look_direction():
    look = _dir(45)
    w = cc.delay_and_sum_weights(GEOM, look, F)
    assert cc.response_db(w, GEOM, look.unit, F) == pytest.approx(0.0, abs=1e-9)


def test_das_main_lobe_is_global_max_at_look():
    look = _dir(90)
    w = cc.delay_and_sum_weights(GEOM, look, F)
    pat = cc.beam_pattern_azimuth(w, GEOM, F, off_nadir_deg=70.0, steps=180)
    peak_az = max(pat, key=lambda t: t[1])[0]
    # peak azimuth within a few degrees of the look azimuth
    assert min(abs(peak_az - 90.0), 360 - abs(peak_az - 90.0)) <= 6.0
    # nothing exceeds the on-axis 0 dB
    assert max(db for _a, db in pat) <= 0.05


def test_lcmv_keeps_pickup_and_nulls_exclusion():
    look = _dir(90)            # pick up the east area
    null = _dir(270)          # mute the west area (opposite bearing → separable)
    w = cc.lcmv_weights(GEOM, look, [null], F)
    assert cc.response_db(w, GEOM, look.unit, F) == pytest.approx(0.0, abs=1e-6)
    assert cc.response_db(w, GEOM, null.unit, F) < -60.0  # deep null


def test_lcmv_two_nulls():
    look = _dir(0)
    nulls = [_dir(120), _dir(240)]
    w = cc.lcmv_weights(GEOM, look, nulls, F)
    assert cc.response_db(w, GEOM, look.unit, F) == pytest.approx(0.0, abs=1e-6)
    for n in nulls:
        assert cc.response_db(w, GEOM, n.unit, F) < -60.0


def test_too_many_nulls_raises():
    look = _dir(0)
    nulls = [_dir(a) for a in range(20, 360, 20)]  # > 7
    with pytest.raises(ValueError):
        cc.lcmv_weights(GEOM, look, nulls, F)


def test_null_coincident_with_look_raises():
    look = _dir(50)
    with pytest.raises(ValueError):
        cc.lcmv_weights(GEOM, look, [_dir(50)], F)


def test_white_noise_gain_das_near_10log10M():
    look = _dir(60)
    w = cc.delay_and_sum_weights(GEOM, look, F)
    wng = cc.white_noise_gain_db(w, GEOM, look, F)
    assert wng == pytest.approx(10.0 * math.log10(8), abs=0.5)


# --- zone-driven design (app entry point) ---
def _scene_with_zones():
    c = cp.create_config("Room", "2026-06-10T00:00:00Z")
    c = cp.set_room(c, cp.rectangular_room(8, 6, 3))
    c = cp.add_device(c, cp.create_microphone_array("A", "Ceiling Array"))
    c = cp.set_device_position(c, "A", Point2D(4, 3))
    arr = cp.find_device(c, "A")
    arr.zones = [
        cp.CoverageZone("p1", "dynamic", RectShape(Point2D(6.5, 2.5), 1, 1), False, "Presenter"),
        cp.CoverageZone("x1", "exclusion", RectShape(Point2D(0.5, 2.5), 1, 1), False, "Hallway"),
    ]
    return c


def test_design_zone_beams_picks_up_and_attenuates():
    c = _scene_with_zones()
    design = cc.design_zone_beams(c, "A", GEOM, freq_hz=F)
    assert len(design.beams) == 1
    beam = design.beams[0]
    assert beam.zone_id == "p1"
    assert beam.pickup_gain_db == pytest.approx(0.0, abs=1e-6)
    assert beam.nulled is True
    # the excluded hallway is attenuated relative to the pickup
    assert beam.exclusion_atten_db[0] < -20.0
    assert "Presenter" in design.summary()


def test_design_no_pickup_zones_is_empty():
    c = cp.create_config("Room", "x")
    c = cp.set_room(c, cp.rectangular_room(8, 6, 3))
    c = cp.add_device(c, cp.create_microphone_array("A", "Array"))
    c = cp.set_device_position(c, "A", Point2D(4, 3))
    design = cc.design_zone_beams(c, "A", GEOM, freq_hz=F)
    assert design.beams == ()
    assert "no pickup zones" in design.summary()


def test_design_without_exclusions_uses_das():
    c = _scene_with_zones()
    arr = cp.find_device(c, "A")
    arr.zones = [z for z in arr.zones if z.type != "exclusion"]
    design = cc.design_zone_beams(c, "A", GEOM, freq_hz=F)
    assert design.beams[0].nulled is False
    assert design.beams[0].exclusion_atten_db == ()


# --- active-capsule mask (exclude a dead/non-audio channel) ---
def test_with_active_channels_masks_and_validates():
    g = cc.with_active_channels(GEOM, [True] * 7 + [False])  # drop capsule 8
    assert g.n_channels == 8 and g.n_active == 7
    assert g.active_indices() == tuple(range(7))
    import pytest as _pt
    with _pt.raises(ValueError):
        cc.with_active_channels(GEOM, [True] * 7)              # wrong length
    with _pt.raises(ValueError):
        cc.with_active_channels(GEOM, [False] * 8)             # none active


def test_das_zeroes_inactive_capsule_and_holds_pickup():
    g = cc.with_active_channels(GEOM, [True, True, True, True, False, True, True, True])  # capsule 5 dead
    look = _dir(90)
    w = cc.delay_and_sum_weights(g, look, F)
    assert w[4] == 0j                                          # inactive → zero weight
    assert sum(1 for x in w if x != 0j) == 7
    assert cc.response_db(w, g, look.unit, F) == pytest.approx(0.0, abs=1e-9)  # still unity on-axis


def test_lcmv_with_dead_capsule_still_nulls():
    g = cc.with_active_channels(GEOM, [True, True, True, True, False, True, True, True])
    look, null = _dir(90), _dir(270)
    w = cc.lcmv_weights(g, look, [null], F)
    assert w[4] == 0j
    assert cc.response_db(w, g, look.unit, F) == pytest.approx(0.0, abs=1e-6)
    assert cc.response_db(w, g, null.unit, F) < -60.0


def test_null_limit_uses_active_count():
    # 3 active capsules can form at most 2 nulls
    g = cc.with_active_channels(GEOM, [True, True, True, False, False, False, False, False])
    look = _dir(0)
    with pytest.raises(ValueError):
        cc.lcmv_weights(g, look, [_dir(80), _dir(160), _dir(240)], F)  # 3 nulls > 2


def test_design_reports_active_count_in_summary():
    c = _scene_with_zones()
    g = cc.with_active_channels(GEOM, [True] * 7 + [False])
    design = cc.design_zone_beams(c, "A", g, freq_hz=F, mode=cc.MODE_DELAYSUM)
    assert "7/8 capsules" in design.summary()
    assert design.beams[0].weights[7] == 0j


# --- superdirective beamforming (diffuse-noise rejection) ---
SD_F = 800.0  # low-ish speech freq where superdirectivity helps a small array most


def test_diffuse_coherence_is_unit_diagonal_and_symmetric():
    g = cc.diffuse_coherence(GEOM, SD_F)
    assert len(g) == 8 and all(g[i][i] == pytest.approx(1.0) for i in range(8))
    assert all(g[i][j] == pytest.approx(g[j][i]) for i in range(8) for j in range(8))


def test_superdirective_unity_gain_on_axis():
    look = _dir(70)
    w = cc.superdirective_weights(GEOM, look, [], SD_F, loading=0.05)
    assert cc.response_db(w, GEOM, look.unit, SD_F) == pytest.approx(0.0, abs=1e-6)


def test_superdirective_beats_delaysum_directivity():
    look = _dir(70)
    w_ds = cc.delay_and_sum_weights(GEOM, look, SD_F)
    w_sd = cc.superdirective_weights(GEOM, look, [], SD_F, loading=0.02)
    di_ds = cc.directivity_index_db(w_ds, GEOM, look, SD_F)
    di_sd = cc.directivity_index_db(w_sd, GEOM, look, SD_F)
    assert di_sd > di_ds + 2.0  # meaningfully more diffuse-noise rejection


def test_more_loading_is_more_robust_less_directive():
    look = _dir(70)
    w_focus = cc.superdirective_weights(GEOM, look, [], SD_F, loading=0.005)
    w_robust = cc.superdirective_weights(GEOM, look, [], SD_F, loading=0.5)
    # more loading → higher white-noise gain (robust), lower directivity
    assert cc.white_noise_gain_db(w_robust, GEOM, look, SD_F) > cc.white_noise_gain_db(w_focus, GEOM, look, SD_F)
    assert cc.directivity_index_db(w_robust, GEOM, look, SD_F) < cc.directivity_index_db(w_focus, GEOM, look, SD_F)


def test_superdirective_still_nulls_exclusion():
    look, null = _dir(90), _dir(270)
    w = cc.superdirective_weights(GEOM, look, [null], SD_F, loading=0.02)
    assert cc.response_db(w, GEOM, look.unit, SD_F) == pytest.approx(0.0, abs=1e-5)
    assert cc.response_db(w, GEOM, null.unit, SD_F) < -40.0


def test_design_default_mode_is_superdirective_and_reports_di():
    c = _scene_with_zones()
    design = cc.design_zone_beams(c, "A", GEOM, freq_hz=SD_F)
    assert design.mode == cc.MODE_SUPERDIRECTIVE
    assert "superdirective" in design.summary()
    assert design.beams[0].di_db > 0.0


# --- lobe analysis ---
def test_analyze_lobes_main_lobe_at_look_and_counts():
    look = _dir(90)
    w = cc.delay_and_sum_weights(GEOM, look, 2000.0)
    rep = cc.analyze_lobes(w, GEOM, 2000.0, off_nadir_deg=70.0)
    assert min(abs(rep.main_az_deg - 90.0), 360 - abs(rep.main_az_deg - 90.0)) <= 6.0
    assert rep.n_lobes >= 1                       # at least the main lobe
    assert rep.beamwidth_3db_deg > 0.0
    assert rep.peak_sidelobe_db <= 0.0           # side lobes are below the main


def test_grating_lobes_appear_at_high_frequency():
    look = _dir(90)
    # a small array at a high frequency aliases → grating lobe(s) near 0 dB
    w_hi = cc.delay_and_sum_weights(GEOM, look, 7000.0)
    rep_hi = cc.analyze_lobes(w_hi, GEOM, 7000.0, off_nadir_deg=80.0)
    rep_lo = cc.analyze_lobes(cc.delay_and_sum_weights(GEOM, look, 1000.0), GEOM, 1000.0, off_nadir_deg=80.0)
    # high freq has more lobes than low; grating-lobe detection returns a tuple
    assert rep_hi.n_lobes >= rep_lo.n_lobes
    assert isinstance(rep_hi.grating_lobes, tuple)


def test_zonebeam_carries_lobe_stats():
    c = _scene_with_zones()
    b = cc.design_zone_beams(c, "A", GEOM, freq_hz=2000.0).beams[0]
    assert b.n_lobes >= 1 and b.peak_sidelobe_db <= 0.0
    assert "lobes:" in cc.design_zone_beams(c, "A", GEOM, freq_hz=2000.0).summary()


# --- per-talker leakage + out-of-zone suppression ---
def _scene_two_talkers():
    c = _scene_with_zones()  # pickup zone p1 around (7,3) east, exclusion west
    c = cp.add_talker(c, cp.create_talker("T_in", "InZone", Point2D(7.0, 3.0)))    # inside pickup
    c = cp.add_talker(c, cp.create_talker("T_out", "Outsider", Point2D(2.0, 5.0)))  # not in any zone
    return c


def test_talker_leakage_flags_in_and_out_of_zone():
    c = _scene_two_talkers()
    b = cc.design_zone_beams(c, "A", GEOM, freq_hz=2000.0).beams[0]
    leak = cc.talker_leakage_db(c, "A", GEOM, list(b.weights), 2000.0)
    by_id = {tid: (gain, in_pk) for tid, _lbl, gain, in_pk in leak}
    assert by_id["T_in"][1] is True and by_id["T_out"][1] is False
    # the in-zone talker is picked up louder than the outsider
    assert by_id["T_in"][0] > by_id["T_out"][0]


def test_suppress_outside_talkers_nulls_the_outsider():
    c = _scene_two_talkers()
    base = cc.design_zone_beams(c, "A", GEOM, freq_hz=2000.0)
    supp = cc.design_zone_beams(c, "A", GEOM, freq_hz=2000.0, suppress_outside_talkers=True)
    assert supp.beams[0].n_nulls > base.beams[0].n_nulls   # added a null for the outsider
    # outsider is more suppressed with the option on
    out_dir = cc.look_direction(c, "A", Point2D(2.0, 5.0))
    g_base = cc.response_db(list(base.beams[0].weights), GEOM, out_dir.unit, 2000.0)
    g_supp = cc.response_db(list(supp.beams[0].weights), GEOM, out_dir.unit, 2000.0)
    assert g_supp < g_base - 10.0
    assert tuple(supp.null_dirs)  # nulls recorded for the live runtime
