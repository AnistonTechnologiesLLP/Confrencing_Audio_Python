# AUDIO FRONT-END — PRODUCTION GAPS

> Source-verified inventory of the requested "production pieces" vs what the codebase
> already has. Written in Phase 0. **Principle: if it already exists, document and verify
> it — do not rebuild.** Status legend: **EXISTS** / **PARTIAL** / **MISSING**.

> **✅ FINAL STATE (Phase 7) — ALL GAPS RESOLVED.** Phases 1–6 filled every genuine gap (calibration,
> pre-NR HPF/notch, placement check, 16 kHz egress, transcription stream, operator workflow); Phase 7
> verified the whole system end-to-end (full non-GUI suite **1034 passed**, mypy clean, safeguards
> proven). Each section's resolution is noted inline below. See
> [reports/audio/final_verification_report.md](../reports/audio/final_verification_report.md) +
> [docs/AUDIO_FRONTEND_DEPLOYMENT_GUIDE.md](AUDIO_FRONTEND_DEPLOYMENT_GUIDE.md). Every addition is
> default-OFF; with all opt-in features off the pipeline is byte-identical to pre-Phase-1.

This codebase is mature (v1.18.0+, ~900 non-GUI tests green). Most of the heavy DSP the
brief lists as "missing" is already present and wired. The genuine gaps are narrower than
the brief assumes, which is the whole point of doing discovery first.

---

## 1. Per-capsule calibration — **MISSING** (genuine gap → Phase 1)

There is **no** per-capsule gain / polarity / delay / phase alignment of the 8 capsules
before DOA and beamforming.

What exists instead:
- **Uniform input preamp** — `conf_pipeline_control/preamp.py` `InputPreamp` applies *one*
  dB gain to **all 8 channels identically** (`process_block`), before the beam. Not per-capsule.
- **Dead-capsule mask** — `geometry.py` `ArrayGeometry.active` + `polaris_beamformer.py`
  `_resolve_active_mask` (L1061) drop a known-dead capsule (e.g. #5) from the solve. On/off only,
  no correction.
- **Bearing ("front") calibration** — `scripts/calibrate_front.py`, `scripts/learn_bearing.py`,
  GUI `live_calib_btn`. This calibrates **which way the array points** (one scalar bearing),
  NOT per-capsule response. Different thing.
- **Directivity calibration** — `tests/test_directivity_calibration.py` exists; this is about
  **simulation** capsule directivity patterns (design/coverage side), not runtime hardware
  alignment. Verify scope in Phase 1; it is not the runtime per-capsule calibration asked for.

Beamforming currently **assumes the 8 capsules are perfectly gain/phase matched** — real MEMS
mismatch (the brief's measurement-first concern) is uncorrected. This is a real, well-scoped gap.

---

## 2. Pipeline order: HPF/notch before DFN3 — **RESOLVED in Phase 2** (was MISSING / WRONG ORDER)

> **Phase 2 outcome:** added an opt-in **pre-NR linear cleanup** stage (a reused `StreamingPeq` with
> HPF + notches) that runs BETWEEN dereverb and post-NR in both chains — default-OFF, byte-identical
> when off, zero added latency. Order proven by test. See
> [reports/audio/phase2_pipeline_order_report.md](../reports/audio/phase2_pipeline_order_report.md)
> and [docs/PRE_NR_CLEANUP_GUIDE.md](PRE_NR_CLEANUP_GUIDE.md). The original finding is kept below.

Verified at source in **both** chains (`polaris_beamformer.py:1820–1876`, `live.py:557–625`):

```
preamp → beam → AEC → transient → dereverb → post-NR(gate|omlsa|wiener|DFN3) → PEQ → AGC → zone-gain → band-limit → voice-gate
```

Findings:
- **No high-pass (HPF) stage exists anywhere in code.** The repo's own `CLAUDE.md` documents a
  `speech-HP` stage in the order, but `speech_band`/`speech-HP` appears **only in CLAUDE.md** —
  there is no implementation (verified by grep across the whole repo). The documented order is
  partly aspirational.
- The **only linear EQ is PEQ** (`peq.py` `StreamingPeq`, RBJ biquads incl. a `highpass` type and
  `bell` for notches). It is **OFF by default** (no bands) and positioned **AFTER post-NR/DFN3**.
- The only default-ON linear filter is the **band-limit low-pass** (~5.6 kHz anti-aliasing,
  `polaris_beamformer.py` `_band_limit`), which runs **near the very end** (after AGC), and only on
  the `process_block` path (not on `live.py`).

So today, predictable rumble/tonal HVAC noise is NOT removed by a cheap linear filter before the
neural/spectral denoiser — the opposite of the brief's measurement-first spine. Phase 2 needs a
default speech HPF + a linear notch capability placed **before** the post-NR/DFN3 seam, in both chains.

---

## 3. Auto live placement check — **RESOLVED in Phase 3** (was MISSING)

> **Phase 3 outcome:** added `conf_pipeline_control/placement.py` (`analyze_placement` +
> `PlacementResult` + `compare_placements`) and `scripts/check_placement.py` — a measured-capture
> GOOD/ACCEPTABLE/BAD check with tonal-peak/rumble/hiss/clipping/imbalance detection, survey mode, and
> notch/HPF suggestions that feed Phase 2's pre-NR stage. Diagnostics only; no pipeline change. See
> [reports/audio/phase3_placement_check_report.md](../reports/audio/phase3_placement_check_report.md)
> + [docs/PLACEMENT_CHECK_GUIDE.md](PLACEMENT_CHECK_GUIDE.md). Original finding kept below.

No operator-facing, pre-meeting placement/noise check exists.

What exists nearby:
- **Coverage simulation** (`conf_pipeline/sim/`, Design tab) — placement quality is *simulated*
  at design time, not measured live from the array.
- **Capsule-health probe** idea is referenced in `CLAUDE.md` (per-capsule RMS via
  `scripts/device_check.py`) but there is no scored GOOD/ACCEPTABLE/BAD placement tool, no tonal-peak
  detector, no rumble/hiss estimate, no survey (Position A/B/C) mode.

Genuine gap. Note: detected tonal peaks here should feed Phase 2's notch suggestions.

---

## 4. Clean mono output / egress — **RESOLVED in Phase 4** (was MOSTLY EXISTS, small gap)

> **Phase 4 outcome:** added `conf_pipeline_control/egress.py` — a named `EgressRouter` (plugs into the
> existing `output_callback`) with a 48 kHz PCM route, a **16 kHz ASR-ready int16** path, a mono WAV
> sink, and an optional `ExternalPcmSink` virtual-mic hook. Reuses the existing int16 WAV format +
> resampler; rejects raw multichannel; no DSP change. See
> [reports/audio/phase4_egress_report.md](../reports/audio/phase4_egress_report.md) +
> [docs/AUDIO_EGRESS_GUIDE.md](AUDIO_EGRESS_GUIDE.md). Original finding kept below.


Processed **clean mono** egress already exists and is verified to emit post-beam/post-cleaner/
post-AGC mono (never raw 8ch):
- **Clean mono WAV recorder** — `live.py` `record_path` (writes post-chain mono 16-bit), and
  `multibeam.py` `MultiTrackRecorder` (per-person + mixed).
- **Clean mono PCM stream** — `live.py` `sounddevice.OutputStream` live monitor; `multikit.py` /
  `multiroom.py` combined-room mono outputs.
- **A/B proof WAVs + metrics** — `ab_capture.py` `write_ab_proof` (raw-beam vs cleaned, dB reduction).
- **Far-end reference capture** (for AEC) — `reference_capture.py` (internal, not egress).

Gap (Phase 4): **16 kHz mono ASR-ready PCM** path, an explicit/named **egress router** abstraction,
and an optional **virtual-mic / OS audio-router** output. Resampling today exists **only** inside
`reference_capture.py` `_resample_to` (scipy `resample_poly`); there is no general 48k/44.1k→16k path.

---

## 5. Transcription-ready clean stream — **RESOLVED in Phase 5** (was MISSING)

> **Phase 5 outcome:** added `conf_pipeline_control/transcription.py` — a `TranscriptionProvider`
> Protocol + `MockTranscriptionProvider`, a deterministic `SpeechChunker` VAD over 16 kHz mono int16, a
> `TranscriptionSession` model, and a `TranscriptionStream` that consumes Phase 4's `drain_asr_pcm16()`
> (`pump_from_egress`). No real ASR vendor, no network by default, no DSP change; raw 8ch rejected. See
> [reports/audio/phase5_transcription_ready_report.md](../reports/audio/phase5_transcription_ready_report.md)
> + [docs/TRANSCRIPTION_STREAM_GUIDE.md](TRANSCRIPTION_STREAM_GUIDE.md). Original finding kept below.

No ASR / transcription / speech-to-text infrastructure of any kind (no provider interface, no
16 kHz path, no chunking for ASR). Confirmed by repo-wide search. Genuine gap.

---

## 6b. Operator GUI / workflow — **RESOLVED in Phase 6**

> **Phase 6 outcome:** added `conf_pipeline_control/operator.py` (`OperatorStatus` — a headless 7-section
> status model: Device / Calibration / Placement / Pipeline / Output / Transcription / Diagnostics) +
> `conf_pipeline_gui/panels/operator.py` (`OperatorStatusPanel`, a read-only widget) + `scripts/
> operator_diagnostics.py` (CLI + JSON/MD export). Surfaced pre-NR in `active_cleaning_stages()`. Honest
> OFF/failed/uncertain states; suggestions never auto-applied; no DSP/default change. See
> [reports/audio/phase6_gui_operator_workflow_report.md](../reports/audio/phase6_gui_operator_workflow_report.md)
> + [docs/AUDIO_OPERATOR_WORKFLOW_GUIDE.md](AUDIO_OPERATOR_WORKFLOW_GUIDE.md).

## 6. Verification reports and tests — **EXISTS, extend per phase**

Strong existing harness:
- **Offline A/B beamformer compare** — `ab_test.py` (`ab_compare`, DI / WNG / talker-leakage).
- **A/B proof (raw vs cleaned)** — `ab_capture.py` + metrics.
- **Commissioning / as-built report** — `conf_pipeline/report.py` `commissioning_report`
  (latency estimate, AEC ERLE, bed-reduction dB, capsule health, bearing, sign-off).
- **Scripts** — `validate_live_enhance.py`, `calibrate_front.py`, `learn_bearing.py`,
  `device_check.py`, etc.
- **~900 non-GUI tests** green (44 s); GUI smoke tests CI-only (MainWindow hangs locally).

Gap: tests + report sections for the NEW Phase 1/2/3/4/5 features.

---

## Existing DSP the brief lists as "the spine" — all already present (do NOT rebuild)

| Stage | Status | Where |
|------|--------|-------|
| 8ch capture | EXISTS | `live.py` `_open` (sounddevice InputStream, by-name device) |
| DOA (SRP-PHAT, full 360°) | EXISTS | `doa.py` |
| Beamforming (delaysum / fracdelay / superdirective / MVDR / RTF-MVDR) | EXISTS | `beamformer.py`, `polaris_beamformer.py`, `rtf_mvdr.py` |
| Null steering (LCMV, auto-null, seat/exclusion) | EXISTS | `polaris_beamformer.py` `compose_nulls`, `autosteer.py`, `live.py` `_bin_weights` |
| AEC (partitioned-block NLMS + far-end ref) | EXISTS, default OFF | `streaming_aec.py`, `reference_capture.py` |
| Transient suppression | EXISTS, default OFF | `transient.py` |
| Dereverb (Lebart/Habets) | EXISTS, default OFF | `streaming_cleaner.py` `StreamingDereverb` |
| Band-limit (anti-alias LP ~5.6 kHz) | EXISTS, default ON | `polaris_beamformer.py` `_band_limit` |
| DFN3 / OM-LSA / Wiener / gate post-NR | EXISTS, default OFF | `deepfilter_cleaner.py`, `streaming_cleaner.py`, `polaris_beamformer.py` |
| Voice gate (VAD) | EXISTS, default OFF | `voice_gate.py` |
| AGC + limiter (target loudness) | EXISTS, default OFF | `agc.py` `TargetLoudnessAgc` |

**Net:** Phases 1, 2, 3, 5 are genuine gaps; Phase 4 is a small extension; the DSP spine and
verification harness already exist and must be preserved, not rewritten.
