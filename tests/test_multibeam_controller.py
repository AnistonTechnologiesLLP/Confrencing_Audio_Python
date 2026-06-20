"""MultiBeamController orchestration — stub-injected DOA source + mixer (no sounddevice, no real DSP).

Drives plan()/process_block()/status() directly, exactly as the audio + control threads would.
"""
import math

import pytest

np = pytest.importorskip("numpy")

import conf_pipeline as cp
from conf_pipeline.model import Point2D, RoomLayout, RoomObject, SeatAnchor
from conf_pipeline_control.multibeam import BeamSlot, MultiBeamController

FREQS = np.linspace(300.0, 3800.0, 40)
C = 343.0


def _config():
    """Array at origin, bearing 0; seat1 due +Y (bearing 0), seat2 due +X (bearing 90)."""
    c = cp.create_config("rt", "2026-01-01T00:00:00Z")
    c = cp.add_device(c, cp.create_microphone_array("A", "Array", position=Point2D(0.0, 0.0)))
    c = cp.set_array_bearing(c, "A", 0.0)
    c.room = RoomLayout(
        vertices=[Point2D(-3, -3), Point2D(3, -3), Point2D(3, 3), Point2D(-3, 3)], height=3.0, units="meters",
        objects=[RoomObject(id="sofa", kind="sofa", position=Point2D(0.0, 3.0),
                            seats=[SeatAnchor(position=Point2D(0.0, 3.0)),     # sofa-seat1 -> bearing 0
                                   SeatAnchor(position=Point2D(3.0, 0.0))])],  # sofa-seat2 -> bearing 90
    )
    return c


def _unit(az, off=90.0):
    a, n = math.radians(az), math.radians(off)
    s = math.sin(n)
    return np.array([s * math.sin(a), s * math.cos(a), -math.cos(n)])


def _cov_from_sources(geom, azimuths, amps, noise=1e-3):
    elems = np.array(geom.elements, dtype=float)
    M = geom.n_channels
    R = np.zeros((len(FREQS), M, M), dtype=complex)
    for fi, f in enumerate(FREQS):
        k = 2.0 * np.pi * f / C
        acc = noise * np.eye(M, dtype=complex)
        for az, amp in zip(azimuths, amps):
            a = np.exp(1j * k * (elems @ _unit(az)))
            acc += amp * np.outer(a, np.conj(a))
        R[fi] = acc
    return R


class _StubDoa:
    def __init__(self, cov):
        self._cov, self.blocks = cov, 0

    def process_block(self, block):
        self.blocks += 1

    def snapshot_covariance(self):
        return self._cov, FREQS

    def reset(self):
        self._cov = None


class _StubMixer:
    def __init__(self):
        self.slots = None

    def set_slots(self, slots):
        self.slots = list(slots)

    def process_block(self, block):
        mono = np.ones(8, dtype=np.float32)
        return mono * 2.0, [mono, mono * 0.5], [1.0, 0.0]    # mixed, per-beam monos, gates

    def reset(self):
        pass


def _controller(cov, mixer, **kw):
    return MultiBeamController(
        _config(), "A", n_beams=2,
        doa_source_factory=lambda: _StubDoa(cov),
        mixer_factory=lambda: mixer,
        **kw,
    )


def test_plan_detects_snaps_to_seat_and_aims_the_mixer():
    import conf_pipeline_control as cc
    cov = _cov_from_sources(cc.sensibel_8(radius_m=0.04), [0.0], [1.0])    # one talker due +Y
    mixer = _StubMixer()
    ctl = _controller(cov, mixer)
    ctl.plan(t=0.0)
    active = [s for s in mixer.slots if s.active]
    assert len(active) == 1
    assert active[0].seat_id == "sofa-seat1" and round(active[0].azimuth_deg) == 0   # snapped to the seat


def test_plan_with_silence_leaves_all_slots_idle():
    import conf_pipeline_control as cc
    cov = _cov_from_sources(cc.sensibel_8(radius_m=0.04), [], [], noise=1e-3)    # no source -> flat map
    mixer = _StubMixer()
    ctl = _controller(cov, mixer)
    ctl.plan(t=0.0)
    assert all(not s.active for s in mixer.slots)


def test_process_block_dispatches_emits_and_stores_tracks():
    import conf_pipeline_control as cc
    cov = _cov_from_sources(cc.sensibel_8(radius_m=0.04), [0.0], [1.0])
    mixer = _StubMixer()
    doa = _StubDoa(cov)
    out = []
    ctl = MultiBeamController(_config(), "A", n_beams=2, output_callback=out.append,
                             doa_source_factory=lambda: doa, mixer_factory=lambda: mixer)
    block = np.zeros((512, 8), dtype=np.float32)
    mixed = ctl.process_block(block)
    assert doa.blocks == 1                                   # DOA covariance got the raw block
    assert out and np.allclose(out[0], mixed)               # mixed feed emitted to the callback
    assert len(ctl.latest_tracks()) == 2                    # per-beam monos stashed for the recorder


def test_status_reflects_the_planned_beams():
    import conf_pipeline_control as cc
    cov = _cov_from_sources(cc.sensibel_8(radius_m=0.04), [0.0], [1.0])
    ctl = _controller(cov, _StubMixer())
    ctl.plan(t=0.0)
    st = ctl.status()
    assert len(st) == 2
    live = [b for b in st if b.active]
    assert len(live) == 1 and live[0].seat_id == "sofa-seat1"
