"""MultiRoomController — combine N kits into one room-wide capture (stub kits, no hardware/streams)."""
import math

import pytest

np = pytest.importorskip("numpy")

import conf_pipeline as cp
from conf_pipeline.model import Point2D, RoomLayout, RoomObject, SeatAnchor
from conf_pipeline.seat_mapper import seats_owned_by_array
from conf_pipeline_control.multiroom import MultiRoomController, RoomKitSpec

BS = 16  # tiny blocksize for deterministic tests


def _config_two_arrays():
    c = cp.create_config("rt", "2026-01-01T00:00:00Z")
    c = cp.add_device(c, cp.create_microphone_array("A1", "Array 1", position=Point2D(0.0, 0.0)))
    c = cp.add_device(c, cp.create_microphone_array("A2", "Array 2", position=Point2D(10.0, 0.0)))
    c.room = RoomLayout(
        vertices=[Point2D(-2, -2), Point2D(12, -2), Point2D(12, 2), Point2D(-2, 2)], height=3.0, units="meters",
        objects=[RoomObject(id="row", kind="bench", position=Point2D(5.0, 0.0),
                            seats=[SeatAnchor(position=Point2D(1.0, 0.0)),
                                   SeatAnchor(position=Point2D(9.0, 0.0))])],
    )
    return c


class _StubKit:
    def __init__(self, tap):
        self._tap, self.started, self.recorder = tap, False, None
        self.gain = self.mute = None

    def start(self):
        self.started = True

    def stop(self):
        self.started = False

    def set_recorder(self, r):
        self.recorder = r

    def set_gain_db(self, v):
        self.gain = v

    def set_mute(self, v):
        self.mute = v

    def status(self):
        return []

    def read_level(self):
        return 0.0

    def emit(self, mixed):
        self._tap(mixed)


class _Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


def _ctrl(specs=None, *, record=None, fail=(), **kw):
    record = record if record is not None else []

    def factory(config, spec, owned, tap, ctrl):
        if record and record[-1] is None:                    # never
            pass
        k = _StubKit(tap)
        record.append((spec, owned, k))
        idx = len(record) - 1
        if idx in fail:
            raise RuntimeError(f"kit {idx} boom")
        return k

    if specs is None:
        specs = [RoomKitSpec(device=1, array_id="A1"), RoomKitSpec(device=2, array_id="A2")]
    c = MultiRoomController(_config_two_arrays(), specs, blocksize=BS,
                            kit_factory=factory, output_stream_factory=lambda ctrl: None,
                            time_fn=_Clock(), **kw)
    return c, record


def _set(ctrl, k, block, score, t=0.0):
    ctrl._stores[k] = np.asarray(block, dtype=np.float32)
    ctrl._scores[k] = score
    ctrl._last_emit[k] = t


def test_distinct_device_guard():
    with pytest.raises(ValueError):
        MultiRoomController(_config_two_arrays(),
                            [RoomKitSpec(device=4, array_id="A1"), RoomKitSpec(device=4, array_id="A2")])


def test_owned_seats_computed_and_passed_disjoint_and_total():
    c, rec = _ctrl()
    c.start()
    owned = {spec.array_id: set(owned) for spec, owned, _k in rec}
    assert owned["A1"] == {"row-seat1"} and owned["A2"] == {"row-seat2"}
    assert owned["A1"].isdisjoint(owned["A2"])
    assert owned["A1"] | owned["A2"] == {"row-seat1", "row-seat2"}


def test_combine_is_nom_automix_of_live_kits():
    c, _ = _ctrl()
    ones = np.ones(BS, dtype=np.float32)
    _set(c, 0, ones, 1.0)
    _set(c, 1, ones, 1.0)
    out = c._produce(0.0)
    assert np.allclose(out, 2.0 / math.sqrt(2.0))            # two equal open kits → NOM −3 dB


def test_silent_kit_does_not_attenuate_the_active_one():
    c, _ = _ctrl()
    _set(c, 0, np.ones(BS, dtype=np.float32), 1.0)           # talking
    _set(c, 1, np.ones(BS, dtype=np.float32), 0.0)           # silent (score 0)
    assert np.allclose(c._produce(0.0), 1.0)                 # kit 1 excluded from the NOM count → unity


def test_watchdog_drops_a_stalled_kit():
    c, _ = _ctrl(watchdog_blocks=2)                          # ~ 2*BS/44100 s
    _set(c, 0, np.ones(BS, dtype=np.float32), 1.0, t=0.0)    # stale (emitted at t=0)
    _set(c, 1, 2 * np.ones(BS, dtype=np.float32), 1.0, t=1.0)
    out = c._produce(1.0)                                    # at t=1s kit0 is far past the watchdog
    assert np.allclose(out, 2.0)                             # only kit1 survives (unity NOM)
    assert c.status()[0].dead and not c.status()[1].dead


def test_master_mute_and_gain():
    c, _ = _ctrl()
    _set(c, 0, np.ones(BS, dtype=np.float32), 1.0)
    c.set_gain_db(6.0)
    assert np.allclose(c._produce(0.0), 10.0 ** (6.0 / 20.0), atol=1e-4)
    c.set_gain_db(0.0)
    c.set_mute(True)
    assert not np.any(c._produce(0.0))


def test_one_kit_fails_to_start_others_run():
    c, rec = _ctrl(fail=(0,))
    c.start()                                                # must NOT raise (kit1 starts)
    assert c.streaming
    assert c.status()[0].dead and c.status()[0].error
    assert rec[1][2].started is True                         # kit1's stub started


def test_record_tracks_attaches_recorders_and_writes_room_feed(tmp_path):
    c, rec = _ctrl(record=[])
    c.start()
    c.record_tracks(True)
    for _spec, _owned, kit in rec:
        assert kit.recorder is not None                      # a per-kit recorder was attached
    for _ in range(3):
        c._produce(0.0)                                      # room feed accumulates while recording
    c.record_tracks(False)
    paths = [p.rsplit("\\", 1)[-1].rsplit("/", 1)[-1] for p in c.write_tracks(str(tmp_path), prefix="room")]
    assert "room_mixed.wav" in paths                         # the combined room feed was written
