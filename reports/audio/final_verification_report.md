# Final Verification Report — POLARIS Audio Front-End Hardening (Phases 0–7)

## 1. Executive summary
A 7-phase, measurement-first hardening of the POLARIS 8-MEMS audio front-end is **complete and green**.
Every new capability is **default-OFF and additive** — with all opt-in features disabled the pipeline is
byte-identical to the pre-Phase-1 behaviour. Full non-GUI suite: **1034 passed** (900 baseline + 134 new);
mypy clean (71 files); CLI + end-to-end demo verified; all processed-only safeguards hold. No DSP mode
was removed, no default changed, nothing is forced on, and no suggestion is auto-applied.

## 2. Original problem
The array was fed largely raw into DOA/beamforming (no per-capsule alignment), neural cleanup could run
before cheap linear cleanup, there was no live placement check, no named clean-mono egress / 16 kHz ASR
path, no transcription layer, and no consolidated operator status — despite a mature DSP core.

## 3. What already existed (verified, not rebuilt)
DOA (SRP-PHAT), 5 beam modes, LCMV null steering, AEC, dereverb, DFN3/OM-LSA/Wiener/gate post-NR,
target-loudness AGC + limiter, band-limit, voice-gate, clean-mono WAV/PCM/A-B-proof outputs, the
commissioning report, the resampler, and the PySide6 GUI. All retained.

## 4. Phase 1 — Per-capsule calibration
`conf_pipeline_control/calibration.py` — `CalibrationProfile` (camelCase JSON) + `CapsuleCalibrator`
(gain/polarity/integer-delay, bit-exact no-op when neutral, dead-capsule-safe) + `estimate_calibration`
(honest confidence) + `CalibrationHost`, wired **before DOA/beam/null** in both chains. `scripts/
calibrate_capsules.py`. 29 tests.

## 5. Phase 2 — Pre-NR HPF/notch before DFN3
`conf_pipeline_control/pre_nr.py` (band builders + `office_ac_preset`) + a reused `StreamingPeq`
instance inserted **between dereverb and post-NR** in both chains. Default-OFF, **zero latency**, order
proven by test. 20 tests.

## 6. Phase 3 — Auto live placement check
`conf_pipeline_control/placement.py` — `analyze_placement` (rumble/speech/hiss/tones/clipping/imbalance/
hotspot, gain-independent scoring → GOOD/ACCEPTABLE/BAD) + `PlacementResult` + `compare_placements` +
`scripts/check_placement.py`. Diagnostics only; suggestions feed Phase 2. 25 tests.

## 7. Phase 4 — Clean mono egress + 16 kHz ASR
`conf_pipeline_control/egress.py` — `EgressRouter` (plugs into `output_callback`): 48 kHz PCM, **16 kHz
ASR-ready int16**, WAV sink, `ExternalPcmSink` virtual-mic seam. Rejects raw multichannel. 20 tests.

## 8. Phase 5 — Transcription-ready stream
`conf_pipeline_control/transcription.py` — `TranscriptionProvider` Protocol + `MockTranscriptionProvider`
+ deterministic `SpeechChunker` VAD + `TranscriptionSession` + `TranscriptionStream.pump_from_egress`.
Mono-16k-int16 only; no network by default. 22 tests.

## 9. Phase 6 — Operator workflow / diagnostics
`conf_pipeline_control/operator.py` — `OperatorStatus` (7-section status + JSON/MD export) +
`conf_pipeline_gui/panels/operator.py` (`OperatorStatusPanel`) + `scripts/operator_diagnostics.py`;
pre-NR surfaced in `active_cleaning_stages()`. Read-only; no auto-apply. 18 model + 3 panel tests.

## 10. Final pipeline order (verified from source, both chains)
```
capture → preamp → per-capsule calibration → DOA/beamform/null
  → AEC → transient → dereverb → pre-NR HPF/notch → post-NR(DFN3/OM-LSA/Wiener/gate)
  → PEQ → AGC/limiter → [zone-gain] → band-limit → voice-gate
  → processed clean MONO → EgressRouter (48k PCM / 16k ASR int16 / WAV / external sink)
  → TranscriptionStream → provider
```
True-order note: a post-AGC `zone-gain` trim (default off) sits between AGC and band-limit; the
`live.py` path has no separate band-limit FIR (the steered `process_block` path does). Both documented,
not faked.

## 11. Default-on / default-off table
| Feature | Default | Why |
|---|---|---|
| Per-capsule calibration | OFF | requires a valid measured profile |
| Pre-NR HPF/notch | OFF | room-specific, opt-in |
| Office-AC preset | OFF | example measured-room preset only |
| AEC / transient / dereverb | OFF | opt-in per room |
| post-NR (DFN3/OM-LSA/Wiener/gate) | OFF | neural/spectral cleanup, downstream + optional |
| PEQ / AGC / voice-gate | OFF | opt-in |
| Band-limit (~5.6 kHz LP) | **ON** | fixed array-physics anti-alias (the only default-on stage) |
| Placement check | Manual | diagnostic, not live DSP |
| Egress router / Transcription | Optional | consumer/provider seam |
| Real ASR vendor / Virtual mic | Not bundled | no network / no driver by default |

## 12. End-to-end operator flow
Connect + verify 8ch → placement check (move if BAD) → calibrate → enable calibration on a valid profile
→ optionally apply measured HPF/notch → keep DFN3/dereverb optional → verify 48 kHz egress → verify
16 kHz ASR stream → export diagnostics. Full detail in `docs/AUDIO_FRONTEND_DEPLOYMENT_GUIDE.md`.

## 13. Test commands
```bash
pytest -q tests/test_calibration.py tests/test_pre_nr_filter.py tests/test_placement_check.py \
          tests/test_egress.py tests/test_transcription_stream.py tests/test_operator_workflow.py
QT_QPA_PLATFORM=offscreen pytest -q tests/test_gui_operator.py
QT_QPA_PLATFORM=offscreen pytest -q --ignore-glob='tests/test_gui_*.py' -p no:cacheprovider
mypy
scripts/{calibrate_capsules,check_placement,operator_diagnostics}.py --help
```

## 14. Test results
- 7 phase test files + GUI panel probe → **137 passed** (29 + 20 + 25 + 20 + 22 + 18 + 3).
- Full non-GUI suite → **1034 passed in ~43 s** (900 baseline + 134 new; **zero regressions**).
- mypy → **Success, no issues in 71 source files**.
- CLI `--help` → all 3 OK. End-to-end demo (processed mono → egress → ASR → mock) → 1 chunk, mock
  transcript, **0 network calls**.
- MainWindow GUI tests: **CI-only locally** (documented headless hang); single-panel probe runs locally.

## 15. Latency summary
- **Calibration:** + max `delaySamples` (0 by default; typically 0–few samples).
- **Pre-NR HPF/notch:** **0** (IIR biquads, no lookahead).
- **Engine `estimated_latency_ms`** (honest, summed): input block + freq-domain beam STFT frame (freq
  modes) + ~one frame per engaged STFT cleaner (post-NR/dereverb ≈ 12 ms each) + band-limit FIR group
  delay. Example: delaysum @44.1k, block 1411, band-limit on → ~32.7 ms. DFN3 adds ~40–60 ms when on.
- **Egress:** 48 kHz route = 0; 16 kHz route = small resampler group delay + consumer buffering
  (`pending_seconds()`).
- **Transcription chunker:** buffers up to `max_chunk_ms` (8 s default) / `speech + hangover_ms` before
  a chunk is emitted. Reported honestly; nothing hidden.

## 16. Processed-only safeguards (verified)
- **No raw 8ch as clean output** — `EgressRouter.push` rejects `(N,>1)` (test + live guard).
- **No raw 8ch to transcription** — `TranscriptionStream.push_pcm16` rejects multichannel / wrong rate /
  float (test + live guard).
- **No auto-applied suggestions** — placement `to_pre_nr_bands()` is opt-in; operator model
  `autoApplied=false`.
- **No default-forced feature** — every opt-in stage default-OFF; byte-identical when off.
- **No network by default** — mock provider `network_calls == 0`.
- **No global room tones** — tones live in user config / the opt-in preset only.

## 17. Known limitations
- Audio "fencing" is attenuation, not a perfect wall; placement is noise/placement only (no RT60).
- Calibration gain alignment is robust; polarity/delay need a controlled stimulus.
- Lightweight energy VAD; mock ASR provider only (no bundled vendor).
- Virtual mic is an external seam (no driver); operator surface is read-only status + CLI/panel (no new
  live-control wiring into the live panel).
- Full MainWindow GUI verified in CI; live single-shot capture can fail on POLARIS WDM-KS (scripts also
  accept `--wav`).

## 18. Regression risks
- Two parallel DSP chains (`process_block` / `_process_block`) — every new stage landed in both; the
  byte-identical-off suite + 900 baseline tests guard it (all green).
- Live `_process_block` is `pragma: no cover` (hardware) — verified by seam + source-parity.
- Runtime-only config for the new live stages (no schema/TS-parity change) — no migration risk.

## 19. Safe next recommendations (optional, NOT in scope here)
- Wire live controls for calibration/pre-NR/placement-apply into the existing live panel (a larger,
  reviewed GUI change).
- Add an adaptive-VAD option to transcription; a concrete `ExternalPcmSink` for a chosen virtual mic.
- Surface calibration low-confidence + the pre-NR stage in the commissioning report.
- Optional measured-R MVDR / live null-depth capture (pre-existing backlog).

## 20. Commit recommendation
Recommended (run only when explicitly asked):
```
feat(control): harden POLARIS audio front-end production workflow
```
Body should note Phases 1–6: per-capsule calibration before DOA/beam/null; pre-NR HPF/notch before the
denoiser; live placement check; clean-mono egress + 16 kHz ASR path; transcription-ready stream + mock
provider; operator diagnostics surface. All default-OFF, byte-identical when off; 134 new tests; full
suite 1034 green; mypy clean. **No commit performed.**
