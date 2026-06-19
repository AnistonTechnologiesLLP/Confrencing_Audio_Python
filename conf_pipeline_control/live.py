"""Real-time beamforming runtime (numpy + sounddevice — the ``[control]`` extra).

This is the live counterpart of the pure design code: it opens the array as a
multi-channel input, applies the zone-driven beam in the **frequency domain**
(per-FFT-bin weights, Hann-windowed 50 %-overlap-add — a genuinely broadband
steer + null, not a single-frequency approximation), and exposes a live output
level for the meter. Mute/gain are honoured; the beamformed mono output can be
recorded to a WAV.

Everything heavy is imported lazily inside :meth:`_open`, so importing this
module (and the whole app) works without the extra. If the extra is missing,
:meth:`_open` raises a clear, actionable error.

Fidelity, stated plainly: an ``M``-capsule array forms at most ``M − 1`` nulls;
null depth and beamwidth are bounded by the array's aperture and degrade away
from the design band. Excluded areas are **strongly attenuated**, not perfectly
silenced. The per-bin design here is correct array processing for what the
hardware can physically do.
"""
from __future__ import annotations

import math
import queue
import threading
import wave
from typing import Any, Optional

from .audio import controls_available, missing_dependencies
from .beamformer import BeamDesign
from .control import MicController
from .geometry import SOUND_SPEED_MPS, ArrayGeometry
from .polaris_beamformer import (
    DEFAULT_DEREVERB_BETA,
    DEFAULT_DEREVERB_GMIN_DB,
    DEFAULT_DEREVERB_T60,
    DEFAULT_POST_NR_AMOUNT,
    DEFAULT_POST_NR_ENGINE,
    DEFAULT_POST_NR_FLOOR_DB,
    DEFAULT_POST_NR_FRAME,
    DEFAULT_POST_NR_MINSTAT,
    DEFAULT_POST_NR_OVERSUB,
    DEFAULT_POST_NR_PRESERVE_LEVEL,
    DEFAULT_POST_NR_WARMUP_FRAMES,
)
from .steering import Direction

_FRAME = 1024          # FFT length
_HOP = _FRAME // 2     # 50 % overlap


def _install_hint() -> str:
    miss = missing_dependencies()
    pkgs = " + ".join(miss) if miss else "numpy + sounddevice"
    return (
        f"Live audio needs {pkgs}. Install the extra:\n"
        f'    pip install -e ".[control]"'
    )


class LiveBeamController(MicController):
    """Drive a physical array: capture → per-bin beamform → metered mono output."""

    backend = "live"

    def __init__(
        self,
        geometry: ArrayGeometry,
        *,
        device: Optional[int] = None,
        samplerate: float = 48000.0,
        record_path: Optional[str] = None,
        monitor: bool = False,
        output_device: Optional[int] = None,
        track_covariance: bool = False,
        post_nr: bool = False,
        post_nr_engine: str = DEFAULT_POST_NR_ENGINE,
        post_nr_floor_db: float = DEFAULT_POST_NR_FLOOR_DB,
        post_nr_oversub: float = DEFAULT_POST_NR_OVERSUB,
        post_nr_frame: int = DEFAULT_POST_NR_FRAME,
        post_nr_warmup_frames: int = DEFAULT_POST_NR_WARMUP_FRAMES,
        post_nr_minstat: bool = DEFAULT_POST_NR_MINSTAT,
        post_nr_amount: float = DEFAULT_POST_NR_AMOUNT,
        post_nr_preserve_level: bool = DEFAULT_POST_NR_PRESERVE_LEVEL,
        dereverb: bool = False,
        dereverb_t60: float = DEFAULT_DEREVERB_T60,
        dereverb_beta: float = DEFAULT_DEREVERB_BETA,
        dereverb_gmin_db: float = DEFAULT_DEREVERB_GMIN_DB,
        aec: bool = False,
        aec_n_taps: int = 16,
        aec_mu: float = 0.3,
        aec_ref_device: Optional[int] = None,
    ):
        super().__init__(geometry, n_channels=geometry.n_channels)
        self.device = device
        self.samplerate = float(samplerate)
        self.record_path = record_path
        self.monitor = monitor                 # play the beamformed output live
        self.output_device = output_device     # None → system default output
        # opt-in spatial-covariance tap for DOA / auto-steer (off ⇒ zero overhead)
        self.track_covariance = track_covariance
        # post-beam noise reducer on the mono output (the same engine the A/B BeamEngine uses), so the
        # auto-steer / zone live paths get OCTOVOX-style cleaning too. OFF by default. "gate" = light
        # spectral gate; "omlsa"/"wiener" = the OCTOVOX-derived StreamingCleaner. Built in _build_post_nr().
        self.post_nr = bool(post_nr)
        self._post_nr_engine = str(post_nr_engine)
        self._post_nr_floor_db = float(post_nr_floor_db)
        self._post_nr_oversub = float(post_nr_oversub)
        self._post_nr_frame = int(post_nr_frame)
        self._post_nr_warmup_frames = int(post_nr_warmup_frames)
        self._post_nr_minstat = bool(post_nr_minstat)
        self._post_nr_amount = min(1.0, max(0.0, float(post_nr_amount)))
        self._post_nr_preserve_level = bool(post_nr_preserve_level)
        # real-time dereverb on the mono output, BEFORE the noise reducer (same StreamingDereverb the A/B
        # engine uses). OFF by default. Built in _build_post_nr().
        self.dereverb = bool(dereverb)
        self._dereverb_t60 = float(dereverb_t60)
        self._dereverb_beta = float(dereverb_beta)
        self._dereverb_gmin_db = float(dereverb_gmin_db)
        # live AEC on the mono output, BEFORE dereverb; fed a far-end reference (loopback/Stereo Mix).
        self.aec = bool(aec)
        self._aec_n_taps = int(aec_n_taps)
        self._aec_mu = float(aec_mu)
        self._aec_ref_device = aec_ref_device
        # Lazily bound in _open() (numpy / sounddevice objects) — typed Any so the
        # module stays importable + checkable without the [control] extra.
        self._cov: Any = None      # EMA of band covariance, (n_band, M, M) complex
        self._cov_freqs: Any = None  # band bin frequencies (n_band,)
        self._cov_band: Any = None   # band bin indices into the rfft axis
        self._cov_alpha = 0.05     # EMA rate (~230 ms at 44.1 kHz / 512 hop)
        self._cov_lock = threading.Lock()
        self._sd: Any = None
        self._np: Any = None
        self._stream: Any = None
        self._out_stream: Any = None   # separate output stream for monitoring
        self._monitor_q: Any = None    # thread-safe hand-off: input → output
        self._out_channels = 0     # monitor output channels (0 = not monitoring)
        self._wav: Optional[wave.Wave_write] = None
        self._level = 0.0          # written by audio thread, read by GUI
        self._weights: Any = None  # numpy (n_bins, M) complex, or None → passthrough sum
        self._win: Any = None      # Hann window (numpy)
        self._inbuf: Any = None    # numpy (FRAME, M) sliding input
        self._ola: Any = None      # numpy (FRAME,) overlap-add tail
        self._post_nr: Any = None  # post-beam noise reducer (StreamingCleaner / _PostNoiseSuppressor), or None
        self._dereverb: Any = None # real-time dereverb (StreamingDereverb), runs before the noise reducer, or None
        self._aec: Any = None      # live echo canceller (StreamingAec), runs before dereverb, or None
        self._ref_capture: Any = None  # far-end reference source (ReferenceCapture) for the AEC, or None
        self._ab_capture: Any = None   # armed A/B proof capture (raw-vs-cleaned), or None

    # ---- weight computation (per FFT bin, broadband) ----
    def _compute_weights(self):
        np = self._np
        if self._design is None or not self._design.beams:
            self._weights = None
            return
        # the design carries the geometry (incl. the active-capsule mask); use it
        geom = self._design.geometry
        assert geom is not None
        M = geom.n_channels
        elems = np.array(geom.elements, dtype=float)         # (M, 3)
        n_bins = _FRAME // 2 + 1
        freqs = np.fft.rfftfreq(_FRAME, d=1.0 / self.samplerate)  # (n_bins,)

        looks = [b.look for b in self._design.beams]
        # ALL nulls applied in the design (exclusion zones + out-of-zone talkers)
        nulls = list(self._design.null_dirs or self._design.exclusion_dirs)
        active = np.array(geom.active_indices(), dtype=int)   # capsules in use
        na = max(1, len(active))
        superd = self._design.mode == "superdirective"
        loading = float(self._design.loading)
        # inter-capsule distances over active capsules (for the diffuse model)
        ap = elems[active]                                    # (na, 3)
        dist = np.sqrt(((ap[:, None, :] - ap[None, :, :]) ** 2).sum(-1))  # (na, na)
        W = np.zeros((n_bins, M), dtype=complex)

        for bi, f in enumerate(freqs):
            if f <= 0:
                W[bi, active] = 1.0 / na      # DC: plain average (steering undefined)
                continue
            k = 2.0 * math.pi * f / SOUND_SPEED_MPS
            if superd:
                x = k * dist                                  # diffuse coherence Γ = sinc(k d)
                gamma = np.sinc(x / math.pi)                  # np.sinc(t)=sin(pi t)/(pi t)
                R = gamma + loading * np.eye(na)
            else:
                R = None
            acc = np.zeros(M, dtype=complex)
            for look in looks:
                acc += self._bin_weights(np, elems, k, look, nulls, M, active, R)
            W[bi, :] = acc / max(1, len(looks))
        self._weights = W

    @staticmethod
    def _bin_weights(np, elems, k, look: Direction, nulls: list[Direction], M: int, active, R):
        """Per-bin weights over the active capsules, scattered into a full-length-M
        vector. ``R`` is the (na×na) noise covariance for an MVDR/superdirective
        design, or None for plain delay-and-sum / LCMV."""
        def sv(u):
            proj = elems @ np.array(u, dtype=float)
            return np.exp(1j * k * proj)

        na = len(active)
        w = np.zeros(M, dtype=complex)
        a0 = sv(look.unit)[active]                            # (na,)
        try:
            if not nulls or na <= len(nulls):
                if R is None:
                    w[active] = a0 / na                       # delay-and-sum
                else:
                    t = np.linalg.solve(R, a0)                # MVDR: R⁻¹a / aᴴR⁻¹a
                    w[active] = t / (a0.conj() @ t)
            else:
                C = np.stack([a0] + [sv(n.unit)[active] for n in nulls], axis=1)  # (na, K)
                g = np.zeros(C.shape[1], dtype=complex); g[0] = 1.0
                if R is None:
                    w[active] = C @ np.linalg.solve(C.conj().T @ C, g)            # LCMV
                else:
                    RiC = np.linalg.solve(R, C)                                   # R⁻¹C
                    w[active] = RiC @ np.linalg.solve(C.conj().T @ RiC, g)        # MVDR-LCMV
        except Exception:
            w[active] = a0 / na                               # singular → DAS fallback
        return w

    def _build_post_nr(self) -> None:
        """Build the post-beam noise reducer (device-free, so it's unit-testable). Mirrors the engine
        selection in :meth:`PolarisBeamformer._setup_runtime`: ``"omlsa"``/``"wiener"`` → the
        OCTOVOX-derived :class:`StreamingCleaner`; ``"gate"`` → the light spectral gate. ``None`` if off."""
        if not self.post_nr:
            self._post_nr = None
        else:
            from .polaris_beamformer import _LevelPreservingCleaner, _PostNoiseSuppressor
            inner: Any
            if self._post_nr_engine == "dfn3":
                try:
                    from .deepfilter_cleaner import StreamingDeepFilter   # lazy: needs the [dfn] extra (onnxruntime)
                    inner = StreamingDeepFilter(self.samplerate, mix=self._post_nr_amount)
                except Exception as exc:                      # DFN3 unavailable → keep audio, fall back to the gate
                    import sys
                    print(f"[dfn3] unavailable - falling back to the light gate: {exc}", file=sys.stderr)
                    inner = _PostNoiseSuppressor(
                        self.samplerate, frame=self._post_nr_frame, floor_db=self._post_nr_floor_db,
                        oversub=self._post_nr_oversub, warmup_frames=self._post_nr_warmup_frames,
                        minstat=self._post_nr_minstat, amount=self._post_nr_amount)
            elif self._post_nr_engine in ("omlsa", "wiener"):
                from .streaming_cleaner import StreamingCleaner   # lazy: avoids a module-load import cycle
                inner = StreamingCleaner(
                    self.samplerate, mode=self._post_nr_engine, frame=self._post_nr_frame,
                    gmin_db=self._post_nr_floor_db, warmup_frames=self._post_nr_warmup_frames,
                    minstat=self._post_nr_minstat, amount=self._post_nr_amount)
            else:
                inner = _PostNoiseSuppressor(
                    self.samplerate, frame=self._post_nr_frame, floor_db=self._post_nr_floor_db,
                    oversub=self._post_nr_oversub, warmup_frames=self._post_nr_warmup_frames,
                    minstat=self._post_nr_minstat, amount=self._post_nr_amount)
            # Wrap so the cleaned voice keeps its loudness (restore the ~5-7 dB the cleaner removes).
            self._post_nr = _LevelPreservingCleaner(inner) if self._post_nr_preserve_level else inner
        # real-time dereverb (StreamingDereverb): runs on the mono BEFORE the noise reducer; off unless enabled.
        if self.dereverb:
            from .streaming_cleaner import StreamingDereverb
            self._dereverb = StreamingDereverb(
                self.samplerate, t60=self._dereverb_t60, beta=self._dereverb_beta,
                gmin_db=self._dereverb_gmin_db, frame=self._post_nr_frame)
        else:
            self._dereverb = None
        # live AEC (StreamingAec): device-free here; the far-end reference STREAM opens in _open().
        if self.aec:
            from .streaming_aec import StreamingAec
            self._aec = StreamingAec(self.samplerate, n_taps=self._aec_n_taps, mu=self._aec_mu)
        else:
            self._aec = None

    def _start_ref_capture(self) -> None:
        """Open the far-end reference source when AEC is on (never raises; degrades to no reference)."""
        if not self.aec or self._ref_capture is not None:
            return
        from .reference_capture import ReferenceCapture
        rc = ReferenceCapture(self.samplerate, device=self._aec_ref_device)
        rc.start()
        self._ref_capture = rc

    def _stop_ref_capture(self) -> None:
        rc = self._ref_capture
        self._ref_capture = None
        if rc is not None:
            rc.stop()

    @property
    def aec_erle_db(self) -> float:
        """Live AEC echo-return-loss-enhancement (dB); 0 when AEC is off or no echo seen."""
        return float(self._aec.erle_db) if self._aec is not None else 0.0

    # ---- A/B proof capture + latency read-out ----
    def start_ab_capture(self, seconds: float = 8.0) -> Any:
        """Arm an A/B proof capture: _process_block then feeds the raw beam + cleaned mono until
        ``seconds`` are buffered. Returns the :class:`ABCapture`."""
        from .ab_capture import ABCapture

        cap = ABCapture(self.samplerate, seconds=seconds)
        self._ab_capture = cap
        return cap

    @property
    def ab_capture(self) -> Any:
        return self._ab_capture

    def active_cleaning_stages(self) -> str:
        names = []
        if self._aec is not None:
            names.append("AEC")
        if self._dereverb is not None:
            names.append("dereverb")
        if self._post_nr is not None:
            names.append("AI cleaner" if getattr(self._post_nr, "mode", "gate") in ("omlsa", "wiener")
                         else "noise gate")
        return " + ".join(names)

    @property
    def estimated_latency_ms(self) -> float:
        """Honest *estimated* DSP latency (ms): one input hop + the freq-domain beam OLA frame + one
        analysis frame per engaged cleaner. Excludes OS/device buffering."""
        sr = self.samplerate or 44100.0
        ms = 1000.0 / sr
        lat = (_HOP + _FRAME) * ms                               # input hop + beam overlap-add frame
        for stage in (self._aec, self._dereverb, self._post_nr):
            if stage is not None:
                lat += float(getattr(stage, "_F", 0)) * ms
        return float(lat)

    # ---- lifecycle ----
    def _open(self) -> None:
        if not controls_available():
            raise RuntimeError(_install_hint())
        import numpy as np
        import sounddevice as sd

        self._np = np
        self._sd = sd
        self._win = np.hanning(_FRAME).astype(float)
        self._inbuf = np.zeros((_FRAME, self.n_channels), dtype=float)
        self._ola = np.zeros(_FRAME, dtype=float)
        self._compute_weights()
        self._build_post_nr()                  # fresh per session (rebuilt on each connect)

        if self.track_covariance:
            from .doa import DEFAULT_F_HI_HZ, DEFAULT_F_LO_HZ, band_indices

            freqs_full = np.fft.rfftfreq(_FRAME, d=1.0 / self.samplerate)
            self._cov_band = band_indices(freqs_full, DEFAULT_F_LO_HZ, DEFAULT_F_HI_HZ)
            self._cov_freqs = freqs_full[self._cov_band]
            with self._cov_lock:
                self._cov = np.zeros((len(self._cov_band), self.n_channels, self.n_channels), dtype=complex)

        if self.record_path:
            self._wav = wave.open(self.record_path, "wb")
            self._wav.setnchannels(1)
            self._wav.setsampwidth(2)
            self._wav.setframerate(int(self.samplerate))

        # Monitoring uses TWO independent streams (input + output) joined by a
        # queue, NOT one duplex stream: a single full-duplex stream requires both
        # devices on the same host API (PortAudio paBadIODeviceCombination, -9993),
        # which we can't assume when the user picks devices freely.
        if self.monitor:
            out_ch = 2
            try:
                info = sd.query_devices(self.output_device, "output")
                out_ch = max(1, min(2, int(info.get("max_output_channels", 2))))
            except Exception:
                out_ch = 2
            self._out_channels = out_ch
            self._monitor_q = queue.Queue(maxsize=8)
        else:
            self._out_channels = 0
            self._monitor_q = None

        self._stream = sd.InputStream(
            samplerate=self.samplerate,
            channels=self.n_channels,
            blocksize=_HOP,
            device=self.device,
            dtype="float32",
            callback=self._cb_input,
        )
        self._stream.start()
        if self.monitor:
            self._out_stream = sd.OutputStream(
                samplerate=self.samplerate,
                channels=self._out_channels,
                blocksize=_HOP,
                device=self.output_device,
                dtype="float32",
                callback=self._cb_output,
            )
            self._out_stream.start()
        self._start_ref_capture()              # open the far-end reference (AEC) alongside the mic stream

    def _close(self) -> None:
        if self._out_stream is not None:
            try:
                self._out_stream.stop()
                self._out_stream.close()
            finally:
                self._out_stream = None
        if self._stream is not None:
            try:
                self._stream.stop()
                self._stream.close()
            finally:
                self._stream = None
        self._stop_ref_capture()               # after the mic stream is stopped (no in-flight _process_block)
        self._monitor_q = None
        if self._wav is not None:
            try:
                self._wav.close()
            finally:
                self._wav = None
        self._level = 0.0

    def _on_design(self, design: BeamDesign) -> None:
        if self._np is not None:
            self._compute_weights()

    # ---- audio thread ----
    def _process_block(self, indata):  # pragma: no cover (needs hardware)
        """Beamform one HOP-sized block → post-gain mono output (HOP,). Also
        updates the (pre-gain) meter level and writes the WAV if recording."""
        np = self._np
        # slide a FRAME-long window: drop oldest HOP, append new HOP
        self._inbuf[:-_HOP, :] = self._inbuf[_HOP:, :]
        self._inbuf[-_HOP:, :] = indata[:_HOP, :]

        block = self._inbuf * self._win[:, None]
        X = np.fft.rfft(block, axis=0)                 # (n_bins, M)

        if self.track_covariance and self._cov is not None:
            xb = X[self._cov_band, :]                  # (n_band, M) band spectrum
            inst = xb[:, :, None] * np.conj(xb[:, None, :])  # (n_band, M, M) outer
            a = self._cov_alpha
            with self._cov_lock:
                self._cov *= (1.0 - a)
                self._cov += a * inst

        if self._weights is None:
            Y = X.mean(axis=1)                         # passthrough: average capsules
        else:
            Y = np.sum(np.conj(self._weights) * X, axis=1)
        y = np.fft.irfft(Y, n=_FRAME)                  # (FRAME,)

        # overlap-add; emit the first HOP samples
        self._ola[:-_HOP] = self._ola[_HOP:]
        self._ola[-_HOP:] = 0.0
        self._ola += y
        out = self._ola[:_HOP].copy()

        cap = self._ab_capture                        # A/B proof: snapshot the RAW beam before any cleaner
        raw_ab = out if (cap is not None and not cap.done) else None

        # post-beam enhancement on the mono, before the meter and gain — so the meter, recording and monitor
        # all reflect the cleaned voice. Order: AEC → dereverb → denoise. noise_gate=False: no VAD on this
        # path, so the AEC relies on its own far-end-activity gate and the min-statistics NR floor.
        if self._aec is not None:
            rc = self._ref_capture                    # read once: teardown may null it on the control thread
            ref = rc.recent(out.shape[0]) if rc is not None else None
            out = self._aec.process(out, ref, near_end_active=False)
        if self._dereverb is not None:
            out = self._dereverb.process(out, False)
        if self._post_nr is not None:
            out = self._post_nr.process(out, False)
        if raw_ab is not None:
            cap.feed(raw_ab, out)                     # A/B proof: raw beam vs the cleaned (pre-gain) mono

        # metered level is PRE-gain (the base class re-applies gain + mute in
        # read_level, consistently with every backend).
        rms = float(np.sqrt(np.mean(out * out))) if out.size else 0.0
        self._level = min(1.0, rms)

        # post-gain / post-mute signal for recording + monitoring
        g = 0.0 if self._muted else 10.0 ** (self._gain_db / 20.0)
        out_g = np.clip(out * g, -1.0, 1.0)

        if self._wav is not None and not self._muted:
            self._wav.writeframes((out_g * 32767.0).astype("<i2").tobytes())
        return out_g

    def _cb_input(self, indata, frames, time_info, status):  # pragma: no cover
        out = self._process_block(indata)
        if self._monitor_q is not None:                # hand the mono block to the output stream
            try:
                self._monitor_q.put_nowait(out)
            except queue.Full:                         # bound latency: drop oldest, keep newest
                try:
                    self._monitor_q.get_nowait()
                except queue.Empty:
                    pass
                try:
                    self._monitor_q.put_nowait(out)
                except queue.Full:
                    pass

    def _cb_output(self, outdata, frames, time_info, status):  # pragma: no cover
        q = self._monitor_q
        try:
            blk = q.get_nowait() if q is not None else None
        except queue.Empty:
            blk = None
        if blk is not None and blk.shape[0] == outdata.shape[0]:
            outdata[:] = blk[:, None]                  # mono → all output channels
        else:                                          # underrun / size mismatch → silence
            outdata.fill(0.0)

    def _raw_level(self) -> float:
        return self._level

    def snapshot_covariance(self):
        """Thread-safe copy of the current band covariance for DOA.

        Returns ``(cov, freqs)`` — ``cov`` is ``(n_band, M, M)`` complex, ``freqs``
        the band bin frequencies — or ``(None, None)`` if covariance tracking is
        off or the stream hasn't produced a frame yet. Safe to call from a control
        thread while the audio thread updates the estimate."""
        if not self.track_covariance:
            return None, None
        with self._cov_lock:
            if self._cov is None:
                return None, None
            return self._cov.copy(), self._cov_freqs
