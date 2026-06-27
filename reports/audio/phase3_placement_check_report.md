# Phase 3 — Auto Live Placement Check Report

**Goal:** a measurement-driven, operator-facing placement check — analyze a short room-noise capture,
score GOOD/ACCEPTABLE/BAD with reasons + recommendations, detect tonal peaks/rumble/hiss/clipping/
channel imbalance, and emit notch/HPF suggestions that feed Phase 2's pre-NR stage. **Diagnostics
only — no change to the live audio pipeline.**

Status: **COMPLETE.** 25 new tests green; full non-GUI suite 949 → **974 passed**; mypy clean;
CLI verified end-to-end (GOOD vs BAD + survey).

---

## 1. What already existed and was NOT rebuilt
- **`ab_test.py`** (`ab_compare`, DI/WNG/talker-leakage), **`ab_capture.py`** (raw-vs-clean proof),
  **`report.py`** (commissioning) — beamformer/cleaning measurement; complementary, untouched.
- **`conf_pipeline/sim/`** + `test_room_background.py` — *design-time* placement optimization / room-
  noise modeling for simulation. NOT a live measured-capture check; left intact (this is its live
  counterpart).
- **`doa.band_indices`** — reused for band selection. **`record_clip`** (`ab_test.py`) — reused for
  live capture in the CLI. **`pre_nr.build_pre_nr_bands`** — reused for the suggestion conversion.

## 2. What was actually missing
Any *live, measured* placement/noise check. Placement quality was only ever *simulated* at design
time; there was no way for an operator to point the array, record room noise, and get a scored
GOOD/ACCEPTABLE/BAD verdict with tonal-peak detection. Genuine gap, now filled.

## 3. Files changed
New:
- `conf_pipeline_control/placement.py` — `PlacementResult`, `analyze_placement`, `compare_placements`,
  `PlacementError`, the metric/scoring core + a numpy Hann-Welch power spectrum.
- `tests/test_placement_check.py` — 25 hardware-free tests.
- `scripts/check_placement.py` — operator CLI (`--wav`/`--device`/`--compare`, `--json`/`--markdown`).
- `docs/PLACEMENT_CHECK_GUIDE.md`, `reports/audio/phase3_placement_check_report.md`.

Edited (additive, non-behavioral):
- `conf_pipeline_control/__init__.py` — export the placement API.
- `docs/AUDIO_FRONTEND_PHASE_TRACKER.md`, `docs/AUDIO_FRONTEND_PRODUCTION_GAPS.md`.

**No live-DSP source touched.** `polaris_beamformer.py` / `live.py` are unchanged in Phase 3.

## 4. Metrics implemented
From a `(N, channels)` capture (numpy, lazy; scipy not required):
- **Total noise RMS** (time-domain) → `noiseRmsDbfs`.
- **Speech-band noise** (300–3400 Hz) → `speechBandNoiseDbfs`.
- **Low-frequency rumble** (20–200 Hz) → `lowFrequencyRumbleDbfs`.
- **Broadband hiss** (1000–8000 Hz, capped at Nyquist) → `broadbandHissDbfs`.
- **Tonal peak detection** (50–1000 Hz): prominence ≥ 9 dB above the band median, deduped, top-6.
- **Clipping risk**: fraction of `|sample| ≥ 0.99` over a threshold.
- **Channel imbalance**: per-capsule RMS in dB vs the median capsule.
- **Local hotspot**: heuristic (one loud capsule, reinforced by tones/hiss).

Spectrum is a Hann-windowed, 50%-overlap, Parseval-scaled averaged power spectrum (band sum ≈ band
mean-square; validated by `test_band_level_matches_known_tone_power`). Band powers/levels are reported;
**scoring uses bandwidth-normalised density ratios + peak prominence** so results are gain- and
bandwidth-independent (a flat quiet room is GOOD regardless of record level).

## 5. Scoring formula
Deterministic, no ML. Start 100, subtract: rumble −12/−30 (warn/bad), each tone −12 (cap −40), hiss
−8/−20, imbalance −8/−20, clipping −35, very-loud-room (≥ −35 dBFS) −15. Clamp 0–100. Each penalty
appends a human-readable reason + recommendation.

## 6. Status thresholds
`score ≥ 85 → GOOD`, `60–84 → ACCEPTABLE`, `< 60 → BAD`.

## 7. Tonal peak detection method
On the averaged power spectrum within 50–1000 Hz: convert to dB, take the band median as the local
floor, keep local maxima that exceed it by ≥ `TONE_PROMINENCE_DB` (9 dB), sort by prominence, dedupe
within 12 Hz, cap at 6. Reported to bin resolution (~a few Hz at the 8192-pt analysis). DC is ignored;
the range is bounded so broadband hiss and grating-lobe artifacts don't masquerade as tones.

## 8. HPF/notch suggestion behaviour
`detectedTonesHz` → `notchSuggestionsHz`; an HPF (`hpfSuggestionHz` = 120 Hz) is suggested only when
rumble is flagged. `PlacementResult.to_pre_nr_bands()` converts these to Phase 2 pre-NR bands (reusing
`build_pre_nr_bands`). **Nothing is auto-applied**, nothing is a global default, and the guide states
the tones must be re-measured per room. The live pipeline is untouched unless the operator opts in.

## 9. Placement survey behaviour
`compare_placements(results)` returns the highest-scoring result (survey winner). The CLI `--compare
*.json` loads saved results, prints a label/status/score table, and names the recommended position.

## 10. CLI / operator flow
```
python scripts/check_placement.py --device 7 --seconds 10 --label "Table center"
python scripts/check_placement.py --wav room_noise_8ch.wav --out reports/audio/placement_center.json
python scripts/check_placement.py --compare reports/audio/placement_*.json
```
Prints status + score + per-band levels + detected tones + reasons + recommendations + the opt-in
pre-NR suggestion. End-to-end verified: a clean synthetic room → GOOD 100/100; a 60 Hz rumble + 140 Hz
tone room → BAD 31/100 (tones detected at 59/141 Hz); survey picks the clean one.

## 11. Test results
```
tests/test_placement_check.py ......................... 25 passed
full non-GUI suite (offscreen, --ignore-glob='tests/test_gui_*.py') → 974 passed in 41.6s
   (Phase-2 baseline 949 + 25 new; zero regressions)
mypy → Success: no issues found in 68 source files
```
Covers all 18 required cases: GOOD/ACCEPTABLE/BAD, rumble/tone/hiss/clip/imbalance/hotspot detection,
notch suggestions, mono/empty/wrong-channel/wrong-rate safety, JSON round-trip, survey pick-best,
to-pre-NR conversion, determinism, and pipeline-default independence — plus a normalization sanity
check and package-root export parity.

## 12. Known limitations
- Diagnoses **noise/placement**, not full acoustics (no RT60/echo-path measurement).
- Tone frequency resolved to the analysis bin (~a few Hz) — fine for a notch, not a tuner.
- The local-hotspot output is a **heuristic**, not a verdict; benign causes exist.
- Requires room **noise without speech** — speech invalidates the noise-band metrics (operator-driven).
- Live single-shot capture relies on `record_clip`, which can fail on POLARIS WDM-KS standalone; the
  CLI therefore also accepts `--wav` (a clip recorded through the engine).
- No GUI yet (CLI only) — surfacing in the operator UI is Phase 6.

## 13. Safe next phase
Phase 4 — **Clean mono output / egress**: build reliable clean-mono WAV/PCM egress (16 kHz ASR-ready
path, named egress router, optional virtual mic). Phase 3 added a standalone diagnostics layer and
changed no pipeline default, so Phase 4 starts from a green, unchanged pipeline.
