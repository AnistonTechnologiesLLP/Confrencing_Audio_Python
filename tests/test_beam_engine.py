"""Hardware-free tests for BeamEngine — the steered/grid A/B wrapper.

Covers the external-feed seam round-trip on the REAL back-ends (process_block with no device
opened), the equal-power crossfade math + routing flip (fake back-ends), set_mode/get_mode,
the normalized location struct, and device-validation errors. numpy required.
"""
import math

import pytest

np = pytest.importorskip("numpy")

import conf_pipeline_control as cc
from conf_pipeline_control import doa
from conf_pipeline_control.audio import InputDevice
import conf_pipeline_control.beam_engine as be
from conf_pipeline_control.beam_engine import BeamEngine, Location
from conf_pipeline_control.polaris_beamformer import DoaReading, PolarisBeamformer
from conf_pipeline_control.virtual_mic_grid import VirtualMicGrid

C = 343.0


def _unit(az_deg, off_nadir_deg=90.0):
    az = math.radians(az_deg)
    n = math.radians(off_nadir_deg)
    s = math.sin(n)
    return np.array([s * math.sin(az), s * math.cos(az), -math.cos(n)])


def _plane_wave_block(geom, az_deg, sr, n, tones=(2000.0, 3000.0, 4000.0)):
    elems = np.array(geom.elements, dtype=float)
    proj = elems @ _unit(az_deg)
    t = np.arange(n) / sr
    s = sum(np.sin(2 * np.pi * f * t) for f in tones)
    x = np.zeros((n, geom.n_channels), dtype=float)
    for m in range(geom.n_channels):
        x[:, m] = np.roll(s, -int(round(proj[m] / C * sr)))
    return x


class _FakeBackend:
    """Minimal stand-in exposing the seam + location surface BeamEngine uses."""

    def __init__(self, val=0.0):
        self.val = float(val)
        self.reset_called = False
        self._np = np
        self.selected_xy = None
        self._reading = DoaReading(None, 0.0, False, False)
        self._scores = None

    def prepare_external(self):
        pass

    def release_external(self):
        pass

    def reset_transient(self):
        self.reset_called = True

    def process_block(self, block):
        return np.full(block.shape[0], self.val, dtype=np.float32)

    def reading(self):
        return self._reading

    def scores(self):
        return self._scores


# --------------------------------------------------------------------------- #
# External-feed seam round-trip on the REAL back-ends (no device opened)
# --------------------------------------------------------------------------- #
def test_steered_external_feed_recovers_doa():
    bf = PolarisBeamformer(device=None)
    bf.prepare_external()
    assert bf._stream is None and bf._doa_thread is not None     # DSP+thread, no device
    for _ in range(8):
        mono = bf.process_block(_plane_wave_block(bf.geometry, 100.0, 44100.0, bf.blocksize))
        assert mono.shape == (bf.blocksize,)
    bf._doa_tick()                                                # deterministic detect
    az = bf.current_doa_deg
    bf.release_external()
    assert bf._doa_thread is None                                 # thread joined
    assert az is not None and doa._circular_sep(az, 100.0) <= 10.0


def test_grid_external_feed_selects_without_device():
    vmg = VirtualMicGrid(device=None, blocksize=512, radius_m=0.12,
                         room_width_m=1.0, room_depth_m=1.0, grid_cols=9, grid_rows=9)
    vmg.prepare_external()
    assert vmg._stream is None
    rng = np.random.default_rng(0)
    for _ in range(4):
        mono = vmg.process_block(rng.standard_normal((512, 8)))
        assert mono.shape == (512,)
    assert vmg.selected_xy is not None                           # selection plumbing works
    vmg.release_external()
    assert vmg.streaming is False


def test_seam_does_not_change_standalone_cb(monkeypatch):
    # _cb_input must still stamp the watchdog + emit (reconnect logic depends on it).
    bf = PolarisBeamformer(device=None)
    bf.prepare_external()
    bf._last_block_monotonic = None
    bf._cb_input(_plane_wave_block(bf.geometry, 30.0, 44100.0, bf.blocksize), bf.blocksize, None, None)
    assert bf._last_block_monotonic is not None                  # watchdog stamped
    assert not bf.output_queue.empty()                           # emitted to the back-end queue
    bf.release_external()


# --------------------------------------------------------------------------- #
# Crossfade math + routing
# --------------------------------------------------------------------------- #
def test_mix_equal_power_endpoints():
    eng = BeamEngine(device=None, crossfade_blocks=6)
    mo = np.ones(4, dtype=np.float32)
    mi = np.full(4, 3.0, dtype=np.float32)
    assert np.allclose(eng._mix(mo, mi, 0), mo)                  # step 0 → all outgoing
    assert np.allclose(eng._mix(mo, mi, 6), mi)                  # step N → all incoming
    for step in range(7):                                       # equal power across the fade
        p = step / 6.0
        g_out, g_in = math.cos(p * math.pi / 2), math.sin(p * math.pi / 2)
        assert abs(g_out ** 2 + g_in ** 2 - 1.0) < 1e-9


def test_cb_crossfade_routes_and_flips_active():
    eng = BeamEngine(device=None, crossfade_blocks=3)
    fa, fb = _FakeBackend(1.0), _FakeBackend(2.0)
    eng._by_mode = {"steered": fa, "grid": fb}
    eng._active = fa
    eng._mode = "steered"
    eng._steered._np = np                                        # let _cb run its astype/rms path

    eng.set_mode("grid")
    assert eng._fading and eng._incoming is fb and eng.get_mode() == "grid"
    assert fb.reset_called and not fa.reset_called              # incoming reset ONLY

    x = np.zeros((8, 8), dtype=np.float32)
    outs = []
    for _ in range(3):                                          # complete the fade
        eng._cb(x, 8, None, None)
        outs.append(eng.output_queue.get_nowait())
    assert np.allclose(outs[0], 1.0)                           # first fade block ≈ all outgoing
    assert not eng._fading and eng._active is fb               # flipped after crossfade_blocks
    eng._cb(x, 8, None, None)
    assert np.allclose(eng.output_queue.get_nowait(), 2.0)     # steady on incoming


def test_set_mode_unknown_and_noop():
    with pytest.raises(ValueError):
        BeamEngine(device=None, mode="bogus")
    eng = BeamEngine(device=None, mode="steered")
    with pytest.raises(ValueError, match="unknown mode"):
        eng.set_mode("bogus")
    eng.set_mode("steered")                                     # already active → noop
    assert not eng._fading and eng.get_mode() == "steered"


# --------------------------------------------------------------------------- #
# Normalized location
# --------------------------------------------------------------------------- #
def test_current_location_steered():
    eng = BeamEngine(device=None, assumed_range_m=2.0)
    fake = _FakeBackend()
    fake._reading = DoaReading(90.0, 6.0, False, True)
    eng._by_mode["steered"] = fake
    eng._mode = "steered"
    loc = eng.current_location
    assert isinstance(loc, Location)
    assert loc.mode == "steered" and loc.angle_deg == 90.0
    assert abs(loc.confidence - 0.5) < 1e-9                     # 6 dB / 12
    assert loc.xy is not None and abs(loc.xy[0] - 2.0) < 1e-6   # derived from assumed_range


def test_current_location_grid():
    eng = BeamEngine(device=None)
    fake = _FakeBackend()
    fake.selected_xy = (1.0, 1.0)
    fake._scores = np.array([5.0, 1.0, 1.0])
    eng._by_mode["grid"] = fake
    eng._mode = "grid"
    loc = eng.current_location
    assert loc.mode == "grid" and loc.xy == (1.0, 1.0)
    assert abs(loc.angle_deg - 45.0) < 1e-6                     # atan2(1,1), 0°=+Y CW
    assert 0.0 <= loc.confidence <= 1.0


def test_current_location_silence():
    eng = BeamEngine(device=None, mode="steered")
    eng._by_mode["steered"] = _FakeBackend()                   # reading() → azimuth None
    loc = eng.current_location
    assert loc.angle_deg is None and loc.xy is None and loc.confidence == 0.0


# --------------------------------------------------------------------------- #
# Device-validation errors
# --------------------------------------------------------------------------- #
def test_device_not_found_raises(monkeypatch):
    monkeypatch.setattr(be, "controls_available", lambda: True)
    monkeypatch.setattr(be, "list_input_devices", lambda: [InputDevice(7, "POLARIS", 8, 44100.0)])
    with pytest.raises(ValueError, match="not found"):
        BeamEngine(device=99).start()


def test_too_few_channels_raises(monkeypatch):
    monkeypatch.setattr(be, "controls_available", lambda: True)
    monkeypatch.setattr(be, "list_input_devices", lambda: [InputDevice(3, "Stereo Mic", 2, 44100.0)])
    with pytest.raises(cc.DeviceConfigError, match="needs 8"):
        BeamEngine(device=3).start()


def test_missing_extra_raises_install_hint(monkeypatch):
    monkeypatch.setattr(be, "controls_available", lambda: False)
    with pytest.raises(RuntimeError, match=r"\[control\]"):
        BeamEngine(device=None).start()
