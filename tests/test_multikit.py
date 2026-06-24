"""Pure, hardware-free core of the dual-POLARIS "2-Kit" cross-array automix.

These cover the three pieces that carry the correctness invariants and need NO
sounddevice / numpy / hardware:

* ``crossfade_gains`` — equal-power cos/sin ramp (reused from BeamEngine._mix math).
* ``SpeechPresenceScorer`` — the **fan-proof** speech-presence metric (Invariant A):
  a steady directional source (fan/AC) must score ~0 because it does not modulate in
  the syllabic band; real speech (3-8 Hz envelope modulation) scores high.
* ``KitSelector`` — hysteresis/hold selection across kits (modeled on _TalkerTracker),
  driven by the speech-presence score, NOT raw level / a SRP-PHAT voice flag.
"""
import math

import pytest

from conf_pipeline_control.multikit import (
    KitSelector,
    KitSpec,
    KitStatus,
    MultiKitController,
    SelectionState,
    SpeechPresenceScorer,
    _default_engine_factory,
    crossfade_gains,
)
from conf_pipeline_control.fence import KitPose, KitReading
from conf_pipeline_control.polaris_beamformer import DoaReading
from conf_pipeline.model import Point2D

np = pytest.importorskip("numpy")

HOP = 0.032  # s, ~32 ms block (the engine default)


# --------------------------------------------------------------------------- #
# crossfade_gains — equal power, monotone, correct endpoints
# --------------------------------------------------------------------------- #
def test_crossfade_gains_equal_power_and_endpoints():
    total = 6
    last_out, last_in = 1.0, 0.0
    for step in range(0, total + 1):
        g_out, g_in = crossfade_gains(step, total)
        assert abs(g_out * g_out + g_in * g_in - 1.0) < 1e-9     # equal power
        assert g_out <= last_out + 1e-9                          # outgoing falls
        assert g_in >= last_in - 1e-9                            # incoming rises
        last_out, last_in = g_out, g_in
    assert crossfade_gains(0, total) == (1.0, 0.0)
    g_out, g_in = crossfade_gains(total, total)
    assert abs(g_out) < 1e-9 and abs(g_in - 1.0) < 1e-9


def test_crossfade_gains_clamps_out_of_range_step():
    assert crossfade_gains(-3, 6) == (1.0, 0.0)
    g_out, g_in = crossfade_gains(99, 6)
    assert abs(g_out) < 1e-9 and abs(g_in - 1.0) < 1e-9


# --------------------------------------------------------------------------- #
# SpeechPresenceScorer — the fan-proofing core (Invariant A)
# --------------------------------------------------------------------------- #
def _feed(scorer, rms_seq, noise_floor=0.0):
    score = 0.0
    for rms in rms_seq:
        score = scorer.update(rms, noise_floor=noise_floor)
    return score


def test_steady_source_scores_low_modulated_speech_scores_high():
    """A loud STEADY source (a fan) must score ~0; a syllabically-modulated source
    (speech, ~5 Hz) must score clearly higher. This is the directional-steady-source
    test that Invariant A makes mandatory."""
    n = int(3.0 / HOP)                                  # ~3 s to settle
    steady = [0.10] * n                                 # constant level — a fan
    modulated = [0.10 * (1.0 + 0.8 * math.sin(2 * math.pi * 5.0 * k * HOP)) for k in range(n)]

    s_steady = _feed(SpeechPresenceScorer(hop_seconds=HOP), steady)
    s_mod = _feed(SpeechPresenceScorer(hop_seconds=HOP), modulated)

    assert s_steady < 0.05                              # the fan is NOT speech
    assert s_mod > s_steady + 0.10                      # speech clearly beats the fan
    assert s_mod > 0.2


def test_louder_fan_still_loses_to_quieter_speech():
    """The wrong-incumbent guard at the metric level: a LOUDER steady fan must still
    score below a QUIETER modulated talker — selection must not be level-driven."""
    n = int(3.0 / HOP)
    loud_fan = [0.30] * n
    quiet_speech = [0.06 * (1.0 + 0.8 * math.sin(2 * math.pi * 5.0 * k * HOP)) for k in range(n)]
    s_fan = _feed(SpeechPresenceScorer(hop_seconds=HOP), loud_fan)
    s_speech = _feed(SpeechPresenceScorer(hop_seconds=HOP), quiet_speech)
    assert s_speech > s_fan


def test_silence_scores_zero():
    n = int(2.0 / HOP)
    assert _feed(SpeechPresenceScorer(hop_seconds=HOP), [0.0] * n) < 1e-6


def test_reset_clears_state():
    sc = SpeechPresenceScorer(hop_seconds=HOP)
    _feed(sc, [0.1 * (1 + 0.8 * math.sin(2 * math.pi * 5 * k * HOP)) for k in range(60)])
    sc.reset()
    # after reset, a single silent frame is back near zero
    assert sc.update(0.0) < 1e-6


# --------------------------------------------------------------------------- #
# KitSelector — hysteresis / hold, driven by the speech-presence score
# --------------------------------------------------------------------------- #
def test_selector_picks_the_higher_speech_score():
    sel = KitSelector(n_kits=2)
    st = sel.update([0.05, 0.8], t=0.0)
    assert isinstance(st, SelectionState)
    assert st.active == 1 and st.speech_present


def test_selector_hysteresis_no_flap_on_near_equal_scores():
    sel = KitSelector(n_kits=2, switch_margin=0.15)
    sel.update([0.7, 0.2], t=0.0)                        # kit 0 active
    flips = 0
    prev = 0
    # jitter both scores around a near-equal point inside the margin
    for k in range(1, 200):
        a = 0.5 + 0.05 * math.sin(k * 0.7)
        b = 0.5 + 0.05 * math.cos(k * 0.9)
        st = sel.update([a, b], t=k * HOP)
        flips += int(st.active != prev)
        prev = st.active
    assert flips == 0                                    # margin prevents ping-pong


def test_selector_switches_to_speech_over_a_fan_regardless_of_level():
    """Invariant A at the selector level: kit 0 has a high *level* but a low speech
    score (fan); kit 1 has a high speech score (talker). Active must be kit 1."""
    sel = KitSelector(n_kits=2)
    # fan-only first → no false speaker, holds default
    st = sel.update([0.02, 0.0], t=0.0)
    assert not st.speech_present
    # now a real talker on kit 1 (fan still "loud" but low score)
    st = sel.update([0.02, 0.6], t=HOP)
    assert st.active == 1 and st.switching and st.speech_present


def test_selector_holds_through_brief_pause_then_keeps_last_active():
    sel = KitSelector(n_kits=2, hold_seconds=0.4)
    sel.update([0.7, 0.0], t=0.0)                        # kit 0 talking
    # brief pause (both quiet) under the hold → still "present", still kit 0
    st = sel.update([0.05, 0.0], t=0.2)
    assert st.active == 0 and st.speech_present
    # long silence past the hold → no longer present, but still holds last-active (no fan grab)
    st = sel.update([0.0, 0.0], t=2.0)
    assert st.active == 0 and not st.speech_present


def test_selector_fan_only_room_never_marks_a_speaker():
    sel = KitSelector(n_kits=2, speech_threshold=0.15)
    present = False
    for k in range(100):
        st = sel.update([0.05, 0.04], t=k * HOP)         # two fans, both sub-threshold
        present = present or st.speech_present
    assert not present


# --------------------------------------------------------------------------- #
# MultiKitController — driven by STUB engines (no sounddevice, no hardware)
# --------------------------------------------------------------------------- #
class _Clock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t


class _StubEngine:
    """Stands in for a PolarisBeamformer: the controller registers a tap as our
    output_callback; the test drives blocks through it with ``emit``.

    Also exposes a settable ``_doa_reading`` so Task-2 tests can verify
    ``kit_reading()`` maps DoaReading → KitReading correctly.
    """

    def __init__(self) -> None:
        self._tap = None
        self.started = False
        self.current_doa_deg = None
        # Settable for Task-2 kit_reading tests
        self._doa_reading: DoaReading = DoaReading(
            azimuth_deg=None, salience_db=0.0, held=False, active=False
        )

    def start(self) -> None:
        self.started = True

    def stop(self) -> None:
        self.started = False

    def emit(self, block) -> None:
        if self._tap is not None:
            self._tap(block)

    def reading(self) -> DoaReading:
        """Return the current (settable) DoaReading snapshot."""
        return self._doa_reading


def _stub_factory(collect):
    def factory(spec, tap, ctrl):
        e = _StubEngine()
        e._tap = tap
        collect.append(e)
        return e
    return factory


def _no_stream(ctrl):
    return None                                          # headless: drive _produce() directly


def _ctrl(devices=(0, 1), **kw):
    stubs: list = []
    kits = [KitSpec(device=d) for d in devices]
    defaults = dict(sample_rate=16000.0, blocksize=512, engine_factory=_stub_factory(stubs),
                    output_stream_factory=_no_stream)
    defaults.update(kw)
    c = MultiKitController(kits, **defaults)
    return c, stubs


def _blk(val, n):
    return np.full(n, float(val), np.float32)


def test_controller_distinct_device_guard():
    with pytest.raises(ValueError):
        MultiKitController([KitSpec(device=5), KitSpec(device=5)], output_stream_factory=_no_stream)


def test_default_factory_strips_per_kit_agc():
    """Invariant B: per-kit AGC is forced OFF (one AGC lives on the combined output)."""
    c, _ = _ctrl()
    eng = _default_engine_factory(KitSpec(device=None, cfg={"agc_target_db": -10.0}),
                                  lambda m: None, c)
    assert eng.agc_target_db is None


def test_controller_selects_the_speaking_kit_over_a_steady_one():
    """End-to-end through the scorer: kit 0 is a LOUDER steady fan, kit 1 a quieter
    modulated talker → the controller outputs kit 1 (fan-proof, level-independent)."""
    clock = _Clock()
    c, stubs = _ctrl(time_fn=clock)
    c.start()
    bs, hop = c.blocksize, c._hop_s
    for k in range(140):
        clock.t = k * hop
        stubs[0].emit(_blk(0.25, bs))                                # loud steady fan
        a = 0.08 * (1.0 + 0.8 * math.sin(2 * math.pi * 5.0 * k * hop))  # quiet 5 Hz speech
        stubs[1].emit(_blk(a, bs))
        c._produce(clock.t)
    assert c.active_kit == 1
    st = c.status()
    assert isinstance(st[1], KitStatus) and st[1].score > st[0].score


def test_controller_crossfade_is_equal_power_between_kits():
    """A switch ramps over crossfade_blocks; for uncorrelated sources the output power
    stays ~constant (equal-power) and lands exactly on the incoming kit."""
    clock = _Clock()
    c, _ = _ctrl(crossfade_blocks=4, time_fn=clock)
    bs = c.blocksize
    rng = np.random.default_rng(0)
    a = rng.standard_normal(bs).astype(np.float32)
    b = rng.standard_normal(bs).astype(np.float32)
    c._stores = [a, b]
    c._last_emit = [0.0, 0.0]
    c._scores = [0.9, 0.0]
    assert np.allclose(c._produce(0.0), a, atol=1e-5)                 # playing kit 0
    c._scores = [0.0, 0.9]                                            # kit 1 takes over → cross-fade
    outs = []
    for _ in range(5):
        c._last_emit = [clock.t, clock.t]
        outs.append(c._produce(clock.t))
        clock.t += 1e-3
    assert np.allclose(outs[0], a, atol=1e-5)                         # step 0 → outgoing
    assert np.allclose(outs[-1], b, atol=1e-5)                        # fade complete → incoming
    rmss = [float(np.sqrt(np.mean(o * o))) for o in outs]
    assert max(rmss) < 1.4 * min(rmss)                               # no big dip/bump (equal power)
    assert not np.allclose(outs[1], a) and not np.allclose(outs[1], b)  # a genuine blend mid-fade


def test_controller_single_agc_normalizes_the_combined_output():
    """Invariant B: ONE AGC on the combine pulls a loud active kit toward the target."""
    clock = _Clock()
    c, _ = _ctrl(agc_target_db=-20.0, time_fn=clock)
    assert c._agc is not None
    bs = c.blocksize
    c._stores = [_blk(0.5, bs), None]
    c._scores = [0.9, 0.0]
    out = None
    for k in range(250):
        clock.t = k * 0.032
        c._last_emit = [clock.t, None]
        out = c._produce(clock.t)
    target = 10.0 ** (-20.0 / 20.0)
    assert abs(float(np.sqrt(np.mean(out * out))) - target) / target < 0.1


def test_controller_master_mute_and_gain():
    c, _ = _ctrl()
    bs = c.blocksize
    c._stores = [_blk(0.5, bs), None]
    c._scores = [0.9, 0.0]
    c._last_emit = [0.0, None]
    assert np.allclose(c._produce(0.0), 0.5, atol=1e-6)
    c.set_mute(True)
    assert np.allclose(c._produce(0.0), 0.0)
    c.set_mute(False)
    c.set_gain_db(20.0 * math.log10(2.0))                            # +6.02 dB → ×2
    assert np.allclose(c._produce(0.0), 1.0, rtol=0.02)


def test_controller_watchdog_drops_a_stalled_kit():
    """Invariant D: a kit that stops emitting is watch-dogged out of contention; the
    output switches to the live kit and the dead one is flagged — no exception."""
    clock = _Clock()
    c, _ = _ctrl(time_fn=clock, watchdog_blocks=5)
    bs = c.blocksize
    c._stores = [_blk(0.4, bs), _blk(0.4, bs)]
    c._scores = [0.9, 0.0]
    c._last_emit = [0.0, 0.0]
    clock.t = 0.0
    c._produce(0.0)
    assert c.active_kit == 0
    c._scores = [0.9, 0.9]                                            # kit 0 still "scores" but is STALE
    c._last_emit = [0.0, 10.0]                                        # kit 1 fresh, kit 0 long stale
    clock.t = 10.0
    c._produce(10.0)
    st = c.status()
    assert st[0].dead and not st[1].dead
    assert c.active_kit == 1


def test_controller_one_kit_fails_to_start_others_run():
    def factory(spec, tap, ctrl):
        if spec.device == 0:
            raise RuntimeError("boom")
        e = _StubEngine()
        e._tap = tap
        return e
    c = MultiKitController([KitSpec(0), KitSpec(1)], engine_factory=factory,
                           output_stream_factory=_no_stream)
    c.start()                                                         # must NOT raise — kit 1 is up
    assert c.streaming
    st = c.status()
    assert st[0].dead and st[0].error
    assert not st[1].dead


# --------------------------------------------------------------------------- #
# Task-2: set_fence_poses + kit_reading (read-only accessors, no _produce change)
# --------------------------------------------------------------------------- #

def test_set_fence_poses_stores_under_lock():
    """set_fence_poses persists poses retrievable from _fence_poses."""
    c, stubs = _ctrl()
    c.start()
    pose0 = KitPose(position=Point2D(0.0, 0.0), bearing_deg=0.0)
    pose1 = KitPose(position=Point2D(1.5, 0.0), bearing_deg=90.0)
    c.set_fence_poses([pose0, pose1])
    with c._lock:
        stored = list(c._fence_poses)
    assert stored[0] == pose0
    assert stored[1] == pose1


def test_set_fence_poses_accepts_none_entries():
    """None entries are valid (partial configuration)."""
    c, _ = _ctrl()
    c.start()
    c.set_fence_poses([None, None])
    with c._lock:
        stored = list(c._fence_poses)
    assert stored == [None, None]


def test_set_fence_poses_initialised_to_none_in_ctor():
    """_fence_poses slot exists and is all-None before any set_fence_poses call."""
    c, _ = _ctrl()
    with c._lock:
        stored = list(c._fence_poses)
    assert stored == [None, None]


def test_kit_reading_maps_doa_reading_and_level():
    """kit_reading returns a KitReading with azimuth/salience from DoaReading
    and the controller's own level snapshot."""
    c, stubs = _ctrl()
    c.start()
    bs = c.blocksize
    # Emit a block so _levels[0] is non-zero
    stubs[0].emit(_blk(0.3, bs))
    # Set a recognisable DoaReading on stub engine 0
    stubs[0]._doa_reading = DoaReading(
        azimuth_deg=45.0, salience_db=-8.5, held=False, active=True
    )
    kr = c.kit_reading(0)
    assert kr is not None
    assert isinstance(kr, KitReading)
    assert kr.azimuth_deg == pytest.approx(45.0)
    assert kr.salience_db == pytest.approx(-8.5)
    assert kr.level > 0.0           # populated from _levels[0]
    assert kr.active is True        # DoaReading.active=True


def test_kit_reading_active_folds_held():
    """active in KitReading is True when DoaReading.held is True even if active=False."""
    c, stubs = _ctrl()
    c.start()
    stubs[0]._doa_reading = DoaReading(
        azimuth_deg=10.0, salience_db=-5.0, held=True, active=False
    )
    kr = c.kit_reading(0)
    assert kr is not None
    assert kr.active is True        # held=True → active in KitReading


def test_kit_reading_active_false_when_both_flags_false():
    """active is False when DoaReading.held=False and active=False."""
    c, stubs = _ctrl()
    c.start()
    stubs[0]._doa_reading = DoaReading(
        azimuth_deg=None, salience_db=0.0, held=False, active=False
    )
    kr = c.kit_reading(0)
    assert kr is not None
    assert kr.active is False


def test_kit_reading_none_engine_returns_none():
    """kit_reading returns None when the engine slot is None (kit never started)."""
    c, _ = _ctrl()
    # Do NOT call c.start() — engines remain None
    assert c.kit_reading(0) is None
    assert c.kit_reading(1) is None


def test_kit_reading_none_azimuth_still_returns_kit_reading():
    """When DoaReading.azimuth_deg is None, kit_reading still returns a KitReading
    with azimuth_deg=None and active=False (not a Python None)."""
    c, stubs = _ctrl()
    c.start()
    stubs[1]._doa_reading = DoaReading(
        azimuth_deg=None, salience_db=0.0, held=False, active=False
    )
    kr = c.kit_reading(1)
    assert kr is not None
    assert kr.azimuth_deg is None
    assert kr.salience_db == pytest.approx(0.0)
    assert kr.active is False


def test_kit_reading_does_not_hold_controller_lock_while_calling_engine():
    """Verify the lock is released before calling eng.reading() — detect a
    potential deadlock by interleaving: if the controller lock were held across
    eng.reading() a concurrent _on_kit_output (which acquires the same lock)
    would deadlock.  We simulate this by checking the method returns at all
    while _on_kit_output runs concurrently (smoke-level, not timing-sensitive)."""
    import threading

    c, stubs = _ctrl()
    c.start()
    bs = c.blocksize
    results: list = []

    def reader():
        for _ in range(50):
            kr = c.kit_reading(0)
            results.append(kr)

    def emitter():
        for _ in range(50):
            stubs[0].emit(_blk(0.1, bs))

    t1 = threading.Thread(target=reader)
    t2 = threading.Thread(target=emitter)
    t1.start(); t2.start()
    t1.join(timeout=5.0); t2.join(timeout=5.0)
    assert not t1.is_alive() and not t2.is_alive(), "threads did not finish (possible deadlock)"
    # All non-None results are KitReading instances
    assert all(isinstance(r, KitReading) for r in results if r is not None)


def test_produce_output_unchanged_after_set_fence_poses():
    """set_fence_poses is purely additive — _produce output is byte-identical to
    pre-fence baseline (opt-in / bit-exact-off guarantee, Task-2 scope)."""
    c_base, stubs_base = _ctrl()
    c_new, stubs_new = _ctrl()
    c_base.start(); c_new.start()
    bs = c_base.blocksize

    # Identical stores + scores
    blk = _blk(0.4, bs)
    stubs_base[0].emit(blk); stubs_new[0].emit(blk)

    c_new.set_fence_poses([
        KitPose(position=Point2D(0.0, 0.0), bearing_deg=0.0),
        KitPose(position=Point2D(1.5, 0.0), bearing_deg=90.0),
    ])

    out_base = c_base._produce(0.0)
    out_new = c_new._produce(0.0)
    assert np.array_equal(out_base, out_new), "set_fence_poses must not alter _produce output"
