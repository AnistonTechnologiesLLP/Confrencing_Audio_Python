"""Real-time **DeepFilterNet3** voice cleaner for the post-beam seam (``post_nr_engine="dfn3"``).

DeepFilterNet3 is a neural speech denoiser. The official PyTorch package can't install on this
machine (Python 3.14 / no Rust toolchain → ``deepfilterlib`` won't build), so this runs the model via
**ONNX Runtime** on a *self-contained streaming* ONNX graph (raw 10 ms frame in → cleaned frame out +
carried state; the STFT / ERB / deep-filter / ISTFT are all baked into the graph). That ONNX is a
one-time **TorchDF export** of ``ExportableStreamingTorchDF`` (grazder's pure-torch DeepFilterNet3),
bundled at ``models/deepfilternet3_streaming.onnx`` — so the runtime needs only ``onnxruntime`` (the
``[dfn]`` extra), no torch.

It implements the same ``process(block, noise_gate) -> block`` contract as ``_PostNoiseSuppressor`` /
``StreamingCleaner`` (same-length mono out, ``reset()``, internal lock), so the engine dispatches to it
exactly like the other cleaners. DeepFilterNet wants **48 kHz**; the POLARIS path is 44.1 kHz, so the
mono is streamed through a 44.1↔48 kHz resampler around the model. The model has an inherent ~1-frame
lookahead and the framing/resampler add a little more, so the stage runs at a **fixed latency** (~40-60
ms total) — primed like the other post-NR stages. Measured ~2.9 ms / 10 ms hop on a modern CPU core
(RTF ≈ 0.29), so it runs inline on the audio thread; two kits run on their own threads in parallel.
"""
from __future__ import annotations

import threading
from pathlib import Path
from typing import Any, Optional

DFN3_SR = 48000          # DeepFilterNet operates at 48 kHz
DFN3_HOP = 480           # 10 ms @ 48 kHz — the model's frame/hop
DFN3_STATE_LEN = 45304   # flattened streaming-state tensor length (from the exported model)
DEFAULT_ATTEN_LIM_DB = 100.0   # max attenuation the model may apply (100 ≈ unlimited / full cleaning)

_DEFAULT_MODEL = Path(__file__).resolve().parent / "models" / "deepfilternet3_streaming.onnx"


class _StreamingResampler:
    """Overlap-save streaming polyphase resampler (``up``/``down``) with kept history, so block-by-block
    output concatenates without the per-block FIR transient. Reuses ``scipy.signal.resample_poly`` (the
    same resampler the OCTOVOX bridge uses), with a history margin to absorb the filter's edge."""

    def __init__(self, up: int, down: int, np: Any):
        from math import gcd
        g = gcd(up, down)
        self.up, self.down = up // g, down // g
        self._np = np
        self._hist_len = 24 * max(self.up, self.down)        # > the resample_poly Kaiser FIR half-width
        self._hist = np.zeros(0, dtype=np.float32)

    def process(self, x: Any) -> Any:
        np = self._np
        from scipy.signal import resample_poly
        x = np.asarray(x, dtype=np.float32)
        buf = np.concatenate([self._hist, x])
        y = resample_poly(buf, self.up, self.down).astype(np.float32)
        n_hist_out = (len(self._hist) * self.up) // self.down  # output samples belonging to the history
        out = y[n_hist_out:]
        self._hist = buf[-self._hist_len:].copy() if buf.shape[0] > self._hist_len else buf.copy()
        return out

    def reset(self) -> None:
        self._hist = self._np.zeros(0, dtype=self._np.float32)


class StreamingDeepFilter:
    """DeepFilterNet3 cleaner over the ``process(block, noise_gate)`` seam, via ONNX Runtime.

    ``noise_gate`` is ignored (a full neural denoiser needs no VAD gate). Streams the mono at the engine
    rate through 44.1↔48 kHz resamplers and the 480-sample DFN3 frames, carrying the model state; returns
    a same-length, fixed-latency block (zero-primed during the initial fill). Thread-safe (one lock
    serialises ``process``/``reset``)."""

    def __init__(self, sample_rate: float, *, model_path: Optional[str] = None,
                 atten_lim_db: float = DEFAULT_ATTEN_LIM_DB, **_ignored: Any):
        self.sample_rate = float(sample_rate)
        self._atten_path = str(model_path) if model_path else str(_DEFAULT_MODEL)
        self._atten_lim_db = float(atten_lim_db)
        self._lock = threading.Lock()
        self.error: Optional[str] = None       # last process() error (passthrough fallback fired)
        self._np: Any = None
        self._sess: Any = None
        self._init_runtime()
        self._init_state()
        self._warm()

    def _warm(self) -> None:
        """Run the FULL process() path once off the audio thread — the onnxruntime first-inference init
        AND the first ``scipy.signal`` import / resampler FIR design (together ~1 s) — then reset, so the
        first LIVE block (on the PortAudio callback) is ~ms and the stream never stalls into silence."""
        np = self._np
        try:
            self.process(np.zeros(max(DFN3_HOP, int(self.sample_rate * 0.05)), dtype=np.float32), False)
        except Exception:
            pass
        self.reset()

    def _init_runtime(self) -> None:
        try:
            import numpy as np
            import onnxruntime as ort
        except Exception as exc:  # pragma: no cover - depends on the [dfn] extra
            raise RuntimeError(
                "DeepFilterNet3 cleaning needs the [dfn] extra (onnxruntime). "
                "Install with:  pip install -e \".[dfn]\""
            ) from exc
        if not Path(self._atten_path).exists():
            raise RuntimeError(f"DeepFilterNet3 model not found at {self._atten_path}")
        self._np = np
        so = ort.SessionOptions()
        so.intra_op_num_threads = 1          # don't oversubscribe the audio thread
        so.inter_op_num_threads = 1
        so.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
        self._sess = ort.InferenceSession(self._atten_path, so, providers=["CPUExecutionProvider"])
        self._atten = np.array(self._atten_lim_db, dtype=np.float32)   # 0-dim scalar input
        # WARM UP off the audio thread: onnxruntime's first inference does heavy graph/alloc init (~1 s).
        # Without this the first live process() would block the PortAudio callback for ~1 s and stall the
        # stream into silence — this is constructed at Connect (host thread), so pay that cost here.
        warm, wst = np.zeros(DFN3_HOP, dtype=np.float32), np.zeros(DFN3_STATE_LEN, dtype=np.float32)
        for _ in range(2):
            _o, wst, _l = self._sess.run(
                None, {"input_frame": warm, "states": wst, "atten_lim_db": self._atten})

    def _init_state(self) -> None:
        np = self._np
        up = self.sample_rate != DFN3_SR
        self._to48 = _StreamingResampler(DFN3_SR, int(self.sample_rate), np) if up else None
        self._from48 = _StreamingResampler(int(self.sample_rate), DFN3_SR, np) if up else None
        self._states = np.zeros(DFN3_STATE_LEN, dtype=np.float32)
        self._in48 = np.zeros(0, dtype=np.float32)     # accumulated 48 kHz input awaiting full frames
        self._outq = np.zeros(0, dtype=np.float32)     # cleaned output at the ENGINE rate, FIFO
        self._primed = False
        self._total_in = 0

    def process(self, block: Any, noise_gate: bool) -> Any:
        np = self._np
        x = np.asarray(block, dtype=np.float32).reshape(-1)
        n = x.shape[0]
        if n == 0:
            return x
        # Realtime-safe: an exception out of this call would kill the PortAudio stream (→ silence), and
        # emitting zeros while priming/underrunning is also silence. So on prime / underrun / ANY error we
        # PASS THROUGH the raw voice — the user always hears speech (raw until the cleaner is primed, then
        # cleaned). Never silence, never a throw.
        try:
            with self._lock:
                self._total_in += n
                x48 = self._to48.process(x) if self._to48 is not None else x
                self._in48 = np.concatenate([self._in48, x48])
                n_frames = self._in48.shape[0] // DFN3_HOP
                if n_frames:
                    take = n_frames * DFN3_HOP
                    chunk, self._in48 = self._in48[:take], self._in48[take:]
                    enh = np.empty(take, dtype=np.float32)
                    for f in range(n_frames):
                        fr = chunk[f * DFN3_HOP:(f + 1) * DFN3_HOP]
                        out, self._states, _lsnr = self._sess.run(
                            None, {"input_frame": fr, "states": self._states, "atten_lim_db": self._atten})
                        enh[f * DFN3_HOP:(f + 1) * DFN3_HOP] = np.asarray(out, dtype=np.float32).reshape(-1)
                    y = self._from48.process(enh) if self._from48 is not None else enh
                    self._outq = np.concatenate([self._outq, y])
                if not self._primed:
                    if self._outq.shape[0] < n:
                        return x                       # passthrough until primed (never silence)
                    self._primed = True
                if self._outq.shape[0] < n:            # underrun (jitter): passthrough rather than gap
                    return x
                out_block, self._outq = self._outq[:n].copy(), self._outq[n:]
                return out_block
        except Exception as exc:                       # never throw into the audio callback
            self.error = f"dfn3 process error: {exc}"
            return x

    def reset(self) -> None:
        with self._lock:
            self._init_state()
