"""Wideband (subband) beam design — the beam is proven across the speech band,
not just at the 1 kHz reference (pure stdlib; the numpy cross-check against the
live per-FFT-bin path is skipped when the [control] extra is absent)."""
import math

import pytest

import conf_pipeline as cp
from conf_pipeline.model import Point2D, RectShape
import conf_pipeline_control as cc
from conf_pipeline_control.beamformer import _weights_for
from conf_pipeline_control.steering import Direction


GEOM = cc.sensibel_8(radius_m=0.05)
F_REF = 1000.0


def _dir(az_deg, off_nadir_deg=70.0):
    az = math.radians(az_deg)
    n = math.radians(off_nadir_deg)
    s = math.sin(n)
    return Direction(unit=(s * math.sin(az), s * math.cos(az), -math.cos(n)),
                     azimuth_deg=az_deg, off_nadir_deg=off_nadir_deg, distance_m=2.0)


def _scene_with_zones():
    c = cp.create_config("Room", "2026-06-12T00:00:00Z")
    c = cp.set_room(c, cp.rectangular_room(8, 6, 3))
    c = cp.add_device(c, cp.create_microphone_array("A", "Ceiling Array"))
    c = cp.set_device_position(c, "A", Point2D(4, 3))
    arr = cp.find_device(c, "A")
    arr.zones = [
        cp.CoverageZone("p1", "dynamic", RectShape(Point2D(6.5, 2.5), 1, 1), False, "Presenter"),
        cp.CoverageZone("x1", "exclusion", RectShape(Point2D(0.5, 2.5), 1, 1), False, "Hallway"),
    ]
    return c


# --- the wideband default ---
def test_default_design_carries_octave_bands():
    design = cc.design_zone_beams(_scene_with_zones(), "A", GEOM)
    assert design.band_freqs == cc.SPEECH_OCTAVE_CENTERS_HZ
    beam = design.beams[0]
    assert tuple(m.freq_hz for m in beam.band_metrics) == cc.SPEECH_OCTAVE_CENTERS_HZ
    assert all(len(m.weights) == GEOM.n_channels for m in beam.band_metrics)


def test_wideband_pickup_holds_across_bands():
    """Unity gain toward the pickup zone at EVERY band center, not just 1 kHz."""
    beam = cc.design_zone_beams(_scene_with_zones(), "A", GEOM).beams[0]
    for m in beam.band_metrics:
        assert m.pickup_gain_db == pytest.approx(0.0, abs=1e-5), f"pickup off at {m.freq_hz} Hz"


def test_wideband_nulls_hold_across_bands():
    """The excluded area stays deeply attenuated at every band center."""
    beam = cc.design_zone_beams(_scene_with_zones(), "A", GEOM).beams[0]
    assert beam.nulled is True
    for m in beam.band_metrics:
        assert not m.note, f"nulls dropped at {m.freq_hz} Hz: {m.note}"
        assert m.exclusion_atten_db[0] < -40.0, f"shallow null at {m.freq_hz} Hz"


def test_wideband_bearing_design_nulls_across_bands():
    design = cc.design_from_bearings(GEOM, (0.0, 90.0), [(180.0, 90.0)])
    beam = design.beams[0]
    assert len(beam.band_metrics) == len(cc.SPEECH_OCTAVE_CENTERS_HZ)
    for m in beam.band_metrics:
        assert m.pickup_gain_db == pytest.approx(0.0, abs=1e-5)
        assert m.exclusion_atten_db[0] < -40.0, f"shallow null at {m.freq_hz} Hz"


def test_wideband_delaysum_mode_pickup_across_bands():
    design = cc.design_from_bearings(GEOM, (45.0, 90.0), mode=cc.MODE_DELAYSUM)
    for m in design.beams[0].band_metrics:
        assert m.pickup_gain_db == pytest.approx(0.0, abs=1e-9)


def test_band_metrics_report_physics_not_hide_it():
    """Low bands on a small aperture cost white-noise gain — the per-band WNG
    must surface that (more negative at 250 Hz than at 4 kHz), not mask it."""
    beam = cc.design_zone_beams(_scene_with_zones(), "A", GEOM).beams[0]
    by_f = {m.freq_hz: m for m in beam.band_metrics}
    assert by_f[250.0].wng_db < by_f[4000.0].wng_db
    assert all(m.wng_db > cc.RESPONSE_FLOOR_DB for m in beam.band_metrics)


# --- the single-frequency API is the special case ---
def test_band_weights_match_single_frequency_design():
    """The wideband design at band center f equals the legacy single-frequency
    design at freq_hz=f — same math, same weights."""
    c = _scene_with_zones()
    wide = cc.design_zone_beams(c, "A", GEOM)
    for m in wide.beams[0].band_metrics:
        narrow = cc.design_zone_beams(c, "A", GEOM, freq_hz=m.freq_hz, bands=())
        assert narrow.beams[0].weights == pytest.approx(m.weights, abs=1e-12)


def test_bands_opt_out_keeps_legacy_shape_and_scalars():
    c = _scene_with_zones()
    legacy = cc.design_zone_beams(c, "A", GEOM, freq_hz=F_REF, bands=())
    wide = cc.design_zone_beams(c, "A", GEOM, freq_hz=F_REF)
    assert legacy.band_freqs == () and legacy.beams[0].band_metrics == ()
    # the reference-frequency scalars are unaffected by band verification
    assert legacy.beams[0].weights == wide.beams[0].weights
    assert legacy.beams[0].pickup_gain_db == wide.beams[0].pickup_gain_db
    assert legacy.beams[0].di_db == wide.beams[0].di_db
    assert legacy.beams[0].exclusion_atten_db == wide.beams[0].exclusion_atten_db


def test_custom_band_grid_is_honoured():
    design = cc.design_from_bearings(GEOM, (0.0, 90.0), [(180.0, 90.0)], bands=(300.0, 3000.0))
    assert design.band_freqs == (300.0, 3000.0)
    assert tuple(m.freq_hz for m in design.beams[0].band_metrics) == (300.0, 3000.0)


def test_invalid_band_grid_raises():
    with pytest.raises(ValueError):
        cc.design_from_bearings(GEOM, (0.0, 90.0), bands=(1000.0, -200.0))


def test_multi_bearings_band_opt_out_used_by_autosteer():
    design = cc.design_multi_bearings(GEOM, [(0.0, 90.0), (90.0, 90.0)], [(200.0, 90.0)], bands=())
    assert design.band_freqs == ()
    assert all(b.band_metrics == () for b in design.beams)


def test_summary_reports_band_range_and_worst_leak():
    s = cc.design_zone_beams(_scene_with_zones(), "A", GEOM).summary()
    assert "bands 250–8000 Hz (6)" in s
    assert "worst excluded leak" in s


def test_dead_capsule_stays_zero_in_every_band():
    g = cc.with_active_channels(GEOM, [True, True, True, True, False, True, True, True])
    design = cc.design_from_bearings(g, (0.0, 90.0), [(180.0, 90.0)])
    for m in design.beams[0].band_metrics:
        assert m.weights[4] == 0j


# --- broadband verification curves (B2): DI / beamwidth vs frequency ---
def test_frequency_curves_one_per_beam_on_third_octave_grid():
    design = cc.design_zone_beams(_scene_with_zones(), "A", GEOM)
    curves = cc.frequency_curves(design)
    assert len(curves) == len(design.beams) == 1
    c = curves[0]
    assert c.freqs_hz == cc.SPEECH_THIRD_OCTAVE_CENTERS_HZ
    n = len(c.freqs_hz)
    assert len(c.di_db) == len(c.beamwidth_3db_deg) == len(c.wng_db) == n
    assert len(c.n_lobes) == len(c.n_grating) == len(c.notes) == n
    assert c.label == "Presenter"


def test_curve_shows_known_aperture_physics():
    """On a fixed 10 cm aperture, delay-and-sum must measurably narrow and gain
    directivity with rising frequency — the fidelity note as numbers."""
    design = cc.design_from_bearings(GEOM, (0.0, 90.0), mode=cc.MODE_DELAYSUM)
    c = cc.frequency_curves(design)[0]
    lo, hi = c.freqs_hz.index(250.0), c.freqs_hz.index(8000.0)
    assert c.di_db[hi] > c.di_db[lo] + 2.0
    assert c.beamwidth_3db_deg[hi] < c.beamwidth_3db_deg[lo] / 2.0
    # lobe structure grows toward high frequency (spatial aliasing)
    assert c.n_lobes[hi] >= c.n_lobes[lo]


def test_curve_superdirective_beats_delaysum_in_low_band():
    look = (0.0, 90.0)
    grid = (250.0, 400.0, 630.0)
    sd = cc.frequency_curves(cc.design_from_bearings(GEOM, look, loading=0.02), freqs=grid)[0]
    ds = cc.frequency_curves(cc.design_from_bearings(GEOM, look, mode=cc.MODE_DELAYSUM), freqs=grid)[0]
    for i in range(len(grid)):
        assert sd.di_db[i] > ds.di_db[i] + 2.0, f"no superdirective gain at {grid[i]} Hz"


def test_curve_di_consistent_with_band_metrics_on_same_grid():
    design = cc.design_zone_beams(_scene_with_zones(), "A", GEOM)
    curve = cc.frequency_curves(design, freqs=cc.SPEECH_OCTAVE_CENTERS_HZ)[0]
    for i, m in enumerate(design.beams[0].band_metrics):
        assert curve.di_db[i] == pytest.approx(m.di_db, abs=1e-9)
        assert curve.wng_db[i] == pytest.approx(m.wng_db, abs=1e-9)


def test_curve_empty_design_and_bad_grid():
    c = cp.create_config("Room", "x")
    c = cp.set_room(c, cp.rectangular_room(8, 6, 3))
    c = cp.add_device(c, cp.create_microphone_array("A", "Array"))
    c = cp.set_device_position(c, "A", Point2D(4, 3))
    empty = cc.design_zone_beams(c, "A", GEOM)
    assert cc.frequency_curves(empty) == ()
    design = cc.design_from_bearings(GEOM, (0.0, 90.0))
    with pytest.raises(ValueError):
        cc.frequency_curves(design, freqs=(500.0, 0.0))


def test_curve_table_is_readable():
    design = cc.design_zone_beams(_scene_with_zones(), "A", GEOM)
    t = cc.frequency_curves(design)[0].table()
    assert "DI / beamwidth vs frequency (Presenter):" in t
    assert "250 Hz" in t and "8000 Hz" in t
    assert "°" in t and "dB" in t
    assert len(t.splitlines()) == 2 + len(cc.SPEECH_THIRD_OCTAVE_CENTERS_HZ)


# --- consistency with the live per-FFT-bin runtime (the structure we reuse) ---
@pytest.mark.skipif(not cc.controls_available(), reason="needs the [control] extra (numpy)")
def test_stdlib_band_weights_match_live_per_bin_math():
    """The stdlib per-band design and the live runtime's per-bin weights are the
    same formula: at a band center, both produce the same weight vector."""
    import numpy as np

    from conf_pipeline_control.geometry import SOUND_SPEED_MPS
    from conf_pipeline_control.live import LiveBeamController

    look, null = _dir(90, 90.0), _dir(270, 90.0)
    loading = 0.05
    elems = np.array(GEOM.elements, dtype=float)
    active = np.array(GEOM.active_indices(), dtype=int)
    ap = elems[active]
    dist = np.sqrt(((ap[:, None, :] - ap[None, :, :]) ** 2).sum(-1))

    for f in cc.SPEECH_OCTAVE_CENTERS_HZ:
        w_std = _weights_for(GEOM, look, [null], f, cc.MODE_SUPERDIRECTIVE, loading)
        k = 2.0 * np.pi * f / SOUND_SPEED_MPS
        R = np.sinc(k * dist / np.pi) + loading * np.eye(len(active))
        w_live = LiveBeamController._bin_weights(
            np, elems, k, look, [null], GEOM.n_channels, active, R
        )
        assert np.allclose(np.array(w_std), w_live, atol=1e-8), f"mismatch at {f} Hz"
