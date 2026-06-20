"""Streaming transient (table-tap / knock) suppressor for the live beam.

A single-channel temporal de-thump on the beamformed mono. Structure-borne taps couple
mechanically into the array, so the beamformer's spatial nulls can't catch them — this is
the right tool. A fast-envelope **crest** detector flags an impulsive onset (a sharp peak
well above the running level); a short **lookahead** then decides tap vs speech and only
ducks the taps:

  * a table tap / knock is a sharp transient that **decays back to the floor** → duck it;
  * a speech **plosive** (/p/,/t/,/k/) is a short burst **immediately followed by voicing**
    (sustained energy a few ms later) → leave it (Invariant H-B — preserve plosives).

Runs after AEC, before dereverb. Realtime-safe: numpy + one scipy 1-pole for the
envelope, hop-rate gain smoothing (a tiny Python loop over ~8-32 hops/block), and a fixed
``lookahead`` latency carried in a sample tail. Same ``process(block[, noise_gate]) ->
block`` / ``reset()`` contract as the other live stages. It exposes ``duck_active`` (so the
downstream AGC can FREEZE while a duck is in flight — else the AGC chases the dip and lifts
the tap tail, Invariant B) and ``last_reduction_db`` (telemetry for the GUI duck flash).
"""
from __future__ import annotations

import math
from typing import Any, Optional

DEFAULT_TS_THRESHOLD_DB = 12.0      # crest (peak vs running level) that flags an impulsive onset
DEFAULT_TS_DEPTH_DB = -12.0         # how far to duck a confirmed tap
DEFAULT_TS_HOLD_MS = 45.0           # release time — the duck eases back over ~this long
DEFAULT_TS_LOOKAHEAD_MS = 12.0      # plosive-vs-tap classification window (= the added latency)
DEFAULT_TS_HOP = 64                 # gain-smoothing granularity (samples)
DEFAULT_TS_SUSTAIN_RATIO = 0.5      # post-onset energy ≥ ratio·onset ⇒ voicing follows ⇒ speech ⇒ keep
_TAU_FAST_MS = 2.5                  # fast abs-envelope time constant (smooth enough to suppress |sine| ripple)
_FLAT_HOLD_MS = 12.0                # flat-duck duration after a confirmed tap (then the slow release eases back)
_LEVEL_FLOOR = 1e-6


class StreamingTransientSuppressor:
    """Duck impulsive table taps / knocks while preserving speech plosives. ``process`` returns the input
    delayed by ``lookahead_ms`` (the classification latency); a tap is attenuated to ``depth_db`` and
    released over ``hold_ms``; a plosive (burst → voicing) passes through."""

    def __init__(self, sample_rate: float, *, threshold_db: float = DEFAULT_TS_THRESHOLD_DB,
                 depth_db: float = DEFAULT_TS_DEPTH_DB, hold_ms: float = DEFAULT_TS_HOLD_MS,
                 lookahead_ms: float = DEFAULT_TS_LOOKAHEAD_MS, hop: int = DEFAULT_TS_HOP,
                 sustain_ratio: float = DEFAULT_TS_SUSTAIN_RATIO):
        self.sample_rate = float(sample_rate)
        self._thresh_lin = 10.0 ** (float(threshold_db) / 20.0)
        self._depth_lin = 10.0 ** (float(depth_db) / 20.0)
        self._hop = max(1, int(hop))
        self._D = max(1, int(round(lookahead_ms * self.sample_rate / 1000.0)))   # lookahead = latency
        self._rise_n = max(1, int(round(3.0 * self.sample_rate / 1000.0)))       # rise window (~3 ms)
        self._min_crest = 2.0                                # event must be ≥6 dB over background to matter
        self._sustain_ratio = float(sustain_ratio)
        self._a_fast = 1.0 - math.exp(-1.0 / max(1.0, _TAU_FAST_MS * self.sample_rate / 1000.0))
        self._a_slow = 0.05                                  # per-block slow-level EMA
        # per-hop release coefficient: gain eases from depth back to 1.0 over ~hold_ms after the flat hold
        self._rel = 1.0 - math.exp(-self._hop / max(1.0, (hold_ms / 1000.0) * self.sample_rate))
        self._hold_hops = max(1, int(round(_FLAT_HOLD_MS / 1000.0 * self.sample_rate / self._hop)))
        self._hold_ctr = 0                                   # hops of flat duck still owed (carried across blocks)
        self.duck_active = False                             # True while a duck is in flight (AGC-freeze, Inv B)
        self.last_reduction_db = 0.0                         # current duck depth in dB (telemetry, Inv K)
        self._tail: Any = None                               # last D samples (lookahead carry)
        self._env_zi: Any = None                             # scipy 1-pole envelope state
        self._slow = 0.0                                     # running level
        self._gain = 1.0                                     # smoothed gain carry (per hop)
        self._np: Any = None

    def reset(self) -> None:
        self._tail = None
        self._env_zi = None
        self._slow = 0.0
        self._gain = 1.0
        self._hold_ctr = 0
        self.duck_active = False
        self.last_reduction_db = 0.0

    def process(self, block: Any, noise_gate: Any = None) -> Any:
        try:
            import numpy as np
            from scipy.signal import lfilter
        except Exception:                                    # numpy/scipy unavailable → degrade to delay-free no-op
            return block
        self._np = np
        x = np.asarray(block, dtype=np.float32)
        n = x.size
        if n == 0:
            return x
        D = self._D
        if self._tail is None or self._tail.size != D:
            self._tail = np.zeros(D, dtype=np.float32)
        ext = np.concatenate([self._tail, x])                # tail(D) + block; output = ext[:n] (delayed by D)
        # --- fast abs-envelope (1-pole), carried across blocks ---
        a = self._a_fast
        if self._env_zi is None:
            self._env_zi = np.zeros(1, dtype=np.float64)
        fe, self._env_zi = lfilter([a], [1.0, -(1.0 - a)], np.abs(ext).astype(np.float64), zi=self._env_zi)
        # --- running level from a transient-robust low percentile, EMA across blocks (seeded on the first
        # block so the crest threshold is meaningful immediately and natural offsets don't false-trigger) ---
        lvl = float(np.percentile(fe, 25)) if fe.size else 0.0
        self._slow = lvl if self._slow <= 0.0 else (1.0 - self._a_slow) * self._slow + self._a_slow * lvl
        slow = max(self._slow, _LEVEL_FLOOR)
        # --- onset = a sharp RISE in the envelope (not merely loud) so sustained vowels and natural word
        # OFFSETS never trigger; require the event to be meaningfully above the background too ---
        R = self._rise_n
        prev = np.empty_like(fe)
        prev[R:] = fe[:-R]
        prev[:R] = fe[0] if fe.size else 0.0
        rise = fe / np.maximum(prev, _LEVEL_FLOOR)
        significant = fe > slow * self._min_crest            # ≥6 dB over background (ignore quiet ripple)
        onset = (rise > self._thresh_lin) & significant
        # lookahead: the envelope D samples later. Voicing that FOLLOWS the onset ⇒ a plosive (keep);
        # an onset that decays back ⇒ an isolated tap (duck).
        fut = np.empty_like(fe)
        fut[:-D] = fe[D:]
        fut[-D:] = fe[-1] if fe.size else 0.0
        sustained = fut >= self._sustain_ratio * np.maximum(fe, _LEVEL_FLOOR)
        tap = onset & ~sustained                              # impulsive rise AND not followed by voicing
        # --- gain at hop rate: a confirmed tap snaps the gain to depth and HOLDS it flat for ~_FLAT_HOLD_MS
        # (so a short tap fully reaches depth), then the slow release eases back to unity ---
        H = self._hop
        nh = (ext.size + H - 1) // H
        pad = nh * H - ext.size
        tap_p = np.concatenate([tap, np.zeros(pad, dtype=bool)]) if pad else tap
        tap_hop = tap_p.reshape(nh, H).sum(axis=1) > 0       # any tap sample in the hop (ndarray[bool])
        if tap_hop.any():
            # the abs-envelope lags the impulse, so detection fires a hop or two AFTER the raw peak —
            # pre-empt it (we have the lookahead delay) by ducking a few hops EARLIER so the sharp peak
            # is covered, not just its tail.
            th = tap_hop.copy()
            for _s in (1, 2, 3):
                th[:-_s] |= tap_hop[_s:]
            tap_hop = th
        g = self._gain
        hold = self._hold_ctr
        gains = np.empty(nh, dtype=np.float64)
        for k in range(nh):
            if tap_hop[k]:
                hold = self._hold_hops                        # (re)arm the flat duck
            if hold > 0:
                g = self._depth_lin                           # attack: snap to depth and hold
                hold -= 1
            else:
                g = min(1.0, g + self._rel * (1.0 - g))       # release: ease back to unity
            gains[k] = g
        self._gain = g
        self._hold_ctr = hold
        # smooth (de-stair) the hop gains to per-sample by linear interpolation between hop centres —
        # stateless and continuous across blocks (gains[0] already carries from the previous block), so no
        # ramp-from-zero artefact at block boundaries.
        centers = (np.arange(nh, dtype=np.float64) + 0.5) * H
        gain_ps = np.interp(np.arange(ext.size, dtype=np.float64), centers, gains)
        out = (ext * gain_ps).astype(np.float32)[:n]
        self._tail = ext[n:].astype(np.float32)              # last D samples → next call's lookahead
        # telemetry over the emitted region
        gmin = float(gain_ps[:n].min()) if n else 1.0
        self.last_reduction_db = -20.0 * math.log10(max(gmin, 1e-6)) if gmin < 0.999 else 0.0
        self.duck_active = gmin < 0.95
        return out
