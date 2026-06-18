"""First-run setup guide — the pure, Qt-free step model.

The LIVE panel shows a small dismissible checklist that walks a first-time user
through getting audio out of the array: pick a listening mode, connect, check the
capsules, calibrate the front, and hear it. All of the "which step is active /
done / complete" logic lives here as plain functions over a :class:`GuideSnapshot`
so it is fully unit-testable without a ``QApplication``; the Qt layer only builds a
snapshot from the live widgets and renders the rows.

Two honesty points baked into the gates (both real traps):
- the listening-mode combo defaults to "table", so the mode step keys off an
  explicit *touched* flag, never ``value != default``;
- front calibration keys off a dedicated *calibrated* flag, never
  ``front_offset != 0`` (0° is a legitimate "already front-aligned" result).
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class GuideSnapshot:
    """A flat, Qt-free view of the LIVE-panel state the guide reasons over."""
    listening_mode: str = "table"          # currentData(): follow/seat/table/clean/manual
    listening_mode_touched: bool = False   # user actually picked a mode (not the programmatic default)
    has_array: bool = False                # a placed array is selected
    controls_available: bool = False       # the [control] extra is installed (real DSP, not simulation)
    busy: bool = False                     # a live session is running (mirrors _live_busy())
    caps_probed: bool = False              # Detect-silent has run
    front_calibrated: bool = False         # Calibrate-front succeeded
    monitor_on: bool = False               # Monitor-output is ticked
    meter_level: float = 0.0               # 0..1 output-meter fraction the live ticks compute
    heard_ack: bool = False                # manual "Got it, I can hear it" fallback


# QSettings key the LIVE panel persists the "first-run guide completed" flag under.
GUIDE_DONE_SETTING = "live/firstRunGuideDone"

STEP_MODE = "mode"
STEP_CONNECT = "connect"
STEP_DETECT = "detect"
STEP_CALIBRATE = "calibrate"
STEP_HEAR = "hear"


@dataclass(frozen=True)
class GuideStep:
    id: str
    title: str
    optional: bool = False


GUIDE_STEPS: tuple[GuideStep, ...] = (
    GuideStep(STEP_MODE, "Pick how to listen"),
    GuideStep(STEP_CONNECT, "Connect to the array"),
    GuideStep(STEP_DETECT, "Check the capsules", optional=True),
    GuideStep(STEP_CALIBRATE, "Calibrate the front", optional=True),
    GuideStep(STEP_HEAR, "Hear it"),
)

# Front calibration only matters for modes that steer to a direction; for "table"
# / "manual" the front offset is unused, so the step is auto-skipped (not shown).
_FRONT_MODES = frozenset({"follow", "seat", "clean"})


def step_relevant(step_id: str, snap: GuideSnapshot) -> bool:
    """Whether a step applies to the current mode (irrelevant steps are auto-skipped)."""
    if step_id == STEP_CALIBRATE:
        return snap.listening_mode in _FRONT_MODES
    return True


def step_done(step_id: str, snap: GuideSnapshot) -> bool:
    if not step_relevant(step_id, snap):
        return True                                       # not applicable → treated as satisfied
    if step_id == STEP_MODE:
        return snap.listening_mode_touched
    if step_id == STEP_CONNECT:
        return snap.busy
    if step_id == STEP_DETECT:
        return snap.caps_probed
    if step_id == STEP_CALIBRATE:
        return snap.front_calibrated
    if step_id == STEP_HEAR:
        return snap.heard_ack or (snap.busy and snap.monitor_on and snap.meter_level > 0.0)
    return False


def active_step(snap: GuideSnapshot) -> str | None:
    """The first not-done, relevant step to highlight (optional steps included); None when all ticked."""
    for s in GUIDE_STEPS:
        if step_relevant(s.id, snap) and not step_done(s.id, snap):
            return s.id
    return None


def required_done(snap: GuideSnapshot) -> bool:
    """Completion trigger: every relevant *required* step is done. Optional steps
    (detect / calibrate) enrich the setup but never block completion — so a
    no-hardware / simulation run can still finish the guide."""
    return all(
        step_done(s.id, snap)
        for s in GUIDE_STEPS
        if step_relevant(s.id, snap) and not s.optional
    )


def all_done(snap: GuideSnapshot) -> bool:
    """Every relevant step (including optional ones) is ticked."""
    return active_step(snap) is None


def progress(snap: GuideSnapshot) -> tuple[int, int]:
    """(#done, #relevant) over the relevant steps (optional included)."""
    relevant = [s for s in GUIDE_STEPS if step_relevant(s.id, snap)]
    return sum(1 for s in relevant if step_done(s.id, snap)), len(relevant)
