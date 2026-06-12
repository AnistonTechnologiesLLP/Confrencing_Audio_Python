"""Workflow stages: pure predicates over the config + state.

These power the ModeBar status dots, the per-panel next-step hint chips, and
the mode-aware canvas empty-states. They are the old guide-panel checklist
generalised to the five top-level modes (design → simulate → route → deploy →
live), so progress updates no matter how the design is edited.
"""
from __future__ import annotations

import conf_pipeline as cp

MODES = ("design", "simulate", "route", "deploy", "live")
MODE_LABELS = {
    "design": "Design",
    "simulate": "Simulate",
    "route": "Route",
    "deploy": "Deploy",
    "live": "Live",
}

# Status values for a mode dot.
DONE = "done"
PARTIAL = "partial"
TODO = "todo"


# ---- predicates over the live config (moved from guide.py) ----
def has_room(c) -> bool:
    return c.room is not None and len(c.room.vertices) >= 3


def has_array(c) -> bool:
    return any(d.type == "microphoneArray" for d in c.devices)


def has_zone(c) -> bool:
    return any(d.type == "microphoneArray" and d.zones for d in c.devices)


def has_talker(c) -> bool:
    return len(c.talkers) > 0


def is_optimized(c) -> bool:
    # "optimized" ≈ arrays placed AND at least one route exists (auto-route ran)
    arrays = [d for d in c.devices if d.type == "microphoneArray"]
    placed = bool(arrays) and all(d.position is not None for d in arrays)
    return placed and len(c.routes) > 0


def _grade(flags: list[bool]) -> str:
    if all(flags):
        return DONE
    return PARTIAL if any(flags) else TODO


def stage_status(state, validation=None) -> dict[str, str]:
    """Per-mode completion for the ModeBar dots.

    ``validation`` is an optional pre-computed ``cp.validate(config)`` result so
    callers that already validated this change don't pay for it twice. The LIVE
    dot reflects the audio session, which the shell owns — here it only reports
    config-side readiness (a placed array exists).
    """
    c = state.config
    res = validation if validation is not None else cp.validate(c)

    design = _grade([has_room(c), has_array(c), has_zone(c), has_talker(c)])

    covered_all = False
    if c.talkers:
        rep = cp.coverage_report(c)
        covered_all = len(rep.covered) == len(c.talkers)
    if state.sim_recommendation is not None or (c.talkers and covered_all):
        simulate = DONE
    elif state.sim_show_heatmap or state.sim_heatmap is not None:
        simulate = PARTIAL
    else:
        simulate = TODO

    aec_on = any(d.aec.enabled for d in c.devices if cp.is_mic_device(d))
    if is_optimized(c):
        route = DONE
    elif c.routes or aec_on:
        route = PARTIAL
    else:
        route = TODO

    deployed = state.rooms[state.active_room].get("last_deployed") is not None
    if res.ok and deployed:
        deploy = DONE
    elif res.ok and c.devices:
        deploy = PARTIAL
    else:
        deploy = TODO

    live = PARTIAL if any(d.type == "microphoneArray" and d.position for d in c.devices) else TODO
    return {"design": design, "simulate": simulate, "route": route, "deploy": deploy, "live": live}


def next_hint(state, mode: str) -> str:
    """The single most useful next step for a mode's panel header chip."""
    c = state.config
    if mode == "design":
        if not has_room(c):
            return "Next: draw a room (R) — or load a sample"
        if not has_array(c):
            return "Next: add a microphone array"
        if not has_zone(c):
            return "Next: drag a coverage zone (Z)"
        if not has_talker(c):
            return "Next: place a talker (T)"
        if not is_optimized(c):
            return "Next: run Optimize room"
        return ""
    if mode == "simulate":
        if not has_array(c):
            return "Nothing to simulate — add an array in Design"
        if state.sim_recommendation is None:
            return "Next: press Recommend for a placement"
        return ""
    if mode == "route":
        if not c.devices:
            return "Nothing to route — add devices in Design"
        if not c.routes:
            return "Next: run Auto-Route to wire the system"
        return ""
    if mode == "deploy":
        res = cp.validate(c)
        if not res.ok:
            return f"Fix {len(res.errors)} error(s) before deploying"
        if state.rooms[state.active_room].get("last_deployed") is None:
            return "Next: Deploy to snapshot this design"
        return ""
    if mode == "live":
        if not any(d.type == "microphoneArray" and d.position for d in c.devices):
            return "Place a microphone array in Design first"
        return ""
    return ""
