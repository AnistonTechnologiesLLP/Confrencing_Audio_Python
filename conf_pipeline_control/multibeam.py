"""Capture **everyone** — simultaneous multi-talker automix on ONE POLARIS array.

Where the steered engine commits to a single dominant talker and the dual-kit controller *switches*
between kits, this forms **several beams at once** — one per active talker — and automixes them into a
combined feed (plus, later, per-person tracks). It reuses the array's existing multi-talker DOA
(`doa.detect`), the multi-bearing LCMV weights (`beamformer.design_multi_bearings` / the live per-bin
solver), the fan-proof `SpeechPresenceScorer`, and `TargetLoudnessAgc`.

This module is layered so the novel control logic is **pure** (no numpy, no audio) and unit-testable:

- `snap_targets` — the **hybrid** aim: snap each detected azimuth to the nearest defined room seat
  (stable, jitter-free) via :mod:`conf_pipeline.seat_mapper`, falling back to the raw DOA azimuth where
  no seat is near.
- `BeamSlotTracker` — assign the snapped targets to **N persistent beam slots** (a slot keeps the same
  talker/seat across ticks) with per-slot **hold** so a beam doesn't drop on a brief pause.

The realtime mixer + `MultiBeamController` (which apply N weight tables over a shared FFT, gate each
beam, and NOM-automix the result) build on these and land in this same module. numpy is imported lazily
in those parts, so the package root stays heavy-dep-free and the pure planner needs no runtime.

Honest limit: the ~40 mm 8-mic array resolves **2-3 well-separated talkers** (>~40-50° apart); closer
people merge into one beam/slot.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional, Sequence, Tuple

from conf_pipeline.model import angular_separation_deg
from conf_pipeline.seat_mapper import (
    DEFAULT_MAX_SEPARATION_DEG,
    nearest_seat_for_array,
    seat_azimuth_for_array,
)

DEFAULT_N_BEAMS = 3                 # the small array's practical ceiling (2-3 separable talkers)
DEFAULT_HOLD_SECONDS = 0.6          # keep a beam through a brief pause before releasing the slot
DEFAULT_MATCH_RADIUS_DEG = 25.0     # a target within this of a slot's bearing is "the same talker"


@dataclass(frozen=True)
class BeamTarget:
    """A direction to capture this tick: a bearing (array-relative deg, 0°=+Y CW — the DOA frame) and the
    room seat it snapped to (or ``None`` for free DOA)."""

    azimuth_deg: float
    seat_id: Optional[str]
    salience_db: float


@dataclass(frozen=True)
class BeamSlot:
    """One persistent beam slot's published state. ``azimuth_deg is None`` ⇒ the slot is idle (no beam)."""

    index: int
    azimuth_deg: Optional[float]
    seat_id: Optional[str]
    active: bool                    # has a live detection this tick
    held: bool                      # coasting through a brief pause (keeps its bearing)


def snap_targets(
    config: object,
    array_id: Optional[str],
    detections: Sequence[Tuple[float, float]],
    *,
    snap: bool = True,
    max_separation_deg: float = DEFAULT_MAX_SEPARATION_DEG,
) -> List[BeamTarget]:
    """Hybrid aim: map each ``(azimuth_deg, salience_db)`` detection to a `BeamTarget`.

    With ``snap`` on, a detection within ``max_separation_deg`` of a defined room seat takes the seat's
    (stable) bearing and ``seat_id``; otherwise it keeps the raw DOA azimuth (``seat_id=None``). With
    ``snap`` off, or when the config/array has no room/seats/bearing, every target is free DOA."""
    out: List[BeamTarget] = []
    for az, sal in detections:
        if az is None:
            continue
        bearing = float(az)
        seat_id: Optional[str] = None
        if snap and array_id is not None:
            match = nearest_seat_for_array(config, array_id, float(az), max_separation_deg=max_separation_deg)  # type: ignore[arg-type]
            if match is not None:
                seat_bearing = seat_azimuth_for_array(config, array_id, match.seat_id)  # type: ignore[arg-type]
                if seat_bearing is not None:
                    bearing, seat_id = float(seat_bearing), match.seat_id
        out.append(BeamTarget(bearing, seat_id, float(sal)))
    return out


@dataclass
class _SlotState:
    azimuth_deg: Optional[float] = None
    seat_id: Optional[str] = None
    last_seen_t: Optional[float] = None


class BeamSlotTracker:
    """Assign snapped `BeamTarget`s to ``n_slots`` **persistent** beam slots, with per-slot hold.

    A slot keeps the same talker/seat across ticks: a target is matched first by **seat identity**
    (when snapped), then by **nearest bearing** within ``match_radius_deg`` of the slot's current
    direction. Unmatched targets fill idle slots (or steal the stalest one when full). A slot with no
    target this tick **holds** its bearing for ``hold_seconds`` (so a brief pause doesn't drop the
    beam), then releases. Pure + deterministic — the caller supplies monotonic ``t``."""

    def __init__(self, *, n_slots: int = DEFAULT_N_BEAMS, hold_seconds: float = DEFAULT_HOLD_SECONDS,
                 match_radius_deg: float = DEFAULT_MATCH_RADIUS_DEG):
        if n_slots < 1:
            raise ValueError("n_slots must be >= 1")
        self._n = int(n_slots)
        self._hold = float(hold_seconds)
        self._radius = float(match_radius_deg)
        self._slots: List[_SlotState] = [_SlotState() for _ in range(self._n)]

    def update(self, targets: Sequence[BeamTarget], t: float) -> List[BeamSlot]:
        claimed = [False] * self._n
        used = [False] * len(targets)
        order = sorted(range(len(targets)), key=lambda j: -targets[j].salience_db)  # louder talkers first

        # Phase A — keep each target on the slot that already holds it (seat identity, then bearing).
        for j in order:
            tg = targets[j]
            best_i, best_gap, best_is_seat = -1, None, False
            for i in range(self._n):
                if claimed[i]:
                    continue
                s = self._slots[i]
                if s.azimuth_deg is None:
                    continue
                if tg.seat_id is not None and s.seat_id == tg.seat_id:
                    best_i, best_is_seat = i, True
                    break                                          # exact seat match wins outright
                gap = angular_separation_deg(tg.azimuth_deg, s.azimuth_deg)
                if gap <= self._radius and (best_gap is None or gap < best_gap):
                    best_i, best_gap = i, gap
            if best_i >= 0:
                self._assign(best_i, tg, t)
                claimed[best_i] = used[j] = True

        # Phase B — place unmatched targets: an idle slot first, else steal the stalest unclaimed slot.
        for j in order:
            if used[j]:
                continue
            tg = targets[j]
            i = next((k for k in range(self._n) if not claimed[k] and self._slots[k].azimuth_deg is None), -1)
            if i < 0:
                cand = [k for k in range(self._n) if not claimed[k]]
                if not cand:
                    continue                                       # all slots busy with louder talkers
                i = min(cand, key=self._staleness)                 # steal the longest-unseen slot
            self._assign(i, tg, t)
            claimed[i] = used[j] = True

        # Phase C — hold or release the slots that got no target this tick.
        out: List[BeamSlot] = []
        for i in range(self._n):
            s = self._slots[i]
            if claimed[i]:
                out.append(BeamSlot(i, s.azimuth_deg, s.seat_id, active=True, held=False))
            elif (s.azimuth_deg is not None and s.last_seen_t is not None
                  and (t - s.last_seen_t) <= self._hold):
                out.append(BeamSlot(i, s.azimuth_deg, s.seat_id, active=False, held=True))
            else:
                s.azimuth_deg, s.seat_id = None, None              # release the idle/expired slot
                out.append(BeamSlot(i, None, None, active=False, held=False))
        return out

    def _assign(self, i: int, tg: BeamTarget, t: float) -> None:
        s = self._slots[i]
        s.azimuth_deg, s.seat_id, s.last_seen_t = tg.azimuth_deg, tg.seat_id, t

    def _staleness(self, k: int) -> float:
        ls = self._slots[k].last_seen_t
        return ls if ls is not None else float("-inf")

    def reset(self) -> None:
        self._slots = [_SlotState() for _ in range(self._n)]
