"""Per-stage live activity metrics + the loudness-matched raw buffer — ONE implementation.

Both live DSP chains (:class:`PolarisBeamformer.process_block` and
:class:`LiveBeamController._process_block`) call into here so the per-stage meter strip reads
identically and the raw↔processed bypass sounds identical regardless of which controller (steered
vs auto-steer) is driving. Do **not** re-derive these formulas in the controllers.

Honest by construction (the part that's easy to get wrong):
- **Denoise** is metered as the **noise-bed** reduction (the drop in the quiet-gap floor), NOT a
  broadband input-vs-output RMS delta — speech dominates broadband RMS, so a *working* denoiser on
  speech would read ~0 and look broken. A clean input honestly reads ~0 *with the stage still on*.
- **AGC** is a normalizer, so it reports **bipolar applied gain** (+boost / −cut), never "reduction".
- **AEC** reports ERLE gated on far-end activity — with no far-end signal there is no echo to cancel,
  so it shows *idle* (``aec_farend_active=False``) rather than a misleading 0 dB.
- **Dereverb** reports the attenuation it applied this block (in-vs-out around its own call).

Scalar math only (``math.log10``); ``loudness_matched_raw`` imports numpy lazily, like the rest of
the live layer. Stateful (the noise-bed floors) — built per session, reset in ``reset_transient``.
"""
from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass
from typing import Any, Deque, Optional

_EPS = 1e-12
_DEFAULT_BED_WINDOW_S = 0.7   # noise-bed = running min over ~this much audio (catches the quiet gaps)


@dataclass(frozen=True)
class StageActivity:
    """A lock-free snapshot of what each cleaning stage did on the last block.

    Each stage carries its value **and** its ``*_on`` flag so the GUI can tell *off* (grey the meter)
    from *on-but-idle* (a near-zero value, left lit — the stage is working, there's just nothing to do).
    """

    aec_erle_db: float
    aec_on: bool
    aec_farend_active: bool
    dereverb_db: float
    dereverb_on: bool
    denoise_db: float
    denoise_on: bool
    agc_gain_db: float
    agc_on: bool


# The published value when nothing is metered (all stages off / before the first block). A single
# shared immutable instance — assigning it is a tear-free atomic rebind for the GUI reader.
ZERO_ACTIVITY = StageActivity(
    aec_erle_db=0.0, aec_on=False, aec_farend_active=False,
    dereverb_db=0.0, dereverb_on=False,
    denoise_db=0.0, denoise_on=False,
    agc_gain_db=0.0, agc_on=False,
)


class _NoiseFloor:
    """Running-minimum noise-bed estimate over a sliding window of recent block RMS values.

    The min over the window isolates the quiet gaps (the background bed a denoiser lowers); louder
    speech blocks sit above it and don't move it. Window length is a block count derived from the
    sample rate + a nominal block size, so it spans roughly :data:`_DEFAULT_BED_WINDOW_S`.
    """

    def __init__(self, window_blocks: int):
        self._buf: Deque[float] = deque(maxlen=max(1, int(window_blocks)))

    def update(self, rms: float) -> float:
        self._buf.append(float(rms))
        return min(self._buf)

    def reset(self) -> None:
        self._buf.clear()


def _atten_db(in_rms: float, out_rms: float) -> float:
    """Attenuation (dB ≥ 0) the stage applied: how much quieter ``out`` is than ``in``. Clamped at 0
    (a stage never *adds* energy as a meter reading) and 0 when either level is ~silent."""
    if in_rms <= _EPS or out_rms <= _EPS:
        return 0.0
    return max(0.0, 20.0 * math.log10(in_rms / out_rms))


class StageMeter:
    """Owns the per-session noise-bed floors and turns raw per-block scalars into a
    :class:`StageActivity`. One instance per controller, fed once per block."""

    def __init__(self, sample_rate: float, *, block_hint: int = 1024,
                 bed_window_s: float = _DEFAULT_BED_WINDOW_S):
        sr = float(sample_rate) or 44100.0
        blk = max(1, int(block_hint))
        window_blocks = max(4, int(round((bed_window_s * sr) / blk)))
        self._floor_in = _NoiseFloor(window_blocks)
        self._floor_out = _NoiseFloor(window_blocks)

    def update(
        self,
        *,
        aec_on: bool,
        aec_erle_db: float,
        aec_farend: bool,
        dereverb_on: bool,
        dereverb_in_rms: float,
        dereverb_out_rms: float,
        denoise_on: bool,
        denoise_in_rms: float,
        denoise_out_rms: float,
        agc_on: bool,
        agc_gain_lin: Optional[float],
    ) -> StageActivity:
        # AEC — ERLE only meaningful with far-end echo present; otherwise idle.
        erle = float(aec_erle_db) if (aec_on and aec_farend) else 0.0

        # Dereverb — its own applied attenuation this block.
        derev_db = _atten_db(dereverb_in_rms, dereverb_out_rms) if dereverb_on else 0.0

        # Denoise — the NOISE-BED reduction (gap floor drop), not the broadband delta.
        denoise_db = 0.0
        if denoise_on:
            bed_in = self._floor_in.update(denoise_in_rms)
            bed_out = self._floor_out.update(denoise_out_rms)
            denoise_db = _atten_db(bed_in, bed_out)

        # AGC — bipolar applied gain (normalizer, not a suppressor).
        agc_db = 0.0
        if agc_on and agc_gain_lin is not None and agc_gain_lin > _EPS:
            agc_db = 20.0 * math.log10(float(agc_gain_lin))

        return StageActivity(
            aec_erle_db=erle, aec_on=bool(aec_on), aec_farend_active=bool(aec_on and aec_farend),
            dereverb_db=derev_db, dereverb_on=bool(dereverb_on),
            denoise_db=denoise_db, denoise_on=bool(denoise_on),
            agc_gain_db=agc_db, agc_on=bool(agc_on),
        )

    def reset(self) -> None:
        """Drop the noise-bed history (called from ``reset_transient`` on a beam switch)."""
        self._floor_in.reset()
        self._floor_out.reset()


def loudness_matched_raw(pre_cleaner_mono: Any, agc_gain_lin: float) -> Any:
    """The RAW monitor buffer for the bypass: the pre-cleaner (beam) mono scaled by the **same** AGC
    gain the processed leg applied, so an A/B reveals the *cleaning* difference, not a level jump.
    ``agc_gain_lin`` is 1.0 when AGC is off. Returns float32."""
    import numpy as np

    g = float(agc_gain_lin) if agc_gain_lin else 1.0
    return (np.asarray(pre_cleaner_mono, dtype=np.float32) * g).astype(np.float32)
