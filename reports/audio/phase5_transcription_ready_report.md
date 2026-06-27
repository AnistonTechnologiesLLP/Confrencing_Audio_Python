# Phase 5 — Transcription-Ready Clean Stream Report

**Goal:** a pluggable transcription layer over Phase 4's 16 kHz ASR-ready egress — a provider
interface, a deterministic VAD/chunker, a session model, a mock provider, and clean egress
integration. Interface/plumbing only — no real ASR vendor, no network by default, no UI, no DSP change.

Status: **COMPLETE.** 22 new tests green; full non-GUI suite 994 → **1016 passed**; mypy clean;
end-to-end demo verified (processed mono → egress → chunk → mock transcript, 0 network calls).

---

## 1. What already existed and was not rebuilt
- **Phase 4 `EgressRouter.drain_asr_pcm16()`** — the 16 kHz mono int16 source. **Reused** as the input.
- **`Protocol` interface convention** — `HwGain` (preamp), `ExternalPcmSink` (egress). **Mirrored**.
- **VAD utilities** — `octovox_monitor.speech_gate` (adaptive floor) + `multikit.SpeechPresenceScorer`.
  Offered as the pluggable `speech_fn`; not hard-coupled (avoids importing the HTTP-bridge module).
- All DSP / calibration / pre-NR / placement — untouched.

## 2. What was actually missing
Any ASR/transcription path: a provider interface, a session model, a VAD/chunker, and the glue from the
16 kHz egress to a transcriber. Genuine gap (confirmed: zero ASR code in the repo).

## 3. Files changed
New:
- `conf_pipeline_control/transcription.py` — `TranscriptionProvider` (Protocol), `MockTranscriptionProvider`,
  `TranscriptionSession`, `AudioChunk`, `TranscriptResult`, `SpeechChunker`, `TranscriptionStream`,
  `TranscriptionError`.
- `tests/test_transcription_stream.py` — 22 hardware-free tests.
- `docs/TRANSCRIPTION_STREAM_GUIDE.md`, `reports/audio/phase5_transcription_ready_report.md`.

Edited (additive, non-behavioral):
- `conf_pipeline_control/__init__.py` — export the transcription API.
- `docs/AUDIO_FRONTEND_PHASE_TRACKER.md`, `docs/AUDIO_FRONTEND_PRODUCTION_GAPS.md`.

**No live-DSP source touched.** `polaris_beamformer.py` / `live.py` / `egress.py` unchanged in Phase 5.

## 4. Transcription stream design
`TranscriptionStream(provider, *, sample_rate=16000, **vad_kwargs)`:
`start(metadata=…)` → `push_pcm16(pcm16, sample_rate=16000)` / `pump_from_egress(router)` → `stop()` →
`reset()`. It validates the input (§10), runs the `SpeechChunker`, forwards completed chunks to the
provider, tracks the session, and surfaces provider errors as `TranscriptionError`.

## 5. Provider interface
`TranscriptionProvider` is a `@runtime_checkable Protocol` with sync `start_session(session)`,
`send_audio_chunk(chunk)`, `stop_session() -> TranscriptResult` (matches the repo's sync/threaded style;
no asyncio). Models: `TranscriptionSession` (id/rate/channels/encoding/started_at/stopped_at/
chunks_sent/duration_seconds/status[idle|running|stopped|error]/metadata), `AudioChunk`
(pcm16/sample_rate/channels/start_time_seconds/duration_seconds/is_speech/energy_dbfs), `TranscriptResult`
(text/segments/durationSeconds/provider/language, camelCase JSON).

## 6. Session lifecycle
`start` creates a running session + calls `provider.start_session`; pushes increment `chunks_sent` and
`duration_seconds`; `stop` flushes the in-progress chunk, calls `provider.stop_session`, sets `stopped`,
returns the `TranscriptResult`; a provider exception sets `error` and raises `TranscriptionError`.

## 7. VAD/chunking behaviour
`SpeechChunker` over 16 kHz int16: 20 ms frames, a frame is speech when its level ≥ `threshold_dbfs`
(−40 default) — or via an injected `speech_fn` (adaptive). It emits speech-bounded `AudioChunk`s with
`preroll_ms` context + `hangover_ms` tail. **Silence → no chunk; sustained speech → chunks; speech
< `min_speech_ms` (200) → dropped; speech > `max_chunk_ms` (8000) → split; deterministic boundaries;
`reset()` clears state.** No network/provider dependency.

## 8. Mock provider behaviour
`MockTranscriptionProvider` records received chunks, returns a deterministic `TranscriptResult`
(`text="[mock transcript]"` when chunks arrived, else `""`; `durationSeconds` = sum of chunk durations),
supports `fail_on_start` / `fail_on_chunk` error injection, and keeps `network_calls == 0`.

## 9. Integration with EgressRouter
`stream.pump_from_egress(router)` = `push_pcm16(router.drain_asr_pcm16(), sample_rate=router.asr_rate)`.
The egress router is the documented source of truth for ASR input — always processed, clean, mono,
16 kHz int16. Verified end-to-end.

## 10. Processed-only safeguards
`push_pcm16` raises `TranscriptionError` for: a raw multichannel `(N,>1)` array (8ch can never reach
transcription), a wrong sample rate (≠ 16 kHz), and a float buffer (must come through the ASR-safe
int16 egress path). By construction the egress output is post-AGC processed mono.

## 11. Latency / buffering impact
The DSP pipeline latency is unchanged (transcription is downstream). The chunker adds buffering: a chunk
is emitted only when it closes — after `hangover_ms` trailing silence or at `max_chunk_ms`. Worst-case
added latency before a chunk reaches the provider ≈ `max_chunk_ms` (or `speech + hangover_ms` for a
normal utterance). Reported honestly in the guide; tunable.

## 12. Test results
```
tests/test_transcription_stream.py ...................... 22 passed
full non-GUI suite (offscreen, --ignore-glob='tests/test_gui_*.py') → 1016 passed in 42.8s
   (Phase-4 baseline 994 + 22 new; zero regressions)
mypy → Success: no issues found in 70 source files
```
Covers all 18 required cases (+ extras): provider/mock lifecycle, session start/stop, speech→chunks,
silence→none, long-split, short-burst-drop, reset, wrong-rate/raw-8ch/float reject, bytes+array,
timestamps/durations, mock-gets-clean-16k-int16, provider-error-safe, egress integration, no-network,
DSP-defaults-unchanged, result round-trip, export parity. End-to-end demo: 1.2 s tone between silence →
1 speech chunk (0.20 s start with preroll, 1.70 s, 16 kHz int16) → mock transcript; 0 network calls.

## 13. Known limitations
- VAD is a lightweight energy gate (deterministic, configurable). It is not a trained model; for noisy
  rooms plug in `speech_fn` (adaptive `speech_gate`) or a real VAD. Phase 3's placement check / Phase 2
  pre-NR improve the SNR upstream.
- No real ASR provider bundled — `TranscriptionProvider` is the seam (offline or networked, caller's
  choice); `MockTranscriptionProvider` is for tests/dev.
- Chunking adds buffering latency (≤ `max_chunk_ms`); streaming-partial ASR would need a provider that
  accepts incremental audio (the interface already allows per-chunk sends).
- No GUI / live wiring into the engine threads yet (Phase 6) — the stream is a standalone consumer.

## 14. Safe next phase
Phase 6 — GUI / operator workflow: surface calibration, placement check, pipeline order, output route,
latency, and (optionally) a transcription toggle in the operator UI. Phases 1–5 are all standalone,
default-off / no-DSP-change additions, so the GUI phase can wire them in from a green, unchanged pipeline.
