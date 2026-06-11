"""OCTOVOX bridge — pure azimuth/zone mapping (no server, no numpy)."""
import pytest

import conf_pipeline as cp
from conf_pipeline.model import Point2D, RectShape
import conf_pipeline_control as cc


def test_azimuth_conversion_reference_points():
    # this app: 0deg=+Y(CW);  OCTOVOX: 0deg=+X(CCW).  oct = (90 - bearing) % 360
    assert cc.to_octovox_azimuth(0.0) == pytest.approx(90.0)    # +Y (North) -> 90
    assert cc.to_octovox_azimuth(90.0) == pytest.approx(0.0)    # +X (East)  -> 0
    assert cc.to_octovox_azimuth(180.0) == pytest.approx(270.0)  # -Y        -> 270
    assert cc.to_octovox_azimuth(270.0) == pytest.approx(180.0)  # -X        -> 180


def test_azimuth_offset_calibration():
    assert cc.to_octovox_azimuth(0.0, offset_deg=10.0) == pytest.approx(80.0)
    assert cc.to_octovox_azimuth(90.0, offset_deg=-10.0) == pytest.approx(10.0)


def _scene():
    c = cp.create_config("Room", "2026-06-10T00:00:00Z")
    c = cp.set_room(c, cp.rectangular_room(8, 6, 3))
    c = cp.add_device(c, cp.create_microphone_array("A", "Array"))
    c = cp.set_device_position(c, "A", Point2D(4, 3))  # array centred
    return c


def _rect(zid, x, y, w, h, label, kind):
    return cp.CoverageZone(zid, kind, RectShape(Point2D(x, y), w, h), False, label)


def test_zone_azimuths_pickup_to_target_exclusion_to_interferer():
    c = _scene()
    arr = cp.find_device(c, "A")
    arr.zones = [
        _rect("p1", 6, 2.5, 1, 1, "Talker", "dynamic"),   # east of array
        _rect("x1", 1, 2.5, 1, 1, "Door", "exclusion"),   # west of array
    ]
    za = cc.zone_azimuths(c, "A")
    assert za.target_az is not None
    assert len(za.interferer_az) == 1
    # east pickup ≈ compass 90 → octovox 0;  west exclusion ≈ compass 270 → octovox 180
    assert za.target_az == pytest.approx(0.0, abs=5.0)
    assert za.interferer_az[0] == pytest.approx(180.0, abs=5.0)


def test_zone_azimuths_no_pickup_returns_none_with_note():
    c = _scene()
    arr = cp.find_device(c, "A")
    arr.zones = [_rect("x1", 1, 2.5, 1, 1, "Door", "exclusion")]
    za = cc.zone_azimuths(c, "A")
    assert za.target_az is None
    assert "no pickup" in za.note.lower()
    assert len(za.interferer_az) == 1


def test_zone_azimuths_multiple_pickups_targets_first_with_note():
    c = _scene()
    arr = cp.find_device(c, "A")
    arr.zones = [
        _rect("p1", 6, 2.5, 1, 1, "Head", "dynamic"),
        _rect("p2", 2, 2.5, 1, 1, "Foot", "dynamic"),
    ]
    za = cc.zone_azimuths(c, "A")
    assert za.target_az is not None
    assert "Head" in za.note and "2 pickup" in za.note


def test_octovox_client_default_url():
    client = cc.OctovoxClient()
    assert client.base_url == "http://127.0.0.1:5050"
    assert isinstance(cc.octovox_deps_available(), bool)


def test_speech_gate_passes_voice_gates_noise():
    from conf_pipeline_control.octovox_monitor import speech_gate
    nf = None
    seq = [0.003, 0.003, 0.05, 0.003, 0.003]   # noise, noise, VOICE, noise, noise (RMS)
    decisions = []
    for rms in seq:
        is_speech, nf = speech_gate(rms, nf, 2.5)
        decisions.append(is_speech)
    assert decisions[2] is True               # the loud (voice) chunk passes
    assert not any(decisions[i] for i in (0, 1, 3, 4))  # noise chunks are gated


def test_clean_monitor_constructs_with_chunking():
    client = cc.OctovoxClient("http://localhost:9999")  # not contacted here
    mon = cc.CleanMonitor(client, input_device=None, samplerate=44100, chunk_seconds=2.0,
                          target_az=0.0, interferer_az=[180.0])
    assert mon._chunk_samples == 88200  # 2.0 s × 44100
    st = mon.state()
    assert st.running is False and st.chunks_sent == 0


def test_crossfade_join_is_seamless_and_level_matches():
    np = pytest.importorskip("numpy")
    from conf_pipeline_control.octovox_monitor import crossfade_join
    O = 64
    # two overlapping cleaned chunks of a continuous sine; chunk2 starts O before chunk1 ends
    t = np.arange(400) / 400.0
    sig = np.sin(2 * np.pi * 5 * t).astype(np.float32)
    chunk1 = sig[:200].copy()
    chunk2 = sig[200 - O:].copy() * 2.0          # 2× louder → tests level-match

    emit1, tail1 = crossfade_join(None, chunk1, O, np)
    assert len(tail1) == O
    emit2, tail2 = crossfade_join(tail1, chunk2, O, np)
    # the louder chunk2 is pulled back down toward chunk1's level (no jump at the seam)
    seam = np.concatenate([emit1[-5:], emit2[:5]])
    assert np.abs(np.diff(seam)).max() < 0.2     # no click / level step at the join


def test_repair_dead_channels_interpolates_from_ring_neighbors():
    np = pytest.importorskip("numpy")
    # 8-ch ramp signals; capsule index 4 is dead (zeros). active mask has it False.
    y = np.stack([np.full(10, float(i), dtype=np.float32) for i in range(8)])
    y[4] = 0.0
    active = [True, True, True, True, False, True, True, True]
    out = cc.repair_dead_channels(y, active)
    # channel 4 filled with mean of ring-neighbours 3 and 5 → (3+5)/2 = 4
    assert np.allclose(out[4], 4.0)
    # live channels untouched
    assert np.allclose(out[0], 0.0) and np.allclose(out[7], 7.0)
    # all-active mask is a no-op
    assert np.allclose(cc.repair_dead_channels(y, [True] * 8), y)


def test_crossfade_join_first_chunk_holds_tail():
    np = pytest.importorskip("numpy")
    from conf_pipeline_control.octovox_monitor import crossfade_join
    x = np.ones(100, dtype=np.float32)
    emit, tail = crossfade_join(None, x, 16, np)
    assert len(emit) == 100 - 16 and len(tail) == 16


def test_mono_fifo_push_pull_and_underrun():
    np = pytest.importorskip("numpy")
    from conf_pipeline_control.octovox_monitor import _MonoFifo
    f = _MonoFifo()
    assert (f.pull(4) == 0).all()                 # empty → silence
    f.push(np.arange(5, dtype=np.float32))
    assert f.available() == 5
    out = f.pull(3)
    assert list(out) == [0.0, 1.0, 2.0] and f.available() == 2
    out2 = f.pull(4)                              # underrun → zero-padded tail
    assert list(out2[:2]) == [3.0, 4.0] and list(out2[2:]) == [0.0, 0.0]
