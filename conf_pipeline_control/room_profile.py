"""Audio Room Profile — a saveable, room-specific audio-setup document (Phase 9).

A POLARIS room is set up per room, and the right settings (which capsule calibration, which measured
HVAC tones to notch, whether to record / transcribe) are **room-specific**. This module is the editable
**document** that captures those choices: references to a calibration profile + a placement result, the
measured pre-NR HPF/notch suggestions, egress/transcription preferences, operator notes, and a set of
**safety flags**.

It is deliberately **inert**: it stores references + preferences and round-trips to camelCase JSON; it
**never touches the DSP engine**, never enables a feature, and never auto-applies placement suggestions.
Loading/validating a profile only previews + warns. Applying a profile to a running engine is out of
scope here (a deliberate, separate, later step). Room-specific tones are never global defaults; the
guide states they must be re-measured per room.

Pure stdlib (no numpy). The model is mutable so the GUI can build a draft up incrementally.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any, List, Mapping, Optional

PROFILE_VERSION = 1
DEFAULT_DEVICE = "POLARIS_8MEMS"
DEFAULT_SAMPLE_RATE = 48000.0
DEFAULT_CHANNELS = 8


class RoomProfileError(Exception):
    """A room profile is structurally malformed or its file is unreadable (controlled, catchable)."""


# --------------------------------------------------------------------------- #
# Sections (mutable; the GUI edits a draft)
# --------------------------------------------------------------------------- #
@dataclass
class RoomCalibrationRef:
    enabled: bool = False                 # attaching a profile is NOT enabling it
    profile_path: str = ""
    summary: str = ""


@dataclass
class RoomPlacementRef:
    result_path: str = ""
    last_status: str = ""
    last_score: Optional[int] = None
    detected_tones_hz: List[float] = field(default_factory=list)
    notch_suggestions_hz: List[float] = field(default_factory=list)
    hpf_suggestion_hz: Optional[float] = None
    auto_apply_suggestions: bool = False  # must stay False — suggestions are opt-in, per room


@dataclass
class RoomPreNr:
    enabled: bool = False
    hpf_hz: Optional[float] = None
    notches_hz: List[float] = field(default_factory=list)


@dataclass
class RoomEgress:
    clean_mono_48k: bool = True
    asr_16k: bool = False
    wav_recording: bool = False
    external_sink: bool = False


@dataclass
class RoomTranscription:
    enabled: bool = False
    provider: str = "mock"
    sample_rate: int = 16000
    vad_enabled: bool = True


@dataclass
class RoomSafety:
    """All flags default to the SAFE value (False). A True flag means the profile would change a safe
    default; :meth:`AudioRoomProfile.validate` warns about it. The model never sets one True itself."""
    dfn3_forced_on: bool = False
    dereverb_forced_on: bool = False
    placement_suggestions_auto_applied: bool = False
    real_asr_network_call: bool = False
    virtual_mic_driver_bundled: bool = False


@dataclass
class AudioRoomProfile:
    """An editable room-setup profile. camelCase JSON on the wire; snake_case fields. Build it up via the
    fields + :meth:`attach_calibration` / :meth:`copy_placement_suggestions`, then :meth:`save`."""

    version: int = PROFILE_VERSION
    name: str = ""
    device: str = DEFAULT_DEVICE
    sample_rate: float = DEFAULT_SAMPLE_RATE
    channels: int = DEFAULT_CHANNELS
    created_at: str = ""
    updated_at: str = ""
    notes: str = ""
    calibration: RoomCalibrationRef = field(default_factory=RoomCalibrationRef)
    placement: RoomPlacementRef = field(default_factory=RoomPlacementRef)
    pre_nr_cleanup: RoomPreNr = field(default_factory=RoomPreNr)
    egress: RoomEgress = field(default_factory=RoomEgress)
    transcription: RoomTranscription = field(default_factory=RoomTranscription)
    safety: RoomSafety = field(default_factory=RoomSafety)

    # ---- editing helpers (draft only — never touch an engine) ----
    def attach_calibration(self, profile_path: str, *, summary: str = "") -> "AudioRoomProfile":
        """Reference a saved calibration profile. Does NOT enable calibration."""
        self.calibration.profile_path = str(profile_path)
        if summary:
            self.calibration.summary = str(summary)
        return self

    def copy_placement_suggestions(self, result: Any, *, result_path: str = "") -> "AudioRoomProfile":
        """Copy a :class:`~conf_pipeline_control.placement.PlacementResult`'s tones + suggestions into
        this profile DRAFT (the placement section + the pre-NR notch/HPF). It does **not** enable pre-NR
        and does **not** set any auto-apply flag — the operator opts in later, per room."""
        if result_path:
            self.placement.result_path = str(result_path)
        self.placement.last_status = str(getattr(result, "status", ""))
        score = getattr(result, "score", None)
        self.placement.last_score = None if score is None else int(score)
        self.placement.detected_tones_hz = [float(t) for t in getattr(result, "detected_tones_hz", ())]
        self.placement.notch_suggestions_hz = [float(t) for t in getattr(result, "notch_suggestions_hz", ())]
        hpf = getattr(result, "hpf_suggestion_hz", None)
        self.placement.hpf_suggestion_hz = None if hpf is None else float(hpf)
        self.placement.auto_apply_suggestions = False
        # copy into the pre-NR draft (still OFF; never forced on)
        self.pre_nr_cleanup.notches_hz = list(self.placement.notch_suggestions_hz)
        if self.placement.hpf_suggestion_hz is not None:
            self.pre_nr_cleanup.hpf_hz = float(self.placement.hpf_suggestion_hz)
        return self

    # ---- validation (non-throwing; returns warnings) ----
    def validate(self, *, expected_device: Optional[str] = None,
                 expected_rate: Optional[float] = None,
                 expected_channels: Optional[int] = None) -> List[str]:
        """Return a list of human-readable warnings (empty == clean). Never raises and never applies
        anything — a preview/safety check only."""
        w: List[str] = []
        if int(self.version) != PROFILE_VERSION:
            w.append(f"profile version {self.version} (expected {PROFILE_VERSION}) — load with care")
        if expected_device and self.device != expected_device:
            w.append(f"profile device {self.device!r} != current device {expected_device!r}")
        if expected_rate and abs(float(self.sample_rate) - float(expected_rate)) > 1.0:
            w.append(f"profile sampleRate {self.sample_rate} != current rate {expected_rate}")
        if expected_channels and int(self.channels) != int(expected_channels):
            w.append(f"profile channels {self.channels} != current channels {expected_channels}")
        if self.calibration.profile_path and not os.path.exists(self.calibration.profile_path):
            w.append(f"calibration profile file not found: {self.calibration.profile_path}")
        if self.placement.result_path and not os.path.exists(self.placement.result_path):
            w.append(f"placement result file not found: {self.placement.result_path}")
        if self.placement.auto_apply_suggestions:
            w.append("placement.autoApplySuggestions is True — suggestions must be opt-in, not auto-applied")
        for name, val, msg in (
            ("dfn3ForcedOn", self.safety.dfn3_forced_on, "DFN3 forced on"),
            ("dereverbForcedOn", self.safety.dereverb_forced_on, "dereverb forced on"),
            ("placementSuggestionsAutoApplied", self.safety.placement_suggestions_auto_applied,
             "placement suggestions auto-applied"),
            ("realAsrNetworkCall", self.safety.real_asr_network_call, "real ASR network call"),
            ("virtualMicDriverBundled", self.safety.virtual_mic_driver_bundled, "virtual mic driver bundled"),
        ):
            if val:
                w.append(f"unsafe flag {name} is True ({msg}) — a safe profile leaves this False")
        return w

    # ---- JSON ----
    def to_dict(self) -> dict:
        c, pl, pn = self.calibration, self.placement, self.pre_nr_cleanup
        eg, tr, sf = self.egress, self.transcription, self.safety
        return {
            "version": int(self.version), "name": str(self.name), "device": str(self.device),
            "sampleRate": float(self.sample_rate), "channels": int(self.channels),
            "createdAt": str(self.created_at), "updatedAt": str(self.updated_at), "notes": str(self.notes),
            "calibration": {"enabled": bool(c.enabled), "profilePath": str(c.profile_path),
                            "summary": str(c.summary)},
            "placement": {
                "resultPath": str(pl.result_path), "lastStatus": str(pl.last_status),
                "lastScore": (None if pl.last_score is None else int(pl.last_score)),
                "detectedTonesHz": [float(x) for x in pl.detected_tones_hz],
                "notchSuggestionsHz": [float(x) for x in pl.notch_suggestions_hz],
                "hpfSuggestionHz": (None if pl.hpf_suggestion_hz is None else float(pl.hpf_suggestion_hz)),
                "autoApplySuggestions": bool(pl.auto_apply_suggestions)},
            "preNrCleanup": {"enabled": bool(pn.enabled),
                             "hpfHz": (None if pn.hpf_hz is None else float(pn.hpf_hz)),
                             "notchesHz": [float(x) for x in pn.notches_hz]},
            "egress": {"cleanMono48k": bool(eg.clean_mono_48k), "asr16k": bool(eg.asr_16k),
                       "wavRecording": bool(eg.wav_recording), "externalSink": bool(eg.external_sink)},
            "transcription": {"enabled": bool(tr.enabled), "provider": str(tr.provider),
                              "sampleRate": int(tr.sample_rate), "vadEnabled": bool(tr.vad_enabled)},
            "safety": {"dfn3ForcedOn": bool(sf.dfn3_forced_on),
                       "dereverbForcedOn": bool(sf.dereverb_forced_on),
                       "placementSuggestionsAutoApplied": bool(sf.placement_suggestions_auto_applied),
                       "realAsrNetworkCall": bool(sf.real_asr_network_call),
                       "virtualMicDriverBundled": bool(sf.virtual_mic_driver_bundled)},
        }

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> "AudioRoomProfile":
        if not isinstance(d, Mapping):
            raise RoomProfileError(f"room profile must be a JSON object, got {type(d).__name__}")
        try:
            c = dict(d.get("calibration", {}) or {})
            pl = dict(d.get("placement", {}) or {})
            pn = dict(d.get("preNrCleanup", {}) or {})
            eg = dict(d.get("egress", {}) or {})
            tr = dict(d.get("transcription", {}) or {})
            sf = dict(d.get("safety", {}) or {})
            ls = pl.get("lastScore", None)
            hp = pl.get("hpfSuggestionHz", None)
            pnh = pn.get("hpfHz", None)
            return cls(
                version=int(d.get("version", PROFILE_VERSION)), name=str(d.get("name", "")),
                device=str(d.get("device", DEFAULT_DEVICE)),
                sample_rate=float(d.get("sampleRate", DEFAULT_SAMPLE_RATE)),
                channels=int(d.get("channels", DEFAULT_CHANNELS)),
                created_at=str(d.get("createdAt", "")), updated_at=str(d.get("updatedAt", "")),
                notes=str(d.get("notes", "")),
                calibration=RoomCalibrationRef(
                    enabled=bool(c.get("enabled", False)), profile_path=str(c.get("profilePath", "")),
                    summary=str(c.get("summary", ""))),
                placement=RoomPlacementRef(
                    result_path=str(pl.get("resultPath", "")), last_status=str(pl.get("lastStatus", "")),
                    last_score=(None if ls is None else int(ls)),
                    detected_tones_hz=[float(x) for x in pl.get("detectedTonesHz", [])],
                    notch_suggestions_hz=[float(x) for x in pl.get("notchSuggestionsHz", [])],
                    hpf_suggestion_hz=(None if hp is None else float(hp)),
                    auto_apply_suggestions=bool(pl.get("autoApplySuggestions", False))),
                pre_nr_cleanup=RoomPreNr(
                    enabled=bool(pn.get("enabled", False)),
                    hpf_hz=(None if pnh is None else float(pnh)),
                    notches_hz=[float(x) for x in pn.get("notchesHz", [])]),
                egress=RoomEgress(
                    clean_mono_48k=bool(eg.get("cleanMono48k", True)), asr_16k=bool(eg.get("asr16k", False)),
                    wav_recording=bool(eg.get("wavRecording", False)),
                    external_sink=bool(eg.get("externalSink", False))),
                transcription=RoomTranscription(
                    enabled=bool(tr.get("enabled", False)), provider=str(tr.get("provider", "mock")),
                    sample_rate=int(tr.get("sampleRate", 16000)), vad_enabled=bool(tr.get("vadEnabled", True))),
                safety=RoomSafety(
                    dfn3_forced_on=bool(sf.get("dfn3ForcedOn", False)),
                    dereverb_forced_on=bool(sf.get("dereverbForcedOn", False)),
                    placement_suggestions_auto_applied=bool(sf.get("placementSuggestionsAutoApplied", False)),
                    real_asr_network_call=bool(sf.get("realAsrNetworkCall", False)),
                    virtual_mic_driver_bundled=bool(sf.get("virtualMicDriverBundled", False))),
            )
        except (TypeError, ValueError) as exc:
            raise RoomProfileError(f"malformed room profile: {exc}") from exc

    @classmethod
    def from_json(cls, text: str) -> "AudioRoomProfile":
        try:
            d = json.loads(text)
        except (json.JSONDecodeError, TypeError) as exc:
            raise RoomProfileError(f"invalid room profile JSON: {exc}") from exc
        return cls.from_dict(d)

    def to_json(self, *, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent)

    @classmethod
    def load(cls, path: Any) -> "AudioRoomProfile":
        try:
            with open(path, "r", encoding="utf-8") as fh:
                text = fh.read()
        except OSError as exc:
            raise RoomProfileError(f"cannot read room profile {path!r}: {exc}") from exc
        return cls.from_json(text)

    def save(self, path: Any) -> None:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(self.to_json())
