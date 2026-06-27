"""Operator workflow status — surface Phases 1–5 to a non-DSP operator (Phase 6).

The full GUI ``MainWindow`` hangs headless on this box (see ``CLAUDE.md``), so the operator surface is
built as a **hardware-free status model**: :class:`OperatorStatus` gathers the engine + the Phase 1–5
objects into seven sections (Device / Calibration / Placement / Pipeline / Output / Transcription /
Diagnostics) and exports JSON + Markdown. A light read-only GUI panel renders this model; a CLI prints
it. The model only **reads** engine flags — it changes no default and applies nothing.

Honest by construction: OFF stages show OFF, a failed calibration is surfaced (never hidden), a BAD
placement is a warning, and placement suggestions are carried with ``autoApplied=False`` — the operator
opts in, per room. Nothing here forces a cleaner on or routes raw multichannel as the clean output.
"""
from __future__ import annotations

import json
import os
from typing import Any, Dict, List, Optional

OPERATOR_STATUS_VERSION = 1

# Canonical pipeline order (display): (stage label, engine flag attr or None, detail)
_PIPELINE_ORDER = [
    ("preamp", "_preamp", "uniform mic-input gain"),
    ("calibration", "_calib", "per-capsule gain / polarity / delay"),
    ("beam", None, "beamform + DOA + null steering"),
    ("AEC", "aec", "echo cancel"),
    ("transient", "transient_suppress", "tap / knock duck"),
    ("dereverb", "dereverb", "late-reverb suppress"),
    ("pre-NR HPF/notch", "pre_nr", "linear cleanup BEFORE the denoiser"),
    ("post-NR", "post_nr", "DFN3 / OM-LSA / Wiener / gate"),
    ("PEQ", "peq", "tone shaping after cleaning"),
    ("AGC/limiter", "agc_target_db", "target-loudness + limiter"),
    ("band-limit", "beam_bandlimit_hz", "anti-alias low-pass"),
    ("voice-gate", "voice_gate", "mute non-speech (last)"),
]


class OperatorStatus:
    """A read-only snapshot of the audio front-end for the operator. Build it with :meth:`build`."""

    def __init__(self, *, engine: Any = None, device: Optional[Dict[str, Any]] = None,
                 calibration_path: Optional[str] = None,
                 calibration_low_conf: Optional[List[int]] = None,
                 placement: Any = None, egress: Any = None, transcription: Any = None,
                 generated_at: str = "") -> None:
        self._engine = engine
        self._device = device or {}
        self._calibration_path = calibration_path
        self._calibration_low_conf = calibration_low_conf
        self._placement = placement
        self._egress = egress
        self._transcription = transcription
        self._stamp = generated_at

    @classmethod
    def build(cls, *, engine: Any = None, device: Optional[Dict[str, Any]] = None,
              calibration_path: Optional[str] = None, calibration_low_conf: Optional[List[int]] = None,
              placement: Any = None, egress: Any = None, transcription: Any = None,
              generated_at: str = "") -> "OperatorStatus":
        return cls(engine=engine, device=device, calibration_path=calibration_path,
                   calibration_low_conf=calibration_low_conf, placement=placement, egress=egress,
                   transcription=transcription, generated_at=generated_at)

    # ---- Section 1: Device ----
    def _engine_latency_ms(self) -> Optional[float]:
        eng = self._engine
        if eng is None:
            return None
        try:                                                  # property reads lazily-built stage attrs
            return round(float(eng.estimated_latency_ms), 1)
        except Exception:
            return None

    def device_section(self) -> Dict[str, Any]:
        eng = self._engine
        dev = self._device
        sr = dev.get("sampleRate") or getattr(eng, "sample_rate", None)
        ch = dev.get("channels") or getattr(eng, "n_channels", None)
        warns: List[str] = []
        if ch is not None and int(ch) != 8:
            warns.append(f"device reports {ch} channels (POLARIS expects 8)")
        return {
            "name": dev.get("name", "POLARIS (8-MEMS)"),
            "sampleRate": float(sr) if sr else None,
            "channels": int(ch) if ch else None,
            "blockSize": int(getattr(eng, "blocksize", 0)) or None,
            "latencyMs": self._engine_latency_ms(),
            "inputStatus": dev.get("inputStatus", "configured" if eng is not None else "n/a"),
            "outputStatus": dev.get("outputStatus", "via egress" if self._egress is not None else "n/a"),
            "warnings": warns,
        }

    # ---- Section 2: Calibration ----
    def calibration_section(self) -> Dict[str, Any]:
        eng = self._engine
        calib = getattr(eng, "_calib", None) if eng is not None else None
        if calib is None:
            failed = bool(self._calibration_path)
            status = ("Calibration OFF — profile failed to load; running on raw capsules" if failed
                      else "Calibration OFF — raw capsules (default)")
            return {"enabled": False, "profilePath": self._calibration_path, "status": status,
                    "device": "", "sampleRate": None, "channels": None, "createdAt": "",
                    "referenceChannel": None, "gainDbSummary": "", "delaySummary": "",
                    "polaritySummary": "", "latencySamples": 0, "lowConfidenceChannels": []}
        p = calib.profile
        gains = [float(g) for g in p.gain_db] or [0.0]
        n_inv = sum(1 for x in p.polarity if int(x) < 0)
        max_delay = max((int(d) for d in p.delay_samples), default=0)
        return {
            "enabled": True, "profilePath": self._calibration_path, "device": p.device,
            "sampleRate": float(p.sample_rate), "channels": int(p.channels), "createdAt": p.created_at,
            "referenceChannel": int(p.reference_channel),
            "gainDbSummary": f"{min(gains):+.1f} to {max(gains):+.1f} dB",
            "delaySummary": f"max {max_delay} samples",
            "polaritySummary": (f"{n_inv} channel(s) inverted" if n_inv else "none inverted"),
            "latencySamples": int(getattr(calib, "latency_samples", 0)),
            "lowConfidenceChannels": list(self._calibration_low_conf or []),
            "status": f"Calibration ON — {p.channels}ch @ {float(p.sample_rate):.0f} Hz, ref {p.reference_channel}",
        }

    # ---- Section 3: Placement check ----
    def placement_section(self) -> Dict[str, Any]:
        r = self._placement
        if r is None:
            return {"available": False,
                    "note": "Placement check not run — record room noise (no speech) and run it."}
        return {
            "available": True, "status": r.status, "score": int(r.score),
            "reasons": list(r.reasons), "recommendations": list(r.recommendations),
            "detectedTones": list(r.detected_tones_hz), "notchSuggestions": list(r.notch_suggestions_hz),
            "hpfSuggestion": r.hpf_suggestion_hz, "channelImbalanceDb": list(r.channel_imbalance_db),
            "clippingRisk": bool(r.clipping_risk), "localHotspotSuspected": bool(r.local_hotspot_suspected),
            "suggestedPreNrBands": r.to_pre_nr_bands(),
            "autoApplied": False,
            "note": "Suggestions are NOT auto-applied — opt in, and re-measure per room (tones are this room's).",
        }

    # ---- Section 4: Pipeline order + active stages ----
    def _stage_active(self, eng: Any, stage: str, attr: Optional[str], detail: str):
        if eng is None:
            return False, detail
        if stage == "beam":
            return True, f"{getattr(eng, 'mode', '')} {detail}".strip()
        if stage == "preamp":
            return getattr(eng, "_preamp", None) is not None, detail
        if stage == "calibration":
            return getattr(eng, "_calib", None) is not None, detail
        if stage == "pre-NR HPF/notch":
            on = bool(getattr(eng, "pre_nr", False)) and bool(getattr(eng, "_pre_nr_bands", None))
            return on, detail
        if stage == "post-NR":
            on = bool(getattr(eng, "post_nr", False))
            return on, (f"{getattr(eng, '_post_nr_engine', 'gate')} ({detail})" if on else detail)
        if stage == "PEQ":
            return bool(getattr(eng, "peq", False)) and bool(getattr(eng, "_peq_bands", None)), detail
        if stage == "AGC/limiter":
            return getattr(eng, "agc_target_db", None) is not None, detail
        if stage == "band-limit":
            return bool(getattr(eng, "beam_bandlimit_hz", None)), detail
        return bool(getattr(eng, attr, False)) if attr else False, detail

    def _cleaning_summary(self, eng: Any) -> str:
        if eng is None:
            return ""
        out: List[str] = []
        if getattr(eng, "aec", False):
            out.append("AEC")
        if getattr(eng, "transient_suppress", False):
            out.append("transient")
        if getattr(eng, "dereverb", False):
            out.append("dereverb")
        if getattr(eng, "pre_nr", False) and getattr(eng, "_pre_nr_bands", None):
            out.append("HPF/notch")
        if getattr(eng, "post_nr", False):
            out.append(str(getattr(eng, "_post_nr_engine", "gate")))
        if getattr(eng, "voice_gate", False):
            out.append("voice-gate")
        return " + ".join(out) if out else "(none — clean beam only)"

    def pipeline_section(self) -> Dict[str, Any]:
        eng = self._engine
        order = []
        for stage, attr, detail in _PIPELINE_ORDER:
            active, det = self._stage_active(eng, stage, attr, detail)
            order.append({"stage": stage, "active": bool(active), "detail": det})
        return {
            "order": order,
            "activeCleaningStages": self._cleaning_summary(eng),
            "note": "Cleaners are default-OFF and never forced on; this is a read-out, not a control.",
        }

    # ---- Section 5: Output / egress ----
    def egress_section(self) -> Dict[str, Any]:
        r = self._egress
        if r is None:
            return {"available": False, "routes": [], "sampleRate": None, "asrRate": None,
                    "note": "No egress router attached."}
        sr = int(getattr(r, "sample_rate", 48000))
        asr = int(getattr(r, "asr_rate", 16000))
        pending = round(float(r.pending_seconds()), 3) if hasattr(r, "pending_seconds") else 0.0
        return {
            "available": True, "sampleRate": float(getattr(r, "sample_rate", 0.0)), "asrRate": asr,
            "framesPushed": int(getattr(r, "frames_pushed", 0)), "pendingSeconds": pending,
            "algorithmicLatencyMs": float(getattr(r, "algorithmic_latency_ms", 0.0)),
            "wavSink": getattr(r, "_wav", None) is not None,
            "externalSinks": len(getattr(r, "_sinks", []) or []),
            "routes": [f"{sr} Hz mono PCM (conferencing / record / monitor)",
                       f"{asr} Hz mono int16 (ASR-ready)"],
            "note": "Only processed clean mono is routed; raw 8-channel input is rejected. "
                    "Virtual mic is an external adapter seam — no OS driver bundled.",
        }

    # ---- Section 6: Transcription ----
    def transcription_section(self) -> Dict[str, Any]:
        s = self._transcription
        if s is None:
            return {"available": False, "note": "No transcription stream attached."}
        sess = getattr(s, "session", None)
        prov = getattr(s, "provider", None)
        chunker = getattr(s, "_chunker", None)
        from .transcription import MockTranscriptionProvider
        return {
            "available": True,
            "provider": type(prov).__name__ if prov is not None else "",
            "isMock": isinstance(prov, MockTranscriptionProvider),
            "sessionId": getattr(sess, "session_id", None),
            "status": getattr(sess, "status", "idle"),
            "chunksSent": int(getattr(sess, "chunks_sent", 0)),
            "durationSeconds": round(float(getattr(sess, "duration_seconds", 0.0)), 3),
            "vad": {"thresholdDbfs": getattr(chunker, "threshold_dbfs", None),
                    "frame": getattr(chunker, "frame", None)},
            "note": "Mock/dev provider — no real ASR vendor, no network call by default. "
                    "Consumes the Phase 4 clean 16 kHz ASR stream.",
        }

    # ---- Section 7: warnings + export ----
    def warnings(self) -> List[str]:
        w: List[str] = []
        if not self.calibration_section()["enabled"] and self._calibration_path:
            w.append("Calibration profile failed to load — running without calibration.")
        w.extend(self.device_section().get("warnings", []))
        p = self._placement
        if p is not None:
            if p.status == "BAD":
                w.append(f"Placement is BAD ({p.score}/100): " + "; ".join(p.reasons))
            elif p.status == "ACCEPTABLE":
                w.append(f"Placement is ACCEPTABLE ({p.score}/100) — see recommendations.")
            if getattr(p, "clipping_risk", False):
                w.append("Input is near clipping.")
        return w

    def to_dict(self) -> Dict[str, Any]:
        return {
            "version": OPERATOR_STATUS_VERSION, "generatedAt": self._stamp,
            "device": self.device_section(), "calibration": self.calibration_section(),
            "placement": self.placement_section(), "pipeline": self.pipeline_section(),
            "egress": self.egress_section(), "transcription": self.transcription_section(),
            "warnings": self.warnings(),
        }

    def to_markdown(self) -> str:
        d = self.to_dict()
        L: List[str] = [f"# Operator Diagnostics{(' — ' + self._stamp) if self._stamp else ''}", ""]
        dev = d["device"]
        L += ["## Device",
              f"- {dev['name']} · {dev['sampleRate']} Hz · {dev['channels']} ch · "
              f"block {dev['blockSize']} · latency {dev['latencyMs']} ms",
              f"- input: {dev['inputStatus']} · output: {dev['outputStatus']}", ""]
        cal = d["calibration"]
        L += ["## Calibration", f"- {cal['status']}"]
        if cal["enabled"]:
            L += [f"- ref ch {cal['referenceChannel']} · gains {cal['gainDbSummary']} · "
                  f"polarity {cal['polaritySummary']} · delay {cal['delaySummary']} · "
                  f"+{cal['latencySamples']} samples latency"]
        L += [""]
        pl = d["placement"]
        L += ["## Placement Check"]
        if pl["available"]:
            L += [f"- **{pl['status']}** ({pl['score']}/100)"]
            L += [f"- reason: {r}" for r in pl["reasons"]]
            L += [f"- recommend: {r}" for r in pl["recommendations"]]
            if pl["detectedTones"]:
                L += [f"- detected tones (Hz): {', '.join(f'{t:.0f}' for t in pl['detectedTones'])}"]
            L += [f"- {pl['note']}"]
        else:
            L += [f"- {pl['note']}"]
        L += [""]
        pp = d["pipeline"]
        L += ["## Pipeline", f"- active cleaning: {pp['activeCleaningStages']}"]
        L += [f"  - {'[on]' if s['active'] else '[off]'} {s['stage']} — {s['detail']}" for s in pp["order"]]
        L += [f"- {pp['note']}", ""]
        eg = d["egress"]
        L += ["## Output / Egress"]
        L += ([f"- routes: {', '.join(eg['routes'])}",
               f"- frames pushed {eg['framesPushed']} · pending {eg['pendingSeconds']} s · "
               f"latency {eg['algorithmicLatencyMs']} ms", f"- {eg['note']}"]
              if eg["available"] else [f"- {eg['note']}"])
        L += [""]
        tr = d["transcription"]
        L += ["## Transcription"]
        L += ([f"- provider {tr['provider']} (mock={tr['isMock']}) · session {tr['status']} · "
               f"chunks {tr['chunksSent']}", f"- {tr['note']}"]
              if tr["available"] else [f"- {tr['note']}"])
        L += [""]
        if d["warnings"]:
            L += ["## Warnings"] + [f"- ⚠ {w}" for w in d["warnings"]] + [""]
        return "\n".join(L)

    def save(self, out_dir: str, *, stamp: str = "") -> List[str]:
        """Write ``operator_diagnostics_<stamp>.json`` + ``.md`` into ``out_dir``; returns the paths."""
        stamp = stamp or self._stamp or "snapshot"
        os.makedirs(out_dir, exist_ok=True)
        base = os.path.join(out_dir, f"operator_diagnostics_{stamp}")
        jp, mp = base + ".json", base + ".md"
        with open(jp, "w", encoding="utf-8") as f:
            json.dump(self.to_dict(), f, indent=2)
        with open(mp, "w", encoding="utf-8") as f:
            f.write(self.to_markdown())
        return [jp, mp]
