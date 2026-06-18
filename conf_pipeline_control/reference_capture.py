"""Far-end reference capture for live AEC.

Captures the audio the PC is **playing into the room** (the conferencing downlink /
far-end) to use as the echo-cancellation reference. It auto-detects a source in
priority order and degrades gracefully:

  1. **WASAPI loopback** on the default output device — zero wiring; captures
     whatever the PC plays (Zoom / Teams / a media player).
  2. a **"Stereo Mix"**-style loopback *input* device (Realtek "Stereo Mix",
     "What U Hear", anything whose name contains "loopback").
  3. an explicit input **device index** (manual override).

The captured audio is downmixed to mono, resampled to the engine sample rate, and
written to a thread-safe ring. :meth:`recent` returns the most recent ``n`` mono
samples (newest last) for the AEC, fed one block per mic block; the AEC's
multi-tap filter absorbs the residual playout/acoustic delay (bounded by its tap
span). If no source opens, :attr:`available` is ``False`` and :meth:`recent`
returns zeros — the AEC then passes the mic through unchanged.

**Honest limitations (v1):** the reference and mic streams are independent audio
clocks, so they can drift over a long session (classic AEC clock-drift); and the
bulk playout+acoustic delay must fit inside the AEC tap span. Both are documented
refinements (resampling drift-compensation, bulk-delay estimation). Needs the
``[control]`` extra (numpy + sounddevice); scipy is used for resampling when the
source rate differs from the engine rate, with a numpy linear-interp fallback.
"""
from __future__ import annotations

import threading
from typing import Any, Optional


class _Ring:
    """A small lock-guarded mono float32 circular buffer (one writer audio thread,
    one reader audio thread)."""

    def __init__(self, n: int):
        import numpy as np

        self._buf = np.zeros(max(1, int(n)), dtype=np.float32)
        self._n = self._buf.shape[0]
        self._w = 0
        self._filled = 0
        self._lock = threading.Lock()

    def write(self, x: Any) -> None:
        import numpy as np

        x = np.asarray(x, dtype=np.float32).reshape(-1)
        m = x.shape[0]
        if m == 0:
            return
        with self._lock:
            if m >= self._n:                       # incoming bigger than the ring → keep the newest tail
                self._buf[:] = x[-self._n:]
                self._w = 0
                self._filled = self._n
                return
            end = self._w + m
            if end <= self._n:
                self._buf[self._w:end] = x
            else:                                  # wrap
                k = self._n - self._w
                self._buf[self._w:] = x[:k]
                self._buf[:end - self._n] = x[k:]
            self._w = end % self._n
            self._filled = min(self._n, self._filled + m)

    def recent(self, n: int) -> Any:
        """The most recent ``n`` samples, newest LAST; zero-front-padded if the ring
        hasn't filled ``n`` samples yet."""
        import numpy as np

        out = np.zeros(max(0, int(n)), dtype=np.float32)
        if out.shape[0] == 0:
            return out
        with self._lock:
            avail = min(out.shape[0], self._filled)
            if avail == 0:
                return out
            start = (self._w - avail) % self._n
            if start + avail <= self._n:
                seg = self._buf[start:start + avail]
            else:
                k = self._n - start
                seg = np.concatenate([self._buf[start:], self._buf[:avail - k]])
            out[out.shape[0] - avail:] = seg       # right-align: newest at the end
            return out

    def clear(self) -> None:
        with self._lock:
            self._buf[:] = 0.0
            self._w = 0
            self._filled = 0


def _resample_to(x: Any, sr_from: float, sr_to: float) -> Any:
    """Resample a mono block ``sr_from``→``sr_to`` (no-op when equal). scipy polyphase
    if available, else numpy linear interpolation (per-block; edge effects are
    acceptable for a reference predictor)."""
    import numpy as np

    if abs(sr_from - sr_to) < 1e-6 or x.shape[0] == 0:
        return x.astype(np.float32)
    try:
        from math import gcd

        import scipy.signal as ss

        up, down = int(round(sr_to)), int(round(sr_from))
        g = gcd(up, down) or 1
        return ss.resample_poly(x, up // g, down // g).astype(np.float32)
    except Exception:
        n_out = max(1, int(round(x.shape[0] * sr_to / sr_from)))
        xp = np.linspace(0.0, 1.0, num=x.shape[0], endpoint=False)
        fp = np.linspace(0.0, 1.0, num=n_out, endpoint=False)
        return np.interp(fp, xp, x).astype(np.float32)


class ReferenceCapture:
    """Open a far-end reference source and keep a ring of recent mono samples @ the
    engine rate. See the module docstring for the auto-detect order + limitations."""

    def __init__(self, sample_rate: float, *, device: Optional[int] = None,
                 blocksize: int = 1024, ring_seconds: float = 2.0):
        self.sample_rate = float(sample_rate)
        self.device = device                      # explicit manual override, or None to auto-detect
        self.blocksize = int(blocksize)
        self.available = False
        self.source = ""                          # human-readable label of the opened source
        self.error = ""
        self.callback_error = ""                  # last error from the audio callback (control-thread readable)
        self._cb_fail_streak = 0                  # consecutive callback failures → flip available off after N
        self._fail_limit = 50
        self._sd: Any = None
        self._stream: Any = None
        self._src_sr = self.sample_rate
        self._ring = _Ring(int(self.ring_len()))

    def ring_len(self) -> float:
        return self.sample_rate * 2.0        # ~2 s of recent reference is ample for the AEC's tap span

    # ---- source resolution ----
    def _candidates(self):
        """Yield ``(device, channels, samplerate, extra_settings, label)`` to try, in
        priority order. Pure querying — opening is attempted in :meth:`start`."""
        sd = self._sd
        if self.device is not None:                # 1) explicit manual device
            try:
                info = sd.query_devices(self.device)
                yield (self.device, max(1, int(info["max_input_channels"]) or 1),
                       float(info["default_samplerate"]) or self.sample_rate, None,
                       f"device {self.device}: {info['name']}")
            except Exception:
                pass
            return
        try:                                       # 2) WASAPI loopback on the default output
            apis = sd.query_hostapis()
            wi = next((i for i, a in enumerate(apis) if "WASAPI" in a.get("name", "")), None)
            if wi is not None:
                out = apis[wi].get("default_output_device", -1)
                if out is not None and out >= 0:
                    info = sd.query_devices(out)
                    extra = None
                    try:
                        extra = sd.WasapiSettings(loopback=True)
                    except Exception:
                        extra = None
                    if extra is not None:
                        yield (out, max(1, int(info["max_output_channels"]) or 2),
                               float(info["default_samplerate"]) or self.sample_rate, extra,
                               f"WASAPI loopback: {info['name']}")
        except Exception:
            pass
        try:                                       # 3) Stereo Mix / loopback input device
            for i, d in enumerate(sd.query_devices()):
                name = str(d.get("name", "")).lower()
                if int(d.get("max_input_channels", 0)) >= 1 and (
                        "stereo mix" in name or "loopback" in name or "what u hear" in name):
                    yield (i, min(2, int(d["max_input_channels"])),
                           float(d["default_samplerate"]) or self.sample_rate, None,
                           f"Stereo Mix: {d['name']}")
        except Exception:
            pass

    def _make_cb(self):
        import numpy as np

        def _cb(indata, frames, time_info, status):  # pragma: no cover (needs an audio device)
            try:
                x = np.asarray(indata, dtype=np.float32)
                mono = x.mean(axis=1) if x.ndim == 2 and x.shape[1] > 1 else x.reshape(-1)
                if abs(self._src_sr - self.sample_rate) > 1e-6:
                    mono = _resample_to(mono, self._src_sr, self.sample_rate)
                self._ring.write(mono)
                self._cb_fail_streak = 0
            except Exception as exc:                # never raise out of a PortAudio callback (would kill it)
                # ...but don't fail silently forever: record it (cheap repr, only on failure) and, after a
                # persistent run of failures, flip available off so the operator sees the reference is dead.
                self.callback_error = repr(exc)
                self._cb_fail_streak += 1
                if self._cb_fail_streak >= self._fail_limit:
                    self.available = False
        return _cb

    # ---- lifecycle ----
    def start(self) -> bool:
        """Try each candidate source until one opens. Never raises — returns whether a
        source is live (also reflected in :attr:`available`)."""
        try:
            import sounddevice as sd
        except Exception as exc:
            self.error = f"sounddevice unavailable: {exc}"
            return False
        self._sd = sd
        cb = self._make_cb()
        for dev, ch, sr, extra, label in self._candidates():
            try:
                self._src_sr = float(sr)
                stream = sd.InputStream(
                    samplerate=self._src_sr, channels=int(ch), blocksize=self.blocksize,
                    device=dev, dtype="float32", callback=cb, extra_settings=extra,
                )
                stream.start()
                self._stream = stream
                self.available = True
                self.source = label
                self.error = ""
                return True
            except Exception as exc:                # try the next candidate
                self.error = f"{label}: {exc}"
        self.available = False
        return False

    def stop(self) -> None:
        s = self._stream
        self._stream = None
        if s is not None:
            try:
                s.stop()
                s.close()
            except Exception:
                pass
        self.available = False
        self._ring.clear()

    def recent(self, n: int) -> Any:
        """The most recent ``n`` mono reference samples @ the engine rate (newest last;
        zeros if no source is live)."""
        return self._ring.recent(n)
