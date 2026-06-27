"""Auto live placement check — is the POLARIS array in a bad acoustic/noise position?

Phase 3 of the audio front-end hardening. Before a meeting an operator records a few seconds of
*room noise* (no speech) and runs this analyzer, which scores the position **GOOD / ACCEPTABLE / BAD**
with reasons + recommendations, and detects:
  * total + speech-band noise level, low-frequency rumble, broadband hiss,
  * narrowband tonal peaks (fan/HVAC) — which become **notch suggestions** for Phase 2's pre-NR stage,
  * clipping risk, per-capsule level imbalance, and a local-hotspot heuristic.

**Diagnostics only.** This module is a pure analyzer + a small result record; it NEVER touches or
changes the live DSP pipeline. Suggestions are exportable, but applied only if the operator chooses.

**Measurement-first + honest.** Scoring keys off *bandwidth-normalised spectral-density ratios* and
*peak prominences* (gain- and bandwidth-independent), so a flat, quiet room scores GOOD regardless of
record gain. Tones are only suggested as notches when they are confidently prominent — uncertain
findings are not dressed up as facts.

numpy is imported lazily inside the analyzer, so the result record + JSON + survey comparison are
pure-stdlib (usable from the GUI / no-numpy suite). scipy is NOT required.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, List, Mapping, Optional, Sequence, Tuple

PLACEMENT_VERSION = 1
DEFAULT_DEVICE = "POLARIS_8MEMS"

STATUS_GOOD = "GOOD"
STATUS_ACCEPTABLE = "ACCEPTABLE"
STATUS_BAD = "BAD"

# Score → status thresholds (documented, deterministic).
GOOD_MIN_SCORE = 85
ACCEPTABLE_MIN_SCORE = 60

# Analysis bands (Hz). Speech band follows the room-noise convention in the brief (300–3400); the
# DOA scan band is a separate 300–3800 (see conf_pipeline_control.doa).
RUMBLE_BAND_HZ = (20.0, 200.0)
SPEECH_BAND_HZ = (300.0, 3400.0)
TONE_SEARCH_BAND_HZ = (50.0, 1000.0)
HISS_BAND_HZ = (1000.0, 8000.0)

# Detection / scoring thresholds (all RELATIVE / gain-independent except the absolute guards).
TONE_PROMINENCE_DB = 9.0          # a spectral peak must stand this far above the band median to count
MAX_TONES = 6
TONE_MIN_SEPARATION_HZ = 12.0
RUMBLE_WARN_DB = 6.0              # rumble-band density this far over speech-band density ⇒ warn
RUMBLE_BAD_DB = 15.0
HISS_WARN_DB = 3.0
HISS_BAD_DB = 10.0
IMBALANCE_WARN_DB = 4.0          # a capsule this far over the median capsule RMS ⇒ imbalance
IMBALANCE_BAD_DB = 8.0
CLIP_LEVEL = 0.99               # |sample| ≥ this counts as near-clipping
CLIP_FRACTION = 5e-5            # this fraction of samples near-clipping ⇒ clipping risk
NOISE_FLOOR_HIGH_DBFS = -35.0   # absolute: a very loud room regardless of spectral content
HPF_SUGGESTION_HZ = 120.0       # the speech-HPF recommended when rumble is flagged

_DEFAULT_NPERSEG = 8192


class PlacementError(Exception):
    """The placement capture is invalid (empty / mono / wrong shape) or a result file is unreadable."""


# --------------------------------------------------------------------------- #
# PlacementResult — stdlib-only result record (camelCase JSON)
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class PlacementResult:
    """The scored outcome of one placement capture. Wire/JSON keys are camelCase; fields snake_case."""

    version: int = PLACEMENT_VERSION
    device: str = DEFAULT_DEVICE
    sample_rate: float = 48000.0
    channels: int = 8
    duration_seconds: float = 0.0
    label: str = ""
    status: str = STATUS_GOOD
    score: int = 100
    noise_rms_dbfs: float = -120.0
    speech_band_noise_dbfs: float = -120.0
    low_frequency_rumble_dbfs: float = -120.0
    broadband_hiss_dbfs: float = -120.0
    detected_tones_hz: Tuple[float, ...] = ()
    notch_suggestions_hz: Tuple[float, ...] = ()
    hpf_suggestion_hz: Optional[float] = None
    clipping_risk: bool = False
    channel_imbalance_db: Tuple[float, ...] = ()
    local_hotspot_suspected: bool = False
    reasons: Tuple[str, ...] = ()
    recommendations: Tuple[str, ...] = ()

    def to_dict(self) -> dict:
        return {
            "version": int(self.version),
            "device": str(self.device),
            "sampleRate": float(self.sample_rate),
            "channels": int(self.channels),
            "durationSeconds": float(self.duration_seconds),
            "label": str(self.label),
            "status": str(self.status),
            "score": int(self.score),
            "noiseRmsDbfs": float(self.noise_rms_dbfs),
            "speechBandNoiseDbfs": float(self.speech_band_noise_dbfs),
            "lowFrequencyRumbleDbfs": float(self.low_frequency_rumble_dbfs),
            "broadbandHissDbfs": float(self.broadband_hiss_dbfs),
            "detectedTonesHz": [float(f) for f in self.detected_tones_hz],
            "notchSuggestionsHz": [float(f) for f in self.notch_suggestions_hz],
            "hpfSuggestionHz": (None if self.hpf_suggestion_hz is None else float(self.hpf_suggestion_hz)),
            "clippingRisk": bool(self.clipping_risk),
            "channelImbalanceDb": [float(d) for d in self.channel_imbalance_db],
            "localHotspotSuspected": bool(self.local_hotspot_suspected),
            "reasons": list(self.reasons),
            "recommendations": list(self.recommendations),
        }

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> "PlacementResult":
        if not isinstance(d, Mapping):
            raise PlacementError(f"placement result must be a JSON object, got {type(d).__name__}")
        try:
            hpf = d.get("hpfSuggestionHz", None)
            return cls(
                version=int(d.get("version", PLACEMENT_VERSION)),
                device=str(d.get("device", DEFAULT_DEVICE)),
                sample_rate=float(d.get("sampleRate", 48000.0)),
                channels=int(d.get("channels", 8)),
                duration_seconds=float(d.get("durationSeconds", 0.0)),
                label=str(d.get("label", "")),
                status=str(d.get("status", STATUS_GOOD)),
                score=int(d.get("score", 100)),
                noise_rms_dbfs=float(d.get("noiseRmsDbfs", -120.0)),
                speech_band_noise_dbfs=float(d.get("speechBandNoiseDbfs", -120.0)),
                low_frequency_rumble_dbfs=float(d.get("lowFrequencyRumbleDbfs", -120.0)),
                broadband_hiss_dbfs=float(d.get("broadbandHissDbfs", -120.0)),
                detected_tones_hz=tuple(float(f) for f in d.get("detectedTonesHz", ())),
                notch_suggestions_hz=tuple(float(f) for f in d.get("notchSuggestionsHz", ())),
                hpf_suggestion_hz=(None if hpf is None else float(hpf)),
                clipping_risk=bool(d.get("clippingRisk", False)),
                channel_imbalance_db=tuple(float(x) for x in d.get("channelImbalanceDb", ())),
                local_hotspot_suspected=bool(d.get("localHotspotSuspected", False)),
                reasons=tuple(str(s) for s in d.get("reasons", ())),
                recommendations=tuple(str(s) for s in d.get("recommendations", ())),
            )
        except (TypeError, ValueError) as exc:
            raise PlacementError(f"malformed placement result: {exc}") from exc

    @classmethod
    def from_json(cls, text: str) -> "PlacementResult":
        try:
            d = json.loads(text)
        except (json.JSONDecodeError, TypeError) as exc:
            raise PlacementError(f"invalid placement JSON: {exc}") from exc
        return cls.from_dict(d)

    def to_json(self, *, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent)

    @classmethod
    def load(cls, path: Any) -> "PlacementResult":
        try:
            with open(path, "r", encoding="utf-8") as fh:
                text = fh.read()
        except OSError as exc:
            raise PlacementError(f"cannot read placement result {path!r}: {exc}") from exc
        return cls.from_json(text)

    def save(self, path: Any) -> None:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(self.to_json())

    def to_pre_nr_bands(self, hpf_hz: Optional[float] = None) -> List[dict]:
        """Convert this result's HPF + notch suggestions into Phase 2 pre-NR PEQ bands (reuses
        :func:`conf_pipeline_control.pre_nr.build_pre_nr_bands`). ``hpf_hz`` overrides the suggested HPF.

        The operator opts in by passing the result to ``pre_nr_bands=…``; nothing is auto-applied, and
        these tones are this room's — re-measure elsewhere."""
        from .pre_nr import build_pre_nr_bands

        hpf = hpf_hz if hpf_hz is not None else self.hpf_suggestion_hz
        return build_pre_nr_bands(hpf_hz=hpf, notches=list(self.notch_suggestions_hz))


def compare_placements(results: Sequence[PlacementResult]) -> PlacementResult:
    """Survey mode: return the result with the highest score (ties → the first). Empty ⇒ error."""
    if not results:
        raise PlacementError("no placement results to compare")
    return max(results, key=lambda r: r.score)


# --------------------------------------------------------------------------- #
# Spectral helpers (numpy, lazy)
# --------------------------------------------------------------------------- #
def _power_spectrum(x: Any, fs: float, nperseg: int = _DEFAULT_NPERSEG) -> Tuple[Any, Any]:
    """Averaged one-sided power spectrum (Hann, 50% overlap), 'spectrum' scaled so a tone of amplitude
    A reads ~A²/2 at its bin and the band sums approximate that band's mean-square. Returns (freqs, Pxx)."""
    import numpy as np

    x = np.asarray(x, dtype=np.float64)
    n = x.size
    nperseg = int(min(nperseg, n))
    if nperseg < 8:
        nperseg = n
    win = np.hanning(nperseg)
    # Parseval-consistent 'power' scaling: the band SUM of Pxx equals that band's mean-square (so a tone
    # of amplitude A sums to ~A²/2 over its lobe). Window-power normalised (sum(win²)), not sum(win)².
    scale = 2.0 / (nperseg * np.sum(win ** 2))
    hop = max(1, nperseg // 2)
    acc = None
    count = 0
    i = 0
    while i + nperseg <= n:
        seg = x[i:i + nperseg] * win
        p = (np.abs(np.fft.rfft(seg)) ** 2) * scale
        acc = p if acc is None else acc + p
        count += 1
        i += hop
    if acc is None:                                    # capture shorter than one segment
        w = np.hanning(n)
        acc = (np.abs(np.fft.rfft(x * w)) ** 2) * (2.0 / (n * np.sum(w ** 2)))
        count = 1
    pxx = acc / count
    pxx[0] *= 0.5                                       # DC is one-sided, not doubled
    if nperseg % 2 == 0:
        pxx[-1] *= 0.5                                  # Nyquist likewise
    freqs = np.fft.rfftfreq(nperseg, 1.0 / fs)
    return freqs, pxx


def _band_power(freqs: Any, pxx: Any, lo: float, hi: float) -> float:
    from .doa import band_indices

    idx = band_indices(freqs, lo, hi)
    if idx.size == 0:
        return 0.0
    return float(pxx[idx].sum())


def _band_density(freqs: Any, pxx: Any, lo: float, hi: float) -> float:
    """Mean power-spectral density across [lo, hi] — bandwidth-normalised (per used bin)."""
    from .doa import band_indices

    idx = band_indices(freqs, lo, hi)
    if idx.size == 0:
        return 0.0
    return float(pxx[idx].mean())


def _detect_tones(freqs: Any, pxx: Any, *, band: Tuple[float, float], prominence_db: float,
                  max_tones: int, min_sep_hz: float) -> List[float]:
    import numpy as np

    lo, hi = band
    mask = (freqs >= lo) & (freqs <= hi)
    f = freqs[mask]
    p = pxx[mask]
    if f.size < 5:
        return []
    pdb = 10.0 * np.log10(p + 1e-20)
    floor = float(np.median(pdb))
    cands: List[Tuple[float, float]] = []
    for i in range(1, len(pdb) - 1):
        if pdb[i] >= pdb[i - 1] and pdb[i] >= pdb[i + 1] and (pdb[i] - floor) >= prominence_db:
            cands.append((pdb[i] - floor, float(f[i])))
    cands.sort(reverse=True)                            # strongest prominence first
    chosen: List[float] = []
    for _prom, freq in cands:
        if all(abs(freq - c) >= min_sep_hz for c in chosen):
            chosen.append(freq)
        if len(chosen) >= max_tones:
            break
    return sorted(round(c, 1) for c in chosen)


def _dbfs(power: float) -> float:
    import numpy as np

    return float(10.0 * np.log10(power + 1e-20))


# --------------------------------------------------------------------------- #
# analyze_placement — the scored analyzer
# --------------------------------------------------------------------------- #
def analyze_placement(capture: Any, *, sample_rate: float, device: str = DEFAULT_DEVICE,
                      label: str = "", duration_seconds: Optional[float] = None) -> PlacementResult:
    """Analyze a room-noise capture ``(N, channels)`` and return a scored :class:`PlacementResult`.

    Raises :class:`PlacementError` for empty or mono (1-D) input. Any channel count ≥ 1 and any sample
    rate are handled; the hiss band is capped at the Nyquist frequency."""
    import numpy as np

    x = np.asarray(capture, dtype=np.float64)
    if x.ndim != 2:
        raise PlacementError(f"capture must be (N, channels); mono / {x.ndim}-D not supported")
    n, ch = x.shape
    if n == 0 or ch == 0:
        raise PlacementError("capture is empty")
    fs = float(sample_rate)
    dur = float(duration_seconds) if duration_seconds is not None else (n / fs if fs > 0 else 0.0)
    nyq = 0.5 * fs

    # ---- levels ----
    per_ch_rms = np.sqrt(np.mean(x * x, axis=0))                       # (ch,)
    overall_rms = float(np.sqrt(np.mean(x * x)))
    noise_rms_dbfs = float(20.0 * np.log10(overall_rms + 1e-20))

    # ---- spectrum (averaged across channels) ----
    mono = x.mean(axis=1)
    freqs, pxx = _power_spectrum(mono, fs)
    hiss_hi = min(HISS_BAND_HZ[1], 0.999 * nyq)
    speech_power = _band_power(freqs, pxx, *SPEECH_BAND_HZ)
    rumble_power = _band_power(freqs, pxx, *RUMBLE_BAND_HZ)
    hiss_power = _band_power(freqs, pxx, HISS_BAND_HZ[0], hiss_hi)
    speech_dbfs = _dbfs(speech_power)
    rumble_dbfs = _dbfs(rumble_power)
    hiss_dbfs = _dbfs(hiss_power)

    # density ratios (gain- AND bandwidth-independent): the basis for scoring
    speech_density = _band_density(freqs, pxx, *SPEECH_BAND_HZ) + 1e-20
    rumble_over_speech_db = 10.0 * np.log10((_band_density(freqs, pxx, *RUMBLE_BAND_HZ) + 1e-20) / speech_density)
    hiss_over_speech_db = 10.0 * np.log10((_band_density(freqs, pxx, HISS_BAND_HZ[0], hiss_hi) + 1e-20) / speech_density)

    # ---- tones ----
    tone_hi = min(TONE_SEARCH_BAND_HZ[1], 0.999 * nyq)
    tones = _detect_tones(freqs, pxx, band=(TONE_SEARCH_BAND_HZ[0], tone_hi),
                          prominence_db=TONE_PROMINENCE_DB, max_tones=MAX_TONES,
                          min_sep_hz=TONE_MIN_SEPARATION_HZ)

    # ---- clipping ----
    clip_frac = float(np.mean(np.abs(x) >= CLIP_LEVEL))
    clipping_risk = clip_frac >= CLIP_FRACTION

    # ---- channel imbalance (dB vs the median capsule) ----
    median_rms = float(np.median(per_ch_rms)) + 1e-20
    imbalance_db = [float(20.0 * np.log10((r + 1e-20) / median_rms)) for r in per_ch_rms]
    max_imbalance = max(imbalance_db) if ch > 1 else 0.0
    hottest_ch = int(np.argmax(imbalance_db)) if ch > 1 else 0

    # ---- score + reasons ----
    score = 100
    reasons: List[str] = []
    recs: List[str] = []
    hpf_suggestion: Optional[float] = None

    if rumble_over_speech_db >= RUMBLE_BAD_DB:
        score -= 30
        reasons.append(f"Strong low-frequency rumble ({rumble_over_speech_db:.0f} dB over the speech band)")
        recs.append("Move the array off the desk surface / away from AC ducts and structural vibration")
        recs.append(f"Consider a speech high-pass around {HPF_SUGGESTION_HZ:.0f} Hz")
        hpf_suggestion = HPF_SUGGESTION_HZ
    elif rumble_over_speech_db >= RUMBLE_WARN_DB:
        score -= 12
        reasons.append(f"Moderate low-frequency rumble ({rumble_over_speech_db:.0f} dB over the speech band)")
        recs.append(f"Consider a speech high-pass around {HPF_SUGGESTION_HZ:.0f} Hz")
        hpf_suggestion = HPF_SUGGESTION_HZ

    if tones:
        score -= min(40, 12 * len(tones))
        tone_str = ", ".join(f"{t:.0f}" for t in tones)
        reasons.append(f"Tonal peak(s) at {tone_str} Hz (likely HVAC/fan)")
        recs.append("Move the array 0.5–1 m away from airflow/vent/fan and re-check")
        recs.append(f"Consider notch filters at {tone_str} Hz (pre-NR stage)")

    if hiss_over_speech_db >= HISS_BAD_DB:
        score -= 20
        reasons.append(f"High broadband hiss ({hiss_over_speech_db:.0f} dB over the speech band)")
        recs.append("Check for a nearby fan/air outlet or a noisy USB hub/cable; re-check at a new spot")
    elif hiss_over_speech_db >= HISS_WARN_DB:
        score -= 8
        reasons.append(f"Elevated broadband hiss ({hiss_over_speech_db:.0f} dB over the speech band)")

    if max_imbalance >= IMBALANCE_BAD_DB:
        score -= 20
        reasons.append(f"Capsule {hottest_ch} is {max_imbalance:.1f} dB louder than the median capsule")
        recs.append("Check for a blocked/covered capsule or very local airflow on one side")
    elif max_imbalance >= IMBALANCE_WARN_DB:
        score -= 8
        reasons.append(f"Capsule {hottest_ch} is {max_imbalance:.1f} dB above the median capsule")

    if clipping_risk:
        score -= 35
        reasons.append("Input is near clipping")
        recs.append("Lower the input gain or move away from a loud nearby source, then re-check")

    if noise_rms_dbfs >= NOISE_FLOOR_HIGH_DBFS:
        score -= 15
        reasons.append(f"Overall room noise is high ({noise_rms_dbfs:.0f} dBFS)")

    # local-hotspot heuristic (NOT a hard truth): one loud capsule, reinforced by tones/hiss
    local_hotspot = bool(
        max_imbalance >= IMBALANCE_BAD_DB
        or (max_imbalance >= IMBALANCE_WARN_DB and (bool(tones) or hiss_over_speech_db >= HISS_WARN_DB))
    )
    if local_hotspot and not any("capsule" in r.lower() for r in recs):
        recs.append("A local hotspot (airflow/object near one capsule) is suspected — try re-aiming/relocating")

    score = int(max(0, min(100, score)))
    status = STATUS_GOOD if score >= GOOD_MIN_SCORE else (
        STATUS_ACCEPTABLE if score >= ACCEPTABLE_MIN_SCORE else STATUS_BAD)
    if not reasons:
        reasons.append("Room noise is low and no strong local tones were detected.")
        recs.append("This position looks good — proceed.")

    return PlacementResult(
        version=PLACEMENT_VERSION, device=device, sample_rate=fs, channels=ch,
        duration_seconds=dur, label=label, status=status, score=score,
        noise_rms_dbfs=noise_rms_dbfs, speech_band_noise_dbfs=speech_dbfs,
        low_frequency_rumble_dbfs=rumble_dbfs, broadband_hiss_dbfs=hiss_dbfs,
        detected_tones_hz=tuple(tones), notch_suggestions_hz=tuple(tones),
        hpf_suggestion_hz=hpf_suggestion, clipping_risk=clipping_risk,
        channel_imbalance_db=tuple(round(d, 2) for d in imbalance_db),
        local_hotspot_suspected=local_hotspot, reasons=tuple(reasons), recommendations=tuple(recs),
    )
