"""Multi-azimuth DOA detection, sector gating, and multi-look design (numpy)."""
import math

import numpy as np
import pytest

import conf_pipeline_control as cc
from conf_pipeline_control import doa


GEOM = cc.sensibel_8(radius_m=0.05)
FREQS = np.linspace(doa.DEFAULT_F_LO_HZ, doa.DEFAULT_F_HI_HZ, 40)
C = 343.0


def _unit(az_deg, off_nadir_deg=90.0):
    az = math.radians(az_deg)
    n = math.radians(off_nadir_deg)
    s = math.sin(n)
    return np.array([s * math.sin(az), s * math.cos(az), -math.cos(n)])


def _cov_from_sources(geom, azimuths, *, off_nadir=90.0, noise=1e-3, powers=None):
    """Synthesize a band covariance R(f) from plane-wave sources at given azimuths."""
    elems = np.array(geom.elements, dtype=float)
    M = geom.n_channels
    R = np.zeros((len(FREQS), M, M), dtype=complex)
    for fi, f in enumerate(FREQS):
        k = 2.0 * np.pi * f / C
        acc = noise * np.eye(M, dtype=complex)
        for si, az in enumerate(azimuths):
            a = np.exp(1j * k * (elems @ _unit(az, off_nadir)))
            p = 1.0 if powers is None else powers[si]
            acc += p * np.outer(a, np.conj(a))
        R[fi] = acc
    return R


# --- SRP-PHAT detection ---
def test_single_source_recovered():
    R = _cov_from_sources(GEOM, [80.0])
    res = doa.detect(R, FREQS, GEOM)
    assert res.active
    assert len(res.detections) >= 1
    top = res.detections[0]
    assert doa._circular_sep(top.azimuth_deg, 80.0) <= 6.0


def test_two_separated_sources_both_found():
    R = _cov_from_sources(GEOM, [60.0, 200.0])
    res = doa.detect(R, FREQS, GEOM, max_talkers=3, min_separation_deg=40.0)
    found = [d.azimuth_deg for d in res.detections]
    assert len(found) == 2
    assert any(doa._circular_sep(a, 60.0) <= 8.0 for a in found)
    assert any(doa._circular_sep(a, 200.0) <= 8.0 for a in found)


def test_too_close_sources_merge_into_one():
    # 15° apart, below the 40° resolution floor → a single merged detection
    R = _cov_from_sources(GEOM, [60.0, 75.0])
    res = doa.detect(R, FREQS, GEOM, max_talkers=3, min_separation_deg=40.0)
    assert len(res.detections) == 1


def test_silence_is_inactive():
    # white/diffuse covariance (identity per bin) → flat map, nobody talking
    R = np.broadcast_to(np.eye(GEOM.n_channels, dtype=complex), (len(FREQS), 8, 8)).copy()
    res = doa.detect(R, FREQS, GEOM)
    assert res.active is False
    assert res.detections == []


def test_peak_picker_caps_and_orders_and_separates():
    # Unit-test the picker on a clean map (independent of array resolution):
    # three peaks, ask for two → strongest two, separation enforced.
    grid = np.arange(0.0, 360.0, 2.0)
    power = np.zeros_like(grid)
    for az, h in [(40.0, 10.0), (160.0, 8.0), (280.0, 6.0)]:
        power[int(az / 2.0)] = h
    picked = doa._pick_peaks(grid, power, max_talkers=2, min_separation_deg=40.0, min_salience_db=3.0)
    assert [p.azimuth_deg for p in picked] == [40.0, 160.0]   # capped + strongest first


def test_peak_picker_rejects_below_salience():
    grid = np.arange(0.0, 360.0, 2.0)
    power = np.zeros_like(grid)
    power[int(40 / 2.0)] = 1.0                                # below the 3 dB floor
    assert doa._pick_peaks(grid, power, max_talkers=3, min_separation_deg=40.0, min_salience_db=3.0) == []


def test_detection_works_with_dead_capsule():
    g = cc.with_active_channels(GEOM, [True, True, True, True, True, False, True, True])  # capsule 5 dead
    R = _cov_from_sources(GEOM, [120.0])
    res = doa.detect(R, FREQS, g)
    assert res.active
    assert doa._circular_sep(res.detections[0].azimuth_deg, 120.0) <= 8.0


# --- sector ("radius") gate ---
def test_in_sector_wraps_around_zero():
    assert doa.in_sector(350.0, center_deg=0.0, half_width_deg=20.0)
    assert doa.in_sector(10.0, center_deg=0.0, half_width_deg=20.0)
    assert not doa.in_sector(40.0, center_deg=0.0, half_width_deg=20.0)


def test_in_sector_front_offset():
    # offset puts the array's azimuth 90° at the room "front" (center 0)
    assert doa.in_sector(90.0, center_deg=0.0, half_width_deg=15.0, front_offset_deg=90.0)
    assert not doa.in_sector(0.0, center_deg=0.0, half_width_deg=15.0, front_offset_deg=90.0)


def test_sector_gate_flags_detections():
    dets = [doa.Detection(10.0, 9.0), doa.Detection(180.0, 8.0)]
    doa.sector_gate(dets, center_deg=0.0, half_width_deg=45.0)
    assert dets[0].in_sector is True
    assert dets[1].in_sector is False


# --- multiple sectors: a talker is "in coverage" if inside ANY sector ---
def test_in_any_sector_two_sectors():
    specs = [(0.0, 20.0, 0.0), (180.0, 20.0, 0.0)]   # (center, half_width, front_offset)
    assert doa.in_any_sector(10.0, specs)            # inside sector A
    assert doa.in_any_sector(190.0, specs)           # inside sector B
    assert not doa.in_any_sector(90.0, specs)        # in the gap between them


def test_in_any_sector_wraps_around_zero():
    specs = [(0.0, 20.0, 0.0)]
    assert doa.in_any_sector(350.0, specs)
    assert doa.in_any_sector(10.0, specs)
    assert not doa.in_any_sector(40.0, specs)


def test_in_any_sector_empty_specs_is_false():
    assert doa.in_any_sector(123.0, []) is False


def test_in_any_sector_front_offset():
    specs = [(0.0, 15.0, 90.0)]                       # front offset rotates the reference
    assert doa.in_any_sector(90.0, specs)
    assert not doa.in_any_sector(0.0, specs)


def test_sector_gate_multi_flags_gap_talker():
    specs = [(0.0, 20.0, 0.0), (180.0, 20.0, 0.0)]
    dets = [doa.Detection(10.0, 9.0), doa.Detection(90.0, 8.0), doa.Detection(190.0, 7.0)]
    doa.sector_gate_multi(dets, specs)
    assert dets[0].in_sector is True                 # in sector A
    assert dets[1].in_sector is False                # in the gap
    assert dets[2].in_sector is True                 # in sector B


# --- multi-look design (drives the live extractor) ---
def test_design_multi_bearings_one_beam_per_look():
    d = cc.design_multi_bearings(GEOM, [(0.0, 90.0), (120.0, 90.0)], [(200.0, 90.0)], freq_hz=2000.0)
    assert len(d.beams) == 2
    assert d.beams[0].look.azimuth_deg == 0.0 and d.beams[1].look.azimuth_deg == 120.0
    assert len(d.null_dirs) == 1
    for b in d.beams:
        assert b.pickup_gain_db == pytest.approx(0.0, abs=1e-5)


def test_design_multi_bearings_empty_looks_is_empty():
    d = cc.design_multi_bearings(GEOM, [], [(90.0, 90.0)], freq_hz=2000.0)
    assert d.beams == ()


def test_design_multi_bearings_respects_dead_capsule_and_budget():
    g = cc.with_active_channels(GEOM, [True, True, True, True, True, False, True, True])
    nulls = [(a, 90.0) for a in range(20, 360, 20)]      # 17 > budget (n_active-1 = 6)
    d = cc.design_multi_bearings(g, [(0.0, 90.0)], nulls, freq_hz=2000.0)
    assert len(d.null_dirs) == 6
    assert d.beams[0].weights[5] == 0j                   # dead capsule stays zero


# --- auto-steer control loop (stubbed controller, no hardware) ---
class _StubCtrl:
    """Stands in for LiveBeamController: feeds a fixed covariance, records calls."""

    def __init__(self, cov, freqs):
        self._cov, self._freqs = cov, freqs
        self.applied = None
        self.muted = None

    def snapshot_covariance(self):
        return self._cov, self._freqs

    def apply_design(self, design):
        self.applied = design

    def set_mute(self, m):
        self.muted = m


def _autosteer(geom, sector, **kw):
    a = cc.AutoSteerController(geom, sector, device=None, **kw)  # no connect() → no hardware
    return a


def test_autosteer_steers_in_sector_and_nulls_out():
    R = _cov_from_sources(GEOM, [10.0, 200.0])          # 10° in sector, 200° out
    a = _autosteer(GEOM, cc.SectorConfig(center_deg=0.0, half_width_deg=45.0))
    a.ctrl = _StubCtrl(R, FREQS)
    a._tick()
    assert a.ctrl.applied is not None
    looks = [b.look.azimuth_deg for b in a.ctrl.applied.beams]
    assert any(doa._circular_sep(x, 10.0) <= 8.0 for x in looks)
    nulls = [d.azimuth_deg for d in a.ctrl.applied.null_dirs]
    assert any(doa._circular_sep(x, 200.0) <= 8.0 for x in nulls)
    assert a.ctrl.muted is False


def test_autosteer_mutes_when_sector_empty():
    R = np.broadcast_to(np.eye(GEOM.n_channels, dtype=complex), (len(FREQS), 8, 8)).copy()
    a = _autosteer(GEOM, cc.SectorConfig(center_deg=0.0, half_width_deg=45.0))
    a.ctrl = _StubCtrl(R, FREQS)
    a._tick()
    assert a.ctrl.muted is True
    assert a.ctrl.applied is None


# --- multiple sectors on the controller ---
def _two_sectors():
    # sector A around 0° (±20°) and sector B around 180° (±20°); the rest is a "gap"
    return [cc.SectorConfig(center_deg=0.0, half_width_deg=20.0),
            cc.SectorConfig(center_deg=180.0, half_width_deg=20.0)]


def test_autosteer_two_sectors_follow_both_talkers():
    # one talker in sector A (10°), one in sector B (190°): with two sectors BOTH are followed
    # (single-sector auto-steer would null the 190° talker).
    R = _cov_from_sources(GEOM, [10.0, 190.0])
    a = _autosteer(GEOM, cc.SectorConfig(center_deg=0.0, half_width_deg=20.0), sectors=_two_sectors())
    a.ctrl = _StubCtrl(R, FREQS)
    a._tick()
    assert a.ctrl.applied is not None
    looks = [b.look.azimuth_deg for b in a.ctrl.applied.beams]
    assert any(doa._circular_sep(x, 10.0) <= 8.0 for x in looks)    # sector A talker followed
    assert any(doa._circular_sep(x, 190.0) <= 8.0 for x in looks)   # sector B talker followed
    assert a.ctrl.applied.null_dirs == ()                          # neither talker is nulled
    assert a.ctrl.muted is False


def test_autosteer_multi_sector_nulls_gap_talker():
    # one talker in sector A (10°), one in the gap between the sectors (100°): the gap talker is cut.
    R = _cov_from_sources(GEOM, [10.0, 100.0])
    a = _autosteer(GEOM, cc.SectorConfig(center_deg=0.0, half_width_deg=20.0), sectors=_two_sectors())
    a.ctrl = _StubCtrl(R, FREQS)
    a._tick()
    looks = [b.look.azimuth_deg for b in a.ctrl.applied.beams]
    assert any(doa._circular_sep(x, 10.0) <= 8.0 for x in looks)    # in-sector talker followed
    assert all(doa._circular_sep(x, 100.0) > 8.0 for x in looks)    # gap talker NOT followed
    nulls = [d.azimuth_deg for d in a.ctrl.applied.null_dirs]
    assert any(doa._circular_sep(x, 100.0) <= 8.0 for x in nulls)   # gap talker nulled
    assert a.ctrl.muted is False


def test_autosteer_multi_sector_mutes_when_all_empty():
    R = np.broadcast_to(np.eye(GEOM.n_channels, dtype=complex), (len(FREQS), 8, 8)).copy()
    a = _autosteer(
        GEOM, cc.SectorConfig(center_deg=0.0, half_width_deg=20.0),
        sectors=[cc.SectorConfig(center_deg=0.0, half_width_deg=20.0),
                 cc.SectorConfig(center_deg=180.0, half_width_deg=20.0)],
    )
    a.ctrl = _StubCtrl(R, FREQS)
    a._tick()
    assert a.ctrl.muted is True
    assert a.ctrl.applied is None


def test_autosteer_sector_property_backcompat():
    s0 = cc.SectorConfig(center_deg=0.0, half_width_deg=45.0)
    a = _autosteer(GEOM, s0)                       # positional single-sector construction
    assert a.sectors == [s0]
    assert a.sector == s0

    s1 = cc.SectorConfig(center_deg=90.0, half_width_deg=30.0)
    a.sector = s1                                  # legacy single-sector setter
    assert a.sectors == [s1]
    assert a.sector == s1

    s2 = cc.SectorConfig(center_deg=270.0, half_width_deg=10.0)
    a.sectors = [s1, s2]                           # multi-sector setter
    assert a.sector == s1                          # back-compat getter = first sector


def test_autosteer_sectors_kwarg_overrides_positional():
    s_pos = cc.SectorConfig(center_deg=0.0, half_width_deg=45.0)
    s_a = cc.SectorConfig(center_deg=10.0, half_width_deg=15.0)
    s_b = cc.SectorConfig(center_deg=200.0, half_width_deg=15.0)
    a = _autosteer(GEOM, s_pos, sectors=[s_a, s_b])
    assert a.sectors == [s_a, s_b]
