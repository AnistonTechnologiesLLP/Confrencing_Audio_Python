# Transcription Stream Guide

Phase 5 of the audio front-end hardening. The transcription layer turns the **processed clean mono**
into ASR chunks and hands them to a **pluggable provider**. It consumes Phase 4's 16 kHz ASR-ready
egress output, runs a deterministic energy VAD/chunker, and never sees raw 8-channel audio.

> **No real ASR vendor is bundled and nothing goes to the network by default.** A
> `MockTranscriptionProvider` ships for tests/dev; you implement `TranscriptionProvider` to wire a real
> (offline or networked) backend later. The transcription layer never touches the DSP engine.

---

## 1. The flow

```
processed clean mono
  → EgressRouter (Phase 4)
  → drain_asr_pcm16()          # 16 kHz mono int16
  → TranscriptionStream.push_pcm16(...)
  → SpeechChunker (VAD)        # speech-bounded AudioChunks
  → TranscriptionProvider.send_audio_chunk(...)
  → TranscriptResult
```

End to end:

```python
import conf_pipeline_control as cc

router = cc.EgressRouter(sample_rate=48000.0, asr_rate=16000)
bf = cc.PolarisBeamformer(device=13, agc_target_db=-20.0, output_callback=router.push)

stream = cc.TranscriptionStream(cc.MockTranscriptionProvider())   # swap in your real provider
stream.start(metadata={"room": "Boardroom A"})

# on a consumer loop/timer: drain the clean 16 kHz PCM and feed transcription
stream.pump_from_egress(router)        # == push_pcm16(router.drain_asr_pcm16(), sample_rate=16000)

result = stream.stop()                 # TranscriptResult(text=…, durationSeconds=…, provider=…)
```

---

## 2. The provider interface

```python
@runtime_checkable
class TranscriptionProvider(Protocol):
    def start_session(self, session: TranscriptionSession) -> None: ...
    def send_audio_chunk(self, chunk: AudioChunk) -> None: ...
    def stop_session(self) -> TranscriptResult: ...
```

Implement these three sync methods for any backend (a local model, a websocket/streaming ASR, a
batch API — your choice). The session carries `sample_rate=16000`, `channels=1`, `encoding="pcm_s16le"`,
counters, and `metadata`. Each `AudioChunk` is clean 16 kHz mono int16 (`pcm16` bytes) with
`start_time_seconds`, `duration_seconds`, `is_speech`, `energy_dbfs`.

`MockTranscriptionProvider` records the chunks, returns a deterministic `TranscriptResult`, and keeps
`network_calls == 0` — proving the default path is offline.

---

## 3. VAD / chunking

`SpeechChunker` is a deterministic energy gate over 16 kHz int16:

| knob | default | effect |
|---|---|---|
| `frame_ms` | 20 | analysis frame |
| `threshold_dbfs` | −40 | a frame ≥ this level is "speech" |
| `min_speech_ms` | 200 | shorter speech bursts are **dropped** |
| `max_chunk_ms` | 8000 | long speech is **split** at this length |
| `hangover_ms` | 300 | trailing silence kept before closing a chunk |
| `preroll_ms` | 200 | leading context kept before speech onset |

Behaviour: **silence makes no chunk**, sustained speech makes chunks, a long monologue splits into
≤ `max_chunk_ms` pieces, short noise bursts are ignored, boundaries are deterministic, and `reset()`
clears all state. No network, no provider dependency.

**Adaptive option:** pass `speech_fn(rms_norm) -> bool` to use a noise-floor-tracking decision instead
of a fixed threshold — e.g. wrapping the repo's `conf_pipeline_control.octovox_monitor.speech_gate`.

```python
stream = cc.TranscriptionStream(provider, threshold_dbfs=-45, min_speech_ms=250, max_chunk_ms=10000)
```

---

## 4. Input safety (processed-only)

`push_pcm16` accepts **only mono 16 kHz `int16`** (bytes or array) and raises `TranscriptionError` on:

- a **raw multichannel** array (the 8 capsules can never reach transcription),
- a **wrong sample rate** (resample through `EgressRouter` to the ASR rate first),
- a **float** buffer (must come through the ASR-safe int16 egress path).

> The `EgressRouter` is the single **source of truth** for transcription input. Don't feed
> `router.latest_mono()` (raw float) to transcription — use `drain_asr_pcm16()` / `pump_from_egress`,
> which guarantee processed, clean, mono, 16 kHz int16.

---

## 5. Lifecycle & buffering

- `start(metadata=…)` → session `running`, `provider.start_session`.
- `push_pcm16(...)` / `pump_from_egress(router)` → chunk + forward; updates `session.chunks_sent`.
- `stop()` → flush the in-progress chunk, `provider.stop_session()`, session `stopped`, returns the
  `TranscriptResult`.
- `reset()` → clear VAD state + session.
- A provider error surfaces as `TranscriptionError` and sets the session to `error`.

**Buffering latency** is honest: a chunk is only emitted once it closes — after `hangover_ms` of
trailing silence or at `max_chunk_ms`. So the worst-case added latency before a chunk reaches the
provider is ≈ `max_chunk_ms` (or `speech + hangover_ms` for a normal utterance). The DSP pipeline's own
latency is unchanged — transcription is a downstream consumer.
