# Phase 11 — Lobe Control · Report

## SUMMARY
Added operator **Lobe Control** to the LIVE screen: where the array listens (manual angle / seat), how
focused the pickup is (Wide/Medium/Narrow), which direction to suppress (≤2 nulls), and fixed-vs-follow —
plus a compact summary, honest warnings, and a schematic preview. It is **not** calibration: it shapes the
beam *after* capsule alignment. The feature **surfaces and reuses** the existing steering/null machinery
(`set_steering`/`set_nulls`, seat→azimuth helpers, the listening-mode dropdown) rather than rewriting any
of it, and changes **no DSP/engine/calibration default** until the operator touches it. Honest labels only
— a null *reduces* pickup, it does not mute; no "soundproof / 100% block" claims.

## FILES CHANGED
- **NEW** `conf_pipeline_control/lobe_control.py` — `LobeControl` (+ `LobeNull`, `LobeSafety`) model:
  validate / clamp_angle / effective_nulls / summary / warnings / camelCase JSON / `default_lobe_for_mode`
  / `loading_for_width`. Pure stdlib.
- `conf_pipeline_control/__init__.py` — exports the lobe API.
- `conf_pipeline_control/listening_profile.py` — `LpSpatial.beam_width` (default "medium"); built-ins set
  it per mode (table/twokit = "wide", others "medium"); camelCase `beamWidth` round-trips.
- `conf_pipeline_gui/panels/common.py` — **NEW** `LobePreview(QWidget)` (minimal schematic preview) +
  `QPainterPath` import.
- `conf_pipeline_gui/panels/live.py` — the "Lobe control" Card (direction / focus / suppress / summary /
  warnings / preview), `_current_lobe_control` / `_update_lobe_summary` / `_on_lobe_changed` /
  `_apply_lobe_now` (debounced) / `set_lobe_placement_status` / `_lobe_mode_from_listening` /
  `_resolve_seat_az`; lobe refresh wired into `_on_listening_mode_changed`.
- **NEW** `docs/LOBE_CONTROL_GUIDE.md`, this report, `reports/audio/phase11_lobe_control_discovery.md`;
  updated listening/operator guides + tracker.
- **NEW tests** `tests/test_lobe_control.py` (14), `tests/test_gui_lobe_control.py` (8); +3 lobe tests in
  `tests/test_listening_profiles.py`.

## TESTS RUN
- `pytest -q tests/test_lobe_control.py` → **14 passed**.
- `QT_QPA_PLATFORM=offscreen pytest -q tests/test_gui_lobe_control.py` → **8 passed**.
- `tests/test_listening_profiles.py` (21) + `tests/test_gui_listening_profiles.py` + `tests/test_gui_operator.py`
  + `tests/test_gui_room_profile.py` + `tests/test_gui_stage_strip.py` → **78 passed** (narrow set).
- `pytest -q --ignore-glob='tests/test_gui_*.py' -p no:cacheprovider` → **1093 passed in 42 s**, exit 0.
- `mypy` → **Success, no issues in 74 files**.
- CI-only (MainWindow/`win` fixture hangs headless): `test_gui_{live_seat,smoke,coverage,twokit,calibrate_front}`
  — unaffected (Lobe Control is additive; no existing widget/cfg assertions touched).

## RESULTS
All green; every requested test (1–20) covered and passing. No regressions — the +17 vs the previous 1076
are the new lobe + listening tests; the only behavioural change is the new, default-inert LIVE card.

## DISCOVERY FINDINGS (see `phase11_lobe_control_discovery.md`)
Beam direction = **array-relative degrees** via `set_steering(azimuth_deg)` (live, atomic `_W` publish +
`_steer_gen` epoch; debounce in the UI). Nulls via `set_nulls`/`compose_nulls` — **already capped at 2**.
Beam **width is not continuous**: only mode (`delaysum`/`superdirective`/`mvdr`) + the existing `live_robust`
loading slider — so width presets map honestly to (mode + loading), a mode change applies at Connect.
Seat→angle via `cp.seat_azimuth_for_array` / `azimuth_for_array_point` (missing seat ⇒ None). The Canvas
draws wedges/arrows/seat-dots but is MainWindow-only → a self-contained `LobePreview` is used. The live
panel does **not** hold placement status → the warning takes it as an explicit input.

## LOBE MODEL
`LobeControl(version, enabled, mode∈{fixed,follow,seat,table}, main_angle_deg∈[−180,180], beam_width∈
{wide,medium,narrow}, beam_mode, target_seat_id, auto_steer, nulls:[LobeNull(angle_deg,enabled,label)],
safety{requires_calibration_recommended, max_nulls=2, warn_if_placement_bad})`. `validate()` rejects bad
mode/width, out-of-range angles, and >maxNulls enabled nulls; `clamp_angle` wraps to (−180,180];
`effective_nulls()` caps to maxNulls; `summary()` + `warnings(calibration_on, placement_status)` are pure;
camelCase JSON round-trips. `default_lobe_for_mode(mode)` gives a safe per-mode default.

## GUI INTEGRATION
LIVE "Lobe control" card: **Listen toward** (seat combo + manual angle dial −180..180°), **Pickup focus**
(Wide/Medium/Narrow + the honest "not a continuous beamwidth" note), **Suppress direction** (Off/Angle/Seat,
≤2, "reduces not mutes" warning), a one-line **summary**, a **warnings** line, and the **LobePreview**
schematic. Sliders/combos are **debounced** (150 ms) before `set_steering`/`set_nulls`; the summary/preview
update instantly (cheap). It adds **no 7th mode** and touches no existing control.

## LISTENING PROFILE INTEGRATION
`LpSpatial.beam_width` describes each mode's lobe focus (Whole table = **wide**, never narrow; Follow/Seat/
Clean = medium; Two-kits = wide). Manual stays neutral (medium, no override — the operator's controls win).
The lobe mode in the GUI derives from the listening dropdown (`_lobe_mode_from_listening`), so the two stay
consistent without forcing changes.

## CALIBRATION/PLACEMENT WARNINGS
- Calibration OFF (`_calibration_path is None`) ⇒ summary shows `calibration OFF` + warning *"Calibration
  is OFF — lobe direction may be less accurate."* (does not block).
- Placement BAD (`set_lobe_placement_status("BAD")`) ⇒ `placement BAD warning` + *"Placement is BAD —
  lobe/null control may underperform until physical noise is fixed."* (does not block).

## SAFETY GUARANTEES
Audio thread never blocked (atomic `set_steering`/`set_nulls`, debounced UI, no per-tick geometry recompute);
angles clamped to [−180,180]; nulls bounded to 2; no DFN3/dereverb/AEC/voice-gate auto-enable; no engine/
library/CLI/calibration default change; no auto-applied placement notches; no removed controls; no new
listening mode; no push/merge.

## MANUAL GUI CHECK STEPS
1. `./.venv/Scripts/python.exe run_gui.py` → LIVE.
2. Expand **"Lobe control"**; confirm the summary reads `Lobe: … · calibration OFF` and the preview draws.
3. Listening mode **Manual** → "Listen toward" = Manual angle → set 35° → summary `fixed 35°`, preview wedge rotates.
4. **Pickup focus** Narrow → summary `width narrow`, preview wedge narrows; note the "not a continuous beamwidth" caption.
5. **Suppress direction** Angle, 180° → summary `null 180°`, a dashed null line appears + the "reduces, not mutes" warning.
6. Apply a calibration profile (Hardware card) → the lobe summary flips to `calibration ON`.
7. Switch to **Whole table** → lobe shows `whole table`, focus stays non-narrow; **Follow the room** → `follow (auto-steer)`.
8. Operator diagnostics + Audio room profiles still open; no cleanup (DFN3/dereverb/AEC/gate) got enabled.

## KNOWN LIMITATIONS
- The schematic preview is **not** a measured beam pattern (labelled "preview").
- Width is mode+loading, not a continuous physical beamwidth; a focus mode-change applies at Connect.
- Live `set_steering`/`set_nulls` are pushed to the **A/B (steered) engine**; the zone/auto-steer paths
  follow their own logic, so manual lobe direction is most direct there. The A/B card's own seat-lock
  remains as the advanced control (last-write-wins is safe via the `_steer_gen` epoch).
- Placement status is an explicit input (`set_lobe_placement_status`); no automatic placement feed yet.

## REGRESSION RISKS
Low. The model is pure stdlib; the GUI card is additive and default-inert; `_on_listening_mode_changed`
gained one cheap guarded call. The only shared-file touch is `common.py` (added `LobePreview` + one import).
CI must confirm the 4 MainWindow GUI files (unaffected by inspection).

## NEXT STEP
Awaiting review. **Not committed, not pushed, not merged.** On approval: commit on the existing branch.
