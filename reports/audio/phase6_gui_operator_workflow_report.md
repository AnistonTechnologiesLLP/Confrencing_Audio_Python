# Phase 6 — GUI / Operator Workflow Report

**Goal:** surface the Phase 1–5 production features to a non-DSP operator WITHOUT changing default
behaviour — a clear status surface (Device / Calibration / Placement / Pipeline / Output / Transcription
/ Diagnostics), honest OFF/failed/uncertain states, and no auto-applied suggestions.

Status: **COMPLETE.** 18 headless model tests + 3 GUI panel-probe tests green; full non-GUI suite
1016 → **1034 passed**; mypy clean; CLI verified end-to-end.

---

## 1. What already existed and was not rebuilt
- **Live GUI** (`conf_pipeline_gui/`, PySide6): `panels/live.py` (rich live controls), `StageStrip`
  (per-stage activity read-out), the ModeBar workflow shell. **`commissioning_report` +
  `CommissioningInfo`** (`conf_pipeline/report.py`) — the existing integrator diagnostics. All reused /
  untouched; the operator model complements them with the Phase 1–5 status they don't carry.
- The single-panel offscreen **probe pattern** (`test_gui_stage_strip`) — reused to test the new panel.
- All Phase 1–5 objects — read, not rebuilt.

## 2. What was actually missing
A unified operator-facing surface for the new features: calibration status, a placement read-out, the
**full pipeline order with on/off**, egress status, transcription status, and a diagnostics export. The
live panel exposes many *controls* but no consolidated *status/diagnostics* view of Phases 1–5.

## 3. Files changed
New:
- `conf_pipeline_control/operator.py` — `OperatorStatus` (headless 7-section model + JSON/Markdown export).
- `conf_pipeline_gui/panels/operator.py` — `OperatorStatusPanel` (read-only QWidget rendering the model).
- `scripts/operator_diagnostics.py` — operator CLI (print + export).
- `tests/test_operator_workflow.py` (18, headless) + `tests/test_gui_operator.py` (3, panel probe).
- `docs/AUDIO_OPERATOR_WORKFLOW_GUIDE.md`, `reports/audio/phase6_gui_operator_workflow_report.md`.

Edited (minimal):
- `polaris_beamformer.py` + `live.py` — `active_cleaning_stages()` now lists **"HPF/notch"** when pre-NR
  is on (the explicit Phase-2 follow-up; status string only, not audio; `in`-checked tests stay green).
- `conf_pipeline_control/__init__.py` — export `OperatorStatus`.
- `docs/AUDIO_FRONTEND_PHASE_TRACKER.md`, `docs/AUDIO_FRONTEND_PRODUCTION_GAPS.md`.

**No DSP rebuilt; no default changed.** The model only READS engine flags.

## 4. Operator workflow design
A headless **`OperatorStatus`** model is the single source; a CLI prints/exports it and a thin GUI panel
renders it. This honors the documented MainWindow-hangs-headless constraint (logic in the tested model;
the panel is a single-widget offscreen probe; full GUI in CI). Seven sections, each a plain dict
(`device_section()` … `transcription_section()` + `warnings()`), with `to_dict()` / `to_markdown()` /
`save(out_dir, stamp)`.

## 5. Calibration UI/status behaviour
`calibration_section()` reads `engine._calib`: enabled/disabled, profile device/rate/channels/ref/
createdAt, gain range, polarity-inversion count, max delay, added latency. **A profile that failed to
load is surfaced** (`"Calibration OFF — profile failed to load"` + a warning) — never hidden. Default is
OFF; low-confidence channels are carried when an estimate is provided.

## 6. Placement UI/status behaviour
`placement_section()` maps a `PlacementResult` → GOOD/ACCEPTABLE/BAD + score + reasons + recommendations
+ detected tones + `suggestedPreNrBands` (HPF + notches) + imbalance/clipping/hotspot. **`autoApplied`
is always `False`** and the note says "re-measure per room". A BAD/ACCEPTABLE placement becomes a
warning. Verified that building the status does NOT enable pre-NR on the engine.

## 7. Pipeline order / status behaviour
`pipeline_section()` returns the canonical 12-stage order (preamp → calibration → beam → AEC → transient
→ dereverb → **pre-NR HPF/notch** → post-NR → PEQ → AGC/limiter → band-limit → voice-gate) with a real
`active` flag per stage (read from engine flags) + an `activeCleaningStages` summary + a "default-OFF,
never forced on" note. A stage is `[on]` only if truly configured on. `active_cleaning_stages()` now also
surfaces `HPF/notch`.

## 8. Output / egress status behaviour
`egress_section()` reads an `EgressRouter`: the 48 kHz + 16 kHz routes, WAV/external-sink presence,
frames pushed, pending buffer, algorithmic latency, and the note "only processed clean mono is routed;
raw 8-channel input is rejected; virtual mic is an external seam, no driver bundled". The raw-8ch
rejection is re-verified in test.

## 9. Transcription stream status behaviour
`transcription_section()` reads a `TranscriptionStream`: provider name + `isMock`, session id/status/
chunks/duration, VAD config, and the note "mock/dev — no real ASR vendor, no network by default". A
started mock session reports `running`; after stop, `stopped`; `network_calls == 0`.

## 10. Diagnostics export behaviour
`save(out_dir, stamp)` writes `operator_diagnostics_<stamp>.json` + `.md` containing all seven sections
+ warnings. Verified the JSON has every section and the Markdown has the section headings. The CLI
stamps with the wall clock; the model takes the stamp as input (deterministic for tests).

## 11. GUI tests or model tests run
- **Headless model:** `tests/test_operator_workflow.py` → 18 passed (in the default suite).
- **GUI panel probe:** `tests/test_gui_operator.py` → 3 passed offscreen (single-panel construct, NOT
  MainWindow — runs locally in 0.7 s and in CI). The MainWindow-integration tests remain CI-only per the
  documented local hang; nothing is faked.

## 12. Default pipeline impact
**NONE.** The model only reads flags. The single engine edit is the human-readable
`active_cleaning_stages()` string (status, not audio). No default changed, nothing forced on, no
suggestion auto-applied. Verified by `test_building_status_does_not_change_engine_defaults` + the full
suite staying green.

## 13. Known limitations
- The operator surface is read-only status + a CLI/panel; it does not add live *control* wiring into the
  live panel (toggling calibration/pre-NR from the new panel) — that is a deliberate, larger GUI change
  left out to honor "wire the GUI lightly / don't rewrite the GUI architecture".
- Full MainWindow GUI behaviour is CI-only locally (documented hang); the panel is verified via the
  offscreen single-widget probe.
- Latency shown is the engine's *estimate* (it requires the engine to be set up; the CLI calls
  `_setup_runtime` first; on a bare model it reads as available-when-running).
- Calibration low-confidence channels surface only when an estimate object is supplied alongside the
  profile (the profile itself doesn't carry confidence).

## 14. Safe next phase
Phase 7 — Final verification, docs, and deployment guide: an end-to-end pass over all phases
(calibration → pipeline order → placement → egress → transcription), the deployment guide, and the final
verification report. Phases 1–6 are all standalone, default-off / no-DSP-change additions, so Phase 7 is
verification + documentation over a green, unchanged pipeline.
