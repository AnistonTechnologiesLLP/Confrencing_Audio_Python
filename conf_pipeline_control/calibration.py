"""Per-capsule calibration — gain / polarity / integer-sample-delay alignment of the raw 8-MEMS
block, applied at the FRONT of the live path (right after the uniform preamp, BEFORE the beamformer,
DOA and null steering).

Why this exists (Phase 1 of the audio front-end hardening). DOA, beamforming and null steering all
assume the capsules are gain-aligned, phase-consistent, delay-aligned and not polarity-inverted. The
array is otherwise fed raw (only a *uniform* :class:`~conf_pipeline_control.preamp.InputPreamp` gain
and a dead-capsule on/off mask sit in front). Real MEMS parts mismatch; this module corrects that.

Three layers, deliberately separated:
  * :class:`CalibrationProfile` — a small **stdlib-only** (numpy-free) record + camelCase JSON
    save/load + controlled validation. Importing it pulls no heavy deps, so it is usable from the
    numpy-free engine core and the GUI.
  * :class:`CapsuleCalibrator` — the runtime per-block ``(N, M)`` corrector. Mirrors the
    :class:`~conf_pipeline_control.preamp.InputPreamp` contract: a **bit-exact no-op when the profile
    is neutral** (returns the input array unchanged — keeps the existing pipeline byte-identical),
    float32-preserving (plain-float scalars / a float32 gain vector dodge the NEP-50 upcast), and it
    honors the dead-capsule ``active_mask`` so a masked capsule is never gained up / revived.
  * :func:`estimate_calibration` — a synthetic-signal-testable estimator (gain from per-channel RMS,
    polarity from correlation sign, integer delay from cross-correlation) with **honest per-channel
    confidence** — it never fakes certainty on a silent / decorrelated capsule.

:class:`CalibrationHost` is the mixin that wires the corrector into a live backend, exactly mirroring
:class:`~conf_pipeline_control.preamp.PreampHost`. numpy is imported lazily inside the methods that
need it, so the module top stays heavy-dep-free.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, replace
from typing import Any, List, Mapping, Optional, Sequence, Tuple

from .preamp import _db_to_lin

PROFILE_VERSION = 1
DEFAULT_DEVICE = "POLARIS_8MEMS"
DEFAULT_CHANNELS = 8
DEFAULT_SAMPLE_RATE = 48000.0
DEFAULT_GAIN_CLAMP_DB = 12.0     # estimator: cap a single channel's correction (a near-dead capsule)
DEFAULT_MAX_DELAY_SAMPLES = 32   # estimator: search window for the per-capsule delay
DEFAULT_MIN_CORR = 0.2           # estimator: |normalised xcorr| below this ⇒ low-confidence (no fake)


class CalibrationError(Exception):
    """A calibration profile is missing, malformed, or incompatible with the engine.

    Raised by the profile parse/validate/load surface so callers get ONE controlled exception to
    catch; the live hosts catch it and degrade to *calibration off* rather than crashing the runtime.
    """


# --------------------------------------------------------------------------- #
# CalibrationProfile — stdlib only (no numpy); the on-disk / on-the-wire record
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class CalibrationProfile:
    """Per-capsule correction record. Wire/JSON keys are camelCase (project convention); the dataclass
    fields are snake_case. ``gainDb`` / ``delaySamples`` / ``polarity`` are per-channel, length == ``channels``.

    Construction is permissive (so an estimator can build freely); call :meth:`validate` (or use
    :meth:`from_dict` / :meth:`from_json` / :meth:`load`, which validate for you) to enforce shape."""

    version: int = PROFILE_VERSION
    device: str = DEFAULT_DEVICE
    sample_rate: float = DEFAULT_SAMPLE_RATE
    channels: int = DEFAULT_CHANNELS
    created_at: str = ""
    gain_db: Tuple[float, ...] = (0.0,) * DEFAULT_CHANNELS
    delay_samples: Tuple[int, ...] = (0,) * DEFAULT_CHANNELS
    polarity: Tuple[int, ...] = (1,) * DEFAULT_CHANNELS
    reference_channel: int = 0
    notes: str = ""

    @property
    def is_neutral(self) -> bool:
        """True when every correction is the identity (gain 0 dB, polarity +1, delay 0) — the
        corrector is then a bit-exact no-op and the host keeps it off."""
        return (all(float(g) == 0.0 for g in self.gain_db)
                and all(int(d) == 0 for d in self.delay_samples)
                and all(int(p) == 1 for p in self.polarity))

    def validate(self) -> "CalibrationProfile":
        """Raise :class:`CalibrationError` on a malformed profile; return ``self`` when valid."""
        m = self.channels
        if not isinstance(m, int) or m <= 0:
            raise CalibrationError(f"channels must be a positive int, got {self.channels!r}")
        for name, seq in (("gainDb", self.gain_db), ("delaySamples", self.delay_samples),
                          ("polarity", self.polarity)):
            if len(seq) != m:
                raise CalibrationError(f"{name} has {len(seq)} entries, expected channels={m}")
        if any(int(p) not in (-1, 1) for p in self.polarity):
            raise CalibrationError(f"polarity entries must be +1 or -1, got {self.polarity!r}")
        if any(int(d) < 0 for d in self.delay_samples):
            raise CalibrationError(f"delaySamples must be >= 0 (causal), got {self.delay_samples!r}")
        if not (0 <= int(self.reference_channel) < m):
            raise CalibrationError(
                f"referenceChannel {self.reference_channel} out of range [0,{m})")
        if float(self.sample_rate) <= 0.0:
            raise CalibrationError(f"sampleRate must be > 0, got {self.sample_rate!r}")
        return self

    def to_dict(self) -> dict:
        """camelCase JSON-ready dict (lists, not tuples)."""
        return {
            "version": int(self.version),
            "device": str(self.device),
            "sampleRate": float(self.sample_rate),
            "channels": int(self.channels),
            "createdAt": str(self.created_at),
            "gainDb": [float(g) for g in self.gain_db],
            "delaySamples": [int(d) for d in self.delay_samples],
            "polarity": [int(p) for p in self.polarity],
            "referenceChannel": int(self.reference_channel),
            "notes": str(self.notes),
        }

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> "CalibrationProfile":
        if not isinstance(d, Mapping):
            raise CalibrationError(f"profile must be a JSON object, got {type(d).__name__}")
        try:
            m = int(d.get("channels", DEFAULT_CHANNELS))
            prof = cls(
                version=int(d.get("version", PROFILE_VERSION)),
                device=str(d.get("device", DEFAULT_DEVICE)),
                sample_rate=float(d.get("sampleRate", DEFAULT_SAMPLE_RATE)),
                channels=m,
                created_at=str(d.get("createdAt", "")),
                gain_db=tuple(float(x) for x in d.get("gainDb", (0.0,) * m)),
                delay_samples=tuple(int(x) for x in d.get("delaySamples", (0,) * m)),
                polarity=tuple(int(x) for x in d.get("polarity", (1,) * m)),
                reference_channel=int(d.get("referenceChannel", 0)),
                notes=str(d.get("notes", "")),
            )
        except (TypeError, ValueError) as exc:
            raise CalibrationError(f"malformed calibration profile: {exc}") from exc
        return prof.validate()

    @classmethod
    def from_json(cls, text: str) -> "CalibrationProfile":
        try:
            d = json.loads(text)
        except (json.JSONDecodeError, TypeError) as exc:
            raise CalibrationError(f"invalid calibration JSON: {exc}") from exc
        return cls.from_dict(d)

    def to_json(self, *, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent)

    @classmethod
    def load(cls, path: Any) -> "CalibrationProfile":
        """Read + validate a profile from disk. Missing/unreadable file ⇒ :class:`CalibrationError`."""
        try:
            with open(path, "r", encoding="utf-8") as fh:
                text = fh.read()
        except OSError as exc:
            raise CalibrationError(f"cannot read calibration profile {path!r}: {exc}") from exc
        return cls.from_json(text)

    def save(self, path: Any) -> None:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(self.to_json())


# --------------------------------------------------------------------------- #
# CapsuleCalibrator — the runtime per-block (N, M) corrector (numpy, lazy)
# --------------------------------------------------------------------------- #
def _resolve_active(active_mask: Optional[Sequence[bool]], channels: int) -> List[bool]:
    """A length-``channels`` boolean active mask. ``None`` ⇒ all active. A length mismatch is
    tolerated (pad True / truncate) so a stale mask never crashes the audio thread."""
    if active_mask is None:
        return [True] * channels
    active = [bool(a) for a in active_mask]
    if len(active) < channels:
        active = active + [True] * (channels - len(active))
    elif len(active) > channels:
        active = active[:channels]
    return active


class CapsuleCalibrator:
    """Apply a :class:`CalibrationProfile` to a raw ``(N, M)`` float32 block, BEFORE the beamformer.

    Per channel: multiply by ``polarity · 10**(gainDb/20)``, then delay by an integer number of
    samples (a per-channel history ring keeps the delay continuous across blocks). A **neutral**
    profile is a bit-exact no-op (returns the input array unchanged). A masked (dead) capsule is
    forced to identity — calibration never revives or amplifies a disabled channel."""

    def __init__(self, profile: CalibrationProfile, *,
                 active_mask: Optional[Sequence[bool]] = None) -> None:
        self.profile = profile
        m = int(profile.channels)
        self._channels = m
        active = _resolve_active(active_mask, m)
        self._active = active
        gain_lin: List[float] = []
        delays: List[int] = []
        for c in range(m):
            if active[c]:
                sign = 1.0 if int(profile.polarity[c]) >= 0 else -1.0
                gain_lin.append(float(_db_to_lin(float(profile.gain_db[c])) * sign))
                delays.append(int(profile.delay_samples[c]))
            else:                                  # dead capsule: identity, never gained / delayed
                gain_lin.append(1.0)
                delays.append(0)
        self._gain_lin = gain_lin
        self._delays = delays
        self._max_delay = max(delays) if delays else 0
        self._has_gain = any(g != 1.0 for g in gain_lin)
        self._neutral = (not self._has_gain) and (self._max_delay == 0)
        self._gain_vec: Any = None      # float32 (M,) numpy row, built lazily (NEP-50 safe)
        self._hist: Any = None          # (max_delay, M) float32 history ring, built lazily

    @property
    def is_neutral(self) -> bool:
        return self._neutral

    @property
    def latency_samples(self) -> int:
        """Added latency = the largest per-channel delay correction."""
        return int(self._max_delay)

    def process_block(self, block: Any) -> Any:
        """Correct one ``(N, M)`` block. Returns the input unchanged when neutral (no copy)."""
        if self._neutral:
            return block
        import numpy as np

        x = np.asarray(block, dtype=np.float32)
        if self._has_gain:
            if self._gain_vec is None:
                self._gain_vec = np.asarray(self._gain_lin, dtype=np.float32)
            x = x * self._gain_vec[None, :]        # float32 * float32 ⇒ stays float32
        if self._max_delay > 0:
            x = self._apply_delay(np, x)
        return x

    def _apply_delay(self, np: Any, x: Any) -> Any:
        n, m = x.shape
        md = self._max_delay
        if self._hist is None or self._hist.shape != (md, m):
            self._hist = np.zeros((md, m), dtype=np.float32)
        ext = np.concatenate([self._hist, x], axis=0)      # (md + n, m)
        out = np.empty((n, m), dtype=np.float32)
        delays = self._delays
        for c in range(m):
            d = delays[c]
            out[:, c] = ext[md - d:md - d + n, c]          # d == 0 ⇒ exactly x[:, c]
        self._hist = ext[-md:, :].copy()
        return out

    def reset(self) -> None:
        """Drop the delay-line history (atomic rebind) so a re-activated beam doesn't replay a
        stale tail. Mirrors ``InputPreamp.reset`` / the streaming-stage ``reset()`` contract."""
        self._hist = None

    @classmethod
    def for_engine(cls, profile: CalibrationProfile, *, channels: int,
                   sample_rate: Optional[float] = None,
                   active_mask: Optional[Sequence[bool]] = None) -> "CapsuleCalibrator":
        """Build a corrector reconciled to a live engine.

        Channel-count mismatch ⇒ :class:`CalibrationError` (unsafe to apply). Sample-rate mismatch
        with non-zero delays ⇒ drop the delay corrections (sample counts don't transfer across rates)
        and keep gain/polarity. The returned corrector may be neutral (the host then keeps it off)."""
        profile.validate()
        if int(profile.channels) != int(channels):
            raise CalibrationError(
                f"profile has {profile.channels} channels, engine has {channels}")
        prof = profile
        if (sample_rate is not None and float(profile.sample_rate) > 0.0
                and abs(float(profile.sample_rate) - float(sample_rate)) > 1.0
                and any(int(d) != 0 for d in profile.delay_samples)):
            import sys
            print(f"[calibration] profile sampleRate {profile.sample_rate} != engine {sample_rate}; "
                  f"dropping sample-delay corrections (gain/polarity kept)", file=sys.stderr)
            prof = replace(profile, delay_samples=(0,) * int(channels))
        return cls(prof, active_mask=active_mask)


# --------------------------------------------------------------------------- #
# estimate_calibration — synthetic-testable estimator with honest confidence
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class CalibrationEstimate:
    """The estimator's output: a :class:`CalibrationProfile` plus per-channel confidence so a caller
    (or the GUI) can show / withhold low-confidence corrections instead of trusting them blindly."""

    profile: CalibrationProfile
    reference_channel: int
    low_confidence_channels: Tuple[int, ...]
    gain_confidence: Tuple[float, ...]
    polarity_confidence: Tuple[float, ...]
    delay_confidence: Tuple[float, ...]


def _pick_reference(rms: Any, active: Sequence[bool]) -> int:
    """Pick the active, non-silent channel whose RMS is closest to the median (robust to one
    loud/dead capsule). All silent ⇒ channel 0."""
    import numpy as np

    idxs = [c for c in range(len(rms)) if active[c] and float(rms[c]) > 0.0]
    if not idxs:
        return 0
    med = float(np.median(np.array([float(rms[c]) for c in idxs])))
    return int(min(idxs, key=lambda c: abs(float(rms[c]) - med)))


def estimate_calibration(capture: Any, *, sample_rate: float,
                         reference_channel: Optional[int] = None,
                         active_mask: Optional[Sequence[bool]] = None,
                         estimate_polarity: bool = True, estimate_delay: bool = True,
                         max_delay_samples: int = DEFAULT_MAX_DELAY_SAMPLES,
                         gain_clamp_db: float = DEFAULT_GAIN_CLAMP_DB,
                         min_corr: float = DEFAULT_MIN_CORR,
                         device: str = DEFAULT_DEVICE) -> CalibrationEstimate:
    """Estimate a per-capsule :class:`CalibrationProfile` from a multichannel ``(N, M)`` capture.

    * **gain** — per-channel RMS vs the reference channel ⇒ a dB correction (clamped to
      ±``gain_clamp_db`` so a near-dead capsule can't demand a huge boost).
    * **polarity** — sign of the (normalised) cross-correlation peak vs the reference.
    * **delay** — integer lag of the cross-correlation peak; corrections delay every channel to align
      with the **latest-arriving** capsule (causal, non-negative).

    A near-silent or decorrelated (``|xcorr| < min_corr``) channel is left at identity and flagged in
    ``low_confidence_channels`` — the estimator never fakes a correction it cannot justify.
    """
    import numpy as np

    x = np.asarray(capture, dtype=np.float64)
    if x.ndim != 2:
        raise CalibrationError(f"capture must be (N, channels), got shape {x.shape}")
    n, m = x.shape
    active = _resolve_active(active_mask, m)
    rms = np.sqrt(np.mean(x * x, axis=0)) if n else np.zeros(m)
    ref = int(reference_channel) if reference_channel is not None else _pick_reference(rms, active)
    if not (0 <= ref < m):
        raise CalibrationError(f"reference_channel {ref} out of range [0,{m})")
    ref_sig = x[:, ref]
    ref_rms = float(rms[ref])
    ref_norm = float(np.linalg.norm(ref_sig)) or 1.0
    silent_thresh = ref_rms * 1e-3

    gain_db: List[float] = [0.0] * m
    polarity: List[int] = [1] * m
    arrival: List[int] = [0] * m                  # lag vs ref; +ve ⇒ this capsule arrives later
    gconf: List[float] = [0.0] * m
    pconf: List[float] = [0.0] * m
    dconf: List[float] = [0.0] * m
    low: set = set()

    for c in range(m):
        if not active[c]:
            continue
        rc = float(rms[c])
        if rc <= silent_thresh or ref_rms <= 0.0:
            low.add(c)                            # can't calibrate a silent capsule — leave identity
            continue
        gain_db[c] = float(np.clip(20.0 * np.log10(ref_rms / rc), -gain_clamp_db, gain_clamp_db))
        gconf[c] = 1.0
        if c == ref:
            pconf[c] = dconf[c] = 1.0
            continue
        if estimate_polarity or estimate_delay:
            full = np.correlate(x[:, c], ref_sig, mode="full")
            denom = (float(np.linalg.norm(x[:, c])) * ref_norm) or 1.0
            cc = full / denom
            lags = np.arange(-(n - 1), n)
            sel = np.abs(lags) <= int(max_delay_samples)
            cc_sel = cc[sel]
            lag_sel = lags[sel]
            k = int(np.argmax(np.abs(cc_sel)))
            peak = float(cc_sel[k])
            lag = int(lag_sel[k])
            if estimate_polarity:
                polarity[c] = -1 if peak < 0.0 else 1
                pconf[c] = abs(peak)
                if abs(peak) < min_corr:
                    low.add(c)
            if estimate_delay:
                arrival[c] = lag
                dconf[c] = abs(peak)
                if abs(peak) < min_corr:
                    low.add(c)

    if estimate_delay:
        considered = [arrival[c] for c in range(m) if active[c] and c not in low]
        latest = max(considered) if considered else 0
        delays = [max(0, latest - arrival[c]) if (active[c] and c not in low) else 0
                  for c in range(m)]
    else:
        delays = [0] * m

    profile = CalibrationProfile(
        version=PROFILE_VERSION, device=device, sample_rate=float(sample_rate), channels=m,
        created_at="", gain_db=tuple(gain_db), delay_samples=tuple(int(d) for d in delays),
        polarity=tuple(polarity), reference_channel=ref,
        notes=("low-confidence channels: " + ",".join(str(c) for c in sorted(low))) if low else "",
    )
    return CalibrationEstimate(
        profile=profile, reference_channel=ref,
        low_confidence_channels=tuple(sorted(low)),
        gain_confidence=tuple(gconf), polarity_confidence=tuple(pconf),
        delay_confidence=tuple(dconf),
    )


# --------------------------------------------------------------------------- #
# CalibrationHost — the live-backend mixin (mirrors PreampHost)
# --------------------------------------------------------------------------- #
class CalibrationHost:
    """Give a live backend a per-capsule calibration insert, exactly mirroring
    :class:`~conf_pipeline_control.preamp.PreampHost`.

    The host calls :meth:`_init_calibration` in ``__init__`` (right after ``_init_preamp``) and
    :meth:`_apply_calibration` at the FRONT of its per-block process method — immediately after
    ``_apply_preamp`` and BEFORE the beam + covariance read — so DOA, beamforming and null steering
    all see corrected capsules. No ``__init__`` and no state beyond ``_calib`` (a ``None`` class
    default), so it composes with any backend with no MRO surprises."""

    _calib: Optional[CapsuleCalibrator] = None      # class default ⇒ safe before _init / when off

    def _init_calibration(self, *, calibration: Optional[CalibrationProfile] = None,
                          calibration_path: Optional[str] = None,
                          channels: int = DEFAULT_CHANNELS,
                          sample_rate: Optional[float] = None,
                          active_mask: Optional[Sequence[bool]] = None) -> None:
        """Build the corrector only when a non-neutral, compatible profile is supplied. A missing /
        malformed profile or a channel mismatch degrades to *calibration off* (never raises) so the
        default path stays a zero-overhead, byte-identical no-op."""
        prof = calibration
        if prof is None and calibration_path:
            try:
                prof = CalibrationProfile.load(calibration_path)
            except CalibrationError as exc:
                import sys
                print(f"[calibration] ignoring profile {calibration_path!r}: {exc}", file=sys.stderr)
                prof = None
        if prof is None:
            self._calib = None
            return
        try:
            cal = CapsuleCalibrator.for_engine(
                prof, channels=channels, sample_rate=sample_rate, active_mask=active_mask)
        except CalibrationError as exc:
            import sys
            print(f"[calibration] disabled ({exc})", file=sys.stderr)
            self._calib = None
            return
        self._calib = None if cal.is_neutral else cal

    def _apply_calibration(self, block: Any) -> Any:
        """Per-capsule correction on the raw block; a no-op (same object) when calibration is off."""
        c = self._calib
        return c.process_block(block) if c is not None else block
