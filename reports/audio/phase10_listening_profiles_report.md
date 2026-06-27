# Phase 10 — Listening Processing Profiles (recommended defaults; dereverb = per-profile)

## SUMMARY
Phase 10 shipped three things, in order:
1. **Descriptive layer:** each LIVE "Listening mode" (Whole table / Follow / Lock-to-seat / Clean audio /
   Manual / Two kits) is modelled as a `ListeningProfile` with an honest **flow summary** + warnings,
   shown read-only under the LIVE dropdown. The model changes no DSP and applies nothing.
2. **Recommended defaults (user-requested):** the LIVE panel **pre-ticks** the recommended cleanup so a
   fresh session Connects with it on — a **GUI-default change only** (engine/CLI/library defaults stay OFF;
   byte-identical engine tests untouched).
3. **Dereverb policy revision (this update):** dereverb is **NOT a global default**. It is recommended ON
   only for **Follow** and **Clean audio**, and OFF everywhere else (it can colour a dry room). The global
   dereverb checkbox is OFF; dereverb is applied per-profile via the **auto-steer path's own** checkbox.

## FILES CHANGED (this update)
- `conf_pipeline_control/listening_profile.py` — `_recommended_cleanup(dereverb=…)` default → **False**;
  only `follow`/`clean` built-ins pass `dereverb=True`.
- `conf_pipeline_gui/panels/live.py` — global `live_dereverb` default reverted to **OFF**;
  `_on_listening_mode_changed` sets `live_autosteer_dereverb` ON for `follow`/`clean` only (per-profile
  apply on the auto-steer path — never the global switch; Manual is never touched).
- `tests/test_listening_profiles.py` — `test_dereverb_is_restricted_to_steering_modes`; updated
  whole-table + recommended-cleanup tests.
- `tests/test_gui_listening_profiles.py` — `test_dereverb_is_not_globally_preticked`,
  `test_follow_and_clean_enable_dereverb_on_autosteer_path`, `test_table_and_manual_do_not_force_dereverb`;
  updated the pre-tick test (global dereverb now OFF).
- `tests/test_gui_live_seat.py` (CI/MainWindow) — reverted the 2 global-dereverb assertions to OFF.
- Docs: `docs/LISTENING_PROFILES_GUIDE.md`, `docs/AUDIO_OPERATOR_WORKFLOW_GUIDE.md`,
  `docs/AUDIO_FRONTEND_PHASE_TRACKER.md`, this report.
- (Kept from the prior step: OM-LSA denoise + tap-suppression + AGC pre-ticks; the descriptive model,
  flow-summary label, and `preferred_listening_profile_id` on `room_profile.py`.)

## TESTS RUN
- Narrow first: `tests/test_listening_profiles.py` → **18 passed**;
  `tests/test_gui_listening_profiles.py` (offscreen, LivePanel-direct) → **13 passed**.
- User-specified non-GUI suite: `pytest -q --ignore-glob='tests/test_gui_*.py' -p no:cacheprovider` → see RESULTS.
- `mypy` (scope `conf_pipeline` + `conf_pipeline_control`) → **Success, no issues in 73 files**.
- CI-only (MainWindow/`win` fixture hangs headless here): `test_gui_live_seat`, `test_gui_smoke`,
  `test_gui_coverage`, `test_gui_twokit` — `live_seat` edited by inspection (2 reverts).

## RESULTS
- Model + GUI listening tests: **green** (18 + 13). mypy: **clean** (73 files).
- Non-GUI suite (`--ignore-glob='tests/test_gui_*.py' -p no:cacheprovider`): **1070 passed in 42 s**,
  exit 0 — includes the engine byte-identical/default-off tests, all unchanged.
- No regressions: the only behavioural change is GUI checkbox defaults; engine/CLI/library defaults untouched.

## FINAL DEREVERB DEFAULTS
| Mode | Dereverb default | How |
|---|---|---|
| Follow the room (auto-steer) | **ON** (recommended) | mode handler ticks `live_autosteer_dereverb` (auto-steer path) |
| Clean audio (hands-off) | **ON** (recommended) | same — auto-steer path only |
| Whole table | **OFF** | global `live_dereverb` off; base path reads the global only |
| Lock to a seat | **OFF** | A/B-engine path; `live_beameng_dereverb` off |
| Two kits | **OFF** | two-kit cfg has no dereverb wiring |
| Manual (advanced) | **OFF** | handler never touches it — the user's own toggle is the source of truth |

Global `live_dereverb` checkbox: **OFF by default**, every mode. Never force-on globally.

## PROFILE-SPECIFIC BEHAVIOR
- **Follow / Clean:** picking the mode enables dereverb on the auto-steer path's own checkbox; the flow
  summary shows `dereverb ON` and `warnings()` adds "Dereverb is ON — can alter room naturalness."
- **All other modes:** dereverb stays off; the summary shows `dereverb OFF` (it's part of the optional
  chain only when a profile opts in). The other recommended stages are unchanged — OM-LSA denoise +
  tap-suppression on the steering/A-B/two-kit paths, AGC everywhere; AEC + voice gate stay opt-in.
- The apply path is the existing per-checkbox one (`live_autosteer_dereverb` → auto-steer cfg
  `dereverb=`), so it is fixed at Connect like every other stage — no new apply mechanism.

## SAFETY GUARANTEES
- **No global dereverb, no forced dereverb** — off by default, on only for Follow/Clean, untickable.
- **No engine/library/CLI default change**; byte-identical engine tests untouched.
- No DFN3 forced; AEC + voice-gate defaults unchanged; AGC kept as already verified (on). OM-LSA +
  tap-suppression kept (profile-safe, test-covered).
- No modes/controls removed or renamed; **Audio Room Profile Manager untouched**.

## KNOWN LIMITATIONS
- If the operator navigates Follow → Manual, the auto-steer dereverb checkbox stays as last set (on) and
  is visible/untickable — by design, Manual treats the live toggle as the source of truth (picking Manual
  from a fresh panel never enables it).
- Dereverb can colour a dry room; transient suppression adds ~12 ms latency — both unticktable before Connect.
- The base "Whole table" path has no denoiser; its recommended cleanup is AGC only.

## NEXT STEP
Awaiting review. **Not committed, not pushed, not merged.** On approval: commit on the existing feature
branch only.
