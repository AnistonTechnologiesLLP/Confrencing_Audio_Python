# Phase 9 — Audio Room Profile Manager Report

**Goal:** a GUI-managed, room-specific audio-profile system (save / load / view / validate / import /
export) that **never silently changes the live audio pipeline**. Profile management only.

Status: **COMPLETE.** 17 model tests + 7 GUI window tests green; full non-GUI suite 1034 → **1051
passed** (zero regressions); mypy clean (72 files); app wiring verified without building MainWindow.

---

## 1. What already existed and was not rebuilt
- The camelCase-JSON model pattern (`CalibrationProfile`, `PlacementResult`) — mirrored, not rebuilt.
- `PlacementResult` (status/score/tones/suggestions) + `pre_nr.build_pre_nr_bands` — read for the
  copy-suggestions helper; unchanged.
- The Phase-8 menu→window wiring (`OperatorDiagnosticsWindow`) + `QFileDialog` patterns — mirrored.
- All Phase 1–8 code — untouched.

## 2. What was actually missing
A persisted, room-specific **setup document** + a GUI to manage it. Calibration/placement results were
individual artifacts; there was no single record tying a room's calibration + measured tones +
egress/transcription preferences together, and no GUI to save/load/validate one.

## 3. Files changed
New:
- `conf_pipeline_control/room_profile.py` — `AudioRoomProfile` (+ nested section dataclasses) +
  `RoomProfileError`.
- `conf_pipeline_gui/panels/room_profile.py` — `AudioRoomProfilesWindow`.
- `tests/test_audio_room_profile.py` (17, model) + `tests/test_gui_room_profile.py` (7, window probe).
- `docs/AUDIO_ROOM_PROFILE_GUIDE.md`, `reports/audio/phase9_audio_room_profiles_report.md`.

Edited (additive):
- `conf_pipeline_gui/app.py` — menu action "Audio room profiles…" + `_show_room_profiles` handler.
- `conf_pipeline_control/__init__.py` — export `AudioRoomProfile` / `RoomProfileError`.
- `docs/AUDIO_OPERATOR_WORKFLOW_GUIDE.md`, `docs/AUDIO_FRONTEND_PHASE_TRACKER.md`.

**No DSP-core behavior changed.** The model is inert; the only engine-adjacent code is the menu wiring.

## 4. Profile model
A **mutable** `AudioRoomProfile` (an editable draft the GUI builds up) with nested sections
`calibration` / `placement` / `preNrCleanup` / `egress` / `transcription` / `safety` (the requested
camelCase JSON). `to_dict`/`from_dict`/`to_json`/`from_json`/`load`/`save` (lossless round-trip);
`RoomProfileError` for malformed JSON only; **non-throwing `validate(...) -> list[str]`** (warns on
version/device/rate/channel mismatch, missing referenced files, any True safety flag, auto-apply set).
Helpers: `attach_calibration(path)` and `copy_placement_suggestions(result)`. Pure stdlib; mypy-clean.

## 5. GUI integration point
**App menu (☰) → "Audio room profiles…"** → opens `AudioRoomProfilesWindow` (a separate widget,
mirroring the Phase-8 operator window). Buttons: New / Load… / Save… / Import… / Export… / Validate /
Copy placement suggestions…, plus a profile-name field, a read-only summary, a warnings area, and the
prominent safety note. Path-taking methods (`load_path`/`save_path`/`import_path`/`export_path`/
`copy_placement_path`) hold the logic (offscreen-tested); the buttons are thin `QFileDialog` wrappers.

## 6. What the profile stores
Name/device/rate/channels/notes; calibration ref (path + summary, `enabled` flag); placement ref (path +
last status/score + detected tones + notch/HPF suggestions + `autoApplySuggestions`); pre-NR draft
(enabled + HPF + notches); egress prefs (48k / 16k ASR / wav / external sink); transcription prefs
(enabled / provider / rate / VAD); and safety flags.

## 7. What the profile does NOT apply
Nothing. Loading/validating only previews + warns. It does not enable calibration / pre-NR / DFN3 /
dereverb / transcription, does not auto-apply placement suggestions, makes no network call, and bundles
no driver. Applying a profile to a running engine is **out of scope** (a deliberate, separate, later
step). There is **no `apply_to_engine`** on the model or window.

## 8. Safety guarantees (tested)
- `safety.*` flags default all-False; `validate()` warns on any True. The model never sets one True.
- `copy_placement_suggestions` fills the draft but leaves `preNrCleanup.enabled` False,
  `placement.autoApplySuggestions` False, `safety.placementSuggestionsAutoApplied` False.
- `attach_calibration` sets the path but not `calibration.enabled`.
- The window's required note states: *"Profiles are room-specific. Loading a profile does not apply it
  to the running audio engine. Placement suggestions are not auto-applied. Measured notch frequencies
  must be re-measured per room."*

## 9. Test results
```
tests/test_audio_room_profile.py ................. 17 passed
tests/test_gui_room_profile.py ....... 7 passed (offscreen)
full non-GUI suite (--ignore-glob='tests/test_gui_*.py') → 1051 passed (1034 + 17; zero regressions)
mypy → clean (72 source files)
```
Covers the 15 required cases: default validates, JSON round-trip, invalid-version warned, device/rate/
channel mismatch warned, missing calibration/placement file warned, suggestions copied to draft,
suggestions not auto-applied, pre-NR stored not forced, transcription stored no network, safety defaults
safe, window renders offscreen, "not applied automatically" note, import/export JSON, prior suites green.

## 10. Manual GUI check steps
`python run_gui.py` (real desktop) → menu (☰) → **"Audio room profiles…"**. Verify: the window opens with
**New / Load… / Save… / Import… / Export… / Validate / Copy placement suggestions…**, the **safety note**
is visible, **New** then **Save…** writes a JSON, **Load…** reads it back into the summary, **Validate**
shows warnings (e.g. after attaching a missing file), and **nothing changes the running engine**. The
Phase-8 "Audio operator diagnostics…" window still works.

## 11. Known limitations
- The window does not apply profiles to the engine (by design — a later phase). Its `Validate` checks the
  profile's internal consistency (version/files/safety); it does not yet compare against the *live*
  engine's device/rate (the model supports `expected_*` for that — a small follow-up).
- MainWindow isn't tested headless (it hangs offscreen, per CLAUDE.md) — the menu action is verified by
  inspection + a no-construct import check + CI; the window + model are fully tested offscreen.
- `createdAt`/`updatedAt` are caller-set (the model stays deterministic); the GUI can stamp them.

## 12. Safe next step
Optional (not in this phase): an explicit, confirmed "Apply this profile to the running engine" action
(reconnect with the profile's calibration/pre-NR), and a profile picker keyed to the live device's
rate/channels. Phase 9 changed no default and applied nothing.
