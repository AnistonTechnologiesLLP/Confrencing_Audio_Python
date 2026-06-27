# Lobe Control Guide

Phase 11 of the audio front-end work. **Lobe Control** gives the operator plain controls over the
beamformer's *pickup pattern* — where the POLARIS array listens, how focused the pickup is, which
direction to suppress, and whether the beam is fixed or auto-following.

It is **not** capsule calibration. Calibration (Phase 1) aligns the 8 raw MEMS channels; Lobe Control
shapes the beam *after* that alignment. Calibration makes the lobe *accurate*; Lobe Control *aims* it.

---

## 1. What a lobe is
A beamformer combines the 8 capsules so the array listens more in one direction and less in others. The
direction it listens *most* is the **main lobe** — think of it as an adjustable cone of "best hearing"
pointed into the room. Lobe Control points and shapes that cone.

## 2. Main lobe vs. side lobes
A real array never has a single perfect cone. Besides the **main lobe** (the intended direction) there are
smaller **side lobes** — weaker directions the array still partly hears. That's physics, not a bug: it is
why a beam **reduces** off-axis sound rather than removing it. A narrower main lobe usually trades into
stronger side lobes and more self-noise, so "narrow" is not always "better."

## 3. What "null direction" means
A **null** steers a *low-sensitivity* direction toward something you want quieter — a projector fan, an AC
vent, a hallway. The beamformer puts a dip in its pattern there. Crucially:

> **A null REDUCES pickup from that direction. It does NOT mute it.** It is a *reduced-pickup zone*, not a
> hard-mute zone.

You can set up to **2** nulls (the engine's cap). Honest labels only — Lobe Control says *pickup focus*,
*suppress direction*, *reduced-pickup zone*; it never claims "soundproof", "perfect privacy", or "100%
block".

## 4. Why it is not perfect audio fencing
Side lobes, room reflections, and the array's finite size mean a beam/null is a *bias*, not a wall. A
person on the null side will be quieter but still audible — especially their reflections off walls/table,
which arrive from other directions the main lobe still hears. Use Lobe Control to **favour** the talkers
you want and **de-emphasise** a noisy direction; do not promise a participant they are inaudible.

## 5. Why calibration improves lobe accuracy
The beam math assumes the 8 capsules are matched in gain, polarity and timing. If they drift, the array's
idea of "0°" and the shape of the lobe are off, so the lobe points slightly wrong and nulls land
imperfectly. A **calibration profile** (Phase 1 — the LIVE "Load calibration profile…" action) re-aligns
the capsules, so the lobe you aim is the lobe you get. Lobe Control still works with calibration OFF, but
the LIVE summary warns *"Calibration is OFF — lobe direction may be less accurate."*

## 6. Why BAD placement reduces lobe/null effectiveness
If the **placement check** is BAD (the array is too close to a fan, badly positioned, clipping, or the
room is very reverberant), no amount of steering fixes the underlying physics: the noise is loud and
arrives from many directions, so a single null can't bias enough and reflections fill the gaps. When a
placement result is BAD the summary warns *"Placement is BAD — lobe/null control may underperform until
physical noise is fixed."* Fix the placement first; Lobe Control is the last 10%, not the first 90%.

## 7. How the listening modes use Lobe Control
Lobe Control reflects the existing LIVE **Listening mode** (it does not add a new mode):

| Listening mode | Lobe mode | Direction | Focus | Nulls |
|---|---|---|---|---|
| **Whole table** | whole table | broad (no manual aim) | **wide** (never forced narrow) | off |
| **Follow the room** | follow (auto-steer) | auto — follows the talker (DOA) | medium | optional |
| **Lock to a seat** | fixed-to-seat | the selected seat's bearing (or follows if no seat) | medium | optional |
| **Manual (advanced)** | fixed / seat | **your** angle dial or seat — *your controls are the source of truth* | your choice | your choice |
| **Two kits** | whole table | per-kit (combined-room lobe control is limited) | wide | off |

Picking a listening mode does **not** silently rewrite your lobe controls — Lobe Control mirrors the mode
and shows the result in the summary + preview; you adjust direction/focus/nulls explicitly, and they apply
when you Connect (direction + nulls also apply live on a running steered beam).

## 8. The controls (LIVE → "Lobe control" card)
- **Listen toward** — a seat selector *or* a manual angle dial (−180°…+180°, array-relative; 0° = the
  array's front reference set by "Calibrate front"). Lock-to-seat needs the array's bearing set; a missing
  seat safely falls back to following.
- **Pickup focus** — Wide / Medium / Narrow. These map to the engine's real levers (beam **mode** +
  **robustness/loading**): *Wide* = robust/broad, *Medium* = the current default, *Narrow* = more
  directive (but more self-noise). It is **not** a continuous physical beamwidth — a mode change applies at
  Connect. The card says so.
- **Suppress direction** — Off / Angle / Seat, with the "reduces, not mutes" warning. Capped at 2.
- **Summary** — e.g. `Lobe: fixed 35° · width medium · null 180° · calibration ON · placement BAD warning`.
- **Preview** — a small **schematic** top-down sketch (array, main-lobe wedge, null line, seat dot),
  clearly labelled *preview* — not to scale and not a measured beam pattern.

## 9. Safety / behaviour
- Direction + nulls update **live** (debounced) via the engine's atomic `set_steering`/`set_nulls`; they
  never block the audio thread. A focus **mode** change applies at **Connect**.
- Lobe Control changes **no DSP default** until you touch it; it does not enable DFN3, dereverb, AEC, or
  the voice gate; it does not change calibration defaults or auto-apply placement notches.
- It never blocks the operator — calibration-OFF and placement-BAD are *warnings*, not locks.

## 10. Out of scope
No perfect audio fencing / "soundproof" / "100% block" claims; no continuous physical beamwidth; no new
beam math; no removal of the existing controls (the A/B-engine seat-lock, auto-steer sectors, "Calibrate
front", null-the-other-seats all remain). In code: `conf_pipeline_control.lobe_control.LobeControl`.
