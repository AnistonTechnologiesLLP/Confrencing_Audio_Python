# Phase 2 — Pipeline Order (HPF/notch before DFN3) Report

**Goal:** make the measurement-first order available + test-proven — add a **pre-NR linear cleanup**
stage (speech HPF + notches) that runs BEFORE the post-NR/DFN3 denoiser in **both** DSP chains.
Default-OFF, bit-exact pass-through when off, opt-in HVAC preset. Phase-locked to ordering only.

Status: **COMPLETE.** 20 new tests green; full non-GUI suite 929 → **949 passed**; mypy clean;
order proven (post-NR receives HPF-filtered audio).

---

## 1. What already existed and was NOT rebuilt
- **`StreamingPeq`** (`peq.py`) — RBJ-biquad cascade with `highpass` + `bell` (and shelves/lowpass),
  exact zero-latency IIR (`sosfilt`, float64 state), bit-exact pass-through when no bands, the standard
  `process/reset/set_bands` contract. **Reused as the pre-NR stage** — no new filter math.
- **Existing post-NR PEQ** (the tone-shaping PEQ after the denoiser) — left exactly where it is.
- **All denoisers** (DFN3 / OM-LSA / Wiener / gate), dereverb, AEC, AGC, band-limit, voice-gate,
  Phase-1 calibration — untouched and not retuned.

## 2. What was actually missing
A pre-denoise linear stage. Phase 0 verified there was **no HPF anywhere** in code (the `speech-HP`
in CLAUDE.md was never implemented), and the only notch-capable filter (PEQ) ran **after** the
denoiser and was off by default. So predictable rumble/tonal HVAC was not removed before the
neural/spectral stage.

## 3. Files changed
New:
- `conf_pipeline_control/pre_nr.py` — `hpf_band`, `notch_band`, `build_pre_nr_bands`, `office_ac_preset`.
- `tests/test_pre_nr_filter.py` — 20 hardware-free tests.
- `docs/PRE_NR_CLEANUP_GUIDE.md`, `reports/audio/phase2_pipeline_order_report.md`.

Edited (minimal, additive):
- `polaris_beamformer.py` — `pre_nr`/`pre_nr_bands` ctor params; `self._pre_nr_peq` built next to
  `_peq` in `_setup_runtime`; one apply line in `process_block` (between dereverb and post-NR); reset
  next to `_peq.reset()` in `reset_transient`.
- `live.py` — same params/storage/attr; built in `_build_post_nr`; one apply line in `_process_block`.
- `conf_pipeline_control/__init__.py` — export the pre-NR builders.
- `docs/AUDIO_FRONTEND_PHASE_TRACKER.md`, `docs/AUDIO_FRONTEND_PRODUCTION_GAPS.md`.

## 4. Old pipeline order (both chains)
```
preamp → calibration → beam → AEC → transient → dereverb → post-NR(DFN3/OM-LSA/Wiener/gate) → PEQ → AGC → zone → band-limit → voice-gate
```
(No HPF anywhere; PEQ — the only notch-capable filter — sat AFTER the denoiser.)

## 5. New pipeline order (both chains)
```
preamp → calibration → beam → AEC → transient → dereverb
       → PRE-NR HPF/notch (NEW)  → post-NR(DFN3/OM-LSA/Wiener/gate)
       → PEQ → AGC → zone → band-limit → voice-gate
```
Cheap linear cleanup now precedes the denoiser; the existing post-NR PEQ stays for tone-shaping.

## 6. Exact insertion point in `process_block` (polaris_beamformer.py)
Immediately after the dereverb block, immediately before the post-NR block:
```python
    mono = self._dereverb.process(mono, self._noise_gate)   # (existing) dereverb
    if self._pre_nr_peq is not None:
        mono = self._pre_nr_peq.process(mono)               # ← NEW: pre-NR HPF + notches
    ...
    mono = self._post_nr.process(mono, self._noise_gate)    # (existing) DFN3/OM-LSA/Wiener/gate
```

## 7. Exact insertion point in `_process_block` (live.py)
Identical position and stage:
```python
    out = self._dereverb.process(out, False)                # (existing) dereverb
    if self._pre_nr_peq is not None:
        out = self._pre_nr_peq.process(out)                 # ← NEW: pre-NR HPF + notches
    ...
    out = self._post_nr.process(out, False)                 # (existing) denoiser
```

## 8. Existing PEQ reused or new stage?
**Reused** `StreamingPeq` (a second instance, `_pre_nr_peq`). No duplicate filter math. The existing
post-NR `_peq` is a distinct instance kept in its original position (verified by
`test_polaris_pre_nr_and_existing_peq_coexist_as_distinct_stages`).

## 9. HPF behaviour
A 2nd-order RBJ high-pass (`build_pre_nr_bands(hpf_hz=…)` → a `highpass` band, default Q=0.707
Butterworth). Removes sub-speech rumble; preserves the speech band. Verified: a 50 Hz tone at HPF
120 Hz is cut to < 0.4× while a 1 kHz tone stays > 0.9×.

## 10. Notch behaviour
A narrow negative-gain `bell` per tone (`notch_band` / `build_pre_nr_bands(notches=…)`, default Q=8,
depth 12 dB). Verified: a 140 Hz tone is cut to < 0.5× while a 500 Hz tone stays > 0.85×; multiple
notches each attenuate their own tone.

## 11. Default-off / opt-in behaviour
`pre_nr=False` by default ⇒ `_pre_nr_peq` is a `StreamingPeq` with no bands ⇒ **bit-exact pass-through
(same object)** ⇒ the whole pipeline is byte-identical (the 929 prior tests still pass unchanged).
Invalid bands (0 Hz / negative-Q / unknown type) are dropped safely to a no-op. DFN3 and dereverb stay
OFF by default — Phase 2 does not enable any neural/heavy stage.

## 12. Preset behaviour
`office_ac_preset()` returns HPF 120 Hz + notches at 102/140/177 Hz — **a measured-room EXAMPLE you opt
into**, NOT a global default and NOT applied unless passed to `pre_nr_bands=…`. Room-specific tones live
only in presets / user config; re-measure per room (Phase 3's placement check will report the actual
tones). Documented in `docs/PRE_NR_CLEANUP_GUIDE.md`.

## 13. Test results
```
tests/test_pre_nr_filter.py ............ 20 passed
full non-GUI suite (offscreen, --ignore-glob='tests/test_gui_*.py') → 949 passed in 39.6s
   (Phase-1 baseline 929 + 20 new; zero regressions)
mypy → Success: no issues found in 67 source files
```
Order proof: `test_polaris_pre_nr_runs_before_post_nr_in_process_block` puts a recorder in the post-NR
slot and shows it receives HPF-attenuated audio (energy < 0.25× vs the no-pre-NR engine).

## 14. Latency impact
**Zero.** The pre-NR stage is IIR biquads (no lookahead, no STFT framing). `estimated_latency_ms` sums
`_F` only over `(aec, dereverb, post_nr)` + the band-limit FIR; the PEQ biquad has no `_F` and isn't in
that tuple. Verified equal on/off by `test_polaris_pre_nr_does_not_change_latency_estimate`.

## 15. Known limitations
- Live `_process_block` is `pragma: no cover (needs hardware)` — the live pre-NR is verified by the
  device-free `_build_post_nr` build + the HPF actually filtering + source-parity with the
  headless-tested polaris order proof.
- No GUI control yet and no live `set_pre_nr_bands` setter (deferred to Phase 6); the stage is set at
  engine construction. `BeamEngine`/`AutoSteerController` fan-out also deferred.
- `active_cleaning_stages()` / the commissioning report don't list the pre-NR stage yet (kept output
  byte-identical; a one-line surfacing can be added with the GUI in Phase 6).
- Pre-NR config is runtime-only (matches the `peq_bands`/`post_nr` convention); not persisted to the
  camelCase config schema, so no `CONFIG_VERSION`/TS-parity change was needed.

## 16. Safe next phase
Phase 3 — **Auto live placement check**: a pre-meeting room-noise check that scores GOOD/ACCEPTABLE/BAD
and **detects tonal peaks** — which become the notch frequencies this Phase 2 pre-NR stage consumes
(`build_pre_nr_bands(notches=detected_tones)`). Phase 2 leaves the pipeline green and default-unchanged.
