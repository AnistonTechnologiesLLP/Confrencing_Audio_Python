# Design — "Cut a zone" toggle (commissioning nicety)

**Date:** 2026-06-26 · **Status:** approved design, pre-plan · **Branch:** `feat/zone-cut-toggle` (off `master`)

> Part of the POLARIS table-array coverage workflow's **#3 "live commissioning niceties"** group.
> That group is three INDEPENDENT mini-features — (a) this **cut-a-zone toggle**, (b) apply per-zone
> `gain_db` live, (c) learn-array-orientation. This spec covers **(a) only**; (b)/(c) are hardware-
> dependent and get their own cycles when the user is at the kit. This one is config-plane + GUI,
> fully headless-verifiable, and **independent of sub-features #1/#2** (exclusion zones predate them),
> so it branches off `master`.

## Context / problem

A coverage zone has a `type`: `dynamic` (steered pickup), `dedicated` (always-on pickup), or
`exclusion` (no-pickup / "cut"). The live engine **already honors exclusion zones**: an exclusion
zone is never a steer target (`is_pickup_zone` filtering), and when the runtime "Cut the door &
anyone outside the pickup area" toggle is on, `seat_mapper.exclusion_zone_azimuths` feeds the
beamformer's null set (`conf_pipeline_control/autosteer.py:30,318`). But there is **no way to flip an
existing zone's type** — to "cut" a pickup zone today you must delete it and redraw it as a no-pickup
area. After auto-generating zones (sub-feature #2), a commissioner often wants to silence one of them
(the zone over a hallway / HVAC corner) with one click. The missing piece is purely **design-time**.

## Goal & success criteria

A one-click **"Cut (no pickup)"** toggle on a coverage zone that flips it between active (`dynamic`)
and cut (`exclusion`), honored live by the existing exclusion handling with no live-DSP change.

Done when:
- Toggling a pickup zone to "cut" sets `type="exclusion"` (and `always_on=False`); un-cutting restores
  `type="dynamic"`. The flip is one undoable GUI action and re-renders the zone in the existing
  exclusion canvas style.
- A cut zone is reported `not is_pickup_zone` and appears in `exclusion_zone_azimuths` (so the live
  null path / pickup filtering already excludes it) — verified by tests, no live-DSP edit.
- `conf_pipeline` stays numpy-free; headless unit + GUI-probe tests pass. **No schema change** (`type`
  and `always_on` are already serialized at `CONFIG_VERSION = 5`).

Out of scope: per-zone live `gain_db` and learn-array-orientation (the other #3 items); any change to
the live DSP path or the existing runtime `zone_cut` toggle; a general zone-type editor UI beyond the
cut toggle (a future enhancement could expose `dedicated` too).

## Architecture & invariants

- **`conf_pipeline/coverage.py`** (EDIT) — new pure function mirroring the existing
  `set_zone_gain_db`/`set_zone_output_channel` idiom:
  `set_zone_type(array: MicrophoneArray, zone_id: str, zone_type: CoverageZoneType) -> MicrophoneArray`.
  It returns a copy of the array with that zone's `type` changed and:
  - **enforces the invariant** `always_on = (zone_type == "dedicated")`;
  - **clears `output_channel`** when flipping to `exclusion` (an exclusion zone may not carry a channel —
    `validation.py` forbids it);
  - **regenerates the array's output ports** (channel/lobe ports shift) via `generate_array_output_ports`,
    like the sibling setters;
  - **guards the manual-mode lobe cap:** if the array is in `manual` mode and the flip would raise the
    pickup-zone count above `MAX_MANUAL_LOBES`, raise `CoverageError("MANUAL_LOBE_LIMIT", …)` (flipping
    *to* exclusion lowers the count and is always safe; only un-cutting can trip it);
  - raises `CoverageError("COVERAGE_ZONE_INVALID", …)` if `zone_id` is not on the array.
- **`conf_pipeline/api.py`** (EDIT) — config-level wrapper
  `set_zone_type(config, array_id, zone_id, zone_type) -> SystemConfig`, mirroring the existing
  `set_zone_gain_db` wrapper (find array → delegate → swap into devices). Exported as `cp.set_zone_type`
  from `conf_pipeline/__init__.py`.
- **`conf_pipeline_gui/panels/design.py`** (EDIT) — a **"Cut (no pickup)"** `QCheckBox` in the existing
  per-zone selection editor (where `output_channel`/`gain_db` are edited, ~lines 264-308). Checked iff
  the selected zone's `type == "exclusion"`. Its handler calls
  `state.set_config(cp.set_zone_type(config, array_id, zone_id, "exclusion" if checked else "dynamic"))`
  (one undo), catching `CoverageError` → toast. The canvas already styles exclusion zones distinctly
  (`canvas._zone_style`), so the cut state renders for free.
- **Live layer:** **no change.** A cut zone is excluded from steer targets (`is_pickup_zone`) and nulled
  by the existing runtime toggle's `exclusion_zone_azimuths` path. (Documented in the README note: to
  *actively null* a cut zone live, the existing "Cut the door…" runtime toggle must be on; a cut zone is
  never *steered to* regardless.)

Each unit is small and independently testable: `set_zone_type` is a pure array→array transform; the
config wrapper is a thin swap; the GUI is one checkbox + handler.

## Schema / persistence

**None.** `CoverageZone.type` and `always_on` are already serialized (camelCase, `CONFIG_VERSION = 5`).
Flipping a type is ordinary data mutation → no version bump, no migration, no TS-sibling edit. Existing
configs round-trip byte-identically.

## Edge cases / decisions

- **Un-cut restores `dynamic`, not `dedicated`.** A zone that was originally `dedicated`, once cut and
  un-cut, becomes `dynamic`. Accepted: `dedicated` (always-on) is rare and the cut toggle is binary
  (active ↔ cut); restoring to `dynamic` (the common steered-pickup type) is the sensible default. A
  general type editor (future) can set `dedicated` explicitly.
- **Flipping to exclusion clears `output_channel`.** Required by validation. The channel assignment is
  not restored on un-cut (the user re-assigns or runs `auto_assign_zone_channels`). Documented.
- **Manual-mode cap:** un-cutting that would exceed `MAX_MANUAL_LOBES` raises `CoverageError`; the GUI
  shows the error as a toast and leaves the zone cut.
- **Idempotent:** setting a zone to the type it already has is a no-op-equivalent (returns an equal
  array); the GUI checkbox only fires on a real state change.

## Testing

Headless, numpy-free (default suite):
- `set_zone_type`: dynamic→exclusion sets `always_on=False` + clears any `output_channel`; exclusion→
  dynamic restores pickup; flipping to/from `dedicated` sets `always_on` correctly; unknown `zone_id`
  raises; un-cut past `MAX_MANUAL_LOBES` in manual mode raises `CoverageError`; ports regenerated;
  `cp.validate` passes after a flip; `serialize`/`deserialize` round-trips a cut zone byte-identically.
- A cut (`exclusion`) zone is `not is_pickup_zone` and appears in `seat_mapper.exclusion_zone_azimuths`
  for a posed array (proving the live path already honors it — no live edit).
- GUI probe (single-panel construct, `QT_QPA_PLATFORM=offscreen`): the per-zone "Cut" checkbox reflects
  the selected zone's type, and toggling it flips the zone in `state.config` in exactly one undo step.
  (Full `MainWindow` behaviour is CI-only per repo policy.)

Verification: `QT_QPA_PLATFORM=offscreen ./.venv/Scripts/python.exe -m pytest -q` and
`./.venv/Scripts/python.exe -m mypy`. green-gate for the suite; schema-parity-guard is N/A (no schema
change) but a confirming pass is fine since `coverage.py`/`api.py`/`__init__.py` are touched.

## Risks / open questions

- **Discoverability vs. the runtime toggle:** there are now two "cut" concepts — the design-time zone
  *type* (this feature) and the runtime "Cut the door…" *behavior* toggle. The README/tooltip must
  state the relationship clearly (cut = mark a zone no-pickup; the runtime toggle decides whether to
  actively null no-pickup zones during auto-steer; a cut zone is never steered to regardless). Naming
  the checkbox "Cut (no pickup)" and the existing one "Cut the door & anyone outside…" keeps them
  distinct.
- **Losing channel/dedicated state on cut** (above) — accepted, documented; a future general type
  editor can preserve more.
