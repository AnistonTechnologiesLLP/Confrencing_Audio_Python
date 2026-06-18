"""Streaming acoustic echo canceller (AEC) for the live beam.

Cancels the far-end loudspeaker echo from the beamformed mono using a far-end
**reference** signal — what the PC plays into the room, captured by WASAPI
loopback / Stereo Mix (see :mod:`reference_capture`). It is a frequency-domain
**partitioned-block NLMS** ported from New_OCTOVOX ``prod_pipeline.aec_partitioned``
and made streaming: the per-(bin, tap) complex weights and a reference-frame FIFO
are carried across blocks (overlap-add STFT, same Hann 50 %-hop machinery as the
post-beam cleaners).

``process(mic_block, ref_block, near_end_active) -> cancelled_block``. The adaptive
update is **gated to far-end-only frames** (reference active AND near-end silent),
so the filter converges on the echo path and does NOT chase near-end speech during
double-talk; the leaky, magnitude-clipped update keeps it stable either way. When
the reference is silent or ``None`` it is a clean PASS-THROUGH — it never fabricates
cancellation. :attr:`erle_db` is a live echo-return-loss-enhancement readout.

It runs at the post-beam seam **before** dereverb/cleaner (AEC → dereverb → denoise
→ AGC). **Single-beam tradeoff:** textbook AEC runs per-mic *before* the beam (the
echo couples into every capsule); this cancels on the beamformed mono — the
pragmatic seam for a one-beam engine, but it sees a beam that may re-steer during a
call. It is a solid first opt-in stage; per-mic pre-beam AEC is the future upgrade.

Pure numpy (no scipy / torch), so it runs on the realtime audio thread.
"""
from __future__ import annotations

import threading
from typing import Any

DEFAULT_AEC_FRAME = 512          # STFT frame (hop = frame/2)
DEFAULT_AEC_NTAPS = 12           # echo tail ≈ n_taps × hop ≈ 12 × 5.8 ms ≈ 70 ms @ 44.1 kHz
DEFAULT_AEC_MU = 0.3             # NLMS step size
DEFAULT_AEC_LEAK = 0.999         # leaky-integration factor on the weights (stability)
DEFAULT_AEC_REF_FLOOR = 1e-7     # per-frame mean ref power below this ⇒ far-end silent ⇒ don't adapt
DEFAULT_AEC_ERLE_ALPHA = 0.95    # EMA on the ERLE power accumulators (recent, not lifetime)


class StreamingAec:
    """Streaming partitioned-block NLMS echo canceller (see module docstring)."""

    def __init__(self, sample_rate: float, *, frame: int = DEFAULT_AEC_FRAME,
                 n_taps: int = DEFAULT_AEC_NTAPS, mu: float = DEFAULT_AEC_MU,
                 leak: float = DEFAULT_AEC_LEAK, ref_floor: float = DEFAULT_AEC_REF_FLOOR,
                 erle_alpha: float = DEFAULT_AEC_ERLE_ALPHA):
        self._sr = float(sample_rate)
        self._F = max(2, (int(frame) // 2) * 2)          # even ≥ 2 (Hann 50%-hop COLA)
        self._H = self._F // 2
        self._K = max(1, int(n_taps))
        self._mu = max(0.0, float(mu))
        self._leak = min(1.0, max(0.0, float(leak)))
        self._ref_floor = max(0.0, float(ref_floor))
        self._erle_alpha = min(1.0, max(0.0, float(erle_alpha)))
        self._lock = threading.Lock()                    # serialize process() (audio) vs reset() (control)
        self._init_state()

    def _init_state(self) -> None:
        import numpy as np

        nb = self._F // 2 + 1
        self._win = np.hanning(self._F).astype(float)
        self._inbuf_m = np.zeros(self._F, dtype=float)   # sliding mic analysis frame
        self._inbuf_r = np.zeros(self._F, dtype=float)   # sliding reference analysis frame
        self._inq_m = np.zeros(0, dtype=float)           # pending mic samples
        self._inq_r = np.zeros(0, dtype=float)           # pending reference samples
        self._ola = np.zeros(self._F, dtype=float)       # overlap-add synthesis accumulator
        self._outq = np.zeros(self._F, dtype=float)      # synthesized output FIFO (primed = fixed latency)
        self._W = np.zeros((nb, self._K), dtype=np.complex128)       # per-(bin, tap) weights
        self._rfifo = np.zeros((self._K, nb), dtype=np.complex128)   # last K reference frames (newest at [0])
        self._mic_pow = 0.0                              # ERLE EMA accumulators (echo-present frames only)
        self._err_pow = 0.0
        self._n_obs = 0

    def reset(self) -> None:
        with self._lock:
            self._init_state()

    @property
    def erle_db(self) -> float:
        """Echo-return-loss-enhancement (dB), EMA over echo-present frames; 0 until any echo is seen."""
        import numpy as np

        if self._n_obs == 0 or self._mic_pow <= 0.0:
            return 0.0
        return float(10.0 * np.log10((self._mic_pow + 1e-20) / (self._err_pow + 1e-20)))

    def process(self, mic_block: Any, ref_block: Any, near_end_active: bool = False) -> Any:
        """Cancel far-end echo from ``mic_block`` using ``ref_block`` (same length, time-aligned).

        ``near_end_active`` True (a near-end talker is speaking) FREEZES the adaptive update for that
        block to survive double-talk; the current filter still subtracts the echo. ``ref_block`` None or
        silent ⇒ pass-through. Returns a same-length ``float32`` block."""
        import numpy as np

        with self._lock:
            x = np.asarray(mic_block, dtype=float).reshape(-1)
            n = x.shape[0]
            if ref_block is None:
                r = np.zeros(n, dtype=float)
            else:
                r = np.asarray(ref_block, dtype=float).reshape(-1)
                if r.shape[0] < n:
                    r = np.concatenate([r, np.zeros(n - r.shape[0])])
                elif r.shape[0] > n:
                    r = r[:n]
            F, H = self._F, self._H
            self._inq_m = np.concatenate([self._inq_m, x])
            self._inq_r = np.concatenate([self._inq_r, r])
            while self._inq_m.shape[0] >= H:
                hop_m, self._inq_m = self._inq_m[:H], self._inq_m[H:]
                hop_r, self._inq_r = self._inq_r[:H], self._inq_r[H:]
                self._inbuf_m[:-H] = self._inbuf_m[H:]
                self._inbuf_m[-H:] = hop_m
                self._inbuf_r[:-H] = self._inbuf_r[H:]
                self._inbuf_r[-H:] = hop_r
                Mt = np.fft.rfft(self._inbuf_m * self._win)            # (nb,) complex
                Rt = np.fft.rfft(self._inbuf_r * self._win)
                self._rfifo[1:] = self._rfifo[:-1]                     # shift the ref FIFO (newest at [0])
                self._rfifo[0] = Rt
                yhat = np.einsum("fk,kf->f", self._W, self._rfifo)     # predicted echo = Σ_k W[:,k]·R[t−k]
                e = Mt - yhat
                rpow = float(np.mean(Rt.real * Rt.real + Rt.imag * Rt.imag))
                echo_present = rpow > self._ref_floor
                if echo_present and not near_end_active:               # adapt on FAR-END-ONLY frames
                    denom = np.sum(np.abs(self._rfifo) ** 2, axis=0) + 1e-12       # tap-window power (nb,)
                    step = self._mu * e / denom                                    # (nb,)
                    self._W = self._leak * self._W + step[:, None] * np.conj(self._rfifo).T
                    np.clip(self._W.real, -10.0, 10.0, out=self._W.real)           # stability clamp
                    np.clip(self._W.imag, -10.0, 10.0, out=self._W.imag)
                if echo_present:                                       # ERLE only where there is echo to cancel
                    a = self._erle_alpha
                    self._mic_pow = a * self._mic_pow + (1.0 - a) * float(np.mean(np.abs(Mt) ** 2))
                    self._err_pow = a * self._err_pow + (1.0 - a) * float(np.mean(np.abs(e) ** 2))
                    self._n_obs += 1
                y = np.fft.irfft(e, n=F)
                self._ola[:-H] = self._ola[H:]
                self._ola[-H:] = 0.0
                self._ola += y                                         # overlap-add (Hann COLA)
                self._outq = np.concatenate([self._outq, self._ola[:H].copy()])
            if self._outq.shape[0] >= n:
                out = self._outq[:n]
                self._outq = self._outq[n:]
            else:                                                      # one-time underflow at start (front-pad)
                out = np.concatenate([np.zeros(n - self._outq.shape[0]), self._outq])
                self._outq = self._outq[:0]
            return out.astype(np.float32)
