"""Transcription-ready clean stream — turn the processed mono into ASR chunks for a pluggable provider.

Phase 5 of the audio front-end hardening. It consumes Phase 4's **16 kHz ASR-ready mono int16**
(`EgressRouter.drain_asr_pcm16()`), runs a deterministic energy **VAD/chunker**, and hands speech
chunks to a pluggable :class:`TranscriptionProvider`. A :class:`MockTranscriptionProvider` ships for
tests; **no real ASR vendor is bundled and nothing goes to the network by default.**

Hard rules (enforced):
  * the stream accepts ONLY mono 16 kHz ``int16`` (bytes or array); raw multichannel, a wrong sample
    rate, and float buffers are rejected — the clean ASR-safe egress path is the single source of input;
  * it never touches the DSP engine — it is a standalone consumer of the egress output.

The VAD is a simple, deterministic absolute-energy gate (configurable). The repo's adaptive
``conf_pipeline_control.octovox_monitor.speech_gate`` can be plugged in via ``speech_fn`` for a
noise-floor-tracking decision. numpy is imported lazily; the models + provider are pure stdlib.
"""
from __future__ import annotations

import json
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Protocol, Tuple, runtime_checkable

ASR_SAMPLE_RATE = 16000

# Session statuses
STATUS_IDLE = "idle"
STATUS_RUNNING = "running"
STATUS_STOPPED = "stopped"
STATUS_ERROR = "error"

# VAD/chunker defaults
DEFAULT_FRAME_MS = 20
DEFAULT_THRESHOLD_DBFS = -40.0
DEFAULT_MIN_SPEECH_MS = 200
DEFAULT_MAX_CHUNK_MS = 8000
DEFAULT_HANGOVER_MS = 300
DEFAULT_PREROLL_MS = 200


class TranscriptionError(Exception):
    """Bad ASR input (non-mono / wrong rate / float / no session) or a provider failure."""


# --------------------------------------------------------------------------- #
# Models (stdlib)
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class AudioChunk:
    """One speech-bounded clip of ASR-ready mono PCM (little-endian int16 bytes)."""

    pcm16: bytes
    sample_rate: int = ASR_SAMPLE_RATE
    channels: int = 1
    start_time_seconds: float = 0.0
    duration_seconds: float = 0.0
    is_speech: bool = True
    energy_dbfs: float = -120.0


@dataclass
class TranscriptionSession:
    """The lifecycle/metadata of one transcription session (mutable: counters + status update)."""

    session_id: str
    sample_rate: int = ASR_SAMPLE_RATE
    channels: int = 1
    encoding: str = "pcm_s16le"
    started_at: str = ""
    stopped_at: str = ""
    chunks_sent: int = 0
    duration_seconds: float = 0.0
    status: str = STATUS_IDLE
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class TranscriptResult:
    """A provider's transcript. camelCase JSON for parity with the rest of the project."""

    text: str = ""
    segments: Tuple[Any, ...] = ()
    duration_seconds: float = 0.0
    provider: str = "mock"
    language: str = "unknown"

    def to_dict(self) -> dict:
        return {
            "text": str(self.text),
            "segments": list(self.segments),
            "durationSeconds": float(self.duration_seconds),
            "provider": str(self.provider),
            "language": str(self.language),
        }

    @classmethod
    def from_dict(cls, d: Any) -> "TranscriptResult":
        return cls(
            text=str(d.get("text", "")),
            segments=tuple(d.get("segments", ())),
            duration_seconds=float(d.get("durationSeconds", 0.0)),
            provider=str(d.get("provider", "mock")),
            language=str(d.get("language", "unknown")),
        )

    def to_json(self, *, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent)


# --------------------------------------------------------------------------- #
# Provider interface + mock
# --------------------------------------------------------------------------- #
@runtime_checkable
class TranscriptionProvider(Protocol):
    """The pluggable ASR backend. Implement this to wire a real provider later — receive the session,
    accept clean 16 kHz mono int16 chunks, and return a :class:`TranscriptResult` on stop. The repo
    bundles no vendor; a concrete provider is the integration seam (offline or networked, your choice)."""

    def start_session(self, session: TranscriptionSession) -> None: ...

    def send_audio_chunk(self, chunk: AudioChunk) -> None: ...

    def stop_session(self) -> TranscriptResult: ...


class MockTranscriptionProvider:
    """A deterministic, offline test/dev provider. Records the chunks it receives, returns a fixed
    transcript, supports error injection, and **never makes a network call** (``network_calls`` stays 0)."""

    def __init__(self, *, transcript: str = "[mock transcript]",
                 fail_on_start: bool = False, fail_on_chunk: Optional[int] = None) -> None:
        self.received: List[AudioChunk] = []
        self.started = False
        self.stopped = False
        self.network_calls = 0
        self._transcript = transcript
        self._fail_on_start = fail_on_start
        self._fail_on_chunk = fail_on_chunk
        self.session: Optional[TranscriptionSession] = None

    def start_session(self, session: TranscriptionSession) -> None:
        if self._fail_on_start:
            raise RuntimeError("mock start_session failure")
        self.started = True
        self.session = session

    def send_audio_chunk(self, chunk: AudioChunk) -> None:
        if self._fail_on_chunk is not None and len(self.received) == self._fail_on_chunk:
            raise RuntimeError("mock send_audio_chunk failure")
        self.received.append(chunk)

    def stop_session(self) -> TranscriptResult:
        self.stopped = True
        total = float(sum(c.duration_seconds for c in self.received))
        text = self._transcript if self.received else ""
        return TranscriptResult(text=text, segments=(), duration_seconds=total,
                                provider="mock", language="unknown")


# --------------------------------------------------------------------------- #
# SpeechChunker — deterministic energy VAD over 16 kHz int16
# --------------------------------------------------------------------------- #
class SpeechChunker:
    """Frame 16 kHz mono int16, gate each frame on energy, and emit speech-bounded
    :class:`AudioChunk`s: silence makes no chunk, short bursts (< ``min_speech_ms`` of speech) are
    dropped, long speech is split at ``max_chunk_ms``, with ``preroll_ms`` context + ``hangover_ms``
    tail. Deterministic; ``reset()`` clears all state.

    Default gate: a frame is speech when its level ≥ ``threshold_dbfs``. Pass ``speech_fn(rms_norm)
    -> bool`` (e.g. wrapping ``octovox_monitor.speech_gate``) for an adaptive noise-floor decision."""

    def __init__(self, *, sample_rate: int = ASR_SAMPLE_RATE, frame_ms: int = DEFAULT_FRAME_MS,
                 threshold_dbfs: float = DEFAULT_THRESHOLD_DBFS, min_speech_ms: int = DEFAULT_MIN_SPEECH_MS,
                 max_chunk_ms: int = DEFAULT_MAX_CHUNK_MS, hangover_ms: int = DEFAULT_HANGOVER_MS,
                 preroll_ms: int = DEFAULT_PREROLL_MS,
                 speech_fn: Optional[Callable[[float], bool]] = None) -> None:
        self.sample_rate = int(sample_rate)
        self.frame = max(1, int(self.sample_rate * frame_ms / 1000))
        self.threshold_dbfs = float(threshold_dbfs)
        self._speech_fn = speech_fn
        self._min_speech_frames = max(1, round(min_speech_ms / frame_ms))
        self._max_chunk_frames = max(1, round(max_chunk_ms / frame_ms))
        self._hangover_frames = max(1, round(hangover_ms / frame_ms))
        self._preroll_frames = max(0, round(preroll_ms / frame_ms))
        self.reset()

    def reset(self) -> None:
        import numpy as np

        self._carry: Any = np.zeros(0, dtype=np.int16)
        self._t = 0                                   # samples consumed (for timestamps)
        self._collecting = False
        self._frames: List[Any] = []
        self._speech_count = 0
        self._silence_run = 0
        self._chunk_start = 0
        self._preroll: deque = deque(maxlen=self._preroll_frames)

    def _is_speech(self, frame: Any) -> Tuple[bool, float]:
        import numpy as np

        f = frame.astype(np.float64)
        rms = float(np.sqrt(np.mean(f * f))) if f.size else 0.0
        norm = rms / 32768.0
        dbfs = 20.0 * np.log10(norm + 1e-12)
        if self._speech_fn is not None:
            return bool(self._speech_fn(norm)), float(dbfs)
        return (dbfs >= self.threshold_dbfs), float(dbfs)

    def push(self, samples: Any) -> List[AudioChunk]:
        import numpy as np

        s = np.asarray(samples, dtype=np.int16)
        self._carry = np.concatenate([self._carry, s]) if self._carry.size else s
        out: List[AudioChunk] = []
        F = self.frame
        while self._carry.shape[0] >= F:
            frame = self._carry[:F]
            self._carry = self._carry[F:]
            start = self._t
            self._t += F
            out.extend(self._process_frame(frame, start))
        return out

    def _process_frame(self, frame: Any, start_sample: int) -> List[AudioChunk]:
        is_speech, _dbfs = self._is_speech(frame)
        out: List[AudioChunk] = []
        if not self._collecting:
            if is_speech:
                pre = list(self._preroll)
                self._frames = [f for f, _ in pre] + [frame]
                self._chunk_start = pre[0][1] if pre else start_sample
                self._speech_count = 1
                self._silence_run = 0
                self._collecting = True
                self._preroll.clear()
            else:
                self._preroll.append((frame, start_sample))
        else:
            self._frames.append(frame)
            if is_speech:
                self._speech_count += 1
                self._silence_run = 0
            else:
                self._silence_run += 1
            if self._silence_run >= self._hangover_frames:
                out.extend(self._close())
            elif len(self._frames) >= self._max_chunk_frames:
                out.extend(self._close())              # split: the next speech frame starts a fresh chunk
        return out

    def _close(self) -> List[AudioChunk]:
        import numpy as np

        frames, speech = self._frames, self._speech_count
        self._collecting = False
        self._frames = []
        self._speech_count = 0
        self._silence_run = 0
        self._preroll.clear()
        if speech < self._min_speech_frames or not frames:
            return []                                  # too little speech ⇒ drop (short burst)
        block = np.concatenate(frames).astype(np.int16)
        n = int(block.shape[0])
        f = block.astype(np.float64)
        rms = float(np.sqrt(np.mean(f * f))) if n else 0.0
        dbfs = float(20.0 * np.log10(rms / 32768.0 + 1e-12))
        return [AudioChunk(
            pcm16=block.astype("<i2").tobytes(), sample_rate=self.sample_rate, channels=1,
            start_time_seconds=self._chunk_start / self.sample_rate,
            duration_seconds=n / self.sample_rate, is_speech=True, energy_dbfs=dbfs)]

    def flush(self) -> List[AudioChunk]:
        """Close any in-progress chunk (e.g. on stop). Emits only if it has enough speech."""
        if self._collecting:
            return self._close()
        return []


# --------------------------------------------------------------------------- #
# TranscriptionStream — ties egress → VAD → provider
# --------------------------------------------------------------------------- #
def _gen_session_id() -> str:
    import uuid

    return uuid.uuid4().hex[:12]


class TranscriptionStream:
    """Drive a transcription session: take clean 16 kHz mono int16 (from
    :meth:`EgressRouter.drain_asr_pcm16`), chunk it on speech, and forward chunks to the provider."""

    def __init__(self, provider: Any, *, sample_rate: int = ASR_SAMPLE_RATE,
                 session_id: Optional[str] = None, chunker: Optional[SpeechChunker] = None,
                 **vad_kwargs: Any) -> None:
        self.provider = provider
        self.sample_rate = int(sample_rate)
        self._chunker = chunker or SpeechChunker(sample_rate=sample_rate, **vad_kwargs)
        self._session_id = session_id
        self.session: Optional[TranscriptionSession] = None

    def start(self, *, metadata: Optional[Dict[str, Any]] = None,
              started_at: str = "") -> TranscriptionSession:
        self._chunker.reset()
        self.session = TranscriptionSession(
            session_id=self._session_id or _gen_session_id(), sample_rate=self.sample_rate,
            started_at=started_at, status=STATUS_RUNNING, metadata=dict(metadata or {}))
        self.provider.start_session(self.session)
        return self.session

    def _as_pcm16_mono(self, pcm16: Any) -> Any:
        import numpy as np

        if isinstance(pcm16, (bytes, bytearray, memoryview)):
            return np.frombuffer(bytes(pcm16), dtype="<i2")
        a = np.asarray(pcm16)
        if a.dtype != np.int16:
            raise TranscriptionError(
                f"transcription expects 16 kHz int16 PCM (the ASR-safe egress path); got dtype {a.dtype}. "
                "Convert via EgressRouter.drain_asr_pcm16() first.")
        if a.ndim == 2:
            if a.shape[1] == 1:
                a = a[:, 0]
            else:
                raise TranscriptionError(
                    f"transcription input must be mono; got a {a.shape[1]}-channel array "
                    "(raw multichannel audio must never reach transcription)")
        elif a.ndim != 1:
            raise TranscriptionError(f"expected 1-D mono PCM, got shape {a.shape}")
        return a

    def push_pcm16(self, pcm16: Any, sample_rate: int = ASR_SAMPLE_RATE) -> List[AudioChunk]:
        """Feed clean 16 kHz mono int16 (bytes or array); emit + forward any completed speech chunks."""
        if self.session is None or self.session.status != STATUS_RUNNING:
            raise TranscriptionError("no running session; call start() first")
        if int(sample_rate) != self.sample_rate:
            raise TranscriptionError(
                f"transcription expects {self.sample_rate} Hz; got {sample_rate} "
                "(resample through EgressRouter to the ASR rate first)")
        samples = self._as_pcm16_mono(pcm16)
        chunks = self._chunker.push(samples)
        self.session.duration_seconds += samples.shape[0] / self.sample_rate
        for c in chunks:
            self._send(c)
        return chunks

    def pump_from_egress(self, router: Any) -> List[AudioChunk]:
        """Drain the router's 16 kHz ASR PCM and push it. The egress router is the source of truth for
        ASR input (always processed, clean, mono, int16)."""
        pcm = router.drain_asr_pcm16()
        if not pcm:
            return []
        return self.push_pcm16(pcm, sample_rate=int(getattr(router, "asr_rate", self.sample_rate)))

    def _send(self, chunk: AudioChunk) -> None:
        try:
            self.provider.send_audio_chunk(chunk)
        except Exception as exc:
            if self.session is not None:
                self.session.status = STATUS_ERROR
            raise TranscriptionError(f"provider.send_audio_chunk failed: {exc}") from exc
        if self.session is not None:
            self.session.chunks_sent += 1

    def stop(self, *, stopped_at: str = "") -> TranscriptResult:
        if self.session is None:
            raise TranscriptionError("no session to stop")
        for c in self._chunker.flush():
            self._send(c)
        result = self.provider.stop_session()
        self.session.stopped_at = stopped_at
        self.session.status = STATUS_STOPPED
        return result

    def reset(self) -> None:
        self._chunker.reset()
        self.session = None
