"""Streaming parametric EQ (PEQ) for the live beam.

A real-time cascade of RBJ-cookbook biquads — one per enabled band, types
``bell`` / ``lowShelf`` / ``highShelf`` / ``highpass`` / ``lowpass`` — applied to the
cleaned mono *after* the noise reducer and *before* the AGC (tone-shape the clean
signal, then let the AGC level it). It reuses the shared 4-band model
(:data:`conf_pipeline.model.PEQ_BAND_TYPES` + ranges; bands are
``{"freqHz", "gainDb", "q", "type"}`` dicts, the same shape :func:`conf_pipeline.blocks.default_peq_band`
emits), so the GUI/config and the live filter speak one schema.

**Why exact IIR (scipy ``sosfilt``), not the shared STFT.** The other live stages
(``post_nr`` / dereverb) run on the engine's Hann overlap-add STFT, but a per-frame
spectral multiply time-aliases a long impulse response — exactly the high-Q
**hum-notch** case (a 50 Hz Q≈10 bell rings for many frames). So the PEQ uses an exact
second-order-section recursion with carried **float64** state instead: no aliasing, and
the difference equation stays numerically stable at low ``f/fs`` where float32 would
cancel (Invariant H-A). ``scipy.signal`` ships with the ``[control]`` extra that gates
all this live DSP; it is lazy-imported and degrades to a **true no-op** if unavailable.

Realtime-safe: no device, no locks, bounded per-block numpy/scipy work. The OFF path
(no enabled band) returns the **same input object** — a bit-exact pass-through, so the
stage is invisible when idle.
"""
from __future__ import annotations

import math
from typing import Any, Optional, Sequence

# Band-type strings (mirror conf_pipeline.model.PEQ_BAND_TYPES — kept as literals so this
# realtime module pulls no heavy import at load).
_SHELF_OR_BELL = ("bell", "lowShelf", "highShelf")
_DENORMAL_FLOOR = 1e-25                 # flush sub-this filter state to zero (denormal-stall guard)


def _biquad(kind: str, f0: float, gain_db: float, q: float, fs: float) -> Optional[list]:
    """One normalized RBJ-cookbook second-order section ``[b0, b1, b2, 1, a1, a2]`` (a0-divided),
    or ``None`` when the band is a no-op (0 dB bell/shelf, or out of the open band 0 < f0 < Nyquist)."""
    if not (0.0 < f0 < 0.5 * fs * 0.999) or q <= 0.0:
        return None
    if kind in ("bell", "lowShelf", "highShelf") and abs(gain_db) < 1e-6:
        return None                                   # a 0 dB bell/shelf is identity → skip (keeps the no-op)
    w0 = 2.0 * math.pi * f0 / fs
    cw, sw = math.cos(w0), math.sin(w0)
    alpha = sw / (2.0 * q)
    if kind == "bell":
        a_ = 10.0 ** (gain_db / 40.0)
        b0, b1, b2 = 1.0 + alpha * a_, -2.0 * cw, 1.0 - alpha * a_
        a0, a1, a2 = 1.0 + alpha / a_, -2.0 * cw, 1.0 - alpha / a_
    elif kind in ("lowShelf", "highShelf"):
        a_ = 10.0 ** (gain_db / 40.0)
        sq = 2.0 * math.sqrt(a_) * alpha
        ap1, am1 = a_ + 1.0, a_ - 1.0
        if kind == "lowShelf":
            b0 = a_ * (ap1 - am1 * cw + sq)
            b1 = 2.0 * a_ * (am1 - ap1 * cw)
            b2 = a_ * (ap1 - am1 * cw - sq)
            a0 = ap1 + am1 * cw + sq
            a1 = -2.0 * (am1 + ap1 * cw)
            a2 = ap1 + am1 * cw - sq
        else:                                         # highShelf
            b0 = a_ * (ap1 + am1 * cw + sq)
            b1 = -2.0 * a_ * (am1 + ap1 * cw)
            b2 = a_ * (ap1 + am1 * cw - sq)
            a0 = ap1 - am1 * cw + sq
            a1 = 2.0 * (am1 - ap1 * cw)
            a2 = ap1 - am1 * cw - sq
    elif kind == "highpass":
        b0, b1, b2 = (1.0 + cw) / 2.0, -(1.0 + cw), (1.0 + cw) / 2.0
        a0, a1, a2 = 1.0 + alpha, -2.0 * cw, 1.0 - alpha
    elif kind == "lowpass":
        b0, b1, b2 = (1.0 - cw) / 2.0, 1.0 - cw, (1.0 - cw) / 2.0
        a0, a1, a2 = 1.0 + alpha, -2.0 * cw, 1.0 - alpha
    else:
        return None                                   # unknown type → skip
    return [b0 / a0, b1 / a0, b2 / a0, 1.0, a1 / a0, a2 / a0]


class StreamingPeq:
    """Real-time parametric-EQ cascade. ``process(block[, noise_gate]) -> block`` / ``reset()`` —
    the same contract as the other live stages, so it drops into ``process_block``.

    ``bands`` is a list of ``{"freqHz", "gainDb", "q", "type"[, "enabled"]}`` dicts (the shared
    PEQ model). Bands with ``enabled is False``, a 0 dB bell/shelf, or an out-of-range frequency are
    dropped; if nothing remains the stage is a **bit-exact pass-through** (returns the input object).
    Re-tunable live via :meth:`set_bands` (rebuilds the sections, preserves continuity)."""

    def __init__(self, sample_rate: float, bands: Optional[Sequence[dict]] = None):
        self.sample_rate = float(sample_rate)
        # ``[sos, zi]`` (float64 sections + carried state) or None when idle. Held as ONE reference so the
        # audio thread reads a consistent snapshot and the control thread rebinds atomically (set_bands) —
        # never an in-place mutation that races process() (the CLAUDE.md atomic-rebind rule).
        self._sections: Any = None
        self._ok = True                               # False once a numpy/scipy import has failed (stay a no-op)
        self.set_bands(bands)

    def set_bands(self, bands: Optional[Sequence[dict]]) -> None:
        """(Re)build the section cascade from ``bands`` and rebind it atomically. Safe to call live; keeps the
        running state when the section count is unchanged so a small tweak doesn't click."""
        rows = []
        for b in (bands or []):
            if b.get("enabled") is False:
                continue
            sec = _biquad(str(b.get("type", "bell")), float(b.get("freqHz", 0.0)),
                          float(b.get("gainDb", 0.0)), float(b.get("q", 1.0)), self.sample_rate)
            if sec is not None:
                rows.append(sec)
        if not rows or not self._ok:
            self._sections = None
            return
        try:
            import numpy as np
        except Exception:                             # numpy missing → stay a no-op
            self._ok = False
            self._sections = None
            return
        sos = np.asarray(rows, dtype=np.float64)
        cur = self._sections
        zi = (cur[1] if (cur is not None and cur[0].shape[0] == sos.shape[0])   # same count → keep state (no click)
              else np.zeros((sos.shape[0], 2), dtype=np.float64))
        self._sections = [sos, zi]                    # atomic rebind

    def process(self, block: Any, noise_gate: Any = None) -> Any:
        sec = self._sections                          # snapshot the holder (atomic ref read)
        if sec is None:
            return block                              # true no-op: same object, no copy (Invariant D)
        try:
            import numpy as np
            from scipy.signal import sosfilt
        except Exception:                             # scipy/numpy unavailable at runtime → degrade to no-op
            self._ok = False
            self._sections = None
            return block
        sos, zi = sec
        x = np.asarray(block, dtype=np.float64)
        out, zi2 = sosfilt(sos, x, zi=zi)
        zi2[np.abs(zi2) < _DENORMAL_FLOOR] = 0.0      # flush denormals (stall guard, Invariant H-A)
        sec[1] = zi2                                  # carry state on this snapshot (harmless if rebound meanwhile)
        return out.astype(np.float32, copy=False)

    def reset(self) -> None:
        sec = self._sections
        if sec is not None:
            sec[1][...] = 0.0
