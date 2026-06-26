"""Target-loudness AGC — one scalar gain per block toward a target output RMS.

Extracted so the single-array steered engine (:class:`PolarisBeamformer`) and the
dual-kit combined output (:class:`MultiKitController`) share **one** implementation
rather than two that could drift apart. Control-pure: driven by the output RMS only,
never distance/room. The applied gain is EMA-slewed so it ramps (no pumping), clamped
to ±``max_gain_db``, and **held** through near-silence so pauses don't amplify the
noise floor.

Pure ``conf_pipeline_control`` (numpy imported lazily inside :meth:`process`, like the
rest of the live layer) — constructing one needs no numpy, so it is safe to build in a
back-end ``__init__`` before the runtime is wired.
"""
from __future__ import annotations

from typing import Any

from .tracking import ExponentialTracker

DEFAULT_AGC_MAX_GAIN_DB = 18.0    # clamp |applied gain| to ±this (no run-away boost of the noise floor)
DEFAULT_AGC_SLEW_ALPHA = 0.15     # per-block EMA on the gain — slow enough not to pump speech (~0.2 s @ 32 ms)
DEFAULT_AGC_SILENCE_DB = -55.0    # below this OUTPUT rms, HOLD the gain (don't ramp up near-silence)
DEFAULT_AGC_CEILING_DB = -1.0     # output peak ceiling for the post-gain limiter (≈0.891). The RMS gain
                                  # ignores crest factor, so a loud boost on peaky (e.g. cleaned-voice)
                                  # content drives peaks past full scale → hard clip at the converter.
DEFAULT_AGC_LIMIT_RELEASE_ALPHA = 0.05   # instant-attack / slow-release (~0.3 s) of the brickwall gain (no pumping)


class TargetLoudnessAgc:
    """Normalize a mono block's loudness toward ``target_db`` with one slewed scalar gain.

    The gain saturates at ±``max_gain_db`` and freezes when the output RMS falls below
    ``silence_db`` (so a pause does not pump the floor up to the clamp). Identical math
    to the steered engine's original inline AGC — moved here so both callers share it."""

    def __init__(self, *, target_db: float, max_gain_db: float = DEFAULT_AGC_MAX_GAIN_DB,
                 slew_alpha: float = DEFAULT_AGC_SLEW_ALPHA, silence_db: float = DEFAULT_AGC_SILENCE_DB):
        self.target_db = float(target_db)
        self._target_rms = 10.0 ** (self.target_db / 20.0)
        self.gain_max = 10.0 ** (float(max_gain_db) / 20.0)
        self.gain_min = 10.0 ** (-float(max_gain_db) / 20.0)
        self._silence_rms = 10.0 ** (float(silence_db) / 20.0)
        self._alpha = float(slew_alpha)
        self._tracker = ExponentialTracker(self._alpha)
        # Output peak limiter (instant attack / slow release) — guards the converter against the RMS gain
        # clipping peaky content. Independent of the loudness gain (reported via .tracker), so the stage
        # meter still shows the AGC gain. Plain instance attrs (set here, not lazily).
        self._ceiling = 10.0 ** (DEFAULT_AGC_CEILING_DB / 20.0)
        self._lim_release = float(DEFAULT_AGC_LIMIT_RELEASE_ALPHA)
        self._lim = 1.0                   # running brickwall gain (1.0 = no limiting)

    @property
    def tracker(self) -> ExponentialTracker:
        """The slewing gain EMA (current value via ``.value``)."""
        return self._tracker

    def process(self, mono: Any, *, freeze: bool = False) -> Any:
        """Apply the slewed, clamped, silence-held gain to one mono block; returns float32.

        ``freeze`` HOLDS the gain for this block (no adaptation) — used while a transient suppressor is
        ducking, so the AGC doesn't read the dip as a level drop and pull up, lifting the tap tail and
        noise floor right after the duck."""
        import numpy as np

        rms = float(np.sqrt(np.mean(mono * mono))) if mono.size else 0.0
        if freeze or rms <= self._silence_rms:
            held = self._tracker.value
            desired = held if held is not None else 1.0   # hold (don't pump the floor / chase a transient duck)
        else:
            desired = min(self.gain_max, max(self.gain_min, self._target_rms / rms))
        g = float(self._tracker.update(desired))
        y = mono * g
        # Peak limiter: never let the loudness gain clip at the converter. Instant attack (snap down on the
        # offending block) + slow release toward unity → no pumping; same-block, so zero added latency.
        pk = float(np.max(np.abs(y))) if y.size else 0.0
        need = self._ceiling / pk if pk > self._ceiling else 1.0
        if need < self._lim:
            self._lim = need                              # instant attack
        else:
            self._lim += self._lim_release * (min(1.0, need) - self._lim)   # slow release toward unity
        return (y * self._lim).astype(np.float32)

    def reset(self) -> None:
        """Drop the slew state (atomic rebind of the tracker; an audio thread reads it lock-free)."""
        self._tracker = ExponentialTracker(self._alpha)
        self._lim = 1.0          # drop any held limiter duck so a reconnect starts clean


def _apply_zone_gain(mono: Any, *, enabled: bool, lin: Any) -> Any:
    """Post-AGC per-zone trim.  Bit-exact pass-through when disabled or no trim set
    (returns the SAME array object so the off path is byte-identical).

    Realtime-safe: one multiply + cast when active, zero alloc/lock when off.
    ``lin`` is the linear gain scalar (e.g. ``10**(gain_db/20)``); ``None`` or ``1.0`` = no-op.
    """
    if not enabled or lin is None or lin == 1.0:
        return mono
    return (mono * float(lin)).astype(mono.dtype)
