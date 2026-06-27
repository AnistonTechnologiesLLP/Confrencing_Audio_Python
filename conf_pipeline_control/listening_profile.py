"""Listening Processing Profiles — a *descriptive* recipe for each LIVE listening mode (Phase 10).

The LIVE panel's "Listening mode" dropdown (Whole table / Follow the room / Lock to a seat / Clean audio
/ Manual / Two kits) is a pre-Connect facade: the real DSP config is built at Connect. This module models
each mode as a **descriptive** `ListeningProfile` so the GUI can show an honest **processing-flow
summary** ("capture → preamp → … → output") + warnings. It is **inert** — it describes; it never applies
anything, never enables a feature, and never promotes room-specific tones to global defaults.

Distinct from `AudioRoomProfile` (room-specific saved *setup* references): a ListeningProfile is the live
*processing recipe* for a mode. The built-ins below mirror the real per-mode defaults exactly — **AGC is
off-by-default in every mode**, and **"Clean audio" enables the OM-LSA denoiser ONLY**. Pure stdlib.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from typing import Any, List, Mapping, Optional, Tuple

PROFILE_VERSION = 1

_ENGINE_LABELS = {"omlsa": "OM-LSA", "dfn3": "DeepFilterNet3", "wiener": "Wiener", "gate": "gate",
                  "none": "OFF"}


@dataclass(frozen=True)
class LpSpatial:
    mode: str = "whole_table"
    beam_mode: str = "superdirective"
    auto_steer: bool = False
    lock_seat: Optional[str] = None
    null_steering: bool = False


@dataclass(frozen=True)
class LpCalibration:
    enabled: bool = False
    profile_path: Optional[str] = None


@dataclass(frozen=True)
class LpPreNr:
    enabled: bool = False
    hpf_hz: Optional[float] = None
    notches_hz: Tuple[float, ...] = ()


@dataclass(frozen=True)
class LpPostNr:
    enabled: bool = False
    engine: str = "none"


@dataclass(frozen=True)
class LpToggle:
    enabled: bool = False


@dataclass(frozen=True)
class LpAgc:
    enabled: bool = False
    target_db: float = -20.0


@dataclass(frozen=True)
class LpCleanup:
    aec: bool = False
    transient_suppression: bool = False
    dereverb: bool = False
    pre_nr: LpPreNr = field(default_factory=LpPreNr)
    post_nr: LpPostNr = field(default_factory=LpPostNr)
    peq: LpToggle = field(default_factory=LpToggle)
    agc: LpAgc = field(default_factory=LpAgc)
    voice_gate: LpToggle = field(default_factory=LpToggle)


@dataclass(frozen=True)
class LpOutput:
    monitor: bool = False
    egress: bool = False
    asr_16k: bool = False


@dataclass(frozen=True)
class LpSafety:
    default_safe: bool = True
    requires_headphones: bool = False
    latency_warning: Optional[str] = None
    naturalness_warning: Optional[str] = None
    notes: Tuple[str, ...] = ()


@dataclass(frozen=True)
class ListeningProfile:
    """A descriptive live-processing recipe for one listening mode."""

    version: int = PROFILE_VERSION
    id: str = ""
    name: str = ""
    description: str = ""
    is_built_in: bool = True
    spatial: LpSpatial = field(default_factory=LpSpatial)
    calibration: LpCalibration = field(default_factory=LpCalibration)
    cleanup: LpCleanup = field(default_factory=LpCleanup)
    output: LpOutput = field(default_factory=LpOutput)
    safety: LpSafety = field(default_factory=LpSafety)

    # ---- human-readable processing flow + warnings ----
    def flow_summary(self) -> str:
        """A compact one-line processing flow, e.g. ``capture → preamp → calibration OFF → beam: … →
        auto-steer ON → pre-NR OFF → denoise OM-LSA → AGC OFF → voice gate OFF → output``."""
        c = self.cleanup
        if self.spatial.mode == "two_kits":
            denoise = _ENGINE_LABELS.get(c.post_nr.engine, c.post_nr.engine) if c.post_nr.enabled else "OFF"
            return (f"two arrays → talker select + cross-fade → denoise {denoise} → "
                    f"AGC {_on(c.agc.enabled)} → combined output")
        if self.spatial.auto_steer:
            steer = "auto-steer ON"
        elif self.spatial.mode == "lock_seat":
            steer = f"lock seat {self.spatial.lock_seat}" if self.spatial.lock_seat else "lock to selected seat"
        else:
            steer = "fixed beam"
        denoise = _ENGINE_LABELS.get(c.post_nr.engine, c.post_nr.engine) if c.post_nr.enabled else "OFF"
        parts: List[str] = ["capture", "preamp", f"calibration {_on(self.calibration.enabled)}",
                            f"beam: {self.spatial.beam_mode}", steer]
        if c.aec:
            parts.append("AEC ON")
        if c.transient_suppression:
            parts.append("transient ON")
        if c.dereverb:
            parts.append("dereverb ON")
        parts.append(f"pre-NR {_on(c.pre_nr.enabled)}")
        parts.append(f"denoise {denoise}")
        parts.append(f"AGC {_on(c.agc.enabled)}")
        parts.append(f"voice gate {_on(c.voice_gate.enabled)}")
        out = "output"
        if self.output.egress:
            out += " + egress"
        if self.output.asr_16k:
            out += " + 16k ASR"
        parts.append(out)
        return " → ".join(parts)

    def warnings(self) -> List[str]:
        w: List[str] = []
        c = self.cleanup
        if c.post_nr.enabled and c.post_nr.engine in ("omlsa", "dfn3", "wiener"):
            eng = _ENGINE_LABELS.get(c.post_nr.engine, c.post_nr.engine)
            w.append(f"AI voice cleaning ({eng}) is ON — adds some latency and can slightly alter naturalness.")
        else:
            w.append("DFN3 / denoise is OFF by default.")
        if c.dereverb:
            w.append("Dereverb is ON — can alter room naturalness.")
        w.append("Room-specific notches/calibration must come from the placement check or a room profile, "
                 "not global defaults.")
        for note in self.safety.notes:
            w.append(note)
        if self.safety.latency_warning:
            w.append(self.safety.latency_warning)
        if self.safety.naturalness_warning:
            w.append(self.safety.naturalness_warning)
        return w

    # ---- JSON ----
    def to_dict(self) -> dict:
        s, cal, c = self.spatial, self.calibration, self.cleanup
        return {
            "version": int(self.version), "id": str(self.id), "name": str(self.name),
            "description": str(self.description), "isBuiltIn": bool(self.is_built_in),
            "spatial": {"mode": s.mode, "beamMode": s.beam_mode, "autoSteer": bool(s.auto_steer),
                        "lockSeat": s.lock_seat, "nullSteering": bool(s.null_steering)},
            "calibration": {"enabled": bool(cal.enabled), "profilePath": cal.profile_path},
            "cleanup": {
                "aec": bool(c.aec), "transientSuppression": bool(c.transient_suppression),
                "dereverb": bool(c.dereverb),
                "preNr": {"enabled": bool(c.pre_nr.enabled),
                          "hpfHz": (None if c.pre_nr.hpf_hz is None else float(c.pre_nr.hpf_hz)),
                          "notchesHz": [float(x) for x in c.pre_nr.notches_hz]},
                "postNr": {"enabled": bool(c.post_nr.enabled), "engine": str(c.post_nr.engine)},
                "peq": {"enabled": bool(c.peq.enabled)},
                "agc": {"enabled": bool(c.agc.enabled), "targetDb": float(c.agc.target_db)},
                "voiceGate": {"enabled": bool(c.voice_gate.enabled)},
            },
            "output": {"monitor": bool(self.output.monitor), "egress": bool(self.output.egress),
                       "asr16k": bool(self.output.asr_16k)},
            "safety": {"defaultSafe": bool(self.safety.default_safe),
                       "requiresHeadphones": bool(self.safety.requires_headphones),
                       "latencyWarning": self.safety.latency_warning,
                       "naturalnessWarning": self.safety.naturalness_warning,
                       "notes": list(self.safety.notes)},
        }

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> "ListeningProfile":
        s = dict(d.get("spatial", {}) or {})
        cal = dict(d.get("calibration", {}) or {})
        c = dict(d.get("cleanup", {}) or {})
        pn = dict(c.get("preNr", {}) or {})
        po = dict(c.get("postNr", {}) or {})
        ag = dict(c.get("agc", {}) or {})
        out = dict(d.get("output", {}) or {})
        sf = dict(d.get("safety", {}) or {})
        pnh = pn.get("hpfHz", None)
        return cls(
            version=int(d.get("version", PROFILE_VERSION)), id=str(d.get("id", "")),
            name=str(d.get("name", "")), description=str(d.get("description", "")),
            is_built_in=bool(d.get("isBuiltIn", True)),
            spatial=LpSpatial(mode=str(s.get("mode", "whole_table")),
                              beam_mode=str(s.get("beamMode", "superdirective")),
                              auto_steer=bool(s.get("autoSteer", False)), lock_seat=s.get("lockSeat", None),
                              null_steering=bool(s.get("nullSteering", False))),
            calibration=LpCalibration(enabled=bool(cal.get("enabled", False)),
                                      profile_path=cal.get("profilePath", None)),
            cleanup=LpCleanup(
                aec=bool(c.get("aec", False)), transient_suppression=bool(c.get("transientSuppression", False)),
                dereverb=bool(c.get("dereverb", False)),
                pre_nr=LpPreNr(enabled=bool(pn.get("enabled", False)),
                               hpf_hz=(None if pnh is None else float(pnh)),
                               notches_hz=tuple(float(x) for x in pn.get("notchesHz", []))),
                post_nr=LpPostNr(enabled=bool(po.get("enabled", False)), engine=str(po.get("engine", "none"))),
                peq=LpToggle(enabled=bool((c.get("peq", {}) or {}).get("enabled", False))),
                agc=LpAgc(enabled=bool(ag.get("enabled", False)), target_db=float(ag.get("targetDb", -20.0))),
                voice_gate=LpToggle(enabled=bool((c.get("voiceGate", {}) or {}).get("enabled", False)))),
            output=LpOutput(monitor=bool(out.get("monitor", False)), egress=bool(out.get("egress", False)),
                            asr_16k=bool(out.get("asr16k", False))),
            safety=LpSafety(default_safe=bool(sf.get("defaultSafe", True)),
                            requires_headphones=bool(sf.get("requiresHeadphones", False)),
                            latency_warning=sf.get("latencyWarning", None),
                            naturalness_warning=sf.get("naturalnessWarning", None),
                            notes=tuple(str(x) for x in sf.get("notes", []))),
        )

    @classmethod
    def from_json(cls, text: str) -> "ListeningProfile":
        return cls.from_dict(json.loads(text))

    def to_json(self, *, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent)


def _on(v: bool) -> str:
    return "ON" if v else "OFF"


def _recommended_cleanup(*, denoise: bool, transient: bool, dereverb: bool = False,
                         agc: bool = True) -> LpCleanup:
    """The shipped **recommended** cleanup, mirroring the LIVE panel's pre-ticked defaults: AGC on every
    mode, plus the OM-LSA denoiser + tap-suppression on the steering paths (Follow / Lock-to-seat / Clean /
    Two-kits). **Dereverb is opt-in per profile** (``dereverb=False`` here) — it is NOT a global default
    (it can colour a dry room), and only Follow / Clean recommend it ON. The base "Whole table" path has no
    denoiser, so its ``denoise``/``transient`` stay off; the Two-kits cross-array path has no
    dereverb/transient wiring. AEC and the voice gate remain opt-in (off) — AEC needs a far-end reference,
    the gate can clip soft speech."""
    return LpCleanup(
        transient_suppression=transient,
        dereverb=dereverb,
        post_nr=LpPostNr(enabled=denoise, engine=("omlsa" if denoise else "none")),
        agc=LpAgc(enabled=agc),
    )


# --------------------------------------------------------------------------- #
# Built-in profiles — one per LIVE dropdown mode (honest flags; recommended cleanup ON by default)
# --------------------------------------------------------------------------- #
BUILTIN_LISTENING_PROFILES = {
    "table": ListeningProfile(
        id="whole_table", name="Whole table",
        description="Default meeting-table pickup (fixed zones). Recommended AGC + dereverb; no denoiser "
                    "on this base path.",
        spatial=LpSpatial(mode="whole_table", beam_mode="superdirective", auto_steer=False),
        cleanup=_recommended_cleanup(denoise=False, transient=False)),
    "follow": ListeningProfile(
        id="follow_the_room", name="Follow the room (auto-steer)",
        description="Follow the active talker by direction (DOA / auto-steer), with the recommended "
                    "cleaning chain (OM-LSA + dereverb + AGC).",
        spatial=LpSpatial(mode="auto_steer", beam_mode="superdirective", auto_steer=True, null_steering=True),
        cleanup=_recommended_cleanup(denoise=True, transient=True, dereverb=True)),
    "seat": ListeningProfile(
        id="lock_to_seat", name="Lock to a seat",
        description="Fixed pickup toward a selected seat, with the recommended cleaning chain.",
        spatial=LpSpatial(mode="lock_seat", beam_mode="superdirective", auto_steer=False, null_steering=True),
        cleanup=_recommended_cleanup(denoise=True, transient=True),
        safety=LpSafety(notes=(
            "Lock-to-seat needs a selected seat with a room bearing; otherwise it follows the talker.",))),
    "clean": ListeningProfile(
        id="clean_audio", name="Clean audio (hands-off)",
        description="Auto-steer + the recommended AI cleaning chain (OM-LSA + dereverb + AGC). Hands-off, "
                    "operator-safe meeting audio.",
        spatial=LpSpatial(mode="auto_steer", beam_mode="superdirective", auto_steer=True, null_steering=True),
        cleanup=_recommended_cleanup(denoise=True, transient=True, dereverb=True)),
    "manual": ListeningProfile(
        id="manual", name="Manual (advanced)",
        description="Every control is manual — your live toggles are the source of truth.",
        safety=LpSafety(notes=(
            "Manual mode: your live toggles are the source of truth; this profile does not override them.",))),
    "twokit": ListeningProfile(
        id="two_kits", name="Two kits (combined room)",
        description="Combined-room automix: select the active kit + cross-fade to one output, with "
                    "recommended per-kit OM-LSA cleaning and one combined AGC.",
        spatial=LpSpatial(mode="two_kits", beam_mode="automix", auto_steer=False),
        cleanup=_recommended_cleanup(denoise=True, transient=False, dereverb=False),
        safety=LpSafety(notes=(
            "Combined-room automix (talker select + cross-fade); per-kit OM-LSA cleaning + one combined AGC.",))),
}


def _manual_profile(flags: Mapping[str, Any]) -> ListeningProfile:
    eng = str(flags.get("post_nr_engine", "none") or "none")
    post = bool(flags.get("post_nr", False))
    base = BUILTIN_LISTENING_PROFILES["manual"]
    return replace(
        base,
        spatial=LpSpatial(mode="manual", beam_mode=str(flags.get("beam_mode", "superdirective")),
                          auto_steer=bool(flags.get("auto_steer", False)),
                          null_steering=bool(flags.get("null_steering", False))),
        cleanup=LpCleanup(
            aec=bool(flags.get("aec", False)),
            transient_suppression=bool(flags.get("transient", False)),
            dereverb=bool(flags.get("dereverb", False)),
            pre_nr=LpPreNr(enabled=bool(flags.get("pre_nr", False))),
            post_nr=LpPostNr(enabled=post, engine=(eng if post else "none")),
            peq=LpToggle(enabled=bool(flags.get("peq", False))),
            agc=LpAgc(enabled=bool(flags.get("agc", False))),
            voice_gate=LpToggle(enabled=bool(flags.get("voice_gate", False)))),
    )


def listening_profile_for_mode(mode: str, *, manual_flags: Optional[Mapping[str, Any]] = None) -> ListeningProfile:
    """Return the built-in profile for a LIVE dropdown mode key (``table``/``follow``/``seat``/``clean``/
    ``manual``/``twokit``). For ``manual`` with ``manual_flags`` (the live toggles), build a descriptive
    profile reflecting them. An unknown mode returns the safe ``whole_table`` default."""
    if mode == "manual" and manual_flags is not None:
        return _manual_profile(manual_flags)
    return BUILTIN_LISTENING_PROFILES.get(str(mode), BUILTIN_LISTENING_PROFILES["table"])
