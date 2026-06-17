"""A/B measurement harness — offline beamform actually suppresses an interferer."""
import math

import pytest

np = pytest.importorskip("numpy")

import conf_pipeline as cp
from conf_pipeline.model import Point2D, RectShape
import conf_pipeline_control as cc


def _planewave_8ch(geom, az_deg, sig, sr):
    """Build an 8-ch plane wave arriving from OCTOVOX-style azimuth az_deg."""
    elems = np.array(geom.elements)
    u = np.array([math.cos(math.radians(az_deg)), math.sin(math.radians(az_deg)), 0.0])
    delays = -(elems @ u) / cc.SOUND_SPEED_MPS
    out = np.empty((elems.shape[0], len(sig)), np.float32)
    idx = np.arange(len(sig))
    for m, d in enumerate(delays):
        out[m] = np.interp(idx - d * sr, idx, sig, left=0, right=0)
    return out


def _scene_target_east():
    c = cp.create_config("Room", "x")
    c = cp.set_room(c, cp.rectangular_room(8, 6, 3))
    c = cp.add_device(c, cp.create_microphone_array("A", "Array"))
    c = cp.set_device_position(c, "A", Point2D(4, 3))
    arr = cp.find_device(c, "A")
    arr.zones = [cp.CoverageZone("p1", "dynamic", RectShape(Point2D(6.5, 2.5), 1, 1), False, "Talker")]  # east
    return c


def test_omni_reference_is_channel_mean():
    y = np.stack([np.full(10, float(i), np.float32) for i in range(8)])
    assert np.allclose(cc.omni_reference(y, np), np.arange(8).mean())


def test_beamform_suppresses_interferer_vs_omni():
    geom = cc.sensibel_8(0.05)
    sr = 16000
    n = sr  # 1 s
    t = np.arange(n) / sr
    target = (0.5 * np.sin(2 * np.pi * 600 * t)).astype(np.float32)     # 600 Hz from the east
    interf = (0.5 * np.sin(2 * np.pi * 1500 * t)).astype(np.float32)    # 1500 Hz from the west
    # east ≈ OCTOVOX az 0 (+X); west ≈ az 180. Build the mixed 8-ch field.
    y8 = _planewave_8ch(geom, 0.0, target, sr) + _planewave_8ch(geom, 180.0, interf, sr)

    c = _scene_target_east()
    design = cc.design_zone_beams(c, "A", geom, freq_hz=1000.0)
    beam = cc.apply_design_offline(design, geom, y8, sr, np)
    omni = cc.omni_reference(y8, np)

    def tone_power(x, f):
        X = np.abs(np.fft.rfft(x * np.hanning(len(x))))
        freqs = np.fft.rfftfreq(len(x), 1 / sr)
        k = int(np.argmin(np.abs(freqs - f)))
        return X[k]

    # target/interferer ratio should improve with the beam vs the omni mix
    omni_ratio = tone_power(omni, 600) / (tone_power(omni, 1500) + 1e-9)
    beam_ratio = tone_power(beam, 600) / (tone_power(beam, 1500) + 1e-9)
    assert beam_ratio > omni_ratio * 1.3   # beam favours the target over the interferer


def test_ab_compare_produces_all_variants_and_report():
    geom = cc.sensibel_8(0.05)
    sr = 16000
    y8 = (0.1 * np.random.default_rng(0).standard_normal((8, sr))).astype(np.float32)
    c = _scene_target_east()
    rep = cc.ab_compare(c, "A", geom, y8, sr, freq_hz=1500.0)
    names = [v.name for v in rep.variants]
    assert names == ["omni", "delaysum", "superdirective", "superdirective_aggressive", "nulled"]
    # aggressive has higher directivity (and lower WNG) than safe
    sd = next(v for v in rep.variants if v.name == "superdirective")
    agg = next(v for v in rep.variants if v.name == "superdirective_aggressive")
    assert agg.di_db >= sd.di_db - 0.01 and agg.wng_db < sd.wng_db
    assert "A/B beamformer comparison" in rep.summary
    for v in rep.variants:
        assert v.mono.shape[0] == sr   # every variant is the full-length mono


def test_save_ab_report_writes_wavs(tmp_path):
    geom = cc.sensibel_8(0.05)
    sr = 16000
    y8 = (0.1 * np.random.default_rng(1).standard_normal((8, sr))).astype(np.float32)
    c = _scene_target_east()
    rep = cc.ab_compare(c, "A", geom, y8, sr)
    paths = cc.save_ab_report(rep, str(tmp_path))
    assert any(p.endswith("omni.wav") for p in paths)
    assert any(p.endswith("report.txt") for p in paths)
    assert (tmp_path / "superdirective_aggressive.wav").exists()


# --------------------------------------------------------------------------- #
# measure_null_depth — the 2-source spatial-null A/B (talker + interferer)
# --------------------------------------------------------------------------- #
def test_measure_null_depth_suppresses_the_interferer_and_keeps_the_talker():
    """Beaming a 2-source scene look-only vs look+null: the interferer FROM the null direction drops,
    the talker AT the look direction is essentially untouched. (Engine-convention plane waves so the
    synthetic azimuths line up with design_from_bearings.)"""
    from test_polaris_beamformer import _plane_wave_block   # engine-convention (0°=+Y CW) synthesizer
    geom = cc.sensibel_8(radius_m=0.04)
    sr, n = 44100, 44100
    intf = _plane_wave_block(geom, 90.0, sr, n).T           # interferer from 90° -> (M, samples)
    talk = _plane_wave_block(geom, 0.0, sr, n).T            # talker at the 0° look
    rep = cc.measure_null_depth(geom, intf, sr, look_az_deg=0.0, null_az_deg=90.0, talker_y8=talk, loading=0.02)
    assert rep.null_depth_db < -1.0                          # the 90° interferer is suppressed by the null
    assert rep.look_change_db is not None and abs(rep.look_change_db) < 1.5   # the look (talker) is preserved
    assert rep.null_az_deg == 90.0 and rep.look_az_deg == 0.0 and "Null at 90" in rep.summary


def test_measure_null_depth_without_a_talker_clip():
    from test_polaris_beamformer import _plane_wave_block
    geom = cc.sensibel_8(radius_m=0.04)
    intf = _plane_wave_block(geom, 90.0, 44100, 44100).T
    rep = cc.measure_null_depth(geom, intf, 44100, look_az_deg=0.0, null_az_deg=90.0)
    assert rep.null_depth_db < -1.0 and rep.look_change_db is None
