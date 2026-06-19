"""Dual-POLARIS **cross-array automix** — run two kits, output the active one.

Two POLARIS kits in one room, each auto-steering to its local talker. A selector
picks whichever kit currently has the active *talker* and the output cross-fades to
it; the result is one mono stream (coverage by zone-switching, **not** simultaneity —
double-talk across zones still yields one stream, following the active speaker).

This module is split so the **decision logic is pure and hardware-free**:

* :func:`crossfade_gains` — the equal-power cos/sin ramp (same math as
  ``BeamEngine._mix``), so a kit-to-kit switch is glitch-free.
* :class:`SpeechPresenceScorer` — the **fan-proof** speech-presence metric. A steady
  directional source (a fan/AC at a fixed bearing) reads as a talker to a DOA VAD,
  which is exactly the bug the minimum-statistics post-NR floor exists to dodge. The
  selector therefore must **not** key off the SRP-PHAT voice flag; it keys off
  *syllabic envelope modulation* (~3-8 Hz, which speech has and a fan does not),
  scaled by the kit's own level. Fan → ~0; speech → high; quiet speech still beats a
  loud fan because level is in the denominator, not the numerator.
* :class:`KitSelector` — hysteresis/hold across kits (modeled on
  ``polaris_beamformer._TalkerTracker``): switch only past a score ``switch_margin``,
  hold the active kit through brief pauses, and on a fan-only / silent room **hold the
  last-active kit and report no speaker** (never grab the fan).

The live :class:`MultiKitController` (two ``PolarisBeamformer`` engines + a single
combined output stream) is built on top of these; it lives behind the ``[control]``
extra. The pure pieces above need no numpy and no sounddevice.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Sequence

# Selection / hold defaults (mirrors the steered tracker's tunables, in score units).
DEFAULT_HOP_SECONDS = 0.032        # ~32 ms block (engine default); the score update cadence
DEFAULT_SWITCH_MARGIN = 0.12       # a challenger must beat the incumbent's score by this to switch
DEFAULT_HOLD_SECONDS = 0.4         # coast the "active speaker" through pauses this long
DEFAULT_SPEECH_THRESHOLD = 0.15    # a score above this counts as speech present

# Speech-presence scorer defaults (syllabic-modulation band ≈ 3-8 Hz via a difference of EMAs).
DEFAULT_TAU_FAST = 0.03            # s — fast envelope EMA (upper syllabic corner)
DEFAULT_TAU_SLOW = 0.15            # s — slow envelope EMA (DC / lower corner)
DEFAULT_TAU_MOD = 0.30            # s — smoothing of the rectified band-passed envelope
DEFAULT_MOD_REF = 0.25            # modulation depth that maps to a full (1.0) speech score
_LEVEL_FLOOR = 1e-4              # guards the modulation-depth denominator at silence


def crossfade_gains(step: int, total: int) -> tuple[float, float]:
    """Equal-power crossfade gains ``(g_out, g_in)`` at fade ``step`` of ``total``.

    ``cos²+sin²=1`` keeps perceived loudness constant across a kit switch. ``step`` is
    clamped to ``[0, total]`` so callers can over-run without a special case. The
    cross-fade mixes two **uncorrelated** sources (different talkers in different
    zones) — equal-power is exactly right and there is deliberately **no** inter-kit
    sample/phase alignment (it would be meaningless across two independent clocks)."""
    if total <= 0:
        return (0.0, 1.0)
    s = min(max(step, 0), total)
    p = s / total
    return (math.cos(p * math.pi / 2.0), math.sin(p * math.pi / 2.0))


class SpeechPresenceScorer:
    """Per-kit, fan-proof speech-presence score from the kit's output level envelope.

    Feed it the per-hop output RMS (a scalar); it returns a score in ``[0, 1]`` that is
    high for syllabically-modulated speech and ~0 for a steady source — *regardless of
    level*. The metric is a difference-of-EMAs band-pass on the envelope (≈3-8 Hz, the
    syllabic band) divided by the kit's own slow level: a fan is near-DC so the
    band-passed energy is ~0; a louder fan does not help because level is the
    denominator. Pure (no numpy); deterministic given the hop cadence."""

    def __init__(self, *, hop_seconds: float = DEFAULT_HOP_SECONDS,
                 tau_fast: float = DEFAULT_TAU_FAST, tau_slow: float = DEFAULT_TAU_SLOW,
                 tau_mod: float = DEFAULT_TAU_MOD, mod_ref: float = DEFAULT_MOD_REF):
        self._a_fast = _alpha(hop_seconds, tau_fast)
        self._a_slow = _alpha(hop_seconds, tau_slow)
        self._a_mod = _alpha(hop_seconds, tau_mod)
        self._mod_ref = max(1e-6, float(mod_ref))
        self._fast = 0.0
        self._slow = 0.0
        self._mod = 0.0

    def update(self, rms: float, *, noise_floor: float = 0.0) -> float:
        """Fold one hop's output RMS in and return the speech-presence score.

        ``noise_floor`` is the kit's own (minimum-statistics) noise estimate when the
        engine exposes it — it lifts the denominator so sub-floor wandering does not
        read as modulation. Reuse the existing floor; do not stand up a second one."""
        env = rms if rms > 0.0 else 0.0
        self._fast += self._a_fast * (env - self._fast)
        self._slow += self._a_slow * (env - self._slow)
        bp = self._fast - self._slow                       # band-passed envelope (~syllabic)
        self._mod += self._a_mod * (abs(bp) - self._mod)   # smoothed modulation energy
        level = max(self._slow, noise_floor, _LEVEL_FLOOR)
        mod_depth = self._mod / level
        return min(1.0, mod_depth / self._mod_ref)

    def reset(self) -> None:
        self._fast = self._slow = self._mod = 0.0


@dataclass(frozen=True)
class SelectionState:
    """The selector's per-hop decision."""

    active: int                    # index of the kit being output
    switching: bool                # True on the hop a switch was just committed (start a cross-fade)
    speech_present: bool           # is anyone actually talking (coasted through brief pauses)?
    scores: tuple[float, ...]      # the per-kit speech-presence scores this hop


class KitSelector:
    """Hysteresis/hold selection across kits, driven by the speech-presence score.

    Models ``_TalkerTracker``: hold the active kit, switch to a challenger only when it
    beats the incumbent by ``switch_margin`` *and* clears ``speech_threshold`` (fast
    attack to a new speaker), and on a silent / fan-only room **hold the last-active
    kit and report no speaker** — never switch *to* a non-talker. Time is caller-
    supplied (monotonic seconds) so it is deterministic and testable."""

    def __init__(self, *, n_kits: int = 2, switch_margin: float = DEFAULT_SWITCH_MARGIN,
                 hold_seconds: float = DEFAULT_HOLD_SECONDS,
                 speech_threshold: float = DEFAULT_SPEECH_THRESHOLD):
        if n_kits < 1:
            raise ValueError("n_kits must be >= 1")
        self.n_kits = int(n_kits)
        self.switch_margin = float(switch_margin)
        self.hold_seconds = float(hold_seconds)
        self.speech_threshold = float(speech_threshold)
        self._active = 0
        self._last_speech_t: float | None = None

    def update(self, scores: Sequence[float], t: float) -> SelectionState:
        sc = [float(s) for s in scores]
        if len(sc) != self.n_kits:
            raise ValueError(f"expected {self.n_kits} scores, got {len(sc)}")
        best = max(range(self.n_kits), key=lambda i: sc[i])
        inc_score = sc[self._active]
        any_speech = sc[best] > self.speech_threshold

        switching = False
        if (any_speech and best != self._active
                and sc[best] >= inc_score + self.switch_margin):
            self._active = best                            # fast attack to a clearly-louder speaker
            switching = True

        if any_speech:
            self._last_speech_t = t                        # someone is talking right now
        present = any_speech or (
            self._last_speech_t is not None and (t - self._last_speech_t) <= self.hold_seconds)

        return SelectionState(active=self._active, switching=switching,
                              speech_present=present, scores=tuple(sc))

    def reset(self) -> None:
        self._active = 0
        self._last_speech_t = None


def _alpha(hop_seconds: float, tau_seconds: float) -> float:
    """One-pole EMA coefficient for a time constant ``tau`` at the given hop cadence."""
    if tau_seconds <= 0.0:
        return 1.0
    return 1.0 - math.exp(-float(hop_seconds) / float(tau_seconds))
