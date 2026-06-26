# Design — Auto-generate coverage zones from furniture seating (POLARIS table array)

**Date:** 2026-06-26 · **Status:** approved design, pre-plan · **Branch:** `feat/auto-seat-zones`
(stacked on `feat/aperture-aware-coverage` / PR #25 — rebase onto master once #1 merges)

> Sub-feature **#2 of 3** in the "POLARIS table-array coverage workflow" program.
> **#1** (honest aperture-aware coverage simulation) is done — PR #25. This spec covers **#2 only**:
> turning the room's furniture/seating into coverage zones automatically. **#3** (live commissioning
> niceties: apply zone `gain_db`, zone-cut UI toggle, learn-array-orientation) gets its own cycle.
>
> **Depends on #1:** reuses `conf_pipeline/directivity.py` (`steered_beamwidth_deg`, `separable`) and
> the `polaris-8` profile aperture to decide which seats an array can physically resolve. Without #1 the
> clustering would fall back to the old fixed 35° and re-introduce over-promising.

## Context / problem

A designer lays out a room (DESIGN mode): places a mic array, furniture, and — for coverage — must
**hand-draw a `CoverageZone` per seating area** on the array. That manual step is the tedious part of
the MXA710-style "coverage-area design" workflow we're building. Meanwhile the model already knows the
room's seating: furniture (`RoomObject`) carries explicit `seats: list[SeatAnchor]`, the catalog encodes
`seat_capacity` (chair = 1, sofa = 3, table = 0), `seat_mapper.room_seats(config)` enumerates seats, and
`seats_owned_by_array(config, array_id)` already partitions seats to their nearest array. Nothing turns
that seating into zones, and — critically — a naive "one zone per seat" would **over-promise** on a
40 mm POLARIS, the exact failure #1 just fixed.

## Goal & success criteria

A one-click DESIGN action — **"Auto-generate zones from seating"** — that, for the selected mic array,
derives seats from furniture, clusters them by what the array can *physically resolve*, and writes one
pickup zone per cluster, **replacing** that array's zones (one undo step).

Done when:
- A room with chairs/sofas (no hand-entered seat anchors) generates sensible per-seat zones with one click.
- Seats a `polaris-8` array **cannot separate** (closer than its calibrated beamwidth allows) are
  **merged into one shared zone** — never more than 8 zones/array, never over-promising. Seats it *can*
  resolve stay individual.
- The result is one undoable change; re-running is deterministic (replace-all).
- `conf_pipeline` stays **numpy-free**; headless unit tests pass; **no schema change** (zones are already
  serialized v5 data).

Out of scope (sub-features / YAGNI): seat guessing around bare tables; per-zone output-channel / gain
auto-assignment (the existing `auto_assign_zone_channels` covers channels); any live-DSP change;
room-wide "all arrays at once" (the action targets the selected array — run per array, each owning its
nearest seats); learn-array-orientation (#3).

## Architecture & invariants

All new logic is **pure-stdlib** in a focused new module **`conf_pipeline/seat_zones.py`** (one
responsibility; composes existing pieces, duplicates none). It reuses: `seat_mapper`
(`room_seats`, `seats_owned_by_array`, array-relative azimuth helpers), `directivity`
(`steered_beamwidth_deg`, `separable` — #1), `profiles.device_capabilities`, `furniture`
(`resolved_dimensions`, footprint geometry), and `coverage`/`api` to build + attach zones.

- **No schema change.** `CoverageZone` (type/shape/label/gain_db/output_channel) and `SeatAnchor` are
  already fully serialized at `CONFIG_VERSION = 5`. Generated zones are ordinary config data → no version
  bump, no migration, no TS-sibling edit. (Confirmed: `coverage.py` factories + `add_coverage_zone`
  already round-trip.)
- **Pure / undoable.** The backend is a pure `(config) -> new config` transform; the GUI applies it via
  `AppState.set_config(res.config)` = exactly one undo checkpoint + coverage-cache invalidation + repaint.
- `conf_pipeline` numpy-free (the directivity helpers are stdlib).
- Respect the **8-zones/array** cap (`MAX_ZONES_PER_ARRAY`) and the `always_on == (type=="dedicated")`
  invariant that `validate()` enforces.

## Seat derivation (`derived_room_seats`)

`derived_room_seats(config) -> list[tuple[str, SeatAnchor]]` extends `seat_mapper.room_seats`:

- Furniture with an **explicit `seats`** list → used verbatim (user intent wins; no derivation).
- **chair** / **seat** (catalog capacity 1, no explicit seats) → **1** seat at the furniture's
  `position`, `facing_deg` from its `rotation_deg`.
- **sofa** (catalog capacity N, no explicit seats) → **N** seats evenly spaced across the sofa's width
  (local +X, rotated by `rotation_deg`), at mid-depth.
- **table / desk / cabinet / screen / door / window / plant** → **no** derived seats (no bare-table
  guessing). Covered only if they carry explicit anchors.

Seat IDs keep the existing `"{furniture_id}-seat{i}"` (1-based) scheme so they align with
`coverage_sim.room_targets`.

## Separability clustering (the #1 tie-in)

`generate_seat_zones(config, array_id)` clusters the seats the array **owns**
(`seats_owned_by_array` → nearest-array, so multi-array rooms don't double-cover):

1. For each owned seat, compute its **array-relative azimuth** (`seat_mapper`) and the array's
   **steered half-beamwidth** at that azimuth via `directivity.steered_beamwidth_deg(aperture_m,
   SIM_SPEECH_FREQ_HZ, off_nadir)` — aperture from the array's profile (#1); **falls back to the fixed
   35°** for profiles without a declared aperture (consistent with #1's no-regression fallback).
2. Sort seats by azimuth; greedily merge **consecutive seats that are not `separable(gap, max_half)`**
   into one group. Resolvable seats stay individual; unresolvable neighbours merge.
3. If groups still exceed 8, **force-merge** the closest-azimuth pairs until 8 and add a warning
   ("more seats than this array can address — merged the closest").
4. **No array pose** (missing `position`/`bearing_deg`) → azimuths are undefined → fall back to **one
   zone per seat**, capped at 8 (proximity-merge the overflow), and warn that setting the array bearing
   yields honest grouping.

## Zone construction

Each group → a **dynamic** pickup `CoverageZone` (steered automix coverage — the array picks the active
talker among zones; not `dedicated`/always-on). Shape = axis-aligned **`RectShape`** = the bounding box
of the group's seat points expanded by a ~0.35 m margin, with a ~0.6 m minimum side. Labels:
`"Seat 3"` for a single seat, `"Seats 3–4"` for a merge. The pure backend mints zone IDs
deterministically in the app's existing style — `"{array_id}-z{n}"`, `n = 1…` over the generated set
(safe because replace-all clears prior zones first, so there's no collision). No `output_channel` /
`gain_db` set (left `None`).

## GUI surface

- A **"Auto-generate zones from seating"** button in the DESIGN panel's Coverage group
  (`conf_pipeline_gui/panels/design.py`), beside the existing mode / "+ Zone" controls.
- Handler mirrors the existing one-click generators (`_optimize_room` / `_auto_route`): call
  `cp.generate_seat_zones(config, selected_array_id)`, then `state.set_config(res.config)` (one undo),
  show a `QMessageBox` summary (zones created, which seats were merged and why, any warnings), and a
  toast. The #1 SIMULATE-panel "Coverage warnings" then reflect the generated layout automatically (no
  extra wiring).
- Disabled / guarded with a clear message when the selected array owns no derivable seats.

## Schema / persistence

**None.** Zones + seat anchors are already serialized at `CONFIG_VERSION = 5`. No migration, no
TS-sibling change. (A future sub-feature that persists a *new* field — e.g. marking a zone "auto" —
would trigger the full schema/TS-parity checklist; this one does not.)

## Result type

`generate_seat_zones(config, array_id) -> SeatZoneResult` with:
`config` (new, that array's `zones` replaced), `created: list[str]` (zone labels), `merged:
list[str]` (human-readable "merged because …" notes), `warnings: list[str]`. The GUI renders these in
the summary dialog.

## Testing

Headless, numpy-free (default suite):
- `derived_room_seats`: chair → 1 seat; sofa(N) → N spaced seats; explicit anchors used verbatim;
  table → none.
- `generate_seat_zones` on `polaris-8`: well-spaced seats → individual zones; close seats → one merged
  zone (uses #1's aperture beamwidth); > 8 seats → force-merge + warning; **replace-all** (prior zones,
  manual or generated, are gone); non-aperture profile → 35°-based grouping; no-pose → per-seat fallback
  + warning; never exceeds `MAX_ZONES_PER_ARRAY`; output passes `validate()`.
- Headless DESIGN-panel probe (single-panel construct, `QT_QPA_PLATFORM=offscreen`): button → the
  selected array gains generated zones → exactly one undo step. (Full `MainWindow` behaviour is CI-only
  per repo policy.)

Verification commands (`.venv`, not `.venv311`):
`QT_QPA_PLATFORM=offscreen ./.venv/Scripts/python.exe -m pytest -q` and
`./.venv/Scripts/python.exe -m mypy`. green-gate for the suite; schema-parity-guard is N/A (no schema
change) but a confirming pass is fine since `api.py`/`__init__.py` gain an export.

## Risks / open questions

- **Sofa seat spacing heuristic:** evenly-spaced-across-width is an approximation; acceptable because
  explicit anchors always override it, and a sofa's occupants are rarely separable by a 40 mm array
  anyway (they'll usually merge). If it misleads, allow a per-sofa anchor override (already supported).
- **Clustering by azimuth only (1-D):** seats are grouped by array-relative *azimuth*, ignoring radial
  distance. This matches the array's real resolving power (a planar array resolves azimuth, not range),
  so it is the correct axis; documented so a future enhancement doesn't "fix" it wrongly.
- **Dynamic vs dedicated default:** generated zones are `dynamic` (steered). If a deployment wants
  always-on per-seat capture, the user can change a zone's type after generation; a generator option is a
  future enhancement, not v1.
