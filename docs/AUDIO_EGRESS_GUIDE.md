# Audio Egress Guide (clean mono output)

Phase 4 of the audio front-end hardening. The **egress router** is a small, named layer that takes the
**final processed clean mono** and routes it to the places downstream consumers need it:

- the engine-rate (48 kHz / 44.1 kHz) mono PCM — for conferencing / recording / monitoring,
- a **16 kHz ASR-ready int16** path — for transcription (Phase 5),
- a mono WAV file,
- an optional **external / virtual-mic sink** hook.

> It does **not** rebuild the existing outputs. The live WAV recorder (`record_path`), the PCM monitor
> (`OutputStream`), the A/B proof (`write_ab_proof`) and the multibeam recorders all still work and all
> still emit *processed* mono. The router fills the real gaps (a named API + the 16 kHz path) and reuses
> the existing int16 WAV format and the existing resampler.

---

## 1. Wiring it to the engine

The engine already emits the final processed mono (post AGC/limiter) to its `output_callback`. Point
that at the router — no DSP change:

```python
import conf_pipeline_control as cc

router = cc.EgressRouter(sample_rate=44100.0, wav_path="meeting.wav", asr_rate=16000)
bf = cc.PolarisBeamformer(device=13, agc_target_db=-20.0, output_callback=router.push)
bf.start()   # every processed block now flows through the router
```

You can also push manually (e.g. draining the engine's output queue on your own thread):

```python
mono = bf.process_block(block_8ch)   # final processed mono
router.push(mono)
```

`push` is cheap (store + buffer + WAV write), so it is safe on the realtime audio thread; the 16 kHz
resample happens later, on `drain_asr_array()`.

---

## 2. The routes

| route | call | format |
|---|---|---|
| 48 kHz mono (last block) | `router.latest_pcm16()` | engine-rate `int16` bytes |
| 48 kHz mono (float) | `router.latest_mono()` | engine-rate `float32` |
| **16 kHz ASR-ready** | `router.drain_asr_pcm16()` / `drain_asr_array()` | 16 kHz `int16` PCM (bytes / array) |
| WAV file | `EgressRouter(wav_path=…)` then `router.close()` | mono 16-bit WAV |
| external / virtual mic | `EgressRouter(sinks=[my_sink])` | `int16` bytes @ engine rate |

Standalone converters are exported too: `cc.resample_mono(x, sr_from, sr_to)`, `cc.to_pcm16(x)`,
`cc.pcm16_bytes(x)`.

### 16 kHz ASR drain

```python
# on a consumer thread / timer, pull ASR-ready PCM and hand it to your transcriber (Phase 5):
pcm16_16k = router.drain_asr_pcm16()      # int16 little-endian bytes @ 16 kHz
```

`drain_*` resamples everything buffered since the last drain and clears the buffer. `pending_seconds()`
tells you how much is waiting; the buffer is capped (`max_buffer_seconds`, drops oldest) so an idle
consumer can't grow memory without bound.

---

## 3. Processed-only safety

`push` **refuses any multichannel block** — the raw 8 capsules can never be routed out as the "clean"
output:

```python
router.push(raw_8ch)   # raises EgressError: "raw multichannel audio cannot be routed as the clean output"
```

By construction the engine's `output_callback` carries post-beamform, post-cleanup, post-AGC mono, so
what the router emits is the cleaned voice, level-managed and clip-safe (`to_pcm16` saturates, never
wraps).

---

## 4. Optional virtual mic / OS routing

`EgressRouter(sinks=[...])` fans the clean mono (as `int16` bytes + sample rate) to any object
implementing `ExternalPcmSink`:

```python
class MyLoopbackSink:
    def write(self, pcm16: bytes, sample_rate: int) -> None: ...   # feed your virtual device
    def close(self) -> None: ...

router = cc.EgressRouter(48000.0, sinks=[MyLoopbackSink()])
```

This is the **integration seam** for a system virtual microphone — **BlackHole** / **VB-CABLE**
(macOS/Windows), **PipeWire** / **JACK** (Linux), or any router. It is **disabled by default**, and
this package installs **no** virtual audio driver: you provide the concrete sink that writes to your
platform's loopback device.

---

## 5. Latency

- **48 kHz passthrough route: zero algorithmic latency** — it's format conversion only
  (`algorithmic_latency_ms == 0`).
- **16 kHz ASR route**: the polyphase resampler adds a small filter group delay (a few ms), plus
  whatever you buffer before calling `drain_*` — reported honestly by `pending_seconds()`, not hidden.
- The **engine's** `estimated_latency_ms` is unchanged — egress is downstream of the DSP chain.

---

## 6. Lifecycle

```python
router.reset()   # drop the ASR buffer + last block (does NOT close the WAV)
router.close()   # finalise the WAV file + close external sinks
```
