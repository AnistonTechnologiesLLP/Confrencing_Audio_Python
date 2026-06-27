# Phase 10 — Listening Profiles Discovery (source-cited)

Read-only mapping of the LIVE "Listening mode" dropdown before any code. Goal: model each mode as a
**descriptive** processing profile with an honest flow summary, **without changing apply behaviour**.

## 1. The dropdown (`conf_pipeline_gui/panels/live.py:182–200`)
`self.live_listening_mode` (QComboBox), label "Listening mode", in `lm_row` added to the panel layout
at `:200`. Items (label → data key), in order:
| label | data |
|---|---|
| Follow the room (auto-steer) | `follow` |
| Lock to a seat | `seat` |
| Whole table | `table` ← **default** (`setCurrentIndex(2)`, `:195`) |
| Clean audio (hands-off) | `clean` |
| Manual (advanced) | `manual` |
| Two kits (combined room) | `twokit` |
Signal: `currentIndexChanged` → `_on_listening_mode_changed()` (`:196–197`, gated off `self._refreshing`).

## 2–9. Per-mode behaviour (the honest flag map)
**Apply timing (`:971–1006`, `:1461–1478`):** `_on_listening_mode_changed` is a **pre-Connect facade** —
it ticks the underlying mode checkbox + collapses irrelevant cards, and **returns early when
`_live_busy()`** (modes are fixed at **Connect**, not on dropdown change). So selecting a mode does NOT
reconnect; the real DSP config is built at Connect.

| Mode | Engine | Cleaners ON by default | Notes |
|---|---|---|---|
| **Whole table** (`table`) | `LiveBeamController` (zones) `:1484` | none (AGC off-by-default, dereverb off) | minimal: zone beams + optional AGC/dereverb |
| **Follow the room** (`follow`) | `AutoSteerController` `:1582` | none (post_nr off by default) | DOA auto-steer ON |
| **Lock to a seat** (`seat`) | `BeamEngine` steered `:1678` via `_beameng_steered_cfg` `:1633` | none by default (A/B-engine checkboxes) | superdirective; seat-null + lock; if seat has no array bearing → warns + follows talker (`:1776`) |
| **Clean audio** (`clean`) | `AutoSteerController` `:1582` + **forces `live_autosteer_clean`→OM-LSA** `:992` | **post_nr = OM-LSA (Medium) ONLY** `:1594–1597` | dereverb/AGC/transient/voice-gate/AEC all OFF |
| **Manual** (`manual`) | user's checkbox choice `:1003` | user-controlled (every card shown) | profile must NOT override toggles |
| **Two kits** (`twokit`) | `MultiKitController` `:2014` | per-kit cleaner off by default `:1955`; AGC off-by-default | talker-select automix + cross-fade (no beam/null) |

> **CORRECTION (post-discovery, 2026-06-27):** the line below was WRONG. `live_agc` ships **ON**
> (`live.py:280` `setChecked(True)`, asserted by `test_gui_live_seat.py`), so AGC (`agc_target_db=-20.0`)
> is applied at Connect in every mode. The discovery confused the *engine* default (`PolarisBeamformer`
> agc off) with the *GUI checkbox* default (on). The profile model + this section were corrected; and the
> user then asked to **pre-tick the rest of the recommended cleanup** (dereverb + OM-LSA + taps) as GUI
> defaults too. See `reports/audio/phase10_listening_profiles_report.md`.

**Key facts that constrain the profiles (so the summary doesn't lie):**
- ~~**AGC is OFF by default in EVERY mode**~~ → **AGC is ON by default** (`live_agc` pre-ticked); see the
  correction banner above.
- **"Clean audio" enables the OM-LSA denoiser** (and, post-addendum, the rest of the recommended chain is
  pre-ticked GUI-side: dereverb + taps + AGC) — but never silently DFN3.
- The **A/B-engine cleaner checkboxes** (`live_beameng_postnr` → `post_nr`, `live_beameng_nr_engine` →
  `post_nr_engine`, `..._dereverb`/`..._transient`/`..._voicegate`/`..._aec`, `..._adaptnull` →
  `mode=MVDR/auto_null`) are read by `_beameng_steered_cfg` (`:1633–1663`). These are how "Manual" gets
  its flags.

## 10–11. Engine construction / flag mapping
Modes map to: `LiveBeamController(agc_target_db, dereverb, …)` (table), `AutoSteerController(post_nr,
post_nr_engine, dereverb, agc_target_db, …)` (follow/clean), `BeamEngine(steered_cfg=…)` (seat),
`MultiKitController(specs=[KitSpec(cfg={post_nr,…})])` (twokit). `agc_target_db = -20.0 if live_agc else None`
in every path. Cleaner flags come from the per-mode checkboxes/combos, all default-off.

## 12. pre-NR / calibration / AGC / dereverb / post-NR passing
`post_nr`/`post_nr_engine`/`post_nr_amount`, `dereverb`, `aec`, `transient_suppress`, `voice_gate`,
`agc_target_db`, `pre_nr`/`pre_nr_bands`, `calibration`/`calibration_path` are all `PolarisBeamformer`/
`LiveBeamController` ctor params (Phases 1–2). The GUI passes them at Connect; pre-NR + calibration have
**no GUI toggle** yet (CLI/profile only) → they read OFF in the live summary.

## 13. GUI test pattern
`tests/test_gui_stage_strip.py:73–78`: `LivePanel(AppState())` constructed offscreen
(`QT_QPA_PLATFORM=offscreen`), poke `live_listening_mode.setCurrentIndex(i)` / `.setCurrentText(...)`,
read widget text. Never MainWindow (hangs headless).

## 14. Phase 8/9 wiring (do-not-disturb)
`app.py` menu actions `_show_operator_diagnostics` (`:229`) + `_show_room_profiles` (`:231`) open
`OperatorDiagnosticsWindow` / `AudioRoomProfilesWindow` — independent windows; Phase 10 will not touch
them.

## Design implication
Phase 10 = a **descriptive** layer: a `ListeningProfile` model with built-ins matching the 6 modes
(honest flags — AGC off-by-default everywhere, clean=OM-LSA-only) + a read-only **flow summary** label
under the dropdown that updates on selection. **No change to apply/Connect behaviour.** Manual mode's
summary is built from the live toggles. Optional: a backwards-compatible
`preferredListeningProfileId` on `AudioRoomProfile` (stored, never auto-applied).
