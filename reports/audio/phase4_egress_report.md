# Phase 4 — Clean Mono Output / Egress Report

**Goal:** a named, production-safe egress layer for the processed clean mono — engine-rate (48 kHz)
PCM, a 16 kHz ASR-ready int16 path, a mono WAV sink, and an optional virtual-mic hook. Verify and wrap
the existing outputs; fill only the real gaps. No DSP-default change.

Status: **COMPLETE.** 20 new tests green; full non-GUI suite 974 → **994 passed**; mypy clean;
egress flow verified end-to-end (48k + 16k + WAV; raw 8ch rejected).

---

## 1. What already existed and was not rebuilt
- **Clean mono WAV recorder** — `live.py` `record_path` (mono int16). Untouched.
- **PCM monitor** — `live.py` `sounddevice.OutputStream` (`_out_stream`/`_cb_output`). Untouched.
- **A/B proof** — `ab_capture.write_ab_proof` (`ab_raw.wav`/`ab_clean.wav`, mono int16). Verify-only.
- **Multibeam/Multiroom recorders** — per-person/combined mono. Untouched.
- **Resampler** — `reference_capture._resample_to` (scipy polyphase + numpy fallback). **Reused.**
- **Emit seam** — `PolarisBeamformer._emit` → `output_callback`. **Reused** as the router's feed.
- **int16 WAV format** — `np.clip(x,-1,1)*32767 → "<i2"`. **Reused** verbatim.

## 2. What was actually missing
A **named egress API** and a **16 kHz ASR-ready** path. The existing outputs are device/recorder-bound;
there was no single object a downstream consumer (Phase 5 ASR, a virtual mic) could be handed, and no
48k/44.1k → 16k int16 conversion for transcription. Those are the only gaps Phase 4 fills.

## 3. Files changed
New:
- `conf_pipeline_control/egress.py` — `EgressRouter`, `WavMonoSink`, `ExternalPcmSink` (Protocol),
  `to_pcm16`/`pcm16_bytes`/`resample_mono`, `EgressError`.
- `tests/test_egress.py` — 20 hardware-free tests.
- `docs/AUDIO_EGRESS_GUIDE.md`, `reports/audio/phase4_egress_report.md`.

Edited (additive, non-behavioral):
- `conf_pipeline_control/__init__.py` — export the egress API.
- `docs/AUDIO_FRONTEND_PHASE_TRACKER.md`, `docs/AUDIO_FRONTEND_PRODUCTION_GAPS.md`.

**No live-DSP source touched.** `polaris_beamformer.py` / `live.py` are unchanged in Phase 4 — the
router plugs into the existing `output_callback`.

## 4. Egress router design
`EgressRouter(sample_rate, *, wav_path=None, asr_rate=16000, max_buffer_seconds=30, sinks=())`:
- `push(mono, sample_rate=None)` — validates 1-D mono (squeezes `(N,1)`, **rejects `(N,>1)`**), stores
  the latest block, writes the WAV sink, appends to a bounded engine-rate buffer, fans int16 to sinks.
  Cheap ⇒ realtime-safe.
- `latest_mono()` / `latest_pcm16()` — the 48 kHz route. `drain_asr_array()` / `drain_asr_pcm16()` —
  the 16 kHz route (resample buffer → int16, clear). `pending_seconds()`, `reset()`, `close()`,
  `frames_pushed`, `algorithmic_latency_ms`.

## 5. Clean mono source point
The router is fed the engine's **final processed mono** — `PolarisBeamformer.process_block`'s return
(post AGC/band-limit/voice-gate), delivered through `_emit` → `output_callback`. Wire it as
`PolarisBeamformer(output_callback=router.push)`; or push `process_block(...)`'s return manually.

## 6. WAV path status
Existing `record_path` recorder: unchanged, still primary for the standalone live path. Added
`WavMonoSink` (mono 16-bit, same format) so the router/callback path can also record. Verified: the
written WAV is exactly the pushed processed mono, reads back bit-for-bit.

## 7. PCM monitor path status
Unchanged (`OutputStream` monitor). The router additionally exposes engine-rate `int16` PCM
(`latest_pcm16()`) for consumers that want bytes rather than the sounddevice monitor.

## 8. 48 kHz output behaviour
Engine-rate passthrough: `latest_mono()` (float32) / `latest_pcm16()` (int16 bytes). **Zero algorithmic
latency** (format conversion only). Clip-safe (`to_pcm16` saturates at ±32767, never wraps).

## 9. 16 kHz ASR-ready behaviour
`drain_asr_*` resamples the buffered engine-rate mono to 16 kHz (reusing `_resample_to`) and converts
to little-endian `int16` PCM, clearing the buffer. Verified: 1 s @ 48k → exactly 16000 samples; 44.1k →
16k correct; a 1 kHz tone's dominant frequency survives; silence stays silence; dtype `int16`.

## 10. Optional virtual mic hook status
`ExternalPcmSink` (a `runtime_checkable` Protocol: `write(pcm16, sample_rate)` / `close()`) is the
integration seam, fanned via `sinks=[…]`. **Disabled by default. No OS virtual audio driver is
installed** — the guide documents wiring a concrete sink to BlackHole / VB-CABLE / PipeWire / JACK.
Phase does not fail without a concrete impl (it's optional, tested via a spy sink).

## 11. Processed-only safeguards
`push` raises `EgressError` on any `(N, >1)` block ⇒ raw 8-channel audio can never become the clean
output. By construction the `output_callback` carries post-AGC processed mono. Guarded by
`test_router_rejects_raw_multichannel_as_clean_output` + `test_resample_rejects_multichannel`.

## 12. Latency impact
- 48 kHz route: **0** algorithmic latency.
- 16 kHz route: resampler group delay (a few ms) + consumer buffering (`pending_seconds()`, reported).
- Engine `estimated_latency_ms`: **unchanged** (egress is downstream) — verified by
  `test_engine_latency_unchanged_by_egress`.

## 13. Test results
```
tests/test_egress.py .................... 20 passed
full non-GUI suite (offscreen, --ignore-glob='tests/test_gui_*.py') → 994 passed in 40.8s
   (Phase-3 baseline 974 + 20 new; zero regressions)
mypy → Success: no issues found in 69 source files
```
Covers all 15 required cases (+ extras): mono accept / raw-8ch reject / no-op safe, 48k & 16k
shape+dtype, 48→16k length, int16 clip-saturate, silence→silence, tone-survives-resample, reset,
WAV-uses-processed-mono + read-back, A/B-proof-format consistency, engine-latency-unchanged,
output-callback integration, export parity. End-to-end demo: 48000 frames → 16000-sample ASR PCM + a
valid 1ch/16-bit/48 kHz WAV.

## 14. Known limitations
- The router is a programmatic API (library), not an operator CLI/GUI — surfacing is Phase 6.
- 16 kHz drain resamples per-drain buffer; cross-drain edges are not stitched (fine for ASR; Phase 5
  adds VAD/chunking on top). A WAV write inside `push` does disk I/O on the audio thread if you wire
  `push` as the realtime callback (same as the existing live recorder); the documented alternative is
  to drain the engine's output queue on a consumer thread.
- No real ASR/transcription provider (Phase 5); `ExternalPcmSink` for a virtual mic is a seam, not a
  bundled driver.

## 15. Safe next phase
Phase 5 — **Transcription-ready clean stream**: a `TranscriptionProvider` interface + a mock provider +
VAD/chunking, consuming `drain_asr_pcm16()` (already verified to be clean, processed, 16 kHz int16).
Phase 4 added a standalone egress layer and changed no DSP default.
