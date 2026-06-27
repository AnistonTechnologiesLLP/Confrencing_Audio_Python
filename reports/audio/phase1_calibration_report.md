# Phase 1 — Per-Capsule Calibration Report

**Goal:** add a runtime per-capsule calibration stage (gain / polarity / integer-sample delay) BEFORE
DOA, beamforming and null steering in **both** DSP chains — default-OFF, bit-exact pass-through when
off, with a save/load profile, safe fallbacks, and a synthetic-testable estimator. Phase-locked to
calibration only.

Status: **COMPLETE.** 29 new tests green; full non-GUI suite 900 → **929 passed**; mypy clean;
calibration verified end-to-end through the operator script.

---

## 1. What already existed and was NOT rebuilt

Verified from source and left intact:

- **Uniform input preamp** — `preamp.py` `InputPreamp` / `PreampHost`. One scalar gain to all 8
  capsules. This is the *template* the calibration layer mirrors; it is not per-capsule and was not
  touched (it remains the front stage; calibration sits right after it).
- **Dead-capsule mask** — `geometry.py` `ArrayGeometry.active` + `polaris_beamformer.py`
  `_resolve_active_mask`. On/off only; reused (calibration honors it) but not changed.
- **Front-bearing calibration** — `scripts/calibrate_front.py`, GUI "Calibrate front". Estimates one
  array *bearing* via DOA; a different concept (not per-capsule). Untouched.
- **Directivity calibration** — `tests/test_directivity_calibration.py` + `conf_pipeline/directivity.py`.
  Confirmed to be the analytic-beamwidth-vs-measured-beam check (design/simulation side), **not**
  runtime capsule alignment. Untouched.
- All existing DSP (SRP-PHAT DOA, beam modes, LCMV nulls, AEC, dereverb, post-NR/DFN3, AGC, PEQ,
  band-limit, voice-gate, outputs) — untouched.

## 2. What was actually missing

No runtime gain / polarity / integer-delay alignment of the 8 capsules before spatial processing.
Capsules went raw (uniform-preamp-scaled) into DOA + beamforming, which assume matched capsules. This
was the genuine gap; Phase 1 fills exactly it.

## 3. Files changed

New:
- `conf_pipeline_control/calibration.py` — `CalibrationProfile`, `CapsuleCalibrator`,
  `estimate_calibration` + `CalibrationEstimate`, `CalibrationHost` mixin, `CalibrationError`.
- `tests/test_calibration.py` — 29 hardware-free tests.
- `scripts/calibrate_capsules.py` — operator estimation CLI (live capture or WAV → profile JSON).
- `docs/CALIBRATION_GUIDE.md` — usage + profile format + safety + flow.
- `reports/audio/phase1_calibration_report.md` — this report.

Edited (minimal, additive):
- `conf_pipeline_control/polaris_beamformer.py` — `CalibrationHost` base; `calibration` /
  `calibration_path` ctor params; `_init_calibration(...)` after `_init_preamp`; one apply line in
  `process_block`; delay-line reset in `reset_transient`.
- `conf_pipeline_control/live.py` — same five touch-points (`_process_block` for the apply, `_open`
  for the session reset).
- `conf_pipeline_control/__init__.py` — export the public calibration names.
- `docs/AUDIO_FRONTEND_PHASE_TRACKER.md` — Phase 1 tracking.

## 4. Where calibration is inserted in `process_block` (steered / A-B engine path)

`polaris_beamformer.py`, the FRONT of the per-block chain:

```python
block = self._apply_preamp(block)          # uniform mic-input gain (no-op when off)
block = self._apply_calibration(block)     # ← NEW: per-capsule gain/polarity/delay (no-op when off)
...
mono = self._beam.process(block)           # beam uses the corrected block
...
self._accumulate_covariance(block)         # DOA covariance uses the SAME corrected block
```

The single corrected `block` feeds both the beamformer and the DOA covariance, so steering, nulls and
the beam all see aligned capsules. (Proven by `test_polaris_calibration_runs_before_covariance_and_doa`,
which boosts capsule 0 and observes the covariance auto-power rise.)

## 5. Where calibration is inserted in `_process_block` (zone / auto-steer path)

`live.py`, the FRONT of the per-block chain:

```python
indata = self._apply_preamp(indata)        # uniform gain (no-op when off)
indata = self._apply_calibration(indata)   # ← NEW (no-op when off)
self._inbuf[-_HOP:, :] = indata[:_HOP, :]  # corrected block → STFT → covariance + beam
```

Identical mixin, identical position. `_process_block` is `pragma: no cover (needs hardware)` per the
repo's convention, so the live chain is verified at the construction + shared-seam level
(`test_live_calibration_*`) plus source-parity with the headless-tested polaris path.

## 6. Profile format

camelCase JSON (`CalibrationProfile`): `version`, `device`, `sampleRate`, `channels`, `createdAt`,
`gainDb[8]`, `delaySamples[8]`, `polarity[8]`, `referenceChannel`, `notes`. Stdlib-only (numpy-free)
so it loads in the GUI / engine core. Save/load + `from_dict`/`to_dict`/`from_json`/`to_json`;
`validate()` raises a controlled `CalibrationError`. It is a **sidecar device-calibration artifact**,
deliberately NOT folded into the camelCase room-config schema (`CONFIG_VERSION`), so Phase 1 needs no
schema bump and no TS-parity change.

## 7. Runtime flags / config

Two opt-in constructor params on both engines, default OFF (mirrors the preamp recipe):
- `calibration: CalibrationProfile | None = None`
- `calibration_path: str | None = None` (loaded with safe fallback)

Off ⇒ `_calib is None` ⇒ `_apply_calibration` returns the **same array object** (byte-identical).
The mixin (`CalibrationHost._init_calibration`) reconciles the profile to the engine (channels /
sample-rate / active-mask) and keeps it off on any incompatibility.

## 8. Estimator behaviour

`estimate_calibration(capture, *, sample_rate, reference_channel=None, active_mask=None,
estimate_polarity=True, estimate_delay=True, …) -> CalibrationEstimate`:
- **gain** from per-channel RMS vs the reference (clamped ±12 dB) — robust on any broadband capture.
- **polarity** from the cross-correlation peak sign.
- **delay** from the cross-correlation peak lag; corrections align all capsules to the latest
  arrival (causal, non-negative).
- **confidence** per channel; silent / decorrelated capsules are left uncorrected and flagged in
  `low_confidence_channels` — never a faked correction.

End-to-end check (script on a synthetic 8-ch WAV, ch1 = 2× louder, ch3 inverted):
`gainDb[1] = -6.02`, `polarity[3] = -1`, others identity — exactly correct.

## 9. Dead-capsule interaction

The corrector takes the array's active mask. A masked / dead capsule is forced to identity (gain 1,
polarity +1, delay 0) — never gained up, delayed or revived; the estimator skips it. A profile naming
a correction only for a dead capsule reduces to neutral (stays off). Guarded by
`test_calibrator_active_mask_skips_dead_channel`.

## 10. Tests added (`tests/test_calibration.py`, 29)

Maps to the 14 required:
1. disabled path unchanged → `*_neutral_is_identity_noop`, `*_default_off_is_byte_identical` (polaris+live)
2. neutral profile unchanged → `*_neutral_is_identity_noop` (returns same object)
3. gain scaling → `*_gain_scales_per_channel`
4. polarity flip → `*_polarity_flips_channel`
5. delay shift → `*_integer_delay_shifts_channel` (+ `*_delay_is_continuous_across_blocks`)
6. missing profile safe → `*_path_missing_falls_back_off`, `*_load_missing_file_raises_controlled`
7. malformed safe/controlled → `*_from_malformed_json_raises_controlled`, `*_validate_rejects_*`
8. sample-rate mismatch safe → `*_samplerate_mismatch_drops_delays_keeps_gain`
9. channel-count mismatch safe → `*_channel_mismatch_falls_back_off`
10. applied before DOA/spatial (both chains) → `*_runs_before_covariance_and_doa` (polaris), `*_param_applies_at_seam` (live)
11. dead-capsule safe → `*_active_mask_skips_dead_channel`
12. estimator gain → `*_recovers_gain_offsets`
13. estimator polarity → `*_detects_polarity_inversion`
14. estimator delay → `*_detects_integer_delay`

Plus: float32 preservation, no-input-mutation, reset-clears-state, low-confidence flagging,
package-root export parity, JSON camelCase round-trip, param-builds-corrector.

## 11. Test results

```
tests/test_calibration.py ............................. 29 passed
full non-GUI suite (QT_QPA_PLATFORM=offscreen … --ignore-glob='tests/test_gui_*.py')
   → 929 passed in 44.81s   (Phase-0 baseline 900 + 29 new; no regressions)
mypy → Success: no issues found in 66 source files
```
GUI/MainWindow tests excluded locally (documented to hang on this Windows box; CI-verified).

## 12. Known limitations

- **Live (`_process_block`) is not executed headless** (`pragma: no cover`, needs hardware) — verified
  at the shared-seam + source-parity level, not by a covariance-effect test like the polaris path.
- **Polarity & delay need a controlled, co-located stimulus** to be confident; on a diffuse room
  capture they read low-confidence and are withheld. Gain alignment is the reliable output.
- **No GUI yet** — calibration is reachable only via the constructor params / the CLI script (GUI is
  Phase 6, intentionally out of scope here).
- **Profile not yet fanned out** through `BeamEngine._clean_cfg` / `AutoSteerController` (it is set at
  engine construction). Wiring a live setter + the multi-array controllers can follow when the GUI
  needs it; not required for Phase 1.
- **POLARIS standalone 8-ch capture caveat** (`record_clip`/`sd.rec` can fail on WDM-KS) — the script
  supports `--wav` so you can feed a clip recorded through the engine.

## 13. Safe next phase

Phase 2 — **HPF / notch before DFN3**: add a default speech high-pass + a linear notch seam *before*
the post-NR/DFN3 stage in both chains, preserving the byte-identical-off invariant and every existing
default. Phase 1 is self-contained and leaves the pipeline green; Phase 2 can start cleanly.
