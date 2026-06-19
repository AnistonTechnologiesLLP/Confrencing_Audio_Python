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

    @property
    def tracker(self) -> ExponentialTracker:
        """The slewing gain EMA (current value via ``.value``)."""
        return self._tracker

    def process(self, mono: Any) -> Any:
        """Apply the slewed, clamped, silence-held gain to one mono block; returns float32."""
        import numpy as np

        rms = float(np.sqrt(np.mean(mono * mono))) if mono.size else 0.0
        if rms > self._silence_rms:
            desired = min(self.gain_max, max(self.gain_min, self._target_rms / rms))
        else:
            held = self._tracker.value
            desired = held if held is not None else 1.0   # hold through silence (don't pump the floor)
        g = float(self._tracker.update(desired))
        return (mono * g).astype(np.float32)

    def reset(self) -> None:
        """Drop the slew state (atomic rebind of the tracker; an audio thread reads it lock-free)."""
        self._tracker = ExponentialTracker(self._alpha)
