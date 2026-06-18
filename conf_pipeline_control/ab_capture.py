"""Live A/B proof & measurement — capture raw-beam vs cleaned simultaneously.

Records the beamformed mono **before** the live cleaners (AEC / dereverb / NR) and
**after**, from the SAME audio, then reports a MEASURED reduction — the **noise-bed**
drop (how much quieter the background got), the broadband RMS change, and ERLE when
AEC is on — and writes both clips + the numbers. This turns the cleaning from a
"trust us" black box into proof an integrator can run in the customer's own room —
the transparency edge an onboard-AI competitor can't offer.

The capture is armed on the control thread; :meth:`ABCapture.feed` is called per block
on the audio thread (bounded list appends until a sample cap, then ``done``);
:meth:`ABCapture.finalize` + :func:`write_ab_proof` run on the control thread after
``done``. Pure numpy; WAV export reuses the 16-bit-mono pattern from
``ab_test.save_ab_report``.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Any


def _rms_db(x: Any) -> float:
    """Broadband RMS level in dBFS (−120 for an empty/silent signal)."""
    import numpy as np

    if x.size == 0:
        return -120.0
    return 20.0 * float(np.log10(float(np.sqrt(np.mean(x ** 2))) + 1e-12))


def _quiet_rms_db(x: Any, sr: float, win_ms: float = 30.0, pct: float = 20.0) -> float:
    """The **noise-bed** level: the ``pct``-th percentile of short-window RMS — i.e. how loud the quiet
    gaps are (the background bed a denoiser actually lowers). Speech-dominant windows sit above it, so this
    isolates the background rather than the voice. Falls back to broadband RMS for very short clips."""
    import numpy as np

    n = x.shape[0]
    w = max(1, int(win_ms * 1e-3 * sr))
    if n < 2 * w:
        return _rms_db(x)
    nb = n // w
    blocks = x[:nb * w].reshape(nb, w)
    rms = np.sqrt(np.mean(blocks ** 2, axis=1))
    floor = float(np.percentile(rms, pct))
    return 20.0 * float(np.log10(floor + 1e-12))


@dataclass
class ABProofResult:
    """The measured raw-vs-cleaned comparison + the two captured mono clips."""

    sr: int
    raw: Any                       # mono float32 (pre-cleaner beam output)
    clean: Any                     # mono float32 (post-cleaner output)
    rms_raw_db: float
    rms_clean_db: float
    rms_reduction_db: float        # rms_raw_db − rms_clean_db (overall level change)
    bed_raw_db: float
    bed_clean_db: float
    bed_reduction_db: float        # headline: how much quieter the background bed got (raw − clean)
    erle_db: float                 # AEC echo-return-loss (0 when AEC off)
    stages: str                    # which cleaners were active (e.g. "AEC + dereverb + AI cleaner")
    seconds: float

    def summary(self) -> str:
        s = (f"A/B proof — {self.seconds:.1f}s · cleaning: {self.stages or '(none)'}\n"
             f"  noise-bed:   {self.bed_raw_db:6.1f} -> {self.bed_clean_db:6.1f} dBFS   "
             f"({self.bed_reduction_db:+.1f} dB quieter background)\n"
             f"  overall RMS: {self.rms_raw_db:6.1f} -> {self.rms_clean_db:6.1f} dBFS   "
             f"({self.rms_reduction_db:+.1f} dB)\n")
        if self.erle_db:
            s += f"  AEC ERLE:    {self.erle_db:+.1f} dB\n"
        return s

    def headline(self) -> str:
        """One-line read-out for the status bar."""
        h = f"background {self.bed_reduction_db:+.1f} dB"
        if self.erle_db:
            h += f" · AEC {self.erle_db:+.1f} dB"
        return h


class ABCapture:
    """Armed pre/post mono capture, fed from the audio thread and finalized on the control thread."""

    def __init__(self, sr: float, seconds: float = 8.0):
        self.sr = float(sr)
        self._max = int(max(0.5, float(seconds)) * self.sr)
        self._raw: list = []
        self._clean: list = []
        self._n = 0
        self.done = False
        self._lock = threading.Lock()

    def feed(self, raw_block: Any, clean_block: Any) -> None:
        """Append one block of pre-cleaner (raw) + post-cleaner (clean) mono. Realtime-safe (bounded,
        copies the blocks, no work beyond the appends); flips :attr:`done` once enough is captured."""
        if self.done:
            return
        import numpy as np

        r = np.asarray(raw_block, dtype=np.float32).reshape(-1).copy()
        c = np.asarray(clean_block, dtype=np.float32).reshape(-1).copy()
        with self._lock:
            if self.done:
                return
            self._raw.append(r)
            self._clean.append(c)
            self._n += r.shape[0]
            if self._n >= self._max:
                self.done = True

    def finalize(self, erle_db: float = 0.0, stages: str = "") -> ABProofResult:
        """Concatenate the captured blocks and compute the metrics (control thread, after ``done``)."""
        import numpy as np

        with self._lock:
            raw = np.concatenate(self._raw) if self._raw else np.zeros(0, dtype=np.float32)
            clean = np.concatenate(self._clean) if self._clean else np.zeros(0, dtype=np.float32)
        n = min(raw.shape[0], clean.shape[0])
        raw, clean = raw[:n], clean[:n]
        rr, rc = _rms_db(raw), _rms_db(clean)
        br, bc = _quiet_rms_db(raw, self.sr), _quiet_rms_db(clean, self.sr)
        return ABProofResult(int(self.sr), raw, clean, rr, rc, rr - rc, br, bc, br - bc,
                             float(erle_db), str(stages), (n / self.sr) if self.sr else 0.0)


def write_ab_proof(result: ABProofResult, out_dir: str) -> list:
    """Write ``ab_raw.wav`` + ``ab_clean.wav`` + ``ab_proof.txt`` (16-bit mono, the ab_test pattern).
    Returns the list of written paths."""
    import os
    import wave

    import numpy as np

    os.makedirs(out_dir, exist_ok=True)
    written = []
    for name, mono in (("ab_raw.wav", result.raw), ("ab_clean.wav", result.clean)):
        path = os.path.join(out_dir, name)
        x = np.clip(np.asarray(mono, dtype=np.float32), -1.0, 1.0)
        with wave.open(path, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(int(result.sr))
            w.writeframes((x * 32767.0).astype("<i2").tobytes())
        written.append(path)
    rp = os.path.join(out_dir, "ab_proof.txt")
    with open(rp, "w", encoding="utf-8") as f:
        f.write(result.summary() + "\n")
    written.append(rp)
    return written
