"""Bridge to the **OCTOVOX** voice-cleaning pipeline (HTTP, offline whole-clip).

The two systems are complementary: this app owns the *spatial* design (room model
+ drawn pickup/exclusion zones → which direction to listen), and OCTOVOX owns the
*cleaning* (calibration, dereverb, DeepFilterNet3, residual suppression, VAD
automix, EQ/AGC). So the right hand-off is **raw 8-channel audio + the zone-derived
azimuths**: OCTOVOX runs its own direction-aware beamform-then-clean chain steered
at the talker we picked, nulling the areas we excluded.

OCTOVOX is whole-file/offline (Flask ``/api/clean``). This module sends one clip at
a time. The live "cleaned monitor" (rolling chunks) is built on top in
:mod:`octovox_monitor`.

Pure helpers (azimuth maths, zone→azimuth mapping) need nothing; the HTTP client
needs the ``[octovox]`` extra (``requests`` + ``scipy``). Imports are lazy so this
module loads without them.

**Azimuth conventions.** This app: compass bearing, 0° = +Y, clockwise. OCTOVOX:
math convention, 0° = +X, counter-clockwise. Conversion: ``oct = (90 − bearing) %
360``. ``azimuth_offset_deg`` calibrates the array's physical mounting rotation
(its MIC1 vs the room's +Y); leave at 0 and adjust if the cleaned beam points the
wrong way.
"""
from __future__ import annotations

import io
import time
import wave
from dataclasses import dataclass, field
from typing import Optional

from .steering import exclusion_directions, pickup_directions

OCTOVOX_DEFAULT_URL = "http://127.0.0.1:5050"
OCTOVOX_REQUIRED_SR = 48000
OCTOVOX_CHANNELS = 8


# --------------------------------------------------------------------------- #
# Azimuth mapping (pure)
# --------------------------------------------------------------------------- #
def to_octovox_azimuth(compass_bearing_deg: float, offset_deg: float = 0.0) -> float:
    """Convert this app's compass bearing (0°=+Y, CW) to OCTOVOX's azimuth
    (0°=+X, CCW). ``offset_deg`` accounts for the array's mounting rotation."""
    return (90.0 - (compass_bearing_deg + offset_deg)) % 360.0


@dataclass
class ZoneAzimuths:
    """Zone-derived steering for OCTOVOX's direction-masked extraction."""

    target_az: Optional[float]            # primary pickup direction (OCTOVOX deg), or None
    interferer_az: list[float] = field(default_factory=list)  # excluded directions (OCTOVOX deg)
    note: str = ""


def zone_azimuths(config, array_id: str, *, azimuth_offset_deg: float = 0.0) -> ZoneAzimuths:
    """Map an array's pickup/exclusion zones to OCTOVOX ``target_az`` /
    ``interferer_az``. The primary (first) pickup zone becomes the target; the
    exclusion zones become the competing/interferer directions to suppress."""
    pickups = pickup_directions(config, array_id)
    exclusions = exclusion_directions(config, array_id)

    target = None
    note = ""
    if pickups:
        target = to_octovox_azimuth(pickups[0][1].azimuth_deg, azimuth_offset_deg)
        if len(pickups) > 1:
            label = pickups[0][0].label or pickups[0][0].id
            note = f"{len(pickups)} pickup zones — OCTOVOX extracts one direction; targeting '{label}'"
    else:
        note = "no pickup zone — OCTOVOX will beam automatically (no target direction)"

    interferers = [to_octovox_azimuth(d.azimuth_deg, azimuth_offset_deg) for _z, d in exclusions]
    return ZoneAzimuths(target_az=target, interferer_az=interferers, note=note)


# --------------------------------------------------------------------------- #
# WAV (de)serialization (stdlib)
# --------------------------------------------------------------------------- #
def _encode_wav(y_cn, sr: int) -> bytes:
    """Encode a ``(channels, samples)`` float array in [-1,1] as 16-bit PCM WAV."""
    import numpy as np

    y = np.ascontiguousarray(np.asarray(y_cn, dtype=np.float32))
    if y.ndim == 1:
        y = y[None, :]
    ch, n = y.shape
    inter = np.clip(y.T, -1.0, 1.0)                    # (samples, channels)
    pcm = (inter * 32767.0).astype("<i2")
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(ch)
        w.setsampwidth(2)
        w.setframerate(int(sr))
        w.writeframes(pcm.tobytes())
    return buf.getvalue()


def _decode_wav(data: bytes):
    """Decode WAV bytes → ``(mono float32 [-1,1], sr)`` (downmix if multichannel)."""
    import numpy as np

    with wave.open(io.BytesIO(data), "rb") as w:
        ch, sw, sr, n = w.getnchannels(), w.getsampwidth(), w.getframerate(), w.getnframes()
        raw = w.readframes(n)
    if sw != 2:
        raise ValueError(f"expected 16-bit WAV, got sampwidth {sw}")
    a = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32767.0
    if ch > 1:
        a = a.reshape(-1, ch).mean(axis=1)
    return a, sr


# --------------------------------------------------------------------------- #
# Availability + HTTP client
# --------------------------------------------------------------------------- #
def octovox_deps_available() -> bool:
    """True if the ``[octovox]`` extra (requests + scipy + numpy) is importable."""
    try:
        import numpy  # noqa: F401
        import requests  # noqa: F401
        import scipy.signal  # noqa: F401
    except Exception:
        return False
    return True


def repair_dead_channels(y8, active):
    """Fill dead/inactive capsules with the mean of their nearest active ring-
    neighbours, so OCTOVOX's beamformer isn't fed silent channels.

    OCTOVOX requires exactly 8 channels and has no active-capsule mask, so a dead
    capsule (e.g. the array's silent channel 5) would otherwise corrupt its
    covariance/beam. For a circular array the channel index order is the spatial
    ring order, so the two index-adjacent active capsules are the spatial
    neighbours; their average is a physically-plausible fill. ``active`` is a list
    of bools (one per channel); returns a repaired copy.
    """
    import numpy as np

    y = np.array(y8, dtype=np.float32, copy=True)
    n = y.shape[0]
    act = [i for i, a in enumerate(active) if a]
    if not act or len(act) == n:
        return y
    for i in range(n):
        if i < len(active) and active[i]:
            continue
        nearest = sorted(act, key=lambda j: min(abs(i - j), n - abs(i - j)))[:2]
        y[i] = y[nearest].mean(axis=0)
    return y


def resample_to_48k(y_cn, sr: int):
    """Polyphase-resample ``(channels, samples)`` to 48 kHz (no-op if already 48k)."""
    import numpy as np
    from math import gcd

    y = np.asarray(y_cn, dtype=np.float32)
    if int(sr) == OCTOVOX_REQUIRED_SR:
        return y
    import scipy.signal as ss

    up, down = OCTOVOX_REQUIRED_SR, int(sr)
    g = gcd(up, down)
    return ss.resample_poly(y, up // g, down // g, axis=1).astype(np.float32)


@dataclass
class CleanResult:
    mono: object          # numpy float32 mono @ 48 kHz
    sr: int
    stages: dict
    elapsed_s: float
    clean_url: str


class OctovoxClient:
    """Talks to a running OCTOVOX Flask server over HTTP."""

    def __init__(self, base_url: str = OCTOVOX_DEFAULT_URL, *, timeout: float = 180.0):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def is_up(self) -> bool:
        try:
            import requests
        except Exception:
            return False
        try:
            r = requests.get(self.base_url + "/", timeout=4)
            return r.status_code < 500
        except Exception:
            return False

    def clean_8ch(
        self,
        y8,
        sr: int,
        *,
        target_az: Optional[float] = None,
        interferer_az: Optional[list] = None,
        nr: str = "dfn",
        dereverb: str = "none",
        report: bool = False,
        filename: Optional[str] = None,
        active: Optional[list] = None,
    ) -> CleanResult:
        """Clean one 8-channel clip: (repair dead capsules →) resample → upload →
        /api/clean → fetch mono.

        ``y8`` is ``(8, samples)`` float32 in [-1,1] at ``sr``. ``target_az`` /
        ``interferer_az`` are OCTOVOX-convention degrees (see :func:`zone_azimuths`).
        ``active`` (optional per-channel bool mask) repairs dead capsules first so
        OCTOVOX isn't fed silent channels. Returns the cleaned mono at 48 kHz.
        """
        import numpy as np
        import requests

        y = np.asarray(y8, dtype=np.float32)
        if y.ndim != 2 or y.shape[0] != OCTOVOX_CHANNELS:
            raise ValueError(f"expected ({OCTOVOX_CHANNELS}, samples) array, got {y.shape}")
        if active is not None and not all(active):
            y = repair_dead_channels(y, active)
        y48 = resample_to_48k(y, sr)
        wav = _encode_wav(y48, OCTOVOX_REQUIRED_SR)
        fname = filename or f"conf_{int(time.time() * 1000)}.wav"

        up = requests.post(
            self.base_url + "/api/upload",
            files={"file": (fname, wav, "audio/wav")},
            data={"overwrite": "1"},
            timeout=self.timeout,
        )
        up.raise_for_status()
        uj = up.json()
        if not uj.get("ok"):
            raise RuntimeError(f"OCTOVOX upload rejected: {uj}")

        body: dict = {"filename": fname, "nr": nr, "dereverb": dereverb, "report": bool(report)}
        if target_az is not None:
            body["target_az"] = float(target_az)
        if interferer_az:
            body["interferer_az"] = [float(a) for a in interferer_az]
        cr = requests.post(self.base_url + "/api/clean", json=body, timeout=self.timeout)
        cr.raise_for_status()
        cj = cr.json()
        if not cj.get("ok"):
            raise RuntimeError(f"OCTOVOX clean failed: {cj}")

        wr = requests.get(self.base_url + cj["clean"], timeout=self.timeout)
        wr.raise_for_status()
        mono, msr = _decode_wav(wr.content)
        return CleanResult(
            mono=mono,
            sr=msr,
            stages=cj.get("stages", {}),
            elapsed_s=float(cj.get("elapsed_s", 0.0)),
            clean_url=cj["clean"],
        )
