# AUDIO FRONT-END PHASE TRACKER

> Control file for the phased POLARIS 8-MEMS audio front-end production hardening.
> Read this at the **start of every phase**; update it at the **end of every phase**.
> One source of truth for "where are we and what is safe to do next".

---

Current Phase: **11 — Lobe Control** (operator controls for the beamformer pickup pattern; surfaces/reuses
existing steering + nulls, no rewrites).

Phase 11 plan (after discovery — see `reports/audio/phase11_lobe_control_discovery.md`): Lobe Control is
NOT calibration — it shapes the beam *after* capsule alignment. Discovery confirmed: direction =
**array-relative degrees** via `set_steering(azimuth_deg)` (LIVE, atomic `_W` publish + `_steer_gen`
epoch); nulls via `set_nulls`/`compose_nulls` (**already capped at 2**); beam **width is NOT continuous** —
map Wide/Medium/Narrow honestly to (beam_mode + the existing `live_robust` loading slider), mode-crossing
changes apply at Connect; seat→angle via `cp.seat_azimuth_for_array` / `azimuth_for_array_point` (missing
seat ⇒ None); existing seat-lock/manual-angle/click-to-aim + nullseats already drive `set_steering`/
`set_nulls`; placement status is NOT held by the live panel (warning takes it as an explicit input);
calibration-on = `_calibration_path is not None`. Build:
- NEW `conf_pipeline_control/lobe_control.py` — `LobeControl` (+ `LobeNull`): enabled/mode/`main_angle_deg`
  [−180,180]/`beam_width`{wide,medium,narrow}/`beam_mode`/`target_seat_id`/`auto_steer`/`nulls`(≤2)/safety;
  `validate()` + `summary()` + `warnings(calibration_on, placement_status)` + camelCase JSON +
  `default_lobe_for_mode(mode)`. Pure stdlib, mypy-clean.
- EDIT `conf_pipeline_control/listening_profile.py` — add `beam_width` to `LpSpatial` (default "medium");
  built-ins per mode (table=wide, follow/seat/clean=medium). Manual = user toggles (no override); Whole
  table never narrow; Lock-to-seat tolerates missing seat.
- EDIT `conf_pipeline_gui/panels/live.py` — compact "Lobe Control" Card: manual-angle dial + seat combo
  (debounced → `set_steering`), Wide/Medium/Narrow focus preset (honest note, loading live / mode at
  Connect), suppress-direction off/angle/seat (≤2, "reduces not mutes" warning), mode read-out, summary
  label, minimal `LobePreview` widget, calibration-OFF + placement-BAD warnings. No 7th mode; existing
  controls untouched. Honest labels only (pickup focus / suppress / reduced-pickup — never soundproof).

### Phase 11 default impact: NONE until the operator touches it — `LobeControl` default is a safe,
disabled-ish whole-table lobe; nothing auto-applies; direction/nulls reuse existing live setters; no
DSP/engine/CLI/calibration default change. Tests: `tests/test_lobe_control.py` (model 1–13) +
`tests/test_gui_lobe_control.py` (GUI 14–17, offscreen) + existing room/operator/listening green (18–20).

### Phase 11 out of scope: no perfect audio fencing/"soundproof" claims, no continuous beamwidth DSP, no
new beam math, no removed controls, no auto-apply, no engine/calibration default change, no push/merge.

### Phase 11 OUTCOME (DONE locally; NOT committed):
- **Model** `conf_pipeline_control/lobe_control.py` — `LobeControl`/`LobeNull`/`LobeSafety`: validate
  (mode/width/angle[-180,180]/≤maxNulls), `clamp_angle`, `effective_nulls`, `summary`, `warnings`,
  camelCase JSON, `default_lobe_for_mode`, `loading_for_width`. `tests/test_lobe_control.py` (14). Exported.
- **Listening profiles** — `LpSpatial.beam_width` (default medium; table/twokit=wide, never narrow;
  manual neutral), camelCase `beamWidth` round-trips; +3 tests.
- **GUI** — `conf_pipeline_gui/panels/common.py` `LobePreview` (schematic, pragma-no-cover paint) +
  live.py "Lobe control" Card (direction/focus/suppress/summary/warnings/preview), debounced
  `set_steering`/`set_nulls` apply, `_current_lobe_control`/`_update_lobe_summary`/`set_lobe_placement_status`;
  lobe refresh wired into `_on_listening_mode_changed`. `tests/test_gui_lobe_control.py` (8, offscreen).
- **Reuse-not-rewrite:** no 7th listening mode; existing seat-lock/auto-steer/nullseats/"Calibrate front"
  untouched. Honest labels (reduces-not-mutes). Calibration-OFF + placement-BAD = warnings, never blocks.
- **Verification:** non-GUI suite **1093 passed** (+17), narrow GUI+model **78 passed**, mypy clean (74).
  Docs: `LOBE_CONTROL_GUIDE.md` + `phase11_lobe_control_{discovery,report}.md` + listening/operator guides.

---

### [Phase 10 status]

Current Phase: **10 — Listening Processing Profiles** (descriptive layer; keeps Phase 9 Room Profiles as-is).

Phase 10 plan (after discovery — see `reports/audio/phase10_listening_profiles_discovery.md`): the LIVE
listening-mode dropdown is a **pre-Connect facade** (DSP fixed at Connect, AGC off-by-default in every
mode, "Clean audio" = OM-LSA denoiser ONLY). So Phase 10 is **descriptive**, not a new apply path:
- NEW `conf_pipeline_control/listening_profile.py` — `ListeningProfile` (nested spatial/calibration/
  cleanup{preNr,postNr,peq,agc,voiceGate}/output/safety, camelCase JSON) + `BUILTIN_LISTENING_PROFILES`
  for the 6 modes (`table`/`follow`/`seat`/`clean`/`manual`/`twokit`) with **honest flags** +
  `flow_summary()` + `warnings()` + `listening_profile_for_mode(mode, manual_flags=…)`. mypy-clean.
- EDIT `conf_pipeline_gui/panels/live.py` — add a read-only **flow-summary `QLabel`** under the dropdown;
  `_update_listening_flow_summary()` called at init + at the top of `_on_listening_mode_changed` (before
  the busy guard, additively). Manual mode's summary reads the live toggles (`_current_manual_flags`).
  **No change to Connect/apply behaviour; existing modes untouched.**
- EDIT `conf_pipeline_control/room_profile.py` — add backwards-compatible `preferred_listening_profile_id`
  (stored, NEVER auto-applied). Export the model; docs + report + guide.

### Phase 10 OUTCOME (DONE locally; NOT committed/pushed):
- **Cycle 1 — model:** `conf_pipeline_control/listening_profile.py` + `tests/test_listening_profiles.py`
  (now 17) + backwards-compat `preferred_listening_profile_id` on `room_profile.py` (+1 test → 18). mypy clean.
- **Cycle 2 — GUI flow summary:** read-only flow-summary `QLabel` under the LIVE dropdown
  (`_update_listening_flow_summary` / `_current_manual_flags`), `tests/test_gui_listening_profiles.py` (10).
- **ADDENDUM (user-requested, this session): enable the recommended settings that are off by default.**
  User chose **"pre-tick recommended toggles in LIVE"** (GUI-default change only — engine/CLI/library
  defaults stay OFF; byte-identical engine tests untouched) + **"follow my recommended flow"** = the 4
  stages AGC + dereverb + OM-LSA denoise + tap-suppression (AEC/voice-gate stay opt-in).
  - GUI defaults flipped in `live.py`: `live_beameng_postnr`+`live_beameng_transient` ON;
    `live_autosteer_clean`→OM-LSA + `live_autosteer_transient` ON; `live_twokit_clean`→OM-LSA +
    `live_twokit_agc` ON. (`live_agc` was already ON.)
  - **Discovery correction:** the Phase-10 model wrongly claimed "AGC off in every mode"; in fact
    `live_agc` ships ON (live.py:280, asserted by `test_gui_live_seat.py`). Built-ins corrected.
- **DEREVERB POLICY REVISION (user follow-up, this session): dereverb is NOT global.** User confirmed
  "do NOT leave dereverb ON globally." Final policy: dereverb recommended ON only for **Follow + Clean**;
  OFF for Whole table / Lock-to-seat / Manual / Two kits.
  - `live.py`: global `live_dereverb` default reverted to **OFF**; `_on_listening_mode_changed` now sets
    the **auto-steer path's own** `live_autosteer_dereverb` ON for `follow`/`clean` only (per-profile apply,
    never the global switch; Manual untouched → user's toggle is source of truth).
  - Model built-ins: `_recommended_cleanup(dereverb=…)` default → **False**; only `follow`/`clean` pass
    `dereverb=True` (→ flow shows "dereverb ON" + naturalness warning). table/seat/manual/twokit = off.
  - Tests: model `test_dereverb_is_restricted_to_steering_modes`; GUI `test_dereverb_is_not_globally_preticked`
    + `test_follow_and_clean_enable_dereverb_on_autosteer_path` + `test_table_and_manual_do_not_force_dereverb`;
    reverted the 2 `test_gui_live_seat.py` global-dereverb assertions. OM-LSA/transient/AGC pre-ticks kept.

### Phase 10 default impact: **GUI defaults changed (intended)** — the LIVE panel now Connects with the
recommended cleanup on; **engine/CLI/library defaults unchanged** (cleaners still default OFF in code).
Local-safe suite **1177 passed** (excl. 4 MainWindow files: live_seat/smoke/coverage/twokit → CI), mypy clean.

### Out of scope (still honoured): no engine/CLI default change, no DSP/calibration/placement/DFN3 code
change, no removed/renamed modes or controls, no auto-apply, no real ASR/virtual-mic, no push/merge.
Audio Room Profile Manager kept exactly as-is.

### FOLLOW-UP — "Load calibration profile…" GUI action (this session; NOT committed):
Apply an existing per-capsule CalibrationProfile JSON to the live engine from the LIVE panel (Hardware
card). `apply_calibration_profile(path)` validates via `CalibrationProfile.load` (rejects a bad file,
no state change), stores `_calibration_path` (None ⇒ OFF, **no auto-enable on startup**), plumbs
`calibration_path=` into all three engine builds (zone `LiveBeamController` / `AutoSteerController` /
A-B `BeamEngine` steered+grid cfg), and `_live_reconnect()`s if live (the repo's "fixed at Connect"
rebuild path — no runtime calibration setter exists). `AutoSteerController` gained `calibration` /
`calibration_path` params (forwarded to its inner `LiveBeamController`; default None). app.py
`_operator_status` passes the path so diagnostics shows *Calibration: ON* + details. Calibration **math
unchanged**; **no DSP defaults changed** (calibration_path defaults None everywhere = byte-identical).
Tests: `tests/test_calibration_apply.py` (6 — off-by-default, on-after-apply, autosteer forwarding,
neutral/bad-path stay off) + `tests/test_gui_calibration_apply.py` (4 — default None, apply valid/invalid,
cfg plumbing). Non-GUI suite **1076 passed**, GUI listening/calib/operator/room/stage **39 passed**, mypy clean.

---

### [Phase 9 status archived]
Current Phase (pre-10): **9 — Audio Room Profile Manager — DONE** (profile management only; Phases 0–8 pushed/PR #31).

Outcome: NEW `conf_pipeline_control/room_profile.py` (`AudioRoomProfile` — saveable room-specific setup
doc, camelCase JSON, non-throwing `validate()`, `attach_calibration`/`copy_placement_suggestions`,
all-safe defaults) + NEW `conf_pipeline_gui/panels/room_profile.py` (`AudioRoomProfilesWindow`: New/Load/
Save/Import/Export/Validate/Copy-placement + safety note) + a MainWindow menu action "Audio room
profiles…". **Inert — never applies to the engine, never forces a feature on, never auto-applies
suggestions, no network, no driver.** 17 model + 7 window tests green; full non-GUI suite **1051 passed**
(zero regressions); mypy clean (72); app wiring verified without building MainWindow.
**Staged/UNCOMMITTED** on `feat/audio-frontend-hardening` — awaiting an explicit commit instruction. See
`reports/audio/phase9_audio_room_profiles_report.md`, `docs/AUDIO_ROOM_PROFILE_GUIDE.md`.

Phase 9 plan (written before coding): a GUI-managed room-profile system that saves/loads/validates/
imports/exports room-specific audio setup — **never auto-applies to the live engine**.
- NEW `conf_pipeline_control/room_profile.py` — mutable `AudioRoomProfile` (editable draft) with nested
  sections `calibration` / `placement` / `preNrCleanup` / `egress` / `transcription` / `safety` (the
  user's camelCase JSON shape). `to_dict`/`from_dict`/`to_json`/`from_json`/`load`/`save`,
  `RoomProfileError` (malformed JSON only), and **non-throwing `validate(...) -> list[str]`** (warns on
  version/device/rate/channel mismatch, missing referenced files, any True safety flag, auto-apply set).
  Helpers: `attach_calibration(path)`, `copy_placement_suggestions(result)` — fill the placement +
  pre-NR DRAFT from a `PlacementResult` but **never enable pre-NR or set any auto-apply flag**. Safety
  flags default all-False. (mypy-checked — keep type-clean.)
- NEW `conf_pipeline_gui/panels/room_profile.py` — `AudioRoomProfilesWindow(QWidget)`: New / Load… /
  Save… / Import… / Export… / Validate + profile-name field + read-only summary + warnings + the
  required "not applied automatically / room-specific / re-measure per room" safety note + an optional
  "Copy placement suggestions" (draft-only). Path-taking methods (`load_path`/`save_path`/…) are
  offscreen-testable; the buttons are thin `QFileDialog` wrappers.
- NEW MainWindow menu action "Audio room profiles…" → opens the window (mirrors the Phase-8
  "Audio operator diagnostics…" wiring). MainWindow itself not testable headless (hangs) → inspection + CI.
- Export model + the new window from package roots; docs + report.

### Phase 9 default impact: NONE — profiles are an inert, persisted document; nothing touches the DSP
engine, no default changes, no suggestion auto-applied, no network, no driver. Tests:
`tests/test_audio_room_profile.py` (model, in the suite) + `tests/test_gui_room_profile.py` (window probe).

### Out of scope (phase-locked): no DSP change, no force-on, no auto-apply, no apply-to-engine controls,
no real ASR vendor, no virtual-mic driver, no rebuild of Phase 1–8 code, no push/merge.

---

### [Phase 8 status archived]
Current Phase (pre-9): **8 — Wire OperatorStatusPanel into the main app — DONE** (wiring only; Phases 0–7 complete).

Outcome: added a read-only **"Audio operator diagnostics…"** app-menu action → opens an
`OperatorDiagnosticsWindow` (panel + Refresh + Export) built from `LivePanel.active_engine()`. 8 operator
GUI tests green offscreen; full non-GUI suite **1034 passed** (unchanged); mypy clean; `app.py` wiring
verified without constructing MainWindow. No DSP/default change; all edits in `conf_pipeline_gui`.
**Staged/UNCOMMITTED** on `feat/audio-frontend-hardening` (PR #31 open) — awaiting an explicit commit
instruction. See `reports/audio/phase8_gui_integration_report.md`.

Phase 8 plan (written before coding): the existing read-only `OperatorStatusPanel` is not mounted in the
running app. Lowest-risk wiring = **Option B (menu action opens a window)**, mirroring the existing
"Export commissioning report…" action. Add: (1) `LivePanel.active_engine()` — a public read-only
accessor returning the running flag-bearing engine (`_ab_target()` unwrapped: BeamEngine→`_steered`,
AutoSteer→`ctrl`, else the LiveBeamController) or None; (2) `OperatorDiagnosticsWindow(QWidget)` in
`panels/operator.py` = `OperatorStatusPanel` + Refresh + Export(→`OperatorStatus.save`) + a
`status_provider`; (3) a MainWindow menu action "Audio operator diagnostics…" → builds
`OperatorStatus.build(engine=active_engine())` and shows the window. **Read-only; no DSP control, no
default change, no auto-apply.** Testable offscreen via the single-panel pattern (LivePanel + the new
window); the MainWindow menu action is verified by inspection + CI (MainWindow hangs headless here).
mypy scope (conf_pipeline + conf_pipeline_control) is unaffected — all edits are in `conf_pipeline_gui`.

(Phases 0–7 plans + outcomes retained below.)

---

### [Phases 0–7 status archived below — all DONE]
Current Phase (pre-8): **7 — Final verification, docs, deployment guide — DONE. ALL PHASES (0–7) COMPLETE.**

Outcome: end-to-end verification green — 7 phase test files + GUI probe = **137 passed**; full non-GUI
suite **1034 passed** (900 baseline + 134 new, zero regressions); **mypy clean (71 files)**; 3 CLIs
`--help` OK; e2e demo (processed mono → egress → ASR → mock) = 1 chunk, **0 network calls**; raw-8ch
rejected at egress AND transcription. No code changed in Phase 7 (verification found no bug); no commit
performed (awaiting explicit instruction). Deliverables: `docs/AUDIO_FRONTEND_DEPLOYMENT_GUIDE.md` +
`reports/audio/final_verification_report.md`. **No default behaviour changed across all phases — byte-
identical when every opt-in feature is off.**

(Phase plans + outcomes retained below.)

Current Goal: ✅ Surfaced Phases 1–5 to a non-DSP operator WITHOUT changing defaults: a headless
**`OperatorStatus`** model (7 sections) + `OperatorStatusPanel` (read-only QWidget) + `operator_diagnostics.py`
CLI/export + pre-NR surfaced in `active_cleaning_stages()`. Honest OFF/failed/uncertain states; suggestions
never auto-applied; no DSP/default change.

Files inspected:
- `conf_pipeline_control/polaris_beamformer.py` — `process_block` stage chain (L1812–1880), preamp, band-limit, dead-capsule mask (`_resolve_active_mask` L1061), DOA loop, null composition (`compose_nulls` L555–599)
- `conf_pipeline_control/live.py` — `LiveBeamController`, `_process_block` stage chain (L553–647), capture `_open` (L487–505), per-bin LCMV/MVDR (`_bin_weights` L260–289), WAV record + monitor out
- `conf_pipeline_control/doa.py` — SRP-PHAT DOA (`detect`, `srp_phat_map`)
- `conf_pipeline_control/beamformer.py` — beam-mode design, `lcmv_weights`, mode constants
- `conf_pipeline_control/streaming_aec.py` — `StreamingAec` (partitioned-block NLMS)
- `conf_pipeline_control/streaming_cleaner.py` — `StreamingDereverb`, `StreamingCleaner` (OM-LSA / Wiener)
- `conf_pipeline_control/deepfilter_cleaner.py` — `StreamingDeepFilter` (DFN3 ONNX)
- `conf_pipeline_control/peq.py` — `StreamingPeq` (RBJ biquads; has `highpass`/`lowpass`/`bell`)
- `conf_pipeline_control/agc.py` — `TargetLoudnessAgc`, `_apply_zone_gain`
- `conf_pipeline_control/transient.py`, `voice_gate.py`, `preamp.py`, `geometry.py`, `autosteer.py`, `reference_capture.py`, `ab_capture.py`, `ab_test.py`, `multibeam.py`/`multikit.py`/`multiroom.py`
- `conf_pipeline/model.py` (CONFIG_VERSION=5, `DspBlock`, `MicrophoneArray.bearing_deg`), `conf_pipeline/report.py` (`commissioning_report`)
- `conf_pipeline_gui/panels/live.py` — operator controls inventory
- `tests/` (87 files), `scripts/` (9 tools)

Files changed: *(none — Phase 0 is documentation only)*
- `docs/AUDIO_FRONTEND_PHASE_TRACKER.md` (this file, new)
- `docs/AUDIO_FRONTEND_PRODUCTION_GAPS.md` (new)
- `reports/audio/phase0_discovery_report.md` (new)

Commands run:
- `QT_QPA_PLATFORM=offscreen PYTHONIOENCODING=utf-8 ./.venv/Scripts/python.exe -m pytest -q --ignore-glob='tests/test_gui_*.py' -p no:cacheprovider`
  (baseline; GUI/MainWindow tests excluded — they hang on this Windows box per repo CLAUDE.md, CI-verified instead)

Test status: **GREEN.** Full non-GUI suite **1034 passed** (900 baseline + 29 calibration + 20 pre-NR + 25 placement + 20 egress + 22 transcription + 18 operator; no regressions); **mypy clean** (71 files). Plus `tests/test_gui_operator.py` (3, offscreen panel probe) green explicitly. GUI/MainWindow tests excluded locally (hang on this box; CI-verified).

Open risks:
- Two parallel DSP chains (`PolarisBeamformer.process_block` and `LiveBeamController._process_block`) — **any stage change must land in BOTH** or paths diverge.
- "Bit-exact pass-through when off" invariant: every optional stage returns the *same* array object when disabled, which keeps the suite byte-identical. New stages must honor this.
- Real-time callback safety: no locks across heavy DSP, atomic rebind of shared trackers.
- Schema/TS-parity: any new persisted config field must be mirrored in the TS sibling (`c:\Work\conferencing-audio-pipeline`) and migrated (CONFIG_VERSION).
- Engine sample rate is configurable; POLARIS native = 44100 Hz, DFN3 runs internally at 48 kHz via resamplers. Do not hard-code one rate.

Next step: **ALL PHASES COMPLETE.** Awaiting an explicit instruction to commit (recommended message in
`reports/audio/final_verification_report.md` §20). No further phase work; optional follow-ups are listed
in the final verification report §19.

---

## Phase status at a glance

| Phase | Title | Status | Gap reality (from Phase 0) |
|------|-------|--------|----------------------------|
| 0 | Discovery + baseline | **DONE** | — |
| 1 | Per-capsule calibration | **DONE** | gap filled: `calibration.py` (profile + corrector + estimator) wired into both chains; 29 tests; 929 suite green; mypy clean |
| 2 | HPF/notch before DFN3 | **DONE** | added opt-in pre-NR `StreamingPeq` (HPF+notch) between dereverb and post-NR in both chains; 20 tests; 949 suite green; mypy clean; 0 latency |
| 3 | Auto live placement check | **DONE** | added `placement.py` (analyze/score/survey) + `check_placement.py` CLI; 25 tests; 974 suite green; mypy clean; no pipeline change |
| 4 | Clean mono output / egress | **DONE** | added `egress.py` `EgressRouter` (48k PCM / 16k ASR int16 / WAV / virtual-mic hook); 20 tests; 994 suite green; mypy clean; no DSP change |
| 5 | Transcription-ready stream | **DONE** | added `transcription.py` (provider Protocol + VAD chunker + session + mock + egress pump); 22 tests; 1016 suite green; mypy clean; no DSP change |
| 6 | GUI / operator workflow | **DONE** | added `operator.py` (7-section status model) + `OperatorStatusPanel` + `operator_diagnostics.py` CLI; pre-NR in `active_cleaning_stages()`; 18+3 tests; 1034 suite green; mypy clean |
| 7 | Final verification + docs | **DONE** | e2e verified; deployment guide + final verification report written; 1034 suite green; mypy clean; safeguards proven; no bug found, no code change |

---

## Phase 1 — plan (written BEFORE coding, per spec)

### Discovery answers (the 10 required inspections)
1. **`process_block` raw 8ch in:** `polaris_beamformer.py:1820` — `block = self._apply_preamp(block)`; the *same* `block` feeds the beam (`:1823`) AND the covariance (`_accumulate_covariance(block)` `:1879`) that DOA reads.
2. **`_process_block` raw 8ch in:** `live.py:557` — `indata = self._apply_preamp(indata)`; feeds `_inbuf` → STFT `X` → covariance (`:565–571`) + beam (`:573–576`).
3. **DOA start:** both read the band covariance built from the post-preamp block. Calibrating the block *before* the covariance ⇒ DOA sees corrected capsules.
4. **Beamforming start:** polaris `self._beam.process(block)`; live per-bin `Y = Σ conj(W)·X`.
5. **Dead-capsule mask:** `_resolve_active_mask` (`:1061`) → `with_active_channels(geom, mask)`; applied at the **weight** level (beam ignores the dead capsule), NOT an input transform. Geometry carries `.active`.
6. **Uniform preamp:** `PreampHost` mixin (`preamp.py`) — `_init_preamp`/`_apply_preamp`; one scalar gain, bit-exact no-op (`return block`) when off.
7. **Front-bearing calibration:** `scripts/calibrate_front.py` — one azimuth via DOA; NOT per-capsule. Leave intact.
8. **`test_directivity_calibration.py`:** analytic beamwidth model vs measured delay-sum beam (design-side); NOT runtime alignment. Leave intact.
9. **Config/flag style:** the opt-in recipe — default-OFF key, bit-exact pass-through when off, lazy build, atomic rebind. Mirror `PreampHost` exactly.
10. **DSP test style:** `test_preamp.py` — `pytest.importorskip("numpy")`, deterministic (RNG-free) blocks, `out is x` identity for the off path, float32-preservation, math asserts with tolerances.

### Design — new module `conf_pipeline_control/calibration.py`
- **`CalibrationProfile`** (frozen dataclass; camelCase JSON `version/device/sampleRate/channels/createdAt/gainDb/delaySamples/polarity/referenceChannel/notes`): `validate()`, `is_neutral`, `from_dict`/`to_dict`/`from_json`/`to_json`/`load`/`save`, controlled `CalibrationError`. **Pure stdlib (numpy-free)** ⇒ runs in the default suite.
- **`CapsuleCalibrator`** — runtime per-block `(N,M)` transform: per-channel gain `= polarity · 10^(gainDb/20)` then integer-sample delay via a per-channel history ring. **Bit-exact no-op when neutral** (`return block`), float32-preserving (plain-float scalars / float32 gain vector — NEP-50 safe), honors `active_mask` so a dead capsule is never gained up / revived, `reset()`. numpy lazy.
- **`estimate_calibration(capture, …)`** → `CalibrationEstimate`: gainDb from per-channel RMS vs a reference channel; polarity from correlation sign; integer delay from cross-correlation; per-channel **confidence** (never fakes certainty — low-confidence flagged). numpy lazy. Testable on synthetic signals.
- **`CalibrationHost`** mixin (mirrors `PreampHost`, NO `__init__`): `_init_calibration(...)`, `_apply_calibration(block)`.

### Wiring (BOTH chains — required)
- Add `CalibrationHost` to the bases of `PolarisBeamformer` and `LiveBeamController`.
- New ctor params `calibration: CalibrationProfile|None=None`, `calibration_path: str|None=None` (default OFF; **safe fallback** to off on missing/malformed profile, sample-rate or channel-count mismatch — never crash the runtime).
- Call `_init_calibration(...)` immediately after `_init_preamp(...)`.
- Apply `block = self._apply_calibration(block)` immediately after `self._apply_preamp(block)` — `process_block:1820` and `_process_block:557`. This is **before** beam/covariance/DOA/nulls and **mathematically commutes** with the uniform preamp.
- Reset the calibrator's delay-line in polaris `reset_transient` and live `_open`.
- Export the three public names from `conf_pipeline_control/__init__.py`.

### Tests — `tests/test_calibration.py` (the 14 required + export parity + active-mask safety)
Method: TDD — write the module tests, watch them fail (absent module), implement to green; then the host-wiring tests, watch fail, wire to green; then the full non-GUI suite must stay at the Phase-0 baseline (900 passed) plus the new tests.

### Out of scope (phase-locked): no placement check, no HPF/notch reorder, no transcription, no virtual mic, no egress router, no GUI panel. Only a calibration ctor hook is added.

---

## Phase 1 — outcome (DONE)

Full report: [reports/audio/phase1_calibration_report.md](../reports/audio/phase1_calibration_report.md).
Usage: [docs/CALIBRATION_GUIDE.md](CALIBRATION_GUIDE.md).

**Files changed**
- NEW `conf_pipeline_control/calibration.py` — `CalibrationProfile` / `CapsuleCalibrator` /
  `estimate_calibration` / `CalibrationEstimate` / `CalibrationHost` / `CalibrationError`.
- NEW `tests/test_calibration.py` (29 tests), `scripts/calibrate_capsules.py`,
  `docs/CALIBRATION_GUIDE.md`, `reports/audio/phase1_calibration_report.md`.
- EDIT `polaris_beamformer.py` + `live.py` — `CalibrationHost` base, `calibration`/`calibration_path`
  ctor params, `_init_calibration` after `_init_preamp`, one apply line in the per-block chain, delay
  reset on session/re-activate. EDIT `__init__.py` — exports.

**Insertion point (both chains):** `preamp → _apply_calibration → beam + covariance/DOA → … existing chain`.
`process_block` (polaris) proven via a covariance-effect test; `_process_block` (live) is
`pragma: no cover` so verified at the shared-seam + source-parity level.

**Verification**
- `tests/test_calibration.py` → 29 passed.
- Full non-GUI suite → **929 passed** (900 baseline + 29; zero regressions; byte-identical-off held).
- `mypy` → clean (66 files).
- `scripts/calibrate_capsules.py` end-to-end on a synthetic 8-ch WAV → recovered `gainDb[1] = -6.02`,
  `polarity[3] = -1`.

**Open risks carried forward:** live `_process_block` not executed headless (repo convention); polarity/
delay low-confidence on diffuse captures (gain is the reliable output); no GUI/`BeamEngine` fan-out yet
(Phase 6). None block Phase 2.

**Default behaviour unchanged:** calibration is OFF unless a profile is supplied; the existing pipeline
is byte-identical.

---

## Phase 2 — plan (written BEFORE coding, per spec)

### Discovery answers (the 10 required inspections)
1. **post-NR/DFN3 in `process_block`:** `polaris_beamformer.py:1854` `mono = self._post_nr.process(mono, self._noise_gate)` (dereverb `:1847` before it, PEQ `:1868` after).
2. **post-NR/DFN3 in `_process_block`:** `live.py` `out = self._post_nr.process(out, False)` (dereverb before, PEQ after).
3. **Existing PEQ:** `peq.py` `StreamingPeq` — RBJ-biquad cascade (`bell`/`lowShelf`/`highShelf`/`highpass`/`lowpass`), exact IIR via scipy `sosfilt`, float64 state. `process(block[,gate])/reset()/set_bands()`. Bit-exact pass-through when no bands.
4. **PEQ reusable for pre-NR?** **YES** — it already has `highpass` (HPF) and `bell` (notch via negative gain). A second `StreamingPeq` instance = the pre-NR stage; no new filter math.
5. **HPF-capable biquad exists?** YES — `peq.py:_biquad` `"highpass"` branch (RBJ). Notch = `"bell"` with negative `gainDb`.
6. **Config/flag style:** opt-in recipe — default-OFF key + bands list, bit-exact pass-through, lazy build. Mirror `peq`/`peq_bands`.
7. **Preset pattern:** `conf_pipeline/profiles.py` is *device-capability* profiles, not DSP cleanup presets. So the HVAC example ships as an **opt-in preset function** (`office_ac_preset()`), not a schema change / global default.
8. **Bypass/byte-identical tests:** `test_peq.py` + the suite-wide byte-identical invariant (off path returns same object).
9. **Latency:** `estimated_latency_ms` (`polaris_beamformer.py:1989`) sums `_F` over `(aec, dereverb, post_nr)` + the band-limit FIR. A PEQ biquad has no `_F` and isn't in that tuple ⇒ the pre-NR PEQ adds **0** latency (IIR, no lookahead). Estimator needs no change.
10. **Order/invariant tests:** `test_polaris_beamformer.py` byte-identical asserts; `test_stage_activity*`.

### Design — reuse `StreamingPeq`, add a tiny preset module
- NEW `conf_pipeline_control/pre_nr.py` (pure stdlib, numpy-free): `hpf_band()`, `notch_band()`,
  `build_pre_nr_bands(hpf_hz, notches)`, `office_ac_preset()` (MEASURED-ROOM EXAMPLE, opt-in). Emits
  PEQ-format band dicts (`{freqHz,gainDb,q,type}`).
- The pre-NR stage **is** a second `StreamingPeq` instance (`_pre_nr_peq`), built alongside `_peq`.

### Wiring (BOTH chains)
- New ctor params `pre_nr: bool = False`, `pre_nr_bands: Sequence[dict]|None = None` (mirror `peq`/`peq_bands`); store `self.pre_nr` / `self._pre_nr_bands` / `self._pre_nr_peq=None`.
- Build `self._pre_nr_peq = StreamingPeq(sr, self._pre_nr_bands if self.pre_nr else None)` next to `_peq` (polaris `_setup_runtime`, live `_build_post_nr`).
- Apply `mono = self._pre_nr_peq.process(mono)` **between dereverb and post-NR** (polaris `process_block`, live `_process_block`).
- Reset in polaris `reset_transient` (next to `_peq.reset()`); live setter `set_pre_nr_bands` on polaris (mirror `set_peq_bands`).
- Export the pre_nr builders from `__init__.py`.

### New order (both chains): `… dereverb → **pre-NR HPF/notch (NEW)** → post-NR/DFN3 → existing PEQ → AGC → band-limit → voice-gate`. The existing post-NR PEQ stays put (tone-shaping after cleaning).

### Default-off / preset rules: pre-NR is OFF by default ⇒ pipeline byte-identical. Room-specific notches live ONLY in `office_ac_preset()` / user `pre_nr_bands`, never global defaults. DFN3 + dereverb stay OFF by default.

### Tests — `tests/test_pre_nr_filter.py` (the 15 required + extras): band-builders, HPF/notch/multi-notch attenuation (via reused StreamingPeq), invalid-config safety, **order proof** (a recorder stands in for post-NR and observes it receives HPF-filtered audio ⇒ pre-NR ran first), PEQ coexistence, calibration+pre-NR shape/dtype, latency-unchanged, live built/off-noop (seam + source-parity).

### Out of scope (phase-locked): no placement check, transcription, virtual mic, egress, GUI; no config-schema/TS change (runtime params only); no DFN3 retune; existing PEQ untouched.

---

## Phase 2 — outcome (DONE)

Full report: [reports/audio/phase2_pipeline_order_report.md](../reports/audio/phase2_pipeline_order_report.md).
Usage: [docs/PRE_NR_CLEANUP_GUIDE.md](PRE_NR_CLEANUP_GUIDE.md).

**Files changed**
- NEW `conf_pipeline_control/pre_nr.py` (`hpf_band`/`notch_band`/`build_pre_nr_bands`/`office_ac_preset`),
  `tests/test_pre_nr_filter.py` (20), `docs/PRE_NR_CLEANUP_GUIDE.md`, `reports/audio/phase2_pipeline_order_report.md`.
- EDIT `polaris_beamformer.py` + `live.py` — `pre_nr`/`pre_nr_bands` params, `_pre_nr_peq` built next to
  `_peq`, one apply line BETWEEN dereverb and post-NR, reset next to `_peq.reset()`. EDIT `__init__.py`
  exports; EDIT gaps doc.

**New order (both chains):** `… dereverb → **pre-NR HPF/notch** → post-NR/DFN3 → PEQ → AGC → band-limit → voice-gate`.
The pre-NR stage is a **reused** `StreamingPeq` (no new filter math); the existing post-NR PEQ is untouched.

**Verification**
- `tests/test_pre_nr_filter.py` → 20 passed (incl. an order proof: a recorder in the post-NR slot
  receives HPF-filtered audio).
- Full non-GUI suite → **949 passed** (929 + 20; zero regressions; byte-identical-off held).
- `mypy` → clean (67 files). **Latency unchanged** (IIR biquads, no lookahead).

**Default behaviour unchanged:** `pre_nr` is OFF unless bands are supplied; pipeline byte-identical.
Room-specific notches live only in `office_ac_preset()` / user config, never global defaults.

**Open risks carried forward:** live `_process_block` not executed headless (seam + source-parity);
no GUI/`set_pre_nr_bands`/`BeamEngine` fan-out yet (Phase 6); pre-NR not listed in
`active_cleaning_stages()` yet. None block Phase 3.

---

## Phase 3 — plan (written BEFORE coding, per spec)

### Discovery answers (the 10 required inspections)
1. **Measurement/eval harness:** `ab_test.py` (`ab_compare`, DI/WNG/talker-leakage), `ab_capture.py` (raw-vs-clean proof), `report.py` (commissioning). Placement is a NEW, complementary measurement.
2. **Room/noise sim:** `conf_pipeline/sim/` is design-time placement *optimization*; `test_room_background.py` is room-noise *modeling for sim* — NOT a live measured-capture check. Don't rebuild; this is the live counterpart.
3. **Scripts:** `device_check`, `calibrate_front`, `learn_bearing`, `calibrate_capsules` (Phase 1), `validate_live_enhance`, etc. New: `scripts/check_placement.py` (mirrors their argparse style).
4. **Reports:** `reports/audio/phase0..2`. New: `phase3_placement_check_report.md`.
5. **WAV utils:** `record_clip` (`ab_test.py`, re-exported `cc.record_clip`) for live capture; no shared WAV *reader* — add one in the CLI (mirror `calibrate_capsules._read_wav`).
6. **Clean mono output:** `live.py` record / `ab_capture` / `multibeam` — unrelated to placement (placement reads the RAW 8ch capture).
7. **Spectral utils:** `doa.band_indices(freqs, lo, hi)` (reuse for band masks); rfft/rfftfreq used throughout; no shared Welch — implement a small numpy power-spectrum.
8. **Preset pattern (Phase 2):** `pre_nr.build_pre_nr_bands` / `office_ac_preset` — placement emits suggestions that feed straight into these.
9. **Where it lives:** `conf_pipeline_control/placement.py` (numpy, lazy — like `calibration.py`); analyzer takes an array (testable), CLI handles WAV/live.
10. **Test style:** synthetic + **seeded RNG** (`np.random.default_rng(seed)` — used by 17 existing test files), deterministic, `importorskip("numpy")`.

### Design — `conf_pipeline_control/placement.py`
- **`PlacementResult`** (frozen dataclass, camelCase JSON): version/device/sampleRate/channels/durationSeconds/label/status/score + `noiseRmsDbfs`/`speechBandNoiseDbfs`/`lowFrequencyRumbleDbfs`/`broadbandHissDbfs` + `detectedTonesHz`/`notchSuggestionsHz`/`hpfSuggestionHz` + `clippingRisk`/`channelImbalanceDb`/`localHotspotSuspected` + `reasons`/`recommendations`. `to_dict`/`from_dict`/`to_json`/`from_json`/`save`/`load`; `to_pre_nr_bands()` (reuses `build_pre_nr_bands`). Stdlib-only ⇒ default suite.
- **`analyze_placement(capture, *, sample_rate, device, label, …) -> PlacementResult`** (numpy, lazy). Metrics: band powers via a numpy Hann-Welch power spectrum + `doa.band_indices`; **bandwidth-normalized density ratios** for rumble/hiss (gain- AND bandwidth-independent ⇒ flat noise = GOOD); prominence-based tone peak-pick in 50–1000 Hz; per-channel RMS imbalance vs median; clipping fraction; hotspot heuristic.
- **Bands (Hz):** rumble 20–200, speech 300–3400, tone-search 50–1000, hiss 1000–min(8000, ~Nyquist). **Deterministic 0–100 score** (start 100, subtract documented penalties) → GOOD ≥85 / ACCEPTABLE ≥60 / BAD <60.
- **`compare_placements(results) -> PlacementResult`** (survey: highest score). `PlacementError` for empty/mono/invalid input.
- NEW `scripts/check_placement.py` — `--wav`/`--device`, `--label`, `--out`, `--compare *.json`, `--json`/`--markdown`; prints status + reasons + recommendations.

### Default pipeline impact: **NONE.** Placement is a pure analyzer + CLI; it never enables calibration, pre-NR, or any cleaner. Suggestions are exportable but applied only if the operator chooses.

### Tests — `tests/test_placement_check.py` (the 18 required + extras): GOOD/ACCEPTABLE/BAD, rumble/tone/hiss/clip/imbalance/hotspot detection, notch suggestions, mono/empty/wrong-channel/wrong-rate safety, JSON round-trip, survey pick-best, to-pre-NR conversion, determinism, pipeline-default independence.

### Out of scope (phase-locked): no DFN3/calibration/pre-NR behavior change, no transcription/virtual-mic/egress/ASR, no GUI (CLI only), no global hardcoded tones, no auto-apply of suggestions.

---

## Phase 3 — outcome (DONE)

Full report: [reports/audio/phase3_placement_check_report.md](../reports/audio/phase3_placement_check_report.md).
Usage: [docs/PLACEMENT_CHECK_GUIDE.md](PLACEMENT_CHECK_GUIDE.md).

**Files changed**
- NEW `conf_pipeline_control/placement.py` (`PlacementResult`/`analyze_placement`/`compare_placements`/
  `PlacementError`), `tests/test_placement_check.py` (25), `scripts/check_placement.py`,
  `docs/PLACEMENT_CHECK_GUIDE.md`, `reports/audio/phase3_placement_check_report.md`.
- EDIT `__init__.py` exports; EDIT gaps doc. **No live-DSP source touched** (`polaris_beamformer.py` /
  `live.py` unchanged).

**Metrics:** total/speech-band noise, rumble (20–200), hiss (1000–Nyquist), tonal peaks (50–1000,
prominence ≥9 dB), clipping, per-capsule imbalance, hotspot heuristic. Deterministic 0–100 score from
gain-independent density ratios + prominence → GOOD ≥85 / ACCEPTABLE ≥60 / BAD <60.

**Verification**
- `tests/test_placement_check.py` → 25 passed (all 18 required cases + normalization + export).
- Full non-GUI suite → **974 passed** (949 + 25; zero regressions).
- `mypy` → clean (68 files).
- CLI end-to-end: clean synthetic room → GOOD 100/100; 60 Hz rumble + 140 Hz tone → BAD 31/100 (tones
  at 59/141 Hz, opt-in pre-NR suggestion emitted); `--compare` picks the clean position.

**Default pipeline impact:** NONE — placement is a standalone analyzer + CLI; suggestions are opt-in
and never auto-applied; detected tones are this-room-only, never global defaults.

**Open risks carried forward:** live `record_clip` can fail on POLARIS WDM-KS (CLI also accepts
`--wav`); tone freq to bin resolution; hotspot is a heuristic; no GUI yet (Phase 6). None block Phase 4.

---

## Phase 4 — plan (written BEFORE coding, per spec)

### Discovery answers (the 12 required inspections)
1. **Clean mono WAV path:** `live.py` `record_path` (`self._wav`, mono int16) + `multibeam.MultiTrackRecorder` + `multiroom`. All emit PROCESSED mono. Keep.
2. **PCM monitor:** `live.py` `sounddevice.OutputStream` (`_out_stream`/`_cb_output`/`_monitor_q`), float32 mono. Keep.
3. **A/B proof:** `ab_capture.write_ab_proof` (`ab_raw.wav`/`ab_clean.wav`, mono int16). Keep, verify-only.
4. **Final mono in `process_block`:** the return value `mono` (post AGC/band-limit/voice-gate), emitted via `_emit` → `output_callback`.
5. **Final mono in `_process_block`:** `out_g` (post gain/mute), returned + written to WAV.
6. **Output rate:** engine rate (POLARIS 44100; live default 48000), configurable.
7. **Output dtype:** float32 internally; WAV writes int16 via `np.clip(x,-1,1)*32767 → "<i2"`.
8. **WAV utils:** `wave` used directly; shared int16 pattern in `ab_capture`/`live`. No shared WAV class — reuse the pattern.
9. **Stream utils:** `OutputStream` monitor; no socket/network egress; `output_callback` is the emit seam.
10. **Latency:** `estimated_latency_ms` (engine). Egress is DOWNSTREAM of the engine ⇒ does not change it.
11. **Output tests:** `test_ab_capture`, `test_ab_test`, `test_multibeam_recorder` (seeded RNG, int16 WAV round-trips).
12. **Where the router lives:** NEW `conf_pipeline_control/egress.py` — standalone, plugs into the existing `output_callback` (no engine edit, like Phase 3).

### Design — `conf_pipeline_control/egress.py`
- **`to_pcm16(x)` / `pcm16_bytes(x)`** — clip-safe float→int16 (the shared WAV pattern).
- **`resample_mono(x, sr_from, sr_to)`** — reuse `reference_capture._resample_to` (scipy polyphase + numpy fallback).
- **`ExternalPcmSink`** (Protocol) + **`VirtualMicSink`** doc-stub (disabled by default; documents BlackHole/VB-CABLE/PipeWire/JACK integration — NO driver install). **`WavMonoSink`** (mono int16, reuses the pattern).
- **`EgressRouter(sample_rate, *, wav_path=None, asr_rate=16000, max_buffer_seconds=…, sinks=())`**: `push(mono, sample_rate=None)` (validates **1-D mono; rejects (N,>1) raw multichannel → `EgressError`**; cheap — store latest, WAV-write, append engine-rate buffer, fan int16 to sinks); `latest_mono()` / `latest_pcm16()` (48 kHz route); `drain_asr_pcm16()` / `drain_asr_array()` (resample buffer → 16 kHz int16, clear); `pending_seconds()`, `reset()`, `close()`, `frames_pushed`. `EgressError`.

### Integration (no engine change): `PolarisBeamformer(output_callback=router.push)` — the engine already emits the final processed mono there. The router is otherwise standalone + testable.

### Processed-only safeguard: `push` rejects any 2-D (N,>1) block ⇒ raw 8ch can never become the clean output. By construction the callback feeds post-AGC mono.

### Tests — `tests/test_egress.py` (the 15 required + extras): mono accept / raw-8ch reject / no-op safe, 48 kHz + 16 kHz shape+dtype, 48→16k length, int16 clip-saturate, silence→silence, tone-survives-resample, reset clears, WAV uses processed mono + reads back, engine latency unchanged, prior-phase suites green.

### Out of scope (phase-locked): no real ASR/transcription provider (Phase 5), no transcription/placement/GUI UI, no DSP-default change, no virtual-audio-driver install, no rebuild of existing recorders/monitor.

---

## Phase 4 — outcome (DONE)

Full report: [reports/audio/phase4_egress_report.md](../reports/audio/phase4_egress_report.md).
Usage: [docs/AUDIO_EGRESS_GUIDE.md](AUDIO_EGRESS_GUIDE.md).

**Files changed**
- NEW `conf_pipeline_control/egress.py` (`EgressRouter`, `WavMonoSink`, `ExternalPcmSink`,
  `to_pcm16`/`pcm16_bytes`/`resample_mono`, `EgressError`), `tests/test_egress.py` (20),
  `docs/AUDIO_EGRESS_GUIDE.md`, `reports/audio/phase4_egress_report.md`.
- EDIT `__init__.py` exports; EDIT gaps doc. **No live-DSP source touched** — the router plugs into the
  existing `output_callback`.

**Routes:** 48 kHz `latest_pcm16()`/`latest_mono()` (0 latency) · 16 kHz `drain_asr_pcm16()` (resample
+ int16) · mono WAV sink (reused int16 format) · optional `ExternalPcmSink` virtual-mic hook (off by
default, no driver bundled). `push` rejects raw multichannel ⇒ 8ch can't leak as the clean output.

**Verification**
- `tests/test_egress.py` → 20 passed (all 15 required + extras).
- Full non-GUI suite → **994 passed** (974 + 20; zero regressions; existing recorders/A-B proof intact).
- `mypy` → clean (69 files). End-to-end demo: 48000 frames → 16000-sample 16 kHz ASR PCM + a valid
  1ch/16-bit/48 kHz WAV; raw-8ch push rejected.

**Default pipeline impact:** NONE — standalone egress layer; engine `estimated_latency_ms` unchanged;
no DSP default altered.

**Open risks carried forward:** 16 kHz drain is per-buffer (Phase 5 adds VAD/chunking); WAV write in
`push` is disk I/O on the audio thread if wired as the realtime callback (documented alternative: drain
the output queue on a consumer thread); virtual-mic is a seam, not a driver; no GUI yet (Phase 6).
None block Phase 5.

---

## Phase 5 — plan (written BEFORE coding, per spec)

### Discovery answers (the 10 required inspections)
1. **ASR/transcription code:** NONE (repo-wide grep hits are only Phase 3/4 docs + README). Genuine gap.
2. **Provider/interface pattern:** `@runtime_checkable Protocol` — `HwGain` (preamp), `ExternalPcmSink` (egress). Mirror for `TranscriptionProvider`.
3. **VAD utils:** `octovox_monitor.speech_gate(raw_rms, noise_floor, gate_ratio) -> (is_speech, nf)` (adaptive floor) + `multikit.SpeechPresenceScorer`. Offer `speech_gate` as the pluggable adaptive option; default the chunker to a simple deterministic absolute energy threshold (no warmup ambiguity).
4. **Chunking/streaming utils:** `EgressRouter` buffering; `ab_capture` bounded capture. No general chunker — build one.
5. **`drain_asr_pcm16()`:** returns 16 kHz little-endian int16 bytes, clears the buffer (`router.asr_rate`).
6. **Async/threading:** repo is sync + threads + `queue.Queue` (no asyncio). ⇒ **sync** provider interface (matches the Phase-5 Protocol example).
7. **Stream/session tests:** `test_egress`, `test_ab_capture`, `test_reference_capture` — seeded RNG, `importorskip numpy`.
8. **Docs/report style:** established (guide + numbered report).
9. **Module location:** NEW `conf_pipeline_control/transcription.py` (numpy lazy for VAD energy; models stdlib — like egress/placement).
10. **Latency/buffering:** chunker buffers until a speech chunk closes (hangover) or `max_chunk`; reported honestly.

### Design — `conf_pipeline_control/transcription.py`
- **`TranscriptionProvider`** (`@runtime_checkable Protocol`): `start_session(session)`, `send_audio_chunk(chunk)`, `stop_session() -> TranscriptResult`.
- **`TranscriptionSession`** (dataclass): `session_id`, `sample_rate=16000`, `channels=1`, `encoding="pcm_s16le"`, `started_at`/`stopped_at`, `chunks_sent`, `duration_seconds`, `status` (idle/running/stopped/error), `metadata`.
- **`AudioChunk`** (frozen): `pcm16` bytes, `sample_rate`, `channels`, `start_time_seconds`, `duration_seconds`, `is_speech`, `energy_dbfs`.
- **`TranscriptResult`** (frozen, camelCase JSON): `text`, `segments`, `duration_seconds`, `provider`, `language`.
- **`SpeechChunker`** — deterministic energy VAD over 16 kHz int16: 20 ms frames, absolute `threshold_dbfs` (or injected `speech_fn`, e.g. wrapping `speech_gate`), `min_speech_ms`/`max_chunk_ms`/`hangover_ms`/`preroll_ms`; silence→no chunk, short bursts dropped, long speech split, deterministic boundaries, `reset()`.
- **`TranscriptionStream`** — `start()/push_pcm16(pcm16, sample_rate=16000)/stop()/reset()` + `pump_from_egress(router)`. Validates **mono 16 kHz int16 (bytes or array); rejects raw multichannel + wrong rate + float**. Sends completed chunks to the provider; tracks the session; surfaces provider errors as `TranscriptionError` (session → error).
- **`MockTranscriptionProvider`** — records chunks, deterministic `TranscriptResult`, empty-session handling, error injection, `network_calls == 0` (proves no network).

### Integration: `pcm = router.drain_asr_pcm16(); stream.push_pcm16(pcm, sample_rate=router.asr_rate)` (or `stream.pump_from_egress(router)`). Egress is the documented source of truth for ASR input.

### Default pipeline impact: NONE — transcription is a standalone consumer of the 16 kHz egress; it never touches the DSP engine, makes no network call by default, and rejects raw 8ch.

### Tests — `tests/test_transcription_stream.py` (the 18 required + extras): mock lifecycle, session start/stop, chunking of speech, silence→no-chunk, short-burst-drop, long-speech-split, reset, wrong-rate/raw-8ch/float reject, bytes+array, timestamps/durations, mock-gets-clean-16k-int16, provider-error-safe, egress integration, no-network, DSP-defaults-unchanged.

### Out of scope (phase-locked): no real/paid ASR vendor, no transcription/summary UI, no network-by-default, no GUI, no DSP/calibration/pre-NR/placement change, no virtual-driver install, no Phase 6.

---

## Phase 5 — outcome (DONE)

Full report: [reports/audio/phase5_transcription_ready_report.md](../reports/audio/phase5_transcription_ready_report.md).
Usage: [docs/TRANSCRIPTION_STREAM_GUIDE.md](TRANSCRIPTION_STREAM_GUIDE.md).

**Files changed**
- NEW `conf_pipeline_control/transcription.py` (`TranscriptionProvider`/`MockTranscriptionProvider`/
  `TranscriptionSession`/`AudioChunk`/`TranscriptResult`/`SpeechChunker`/`TranscriptionStream`/
  `TranscriptionError`), `tests/test_transcription_stream.py` (22), `docs/TRANSCRIPTION_STREAM_GUIDE.md`,
  `reports/audio/phase5_transcription_ready_report.md`.
- EDIT `__init__.py` exports; EDIT gaps doc. **No live-DSP source touched.**

**Design:** sync provider Protocol + deterministic energy VAD (`SpeechChunker`: 20 ms frames, threshold
or injected `speech_fn`, min-speech/max-chunk/hangover/preroll) + session model + mock provider +
`pump_from_egress(router)`. Accepts ONLY mono 16 kHz int16; rejects raw 8ch / wrong rate / float.

**Verification**
- `tests/test_transcription_stream.py` → 22 passed (all 18 required + extras).
- Full non-GUI suite → **1016 passed** (994 + 22; zero regressions).
- `mypy` → clean (70 files). End-to-end demo: processed mono → egress 16 kHz → 1 speech chunk
  (0.20 s start, 1.70 s, int16 @ 16 kHz) → mock transcript; **0 network calls**; raw-8ch rejected.

**Default pipeline impact:** NONE — standalone consumer of the 16 kHz egress; no DSP touched, no network
by default.

**Open risks carried forward:** energy VAD is lightweight (adaptive `speech_fn` available); no real ASR
provider bundled (interface seam); chunking adds ≤ `max_chunk_ms` buffering; no GUI/engine-thread wiring
yet (Phase 6). None block Phase 6.

---

## Phase 6 — plan (written BEFORE coding, per spec)

### Discovery answers (the 10 required inspections)
1. **GUI framework/entry:** PySide6; `run_gui.py` → `conf_pipeline_gui.app.main`; panels in `conf_pipeline_gui/panels/` (`live.py` LivePanel, `common.py` StageStrip).
2. **Operator/diagnostics panels:** `panels/live.py` (rich live controls), `StageStrip` (per-stage activity read-out). The live panel already fills `CommissioningInfo` from the engine.
3. **Settings/config controls:** live-panel checkboxes/combos + runtime setters (`set_peq_bands`, `set_preamp_gain_db`, …).
4. **Runtime-flag update pattern:** atomic-rebind setters on the engine; flags are `__init__`-set `self.X` booleans.
5. **Pipeline status display:** `StageActivity`/`StageMeter` (`_stage_metrics`) → `StageStrip`. Covers AEC/dereverb/denoise/AGC; NOT calibration/pre-NR/full order.
6. **Commissioning report:** `conf_pipeline/report.py` `commissioning_report` + `CommissioningInfo` (latency, `active_cleaning_stages`, AEC/ERLE, A/B proof, capsule health). EXISTS — extend, don't rebuild. `active_cleaning_stages()` (polaris+live) lists AEC/dereverb/post-NR — **add pre-NR** (Phase 2 flagged it).
7. **Output/recording controls:** live-panel record/A-B/monitor buttons; egress (Phase 4) not surfaced.
8. **Test style:** headless model tests (`test_stage_activity`) + GUI panel-probe (`test_gui_stage_strip`: `importorskip PySide6`, offscreen, construct a single `QWidget`, assert its `.cell()` accessor — **never MainWindow**).
9. **GUI hang:** building `MainWindow` headless HANGS on this box (CLAUDE.md) → MainWindow GUI is CI-only; single-panel probes work locally.
10. **Safest place:** a headless model first, GUI wired lightly.

### Design
- NEW `conf_pipeline_control/operator.py` — **`OperatorStatus`** (headless): `build(engine=…, device=…, calibration_path=…, placement=…, egress=…, transcription=…)` → 7 section builders (`device`/`calibration`/`placement`/`pipeline`/`egress`/`transcription` + `warnings`), reading engine **flags** defensively (latency via try/except). `to_dict()` / `to_markdown()` / `save(out_dir, stamp)`. Surfaces OFF/failed/uncertain honestly; placement suggestions carried but `autoApplied=False`.
- EDIT `polaris_beamformer.py` + `live.py` `active_cleaning_stages()` — add **"HPF/notch"** when pre-NR is on (the explicit Phase-2 follow-up; `in`-checked tests stay green).
- NEW `scripts/operator_diagnostics.py` — CLI: build the model from engine args + optional placement JSON, print the 7 sections, export `reports/audio/operator_diagnostics_<stamp>.{json,md}`.
- NEW `conf_pipeline_gui/panels/operator.py` — light read-only **`OperatorStatusPanel(QWidget)`** that renders `OperatorStatus.to_dict()` with a `.section()` accessor (mirrors `StageStrip.cell()`), tested via the single-panel probe.

### Default pipeline impact: NONE — the model only READS flags; the only engine edit is the human-readable `active_cleaning_stages()` string (status, not audio). No default changed; nothing forced on; suggestions never auto-applied.

### Tests — `tests/test_operator_workflow.py` (headless model, in the baseline) + `tests/test_gui_operator.py` (panel probe, run explicitly + CI). Cover the 16 required: builds-without-hardware, calibration on/off/missing/malformed-surfaced, placement→GOOD/ACCEPTABLE/BAD + recommendations-not-auto-applied + tones-as-suggestions, order-has-calibration+pre-NR-before-post-NR, active-stages-include-pre-NR, egress 48k+16k, raw-8ch-can't-be-clean-output, mock-session start/stop, no-network, diagnostics-export-sections, defaults-unchanged.

### Out of scope (phase-locked): no DSP rebuild, no real ASR vendor, no virtual-driver install, no auto-apply of suggestions, no MainWindow rewrite, no Phase 7, no default-behaviour change.

---

## Phase 6 — outcome (DONE)

Full report: [reports/audio/phase6_gui_operator_workflow_report.md](../reports/audio/phase6_gui_operator_workflow_report.md).
Usage: [docs/AUDIO_OPERATOR_WORKFLOW_GUIDE.md](AUDIO_OPERATOR_WORKFLOW_GUIDE.md).

**Files changed**
- NEW `conf_pipeline_control/operator.py` (`OperatorStatus`, 7-section model + JSON/MD export),
  `conf_pipeline_gui/panels/operator.py` (`OperatorStatusPanel`), `scripts/operator_diagnostics.py`,
  `tests/test_operator_workflow.py` (18), `tests/test_gui_operator.py` (3), `docs/AUDIO_OPERATOR_WORKFLOW_GUIDE.md`,
  `reports/audio/phase6_gui_operator_workflow_report.md`.
- EDIT `polaris_beamformer.py` + `live.py` — `active_cleaning_stages()` lists "HPF/notch" when pre-NR on
  (status string only). EDIT `__init__.py` exports; EDIT gaps doc.

**Design:** a headless `OperatorStatus` (Device / Calibration / Placement / Pipeline / Output /
Transcription / Diagnostics) reading engine flags + Phase 1–5 objects; a CLI prints/exports it; a thin
read-only GUI panel renders it (single-widget offscreen probe, not MainWindow). Honest OFF/failed/
uncertain; suggestions carried with `autoApplied=false`.

**Verification**
- `tests/test_operator_workflow.py` → 18 passed; `tests/test_gui_operator.py` → 3 passed (offscreen).
- Full non-GUI suite → **1034 passed** (1016 + 18; zero regressions).
- `mypy` → clean (71 files). CLI verified: BAD placement + pre-NR + AGC → full 7-section read-out with
  honest [on]/[off] stages and "active cleaning: HPF/notch".

**Default pipeline impact:** NONE — the model only reads flags; the only engine edit is the
`active_cleaning_stages()` status string. No default changed; nothing forced on; nothing auto-applied.

**Open risks carried forward:** operator surface is read-only status + CLI/panel (no new live control
wiring into the live panel — deliberate); full MainWindow GUI is CI-only locally (documented hang);
latency shown is the engine estimate. None block Phase 7.

---

## Phase 7 — outcome (DONE) — PROJECT COMPLETE

Full report: [reports/audio/final_verification_report.md](../reports/audio/final_verification_report.md).
Deployment: [docs/AUDIO_FRONTEND_DEPLOYMENT_GUIDE.md](AUDIO_FRONTEND_DEPLOYMENT_GUIDE.md).

**Files changed (docs only — no code):** NEW `docs/AUDIO_FRONTEND_DEPLOYMENT_GUIDE.md`,
`reports/audio/final_verification_report.md`; EDIT this tracker + gaps doc.

**Verification (all green):**
- 7 phase test files + GUI panel probe → **137 passed** (29+20+25+20+22+18+3).
- Full non-GUI suite → **1034 passed** (900 baseline + 134 new; zero regressions). mypy → clean (71).
- 3 CLIs `--help` OK; e2e demo → 1 ASR chunk, mock transcript, **0 network calls**.
- Regression rules proven: no raw-8ch egress, no raw-8ch ASR, no auto-apply, nothing default-forced-on,
  no network by default, no global room tones, no DSP mode removed, no output path rebuilt.

**Final state:** all 8 phases (0–7) complete. Every opt-in feature default-OFF; with all off the pipeline
is **byte-identical** to pre-Phase-1. Commit recommended (`feat(control): harden POLARIS audio front-end
production workflow`) but **NOT performed** — awaiting explicit instruction.

### Phase scoreboard (final)
| Phase | New module(s) | Tests | Default impact |
|------|---------------|-------|----------------|
| 1 Calibration | `calibration.py` | 29 | none (OFF) |
| 2 Pre-NR HPF/notch | `pre_nr.py` (+reused PEQ) | 20 | none (OFF) |
| 3 Placement check | `placement.py` | 25 | none (diagnostic) |
| 4 Egress | `egress.py` | 20 | none (optional) |
| 5 Transcription | `transcription.py` | 22 | none (optional) |
| 6 Operator | `operator.py` + panel | 18 + 3 | none (read-only) |
| 7 Verification | docs only | — | none |
| **Total new** | 6 modules + panel + 3 CLIs | **137** | **byte-identical when off** |
