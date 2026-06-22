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
DFN3_LOOKAHEAD = 1440    # model output lag vs input @48k (3 frames; measured by cross-correlation) — dry/wet align
DEFAULT_ATTEN_LIM_DB = 32.0    # cap on the model's max suppression. Matches New_OCTOVOX's offline default:
                               # UNCAPPED (~100) over-suppresses the noise floor into musical-noise territory
                               # and treats a quiet/overlapping 2nd speaker as noise → removes them. 32 dB
                               # denoises hard while staying natural. None/0 = uncapped (most aggressive).
DEFAULT_DFN3_MIX = 1.0   # cleaning amount: 1.0 = full clean; <1 mixes the (lag-aligned) original back in (less muffled)

_DEFAULT_MODEL = Path(__file__).resolve().parent / "models" / "deepfilternet3_streaming.onnx"


class _StreamingResampler:
    """Phase-coherent streaming polyphase resampler (``up``/``down``).

    The naive "``resample_poly(concat(hist, x))[hist_out:]``" overlap-save is WRONG for a rational
    resampler and was the dominant DFN3-distortion source: re-running the *stateless* ``resample_poly``
    on a window whose start is not a multiple of ``down`` RESETS the polyphase commutator phase every
    block; the integer-floor ``hist_out`` trim DRIFTS a fraction of a sample per block (≈+90 samples/s);
    and slicing to the END of ``y`` emits the FIR's unsettled right edge each block. Together these
    dragged a 44.1↔48 kHz round-trip to ≈−10 dB THD+N (vs −80 dB for a correct resampler) — gross,
    speech-band grit on every cleaned block.

    This keeps the same ``resample_poly`` FIR (so the output equals the single-shot *interior*) but emits
    only the SETTLED interior with exact cumulative integer accounting: it holds back ``_margin`` future
    samples for right-edge settling, keeps ``_win_start`` an exact multiple of ``down`` so the polyphase
    phase never resets, and tracks emitted output by a running count so nothing is floored away or
    duplicated. Drift-free by construction; adds a fixed ~``_margin``-sample lookahead (~10 ms/stage,
    absorbed by the cleaner's existing prime fill). Measured round-trip THD+N: −67…−80 dB across
    500–3000 Hz, matching a single-shot resample."""

    def __init__(self, up: int, down: int, np: Any):
        from math import gcd
        g = gcd(up, down)
        self.up, self.down = up // g, down // g
        self._np = np
        self._margin = 4 * max(self.up, self.down) + 1   # future hold-back ≥ resample_poly's settled edge
        self._win: Any = np.zeros(0, dtype=np.float32)   # sliding input window
        self._win_start: int = 0   # global index of _win[0]; INVARIANT: always a multiple of self.down
        self._out_done: int = 0    # cumulative output samples emitted (drift-free accounting)

    def process(self, x: Any) -> Any:
        np = self._np
        from scipy.signal import resample_poly
        x = np.asarray(x, dtype=np.float32).reshape(-1)
        if x.size == 0:
            return x
        self._win = np.concatenate([self._win, x])
        usable_end = self._win_start + self._win.shape[0] - self._margin
        if usable_end <= self._win_start:                # not enough settled input yet
            return np.zeros(0, dtype=np.float32)
        target = max((usable_end * self.up - 1) // self.down, self._out_done)
        y = resample_poly(self._win, self.up, self.down).astype(np.float32)
        base = (self._win_start * self.up) // self.down  # exact: _win_start is a multiple of down
        out = y[self._out_done - base:target - base].copy()
        self._out_done = target
        new_start = ((usable_end - self._margin) // self.down) * self.down   # keep _margin past-context, stay on a down-multiple
        if new_start > self._win_start:
            self._win = self._win[new_start - self._win_start:]
            self._win_start = new_start
        return out

    def reset(self) -> None:
        self._win = self._np.zeros(0, dtype=self._np.float32)
        self._win_start = 0
        self._out_done = 0


class StreamingDeepFilter:
    """DeepFilterNet3 cleaner over the ``process(block, noise_gate)`` seam, via ONNX Runtime.

    ``noise_gate`` is ignored (a full neural denoiser needs no VAD gate). Streams the mono at the engine
    rate through 44.1↔48 kHz resamplers and the 480-sample DFN3 frames, carrying the model state; returns
    a same-length, fixed-latency block (zero-primed during the initial fill). Thread-safe (one lock
    serialises ``process``/``reset``)."""

    def __init__(self, sample_rate: float, *, model_path: Optional[str] = None,
                 atten_lim_db: float = DEFAULT_ATTEN_LIM_DB, mix: float = DEFAULT_DFN3_MIX,
                 **_ignored: Any):
        self.sample_rate = float(sample_rate)
        self._atten_path = str(model_path) if model_path else str(_DEFAULT_MODEL)
        self._atten_lim_db = float(atten_lim_db)
        self._mix = min(1.0, max(0.0, float(mix)))      # cleaning amount: <1 blends the lag-aligned original back in
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
        self._dry48 = np.zeros(DFN3_LOOKAHEAD, dtype=np.float32)  # 48 kHz input history to lag-align the dry/wet mix
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
                    if self._mix < 1.0:                    # "cleaning amount": blend the LAG-ALIGNED original back in
                        buf = np.concatenate([self._dry48, chunk])
                        dry = buf[:take]                    # input delayed by DFN3_LOOKAHEAD → aligned to enh
                        self._dry48 = buf[-DFN3_LOOKAHEAD:].copy()
                        enh = (self._mix * enh + (1.0 - self._mix) * dry).astype(np.float32)
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
