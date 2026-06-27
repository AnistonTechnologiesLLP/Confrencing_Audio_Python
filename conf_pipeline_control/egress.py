"""Clean-mono egress — a named router for the final processed mono signal.

Phase 4 of the audio front-end hardening. The live engine already emits processed clean mono (via the
``output_callback`` seam, post AGC/limiter); the WAV recorder, the PCM monitor and the A/B proof
already write *processed* mono, not the raw 8 channels. This module does NOT rebuild any of those — it
adds the missing pieces around them:

  * a **named `EgressRouter`** that takes ONLY the final processed mono and fans it out;
  * a **16 kHz ASR-ready int16** path (resample + PCM) for the future transcription stage (Phase 5);
  * an engine-rate (48 kHz) mono PCM route + a mono WAV sink (reusing the existing int16 WAV pattern);
  * an optional **external/virtual-mic sink hook** (`ExternalPcmSink`), disabled by default.

**Processed-only safeguard:** :meth:`EgressRouter.push` refuses any multichannel block, so the raw
8-channel capture can never be routed out as the "clean" output. The router plugs straight into the
engine: ``PolarisBeamformer(output_callback=router.push)`` — no DSP change.

numpy is imported lazily; the resampler reuses
:func:`conf_pipeline_control.reference_capture._resample_to` (scipy polyphase, numpy fallback).
"""
from __future__ import annotations

from typing import Any, List, Optional, Protocol, Sequence, runtime_checkable

ASR_RATE = 16000             # the conventional ASR sample rate (Phase 5 consumes this)


class EgressError(Exception):
    """An egress push/conversion got non-mono audio, a rate mismatch, or an unusable sink."""


# --------------------------------------------------------------------------- #
# Conversion utilities
# --------------------------------------------------------------------------- #
def to_pcm16(x: Any) -> Any:
    """Clip-safe float mono → ``int16`` numpy array (the shared WAV pattern; saturates, never wraps)."""
    import numpy as np

    a = np.clip(np.asarray(x, dtype=np.float32), -1.0, 1.0)
    return (a * 32767.0).astype(np.int16)


def pcm16_bytes(x: Any) -> bytes:
    """Clip-safe float mono → little-endian ``int16`` bytes (for WAV / PCM streams / sinks)."""
    import numpy as np

    a = np.clip(np.asarray(x, dtype=np.float32), -1.0, 1.0)
    return (a * 32767.0).astype("<i2").tobytes()


def resample_mono(x: Any, sr_from: float, sr_to: float) -> Any:
    """Resample a mono block ``sr_from → sr_to`` (float32). Reuses the project resampler. Raises
    :class:`EgressError` on non-mono input."""
    import numpy as np

    a = np.asarray(x, dtype=np.float32)
    if a.ndim != 1:
        raise EgressError(f"resample_mono expects a 1-D mono block, got shape {a.shape}")
    from .reference_capture import _resample_to

    return _resample_to(a, float(sr_from), float(sr_to))


# --------------------------------------------------------------------------- #
# Sinks
# --------------------------------------------------------------------------- #
@runtime_checkable
class ExternalPcmSink(Protocol):
    """The optional egress hook for OS audio routing / a virtual microphone.

    Implement this to feed the clean mono PCM to a system loopback device — e.g. **BlackHole** or
    **VB-CABLE** (macOS/Windows), **PipeWire** / **JACK** (Linux), or any router. This package installs
    NO virtual audio driver; it only hands you the int16 bytes + sample rate. Disabled by default —
    pass an instance via ``EgressRouter(sinks=[...])``."""

    def write(self, pcm16: bytes, sample_rate: int) -> None: ...

    def close(self) -> None: ...


class WavMonoSink:
    """A mono 16-bit WAV recorder for the egress router (same int16 format as the live recorder / A/B
    proof). Opens on construction; ``write`` appends; ``close`` finalises the file."""

    def __init__(self, path: str, sample_rate: float) -> None:
        import wave

        self._w: Any = wave.open(path, "wb")
        self._w.setnchannels(1)
        self._w.setsampwidth(2)
        self._w.setframerate(int(round(sample_rate)))
        self.frames = 0

    def write(self, mono: Any, sample_rate: Optional[int] = None) -> None:
        b = pcm16_bytes(mono)
        self._w.writeframes(b)
        self.frames += len(b) // 2

    def close(self) -> None:
        if self._w is not None:
            self._w.close()
            self._w = None


# --------------------------------------------------------------------------- #
# EgressRouter
# --------------------------------------------------------------------------- #
class EgressRouter:
    """Route the final processed clean mono to: the engine-rate PCM (48 kHz), a 16 kHz ASR-ready int16
    path, an optional mono WAV file, and optional external sinks. Fed via :meth:`push` — typically wired
    as ``PolarisBeamformer(output_callback=router.push)``.

    :meth:`push` is cheap (store + buffer + write) so it is safe on the realtime audio thread; the 16 kHz
    resample happens on :meth:`drain_asr_array` (called off the audio thread by the consumer)."""

    def __init__(self, sample_rate: float, *, wav_path: Optional[str] = None,
                 asr_rate: int = ASR_RATE, max_buffer_seconds: float = 30.0,
                 sinks: Sequence[Any] = ()) -> None:
        self.sample_rate = float(sample_rate)
        self.asr_rate = int(asr_rate)
        self._max_buf = max(1, int(max_buffer_seconds * self.sample_rate))
        self._sinks: List[Any] = list(sinks)
        self._wav: Optional[WavMonoSink] = WavMonoSink(wav_path, self.sample_rate) if wav_path else None
        self._buf: List[Any] = []        # engine-rate float32 blocks awaiting ASR drain
        self._buf_len = 0
        self._latest: Any = None         # last pushed processed mono (float32 1-D)
        self.frames_pushed = 0

    def _as_mono(self, mono: Any) -> Any:
        import numpy as np

        a = np.asarray(mono, dtype=np.float32)
        if a.ndim == 2:
            if a.shape[1] == 1:
                a = a[:, 0]
            else:
                raise EgressError(
                    f"egress clean output must be mono; got a {a.shape[1]}-channel block "
                    "(raw multichannel audio cannot be routed as the clean output)")
        elif a.ndim != 1:
            raise EgressError(f"egress expects a 1-D mono block, got shape {a.shape}")
        return a

    def push(self, mono: Any, sample_rate: Optional[float] = None) -> int:
        """Route one processed-mono block. Rejects multichannel input. Returns the frame count."""
        a = self._as_mono(mono)
        if sample_rate is not None and abs(float(sample_rate) - self.sample_rate) > 1e-6:
            raise EgressError(f"push sample_rate {sample_rate} != router rate {self.sample_rate}")
        self._latest = a
        self.frames_pushed += int(a.shape[0])
        if self._wav is not None:
            self._wav.write(a)
        self._buf.append(a)
        self._buf_len += int(a.shape[0])
        while self._buf_len > self._max_buf and len(self._buf) > 1:    # bound memory: drop oldest
            self._buf_len -= int(self._buf.pop(0).shape[0])
        if self._sinks:
            b = pcm16_bytes(a)
            for s in self._sinks:
                try:
                    s.write(b, int(round(self.sample_rate)))
                except Exception:                                      # a sink must never break egress
                    pass
        return int(a.shape[0])

    def latest_mono(self) -> Any:
        """The most recent processed mono block (float32, engine rate), or ``None``."""
        return self._latest

    def latest_pcm16(self) -> bytes:
        """The most recent block as engine-rate ``int16`` PCM bytes (the 48 kHz route)."""
        return pcm16_bytes(self._latest) if self._latest is not None else b""

    def _drain_buffer(self) -> Any:
        import numpy as np

        if not self._buf:
            return np.zeros(0, dtype=np.float32)
        x = self._buf[0] if len(self._buf) == 1 else np.concatenate(self._buf)
        self._buf = []
        self._buf_len = 0
        return x

    def drain_asr_array(self) -> Any:
        """Resample everything buffered since the last drain to the ASR rate and return ``int16`` mono.
        Clears the buffer."""
        import numpy as np

        x = self._drain_buffer()
        if x.size == 0:
            return np.zeros(0, dtype=np.int16)
        return to_pcm16(resample_mono(x, self.sample_rate, self.asr_rate))

    def drain_asr_pcm16(self) -> bytes:
        """Same as :meth:`drain_asr_array`, as little-endian ``int16`` bytes (ASR-ready PCM)."""
        return self.drain_asr_array().astype("<i2").tobytes()

    def pending_seconds(self) -> float:
        return self._buf_len / self.sample_rate if self.sample_rate else 0.0

    @property
    def algorithmic_latency_ms(self) -> float:
        """The engine-rate passthrough route adds **zero** algorithmic latency (format conversion only).
        The 16 kHz drain adds the resampler's small group delay plus whatever the consumer buffers
        before draining — reported via ``pending_seconds()`` — not hidden here."""
        return 0.0

    def add_sink(self, sink: Any) -> None:
        self._sinks.append(sink)

    def reset(self) -> None:
        """Drop the ASR buffer + the latest block. Does not close the WAV file (use :meth:`close`)."""
        self._buf = []
        self._buf_len = 0
        self._latest = None

    def close(self) -> None:
        if self._wav is not None:
            self._wav.close()
            self._wav = None
        for s in self._sinks:
            try:
                s.close()
            except Exception:
                pass
