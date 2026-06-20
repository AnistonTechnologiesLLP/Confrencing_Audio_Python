"""Streaming 'voice only' output gate for the live beam.

Duck the output when the sound is NOT speech — the gaps between phrases, paper rustle, a
steady fan/hum, a knock — by REUSING the syllabic-modulation :class:`SpeechPresenceScorer`
(the same fan-proof speech-vs-noise score the 2-kit selector uses; it is a ratio metric, so
it is **level-invariant** and works downstream of the AGC). Runs at the END of the chain,
after the AGC.

It complements the DOA VAD / auto-steer ``gate_when_empty`` (directional salience) with a
speech-**timbre** test. It is **onset-safe** (Invariant C): a FAST attack opens it quickly
when speech returns, a SLOW release holds it open through brief pauses, and the floor is
**shallow** (a duck, not a mute) so a missed onset is attenuated — never silenced — and
stays recoverable.

It does NOT remove a competing human voice inside the pickup zone (that is speech) — only
the spatial zone-nulling does. Same ``process(block[, noise_gate]) -> block`` / ``reset()``
contract as the other live stages; OFF (absent) ⇒ the stage isn't in the chain at all.
"""
from __future__ import annotations

import math
from typing import Any, Optional

DEFAULT_VG_THRESHOLD = 0.35         # speech-presence score above which the gate is fully open
DEFAULT_VG_FLOOR_DB = -15.0         # shallow floor (duck, NOT mute) so a missed onset is recoverable
DEFAULT_VG_ATTACK_MS = 8.0          # fast attack — open quickly on returning speech (onset-safe)
DEFAULT_VG_RELEASE_MS = 180.0       # slow release — hold open through brief intra-phrase pauses


class VoiceOnlyGate:
    """Speech-presence output gate: attenuate non-speech toward ``floor_db`` with a fast attack / slow
    release, driven by the syllabic-modulation scorer. ``mod_ref`` re-tunes the speech threshold at the
    post-AGC operating point if needed (the scorer is level-invariant, but its modulation reference was
    tuned inside the 2-kit selector)."""

    def __init__(self, sample_rate: float, *, threshold: float = DEFAULT_VG_THRESHOLD,
                 floor_db: float = DEFAULT_VG_FLOOR_DB, attack_ms: float = DEFAULT_VG_ATTACK_MS,
                 release_ms: float = DEFAULT_VG_RELEASE_MS, mod_ref: Optional[float] = None):
        self.sample_rate = float(sample_rate)
        self._threshold = float(threshold)
        self._floor = 10.0 ** (float(floor_db) / 20.0)
        self._attack_ms = max(0.1, float(attack_ms))
        self._release_ms = max(0.1, float(release_ms))
        self._mod_ref = mod_ref
        self._scorer: Any = None
        self._gain = 1.0
        self._prev_rms = 0.0
        self.gate_open = True               # telemetry (Invariant K)
        self.last_reduction_db = 0.0
        self.score = 1.0

    def _ensure(self, hop_seconds: float) -> None:
        if self._scorer is None:
            from .multikit import DEFAULT_MOD_REF, SpeechPresenceScorer   # lazy: reuse the 2-kit scorer
            self._scorer = SpeechPresenceScorer(
                hop_seconds=hop_seconds,
                mod_ref=self._mod_ref if self._mod_ref is not None else DEFAULT_MOD_REF)

    def process(self, block: Any, noise_gate: Any = None) -> Any:
        try:
            import numpy as np
        except Exception:
            return block
        x = np.asarray(block, dtype=np.float32)
        n = x.size
        if n == 0:
            return x
        hop_seconds = n / self.sample_rate
        self._ensure(hop_seconds)
        rms = float(np.sqrt(np.mean(x.astype(np.float64) ** 2)))
        self.score = float(self._scorer.update(rms))
        # open the gate when speech is PRESENT, OR on a sharp level rise (a talker just started — the
        # scorer needs a few hops to confirm, so anticipate the onset to protect the first syllable).
        onset = rms > 3.0 * max(self._prev_rms, 1e-6)
        self._prev_rms = rms
        target = 1.0 if (self.score >= self._threshold or onset) else self._floor
        tau_ms = self._attack_ms if target > self._gain else self._release_ms
        a = 1.0 - math.exp(-hop_seconds / max(1e-4, tau_ms / 1000.0))   # fast attack / slow release
        g_new = self._gain + a * (target - self._gain)
        ramp = np.linspace(self._gain, g_new, n).astype(np.float32)     # de-click within the block
        self._gain = g_new
        self.gate_open = g_new > 0.5
        self.last_reduction_db = (-20.0 * math.log10(max(g_new, 1e-6))) if g_new < 0.999 else 0.0
        return (x * ramp).astype(np.float32)

    def reset(self) -> None:
        self._scorer = None
        self._gain = 1.0
        self._prev_rms = 0.0
        self.gate_open = True
        self.last_reduction_db = 0.0
        self.score = 1.0
