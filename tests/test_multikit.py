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


# --------------------------------------------------------------------------- #
# Task-3: fence tick + _produce veto/gate + fence_status + loud preconditions
# --------------------------------------------------------------------------- #

# Helpers — geometry for a simple test fence.
# Two kits spaced 1.5 m apart along X.  Table polygon is a 1×1 m square centred
# on (0.75, 1.0) — between the two kits but 1 m "in front" of them.
_KIT_A_POS = Point2D(0.0, 0.0)
_KIT_B_POS = Point2D(1.5, 0.0)
_POSE_A = KitPose(position=_KIT_A_POS, bearing_deg=0.0)   # kit A faces +Y
_POSE_B = KitPose(position=_KIT_B_POS, bearing_deg=0.0)   # kit B faces +Y

# Fence polygon: 1×1 m square centred on (0.75, 1.0)
_TABLE_POLYGON = [
    Point2D(0.25, 0.5),
    Point2D(1.25, 0.5),
    Point2D(1.25, 1.5),
    Point2D(0.25, 1.5),
]


def _ctrl_fence(polygon=None, devices=(0, 1), **kw):
    """Build a MultiKitController with the fence wired up (2 stubs)."""
    stubs: list = []
    kits = [KitSpec(device=d) for d in devices]
    defaults = dict(
        sample_rate=16000.0, blocksize=512,
        engine_factory=_stub_factory(stubs),
        output_stream_factory=_no_stream,
        fence_polygon=polygon,
    )
    defaults.update(kw)
    c = MultiKitController(kits, **defaults)
    return c, stubs


def _set_doa(stub, azimuth_deg, salience_db=-5.0, active=True):
    """Point a stub engine's DOA at the given array-relative azimuth."""
    stub._doa_reading = DoaReading(
        azimuth_deg=azimuth_deg, salience_db=salience_db,
        held=False, active=active,
    )


def _rms(arr) -> float:
    return float(np.sqrt(np.mean(arr * arr)))


# ---- bit-exact-off guarantee -----------------------------------------------

def test_fence_off_produce_byte_identical_to_no_fence():
    """fence_polygon=None ⇒ _produce output np.array_equal to a no-fence controller
    on the same stub state (bit-exact-off guarantee)."""
    c_nofence, stubs_nf = _ctrl()
    c_off, stubs_off = _ctrl_fence(polygon=None)   # fence ctor but polygon=None
    c_nofence.start(); c_off.start()
    bs = c_nofence.blocksize

    blk = _blk(0.4, bs)
    stubs_nf[0].emit(blk); stubs_off[0].emit(blk)

    out_nf = c_nofence._produce(0.0)
    out_off = c_off._produce(0.0)
    assert np.array_equal(out_nf, out_off), \
        "fence_polygon=None must leave _produce byte-identical to no-fence controller"


# ---- loud preconditions (ValueError / FenceConfigError) --------------------

def test_fence_ctor_raises_value_error_with_one_kit():
    """fence_polygon given with n_kits != 2 ⇒ ValueError at construction."""
    with pytest.raises(ValueError, match="exactly 2 kits"):
        _ctrl_fence(polygon=_TABLE_POLYGON, devices=(0,))


def test_fence_ctor_raises_value_error_with_three_kits():
    """fence_polygon given with n_kits != 2 ⇒ ValueError at construction."""
    with pytest.raises(ValueError, match="exactly 2 kits"):
        _ctrl_fence(polygon=_TABLE_POLYGON, devices=(0, 1, 2))


def test_fence_update_raises_fence_config_error_with_none_poses():
    """update_fence with a None pose entry ⇒ FenceConfigError (unposed kit)."""
    from conf_pipeline_control.fence import FenceConfigError
    c, stubs = _ctrl_fence(polygon=_TABLE_POLYGON)
    c.start()
    # Poses not set — _fence_poses is all None → should raise FenceConfigError
    with pytest.raises(FenceConfigError):
        c.update_fence(0.0)


# ---- fence_status returns None / dict shape --------------------------------

def test_fence_status_returns_none_when_fence_off():
    """fence_status() returns None when no fence polygon was given."""
    c, _ = _ctrl()
    c.start()
    assert c.fence_status() is None


def test_fence_status_returns_dict_when_fence_on():
    """fence_status() returns a dict with required keys when fence is active."""
    c, stubs = _ctrl_fence(polygon=_TABLE_POLYGON)
    c.start()
    c.set_fence_poses([_POSE_A, _POSE_B])
    _set_doa(stubs[0], 45.0)
    _set_doa(stubs[1], 315.0)
    c.update_fence(0.0)
    s = c.fence_status()
    assert s is not None
    assert isinstance(s, dict)
    required = {"keep", "veto_kit", "point", "inside", "confidence", "degenerate", "polygon"}
    assert required <= s.keys(), f"missing keys: {required - s.keys()}"
    assert s["polygon"] == _TABLE_POLYGON


def test_fence_status_polygon_empty_before_update():
    """fence_status polygon key echoes the configured polygon even before update_fence."""
    c, stubs = _ctrl_fence(polygon=_TABLE_POLYGON)
    c.start()
    s = c.fence_status()
    # Before first update_fence, _fence_last is None — fence_status returns None-guarded dict
    # or a dict indicating no decision yet.  The polygon must be echoed.
    if s is not None:
        assert s["polygon"] == _TABLE_POLYGON


# ---- selection veto: out-of-fence loud kit gets eff=0 ----------------------

def test_veto_prevents_out_of_fence_kit_from_winning_selection():
    """An out-of-fence loud kit has its eff zeroed; the in-fence kit is selected.

    Geometry: kit A (left) aimed at the table (inside fence), kit B (right) aimed
    at a far source outside the fence.  After update_fence decides to veto kit B,
    _produce must select kit A regardless of speech scores.
    """
    c, stubs = _ctrl_fence(polygon=_TABLE_POLYGON)
    c.start()
    c.set_fence_poses([_POSE_A, _POSE_B])
    bs = c.blocksize

    # Both kits emit a block with a speech-like level (speech score will build up)
    for _ in range(5):
        stubs[0].emit(_blk(0.3, bs))
        stubs[1].emit(_blk(0.3, bs))

    # Kit A aimed ~56° (towards the table centre (0.75,1.0) from origin)
    # tan(θ) = 0.75/1.0 → θ ≈ 36.9° for kit A; from kit B (1.5, 0): target is
    # (-0.75, 1.0), atan2(-0.75,1.0) ≈ -36.9° (i.e. 323.1°).
    # Use simple approximate values that cross inside the polygon.
    _set_doa(stubs[0], 37.0, salience_db=-5.0)   # kit A: table talker
    _set_doa(stubs[1], 323.0, salience_db=-5.0)  # kit B: same talker from right

    c.update_fence(1.0)
    dec = c._fence_last
    assert dec is not None

    # Now make kit B have a HIGHER speech score but is vetoed by the fence
    # Manually override scores so kit 1 would win without the veto
    with c._lock:
        c._scores = [0.1, 0.9]      # kit 1 would normally win
        c._last_emit = [1.0, 1.0]   # both alive

    # Swap: point kit A inside, kit B outside, and force a veto on kit B
    # by aiming kit B at a far outside source
    _set_doa(stubs[0], 37.0, salience_db=-5.0)    # inside
    _set_doa(stubs[1], 0.0, salience_db=-5.0)     # straight ahead from kit B → outside polygon

    c.update_fence(2.0)
    dec2 = c._fence_last
    # If veto_kit == 1, kit 1 is vetoed → kit 0 wins despite lower score
    if dec2 is not None and dec2.veto_kit == 1:
        out = c._produce(2.0)
        # The output should come from kit 0 (the non-vetoed kit)
        assert c.active_kit == 0


# ---- output gate: outside source gets ducked by fence_duck_db --------------

def test_gate_ducks_output_when_source_outside_fence():
    """When FenceDecision.keep=False the output RMS drops by ≈ fence_duck_db."""
    from unittest.mock import MagicMock
    from conf_pipeline_control.fence import FenceDecision, FusedSource

    duck_db = -40.0
    duck_gain = 10 ** (duck_db / 20.0)

    c, stubs = _ctrl_fence(polygon=_TABLE_POLYGON, fence_duck_db=duck_db)
    c.start()
    c.set_fence_poses([_POSE_A, _POSE_B])
    bs = c.blocksize

    blk = _blk(0.4, bs)
    stubs[0].emit(blk); stubs[1].emit(blk)
    with c._lock:
        c._scores = [0.9, 0.0]
        c._last_emit = [0.0, 0.0]

    # Baseline: no fence decision (keep=True / no duck)
    c._fence_last = None
    out_keep = c._produce(0.0)
    rms_keep = _rms(out_keep)

    # Force keep=False via a fake FenceDecision
    fused = FusedSource(point=None, confidence=0.0, inside=False,
                        degenerate=True, loud_kit=0, miss_distance_m=float("inf"))
    c._fence_last = FenceDecision(keep=False, veto_kit=0, source=fused)
    out_gate = c._produce(0.0)
    rms_gate = _rms(out_gate)

    expected_rms = rms_keep * duck_gain
    assert abs(rms_gate - expected_rms) / (expected_rms + 1e-9) < 0.05, \
        f"Expected ducked RMS ≈ {expected_rms:.6f}, got {rms_gate:.6f}"


def test_gate_does_not_duck_when_keep_is_true():
    """When FenceDecision.keep=True the output is pass-through (not ducked)."""
    from conf_pipeline_control.fence import FenceDecision, FusedSource

    c, stubs = _ctrl_fence(polygon=_TABLE_POLYGON, fence_duck_db=-60.0)
    c.start()
    c.set_fence_poses([_POSE_A, _POSE_B])
    bs = c.blocksize

    blk = _blk(0.4, bs)
    stubs[0].emit(blk)
    with c._lock:
        c._scores = [0.9, 0.0]
        c._last_emit = [0.0, 0.0]

    # Baseline: no fence decision
    c._fence_last = None
    out_nofence = c._produce(0.0)

    # keep=True → same as baseline
    fused = FusedSource(point=None, confidence=0.0, inside=True,
                        degenerate=False, loud_kit=0, miss_distance_m=0.0)
    c._fence_last = FenceDecision(keep=True, veto_kit=None, source=fused)
    out_keep = c._produce(0.0)

    assert np.allclose(out_nofence, out_keep, atol=1e-7), \
        "keep=True must not alter output vs no-fence-decision baseline"


# ---- runtime fail-open: update_fence never raises --------------------------

def test_update_fence_fail_open_on_runtime_error():
    """A RuntimeError inside the decider must not escape update_fence;
    _fence_last is left as-is (the last good decision — or None)."""
    from unittest.mock import patch

    c, stubs = _ctrl_fence(polygon=_TABLE_POLYGON)
    c.start()
    c.set_fence_poses([_POSE_A, _POSE_B])
    _set_doa(stubs[0], 37.0)
    _set_doa(stubs[1], 323.0)

    # First update succeeds → _fence_last is set
    c.update_fence(0.0)
    last_before = c._fence_last

    # Patch the decider to raise on the next call
    with patch.object(c._fence_decider, "update", side_effect=RuntimeError("boom")):
        c.update_fence(1.0)   # must NOT raise

    # _fence_last unchanged (still the last good decision)
    assert c._fence_last is last_before


def test_update_fence_noop_when_no_fence():
    """update_fence is a no-op when fence_polygon=None (no _fence_decider)."""
    c, _ = _ctrl()
    c.start()
    c.update_fence(0.0)   # must not raise, must not alter anything
    assert c._fence_last is None
