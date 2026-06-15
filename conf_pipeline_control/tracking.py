"""Swappable estimate-smoothing filters for the live trackers.

A *Tracker* turns a stream of noisy per-cycle observations into a stable estimate. Both
POLARIS back-ends need this and historically grew two **ad-hoc** smoothers: the steered path
stabilizes a talker azimuth (a hold/switch machine), the grid path stabilizes a
per-virtual-mic energy map with an EMA so the selection doesn't flicker. This module is the
one interface behind which that smoothing strategy is swappable.

Two families share the :class:`Tracker` lifecycle (``reset()``):

* **Continuous-estimate smoothers** — ``update(value, t=None) -> value`` where *value* is any
  numeric supporting ``+`` / ``*`` (a Python ``float`` **or** a numpy array, so the grid can
  smooth its whole score vector in one call). :class:`ExponentialTracker` (one-pole EMA — the
  default, and exactly the grid's old inline math) and :class:`AlphaBetaTracker` (a
  constant-velocity / g-h filter — the steady-state form of a constant-velocity Kalman, and
  the documented **"Kalman hook"** for trajectory smoothing on a non-wrapping scalar or each
  ``(x, y)`` component).
* **Domain trackers** — e.g. the steered path's talker hold/switch machine
  (:class:`conf_pipeline_control.polaris_beamformer._TalkerTracker`), which arbitrates *which*
  discrete talker to follow. It is a :class:`Tracker` (shares ``reset()``) but keeps its own
  richer ``update`` contract — forcing a wrapping azimuth through an EMA would smear across
  talker switches, so it stays domain-specific by design.

Pure stdlib — no numpy import here, so it stays importable everywhere; array smoothing works
by operating on whatever the caller passes in.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Optional


class Tracker(ABC):
    """Common lifecycle for the live trackers. The one shared contract is :meth:`reset`, so any
    tracker can be wiped on a re-activation / mode switch (BeamEngine relies on this).
    Continuous-estimate smoothers add the :class:`ValueSmoother` ``update`` contract; *domain*
    trackers (e.g. the steered talker hold/switch machine) keep their own ``update`` signature
    and subclass :class:`Tracker` directly."""

    @abstractmethod
    def reset(self) -> None:
        """Forget all state; the next observation re-acquires."""
        ...


class ValueSmoother(Tracker):
    """A :class:`Tracker` that maps a stream of observations to a smoothed estimate of the
    *same* quantity: ``update(value, t=None) -> value``. This is the swappable interface the
    grid's selection smoother is typed against — drop in any subclass."""

    @abstractmethod
    def update(self, value: Any, t: Optional[float] = None) -> Any:
        """Fold one observation in and return the current smoothed estimate."""
        ...


class ExponentialTracker(ValueSmoother):
    """One-pole exponential moving average: ``y = α·x + (1−α)·y_prev``.

    ``alpha`` in [0, 1]: ``1.0`` = no smoothing (pass-through), → 0 = heavy smoothing. Operates
    on a scalar or any value supporting ``+`` / ``*`` (e.g. a numpy array — the grid smooths its
    whole per-virtual-mic score vector per block). The first observation initializes the state."""

    def __init__(self, alpha: float):
        if not (0.0 <= alpha <= 1.0):
            raise ValueError("alpha must be in [0, 1]")
        self.alpha = float(alpha)
        self._state: Any = None

    def update(self, value: Any, t: Optional[float] = None) -> Any:
        if self._state is None:
            self._state = value
        else:
            self._state = self.alpha * value + (1.0 - self.alpha) * self._state
        return self._state

    def reset(self) -> None:
        self._state = None

    @property
    def value(self) -> Any:
        """Current estimate, or ``None`` before the first observation."""
        return self._state


class AlphaBetaTracker(ValueSmoother):
    """Constant-velocity (α-β / g-h) tracker for a scalar — the steady-state form of a
    constant-velocity Kalman filter, and the documented hook for trajectory smoothing.

    Each step predicts ``pos += vel·dt`` then corrects from the residual ``r = z − pos``:
    ``pos += α·r``, ``vel += (β/dt)·r``. Lower α/β → smoother but laggier. Because it carries a
    velocity term it follows a *moving* source with far less lag than a plain EMA at the same
    noise rejection (an EMA lags a ramp by ``(1−α)/α`` slopes; this converges to ~zero lag).

    **Scalar and wrap-unaware:** feed a non-wrapping quantity (an unwrapped angle, or each of
    ``x`` / ``y`` separately) — a raw azimuth would jump across the 0/360 seam. Not wired into
    the default azimuth path; it exists so a host can swap it in via a back-end's ``tracker=``
    seam. ``t`` is accepted for interface uniformity but these steady-state filters use the
    fixed ``dt`` rather than wall-clock spacing."""

    def __init__(self, alpha: float = 0.5, beta: float = 0.1, dt: float = 1.0):
        if not (0.0 < alpha <= 2.0) or not (0.0 <= beta <= 2.0):
            raise ValueError("alpha must be in (0, 2], beta in [0, 2]")
        if dt <= 0.0:
            raise ValueError("dt must be > 0")
        self.alpha = float(alpha)
        self.beta = float(beta)
        self.dt = float(dt)
        self._pos: Optional[float] = None
        self._vel: float = 0.0

    def update(self, value: float, t: Optional[float] = None) -> float:
        z = float(value)
        if self._pos is None:                          # acquire on first sample
            self._pos = z
            self._vel = 0.0
            return self._pos
        self._pos += self._vel * self.dt               # predict
        resid = z - self._pos
        self._pos += self.alpha * resid                # correct position
        self._vel += (self.beta / self.dt) * resid     # correct velocity
        return self._pos

    def reset(self) -> None:
        self._pos = None
        self._vel = 0.0

    @property
    def value(self) -> Optional[float]:
        """Current position estimate, or ``None`` before the first observation."""
        return self._pos
