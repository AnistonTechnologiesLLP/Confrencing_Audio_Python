# Design — Honest aperture-aware coverage simulation (POLARIS table array)

**Date:** 2026-06-25 · **Status:** approved design, pre-plan · **Branch:** `feat/aperture-aware-coverage`

> Sub-feature **#1 of 3** in the "POLARIS table-array coverage workflow" program. The other two get
> their own spec → plan cycles: **#2** auto-zones from furniture seating, **#3** live commissioning
> niceties (apply zone `gain_db`, zone-cut UI toggle, learn-array-orientation). This spec covers **#1
> only**: making the coverage *simulation* tell the truth about a small array's real directivity.

## Context / problem

The coverage-area design workflow (DESIGN → SIMULATE → ROUTE → DEPLOY → LIVE) already works for a
table-placed array: placement (`position`/`elevation`/`bearing_deg`), `CoverageZone`s,
`design_zone_beams` → live LCMV, auto-steer/zone-cut, multibeam, scenes. **But the SIMULATION
over-promises directivity for a small array.** It scores every array with a *fixed* steered half-angle
of **35°** (`conf_pipeline/coverage_sim.py:45` `DEFAULT_PICKUP_BEAM_HALF_DEG`,
`conf_pipeline/sim/types.py` `SimParams.lobe_halfwidth_deg=35.0`, used in `conf_pipeline/sim/scoring.py`
`direct_level_db`/`coverage_score`). 35° is reasonable for a ~1 m ceiling array at ~3 m, but the **40 mm
POLARIS at table range (~1–1.5 m) is far coarser (~near-omni at low speech frequencies, ~35–70° up high)
and has a ~5.6 kHz spatial-aliasing ceiling.** So the designer can lay out per-seat zones the POLARIS
**cannot physically separate**, the sim scores them as covered, and they fail live — the exact
coarse-aperture reality established in the offline audit.

## Goal & success criteria

Make the simulation reflect the array's **real, aperture-limited directivity**, and flag what it can't
do, so a POLARIS table layout is scored honestly.

Done when:
- A 40 mm POLARIS table layout with seats ~0.6 m apart at ~1.2 m is reported **not cleanly separable**
  (a warning), and its coverage score drops vs the old 35° model.
- The analytic beamwidth **matches the measured `sensibel_8` beam within tolerance** (calibration test).
- `conf_pipeline` stays **numpy-free**; headless unit tests pass.
- **No regression:** arrays without a declared aperture (every current config; ceiling profiles unless
  we set one) keep the **legacy 35°** scoring → existing sim/coverage tests are byte-unchanged.

Out of scope (sub-features #2/#3): auto-zone generation, live zone-gain application, zone-cut UI,
bearing-learn, and any change to the live DSP path.

## Architecture & invariants

All new logic is **pure-stdlib in `conf_pipeline/`** (no numpy — hard invariant). New + touched units:

- **`conf_pipeline/directivity.py`** (NEW, pure stdlib) — the physics model, one clear purpose:
  - `steered_beamwidth_deg(aperture_m, freq_hz, steer_deg) -> float` — 3 dB **half-angle** of the
    steered main lobe. Physics: broadside ≈ `k·λ/aperture` (radians, `λ = c/f`), clamped to a near-omni
    ceiling when `λ ≳ aperture`; **widens with the steer angle** off the array reference. The constant
    `k` AND the steer-angle dependence are **set by the calibration** (not asserted here — a circular
    planar array's azimuth/off-nadir dependence is not a simple `1/cos`); `steer_deg`'s exact
    parameterization (azimuth vs off-nadir) is finalized at the calibration step.
  - `alias_ceiling_hz(element_spacing_m) -> float` = `c / (2·spacing)` (≈5.6 kHz for POLARIS).
  - `separable(sep_deg, beamwidth_half_deg) -> bool` — two looks are separable when their angular
    separation exceeds ~the sum of half-beamwidths (a single, documented criterion).
- **`conf_pipeline/profiles.py`** (EDIT) — add optional `aperture_m: Optional[float]` (and
  `element_spacing_m: Optional[float]`) to `DeviceCapabilities` (catalog constants alongside
  `coverage_angle_deg`). Set them for `generic-table-array`/`generic-ceiling-array` and a new
  `polaris-8` profile (radius 40 mm → aperture ≈ 0.08 m, spacing ≈ 0.0306 m). **Catalog-only → NOT
  persisted (configs store only `profileId`) → no `CONFIG_VERSION` bump, no TS-parity.** (Confirmed:
  `persistence.py:54-57` persists `profileId`; profile properties live in code.)
- **`conf_pipeline/sim/scoring.py` + `coverage_sim.py`** (EDIT) — when the array's profile has
  `aperture_m`, use `steered_beamwidth_deg(...)` for the per-target off-broadside angle + a
  representative speech frequency, in place of the fixed `lobe_halfwidth_deg`. When `aperture_m` is
  `None`, **use the legacy 35°** (no-regression fallback).
- **`conf_pipeline/sim/types.py`** (EDIT) — the sim result gains structured **warnings**
  (`unseparable_pairs`, `grating_lobe_risk`). `SimParams` keeps `lobe_halfwidth_deg` as the fallback.
- **Surfaces (GUI, CI-verified only):** SIMULATE panel lists the warnings; `canvas.py` draws the *true*
  beamwidth wedge; `conf_pipeline/report.py` commissioning report includes the warnings. (Logic is
  headless; rendering is verified in CI per the repo's GUI-test policy.)

Calibration lives in **`conf_pipeline_control`** (numpy is allowed there) / a `[control]`-gated test —
it must not pull numpy into `conf_pipeline`.

## The beamwidth model + calibration (Approach A)

`steered_beamwidth_deg` is a closed-form physics approximation; **calibration ties it to the real
array** so the sim and the live beamformer agree:

1. **Measure** the real `sensibel_8` (40 mm) steered beam: using the existing numpy beam analysis
   (`conf_pipeline_control/beamformer.py` `design_from_bearings` + `response_db`/`analyze_lobes`),
   compute the 3 dB main-lobe half-width across representative **speech frequencies** (≈0.5/1/2/3.4 kHz)
   and **steer off-broadside angles** (0/30/60°), for the default mode (delay-sum and superdirective).
2. **Fit/verify** `steered_beamwidth_deg` against those measurements; pick `k` + the off-broadside law
   so the analytic curve matches within tolerance and (a) **reduces to ≈35°** at a large-aperture
   ceiling reference (so ceiling scoring is preserved) and (b) reproduces the POLARIS's near-omni-low /
   coarse-high behaviour.
3. **Guard with a test:** a `[control]`-gated test asserts `|analytic − measured| ≤ ~12°` across the
   grid. This is the "calibrated" guarantee; if the beamformer changes materially, this test fails.

## Warnings (separability + grating-lobe)

Computed in the sim from the honest beamwidth, attached to the result:
- **Separability:** for each pair of pickup zones/seats, angular separation from the array vs
  `separable(...)`; unseparable pairs are listed (and depress the coverage/fairness score so the
  optimizer stops recommending them).
- **Grating-lobe / HF-aliasing:** if coverage depends on directivity above `alias_ceiling_hz`, flag
  "directivity degrades above ~5.6 kHz."

## Schema / persistence

**None.** `aperture_m`/`element_spacing_m` are profile-catalog constants (not serialized); warnings are
computed, not stored. No `CONFIG_VERSION` change, no migration, no TS-sibling edit. (If a future
sub-feature persists a custom per-array aperture, *that* triggers the full schema/TS-parity checklist —
not this one.)

## Testing

Headless, numpy-free (the default suite):
- `directivity.py` units: monotonic in freq (narrower up high), aperture (larger → narrower), and
  off-broadside (wider); near-omni when `λ ≫ aperture`; ceiling-reference aperture ≈ 35°; `separable`
  boundary; `alias_ceiling_hz` ≈ 5.6 kHz for POLARIS spacing.
- scoring/coverage_sim: a POLARIS table layout (seats ~0.6 m @ ~1.2 m) yields `unseparable_pairs` and a
  **lower** score than the legacy model; a ceiling array (no aperture / ceiling aperture) is **unchanged**.
- Regression: existing `tests/test_coverage*.py` / sim tests pass untouched (legacy fallback path).

`[control]`-gated (numpy): the calibration test (analytic vs measured `sensibel_8` beam).

Verification commands (`.venv`, not `.venv311`):
`QT_QPA_PLATFORM=offscreen ./.venv/Scripts/python.exe -m pytest -q` and `./.venv/Scripts/python.exe -m mypy`.
Use the **green-gate** agent for the suite; **schema-parity-guard** is N/A here (no schema change) but
worth a confirming pass since `profiles.py` is touched.

## Risks / open questions

- **Calibration fidelity:** if the analytic form can't match the measured beam within tolerance across
  the grid, fall back to **Approach B** (a small per-profile beamwidth lookup table baked from the
  measurement) — same interfaces, table instead of formula. Decide at the calibration step.
- **Representative frequency vs per-band:** the sim is single-narrowband today. Start with one
  representative speech frequency for the beamwidth; per-band refinement is a later enhancement if the
  single value misleads.
- **Ceiling continuity:** must verify the chosen `k` keeps ceiling-array scores within noise of today's
  (the calibration's "reduces to 35°" check + the regression tests cover this).
