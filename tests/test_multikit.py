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

from conf_pipeline_control.multikit import (
    KitSelector,
    SelectionState,
    SpeechPresenceScorer,
    crossfade_gains,
)

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
