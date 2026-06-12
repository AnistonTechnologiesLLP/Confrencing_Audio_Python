"""Near-live **cleaned monitor**: array → OCTOVOX → delayed playback.

OCTOVOX is whole-file/offline (neural stages aren't low-latency), so a true
talk-and-hear-yourself monitor isn't possible. This runs the next best thing: it
captures rolling **chunks** of the raw 8-channel array, sends each to OCTOVOX
(steered by the zone-derived azimuths), and plays the cleaned mono back. The
result is a continuously-updating cleaned monitor delayed by roughly
``chunk_seconds + processing`` (typically ~4–5 s), with audible seams at chunk
boundaries (each chunk is cleaned independently). Use it to hear *how clean* the
zone-steered result is, not for real-time talkback.

Needs the ``[octovox]`` extra (numpy + sounddevice + requests + scipy). The audio
threads and HTTP worker are kept separate so a slow clean never blocks capture.
"""
from __future__ import annotations

import queue
import threading
from dataclasses import dataclass
from typing import Any, Optional

from .octovox_bridge import OctovoxClient


class _MonoFifo:
    """Thread-safe mono sample FIFO feeding the output stream."""

    def __init__(self):
        self._lock = threading.Lock()
        self._buf = None  # numpy array, lazily created

    def push(self, x):
        import numpy as np

        with self._lock:
            self._buf = x.astype(np.float32) if self._buf is None else np.concatenate([self._buf, x])

    def pull(self, n):
        import numpy as np

        with self._lock:
            if self._buf is None or len(self._buf) == 0:
                return np.zeros(n, dtype=np.float32)
            if len(self._buf) >= n:
                out, self._buf = self._buf[:n], self._buf[n:]
                return out
            out = np.zeros(n, dtype=np.float32)
            out[: len(self._buf)] = self._buf
            self._buf = self._buf[:0]
            return out

    def available(self) -> int:
        with self._lock:
            return 0 if self._buf is None else len(self._buf)


def speech_gate(raw_rms, noise_floor, gate_ratio):
    """Decide whether a raw chunk contains speech, tracking the noise floor.

    OCTOVOX peak-normalises / AGCs each chunk independently, so a **noise-only**
    chunk has its residual noise amplified to near-full scale (louder than the
    actual voice). Gating those chunks to silence is what stops the "only noise"
    pumping. The floor follows the signal down fast and rises slowly; a chunk is
    speech when it sits ``gate_ratio`` above the floor. Returns
    ``(is_speech, new_noise_floor)``."""
    if noise_floor is None or noise_floor <= 0.0:
        nf = max(raw_rms, 1e-9)
    elif raw_rms < noise_floor:
        nf = 0.5 * noise_floor + 0.5 * raw_rms          # track down quickly
    else:
        nf = 0.98 * noise_floor + 0.02 * raw_rms        # rise slowly
    return (raw_rms > nf * gate_ratio), nf


def crossfade_join(prev_tail, cleaned, overlap, np):
    """Join an independently-cleaned chunk onto the stream without a seam.

    ``cleaned`` overlaps the previous chunk by ``overlap`` samples (the capture
    overlapped by the same amount). We (1) level-match this chunk to the previous
    tail — undoing OCTOVOX's per-chunk peak-normalisation, which otherwise pumps —
    and (2) equal-power crossfade the overlap region, hiding the click and the
    neural stages' edge transient. Returns ``(emit, new_tail)``: ``emit`` goes to
    the speaker now, ``new_tail`` is held to crossfade the next chunk.
    """
    o = int(overlap)
    if o <= 0 or len(cleaned) <= 2 * o:
        return cleaned, cleaned[:0]
    if prev_tail is None or len(prev_tail) != o:
        # first chunk: emit all but the tail (held for the next crossfade)
        return cleaned[:-o], cleaned[-o:].copy()

    head = cleaned[:o]
    pr = float(np.sqrt((prev_tail ** 2).mean()) + 1e-9)
    cr = float(np.sqrt((head ** 2).mean()) + 1e-9)
    g = min(2.0, max(0.5, pr / cr))            # clamp the level match to ±6 dB
    cleaned = cleaned * g
    head = cleaned[:o]

    t = np.linspace(0.0, 1.0, o, endpoint=False)
    fade_out = np.cos(t * np.pi / 2.0)
    fade_in = np.sin(t * np.pi / 2.0)
    xf = prev_tail * fade_out + head * fade_in
    emit = np.concatenate([xf, cleaned[o:-o]])
    return emit.astype(np.float32), cleaned[-o:].copy()


@dataclass
class MonitorState:
    running: bool
    connected_server: bool
    chunks_sent: int
    chunks_played: int
    dropped: int
    gated: int
    last_elapsed_s: float
    buffered_s: float
    error: str = ""


class CleanMonitor:
    """Rolling-chunk array→OCTOVOX→playback monitor (see module docstring)."""

    def __init__(
        self,
        client: OctovoxClient,
        *,
        input_device: Optional[int],
        samplerate: int = 44100,
        chunk_seconds: float = 3.0,
        target_az: Optional[float] = None,
        interferer_az: Optional[list] = None,
        output_device: Optional[int] = None,
        nr: str = "dfn",
        in_channels: int = 8,
        overlap_seconds: float = 0.3,
        active: Optional[list] = None,
        gate_ratio: float = 2.5,
    ):
        self.client = client
        self.input_device = input_device
        self.samplerate = int(samplerate)
        self.chunk_seconds = float(chunk_seconds)
        self.target_az = target_az
        self.interferer_az = interferer_az
        self.output_device = output_device
        self.nr = nr
        self.in_channels = in_channels
        self.overlap_seconds = float(overlap_seconds)
        self.active = active        # per-capsule mask → repair dead capsules before sending
        self.gate_ratio = float(gate_ratio)   # speech gate (vs noise floor); 0 disables
        self._noise_floor = None
        self.gated = 0

        # Lazily bound in start() (numpy / sounddevice) — Any keeps the module
        # importable + checkable without the [octovox] extra.
        self._sd: Any = None
        self._np: Any = None
        self._in_stream: Any = None
        self._out_stream: Any = None
        self._out_channels = 2
        self._fifo = _MonoFifo()
        self._clean_q: "queue.Queue" = queue.Queue(maxsize=4)
        self._worker: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._acc: list[Any] = []  # accumulating raw-input blocks (numpy arrays)
        self._acc_n = 0
        self._chunk_samples = int(self.chunk_seconds * self.samplerate)
        self._overlap_in = int(self.overlap_seconds * self.samplerate)   # input-rate overlap
        self._overlap_out = int(self.overlap_seconds * 48000)            # output-rate overlap
        self._emit_len = int(max(0.0, self.chunk_seconds - self.overlap_seconds) * 48000)  # silence len for gated chunks
        self._prev_tail: Any = None                                      # held for crossfade (numpy)
        # counters (plain ints; GIL keeps reads coherent for a status display)
        self.chunks_sent = 0
        self.chunks_played = 0
        self.dropped = 0
        self.last_elapsed_s = 0.0
        self.error = ""
        self._running = False

    # ---- lifecycle ----
    def start(self) -> None:
        import numpy as np
        import sounddevice as sd

        self._np = np
        self._sd = sd
        try:
            info = sd.query_devices(self.output_device, "output")
            self._out_channels = max(1, min(2, int(info.get("max_output_channels", 2))))
        except Exception:
            self._out_channels = 2

        self._stop.clear()
        self._worker = threading.Thread(target=self._clean_loop, name="octovox-clean", daemon=True)
        self._worker.start()

        self._in_stream = sd.InputStream(
            samplerate=self.samplerate, channels=self.in_channels, blocksize=1024,
            device=self.input_device, dtype="float32", callback=self._in_cb,
        )
        self._out_stream = sd.OutputStream(
            samplerate=48000, channels=self._out_channels, blocksize=1024,
            device=self.output_device, dtype="float32", callback=self._out_cb,
        )
        self._in_stream.start()
        self._out_stream.start()
        self._running = True

    def stop(self) -> None:
        self._stop.set()
        for s in (self._in_stream, self._out_stream):
            if s is not None:
                try:
                    s.stop()
                    s.close()
                except Exception:
                    pass
        self._in_stream = self._out_stream = None
        if self._worker is not None:
            self._worker.join(timeout=2.0)
            self._worker = None
        if self._prev_tail is not None and self._np is not None:
            self._fifo.push(self._prev_tail)   # flush the final held tail
            self._prev_tail = None
        self._running = False

    def state(self) -> MonitorState:
        buffered = self._fifo.available() / 48000.0
        return MonitorState(
            running=self._running,
            connected_server=True if self.error == "" else False,
            chunks_sent=self.chunks_sent,
            chunks_played=self.chunks_played,
            dropped=self.dropped,
            gated=self.gated,
            last_elapsed_s=self.last_elapsed_s,
            buffered_s=buffered,
            error=self.error,
        )

    # ---- audio threads ----
    def _in_cb(self, indata, frames, time_info, status):  # pragma: no cover (needs hardware)
        np = self._np
        self._acc.append(indata[:, : self.in_channels].copy())
        self._acc_n += frames
        if self._acc_n >= self._chunk_samples:
            buf = np.concatenate(self._acc, axis=0)          # (N, 8)
            chunk = buf[: self._chunk_samples].T             # (8, chunk_samples)
            try:
                self._clean_q.put_nowait(chunk)
                self.chunks_sent += 1
            except queue.Full:
                self.dropped += 1  # OCTOVOX can't keep up → drop this chunk
            # retain the last `overlap` samples so the next chunk overlaps this one
            keep = buf[self._chunk_samples - self._overlap_in :]
            self._acc, self._acc_n = [keep], keep.shape[0]

    def _out_cb(self, outdata, frames, time_info, status):  # pragma: no cover
        mono = self._fifo.pull(frames)
        outdata[:] = mono[:, None]

    def _clean_loop(self):  # pragma: no cover (needs server)
        while not self._stop.is_set():
            try:
                chunk = self._clean_q.get(timeout=0.2)
            except queue.Empty:
                continue
            # Speech gate: a noise-only chunk would be normalised up to full scale by
            # OCTOVOX (louder than the voice), so play silence and skip the round-trip.
            if self.gate_ratio > 0:
                raw_rms = float(self._np.sqrt((chunk ** 2).mean())) if chunk.size else 0.0
                is_speech, self._noise_floor = speech_gate(raw_rms, self._noise_floor, self.gate_ratio)
                if not is_speech:
                    self._fifo.push(self._np.zeros(self._emit_len, dtype=self._np.float32))
                    self._prev_tail = None
                    self.gated += 1
                    continue
            try:
                res = self.client.clean_8ch(
                    chunk, self.samplerate,
                    target_az=self.target_az, interferer_az=self.interferer_az,
                    nr=self.nr, active=self.active,
                )
                emit, self._prev_tail = crossfade_join(
                    self._prev_tail, res.mono, self._overlap_out, self._np
                )
                self._fifo.push(emit)
                self.last_elapsed_s = res.elapsed_s
                self.chunks_played += 1
                self.error = ""
            except Exception as exc:
                self.error = str(exc)
