"""Lobe Control — the operator's beamformer **pickup-pattern** state (Phase 11).

Distinct from capsule calibration (Phase 1, which aligns the 8 raw MEMS channels): a `LobeControl` shapes
the beam *after* calibration — where it listens (main angle / seat), how focused (width preset), which
direction to suppress (nulls), and whether it is fixed or auto-following. It is a **descriptive +
validating** model: it bounds the main angle and the null count, emits an honest summary + warnings, and
round-trips camelCase JSON. It applies no DSP itself and makes **no perfect-fencing promise** — a null
*reduces* pickup from a direction, it does not mute it. Pure stdlib (no numpy / Qt).
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, List, Mapping, Optional

LOBE_VERSION = 1
LOBE_MODES = ("fixed", "follow", "seat", "table")     # fixed angle / follow speaker / lock seat / whole table
LOBE_WIDTHS = ("wide", "medium", "narrow")
DEFAULT_BEAM_MODE = "superdirective"

# Width presets map to EXISTING capability — beam mode + diagonal loading — NOT a continuous beamwidth:
# higher loading = more robust / broader pickup; lower = more directive / narrower (but more self-noise).
_WIDTH_LOADING = {"wide": 0.12, "medium": 0.05, "narrow": 0.02}


class LobeControlError(ValueError):
    """Raised on an invalid lobe configuration (bad mode/width, out-of-range angle, or too many nulls)."""


@dataclass
class LobeNull:
    """A single suppress-direction. ``enabled`` lets the GUI keep a remembered angle without applying it."""

    angle_deg: float = 0.0
    enabled: bool = True
    label: str = ""


@dataclass
class LobeSafety:
    requires_calibration_recommended: bool = True
    max_nulls: int = 2
    warn_if_placement_bad: bool = True


@dataclass
class LobeControl:
    """Operator pickup-pattern state for the POLARIS steered beam."""

    version: int = LOBE_VERSION
    enabled: bool = True
    mode: str = "table"
    main_angle_deg: float = 0.0
    beam_width: str = "medium"
    beam_mode: str = DEFAULT_BEAM_MODE
    target_seat_id: Optional[str] = None
    auto_steer: bool = False
    nulls: List[LobeNull] = field(default_factory=list)
    safety: LobeSafety = field(default_factory=LobeSafety)

    # ---- validation ----
    @staticmethod
    def clamp_angle(deg: float) -> float:
        """Wrap any angle into (−180, 180] — azimuth is periodic, so the GUI clamps input before setting."""
        a = float(deg) % 360.0
        if a > 180.0:
            a -= 360.0
        return a

    def validate(self) -> "LobeControl":
        """Raise :class:`LobeControlError` on an invalid config; return ``self`` when valid."""
        if self.mode not in LOBE_MODES:
            raise LobeControlError(f"mode must be one of {LOBE_MODES}, got {self.mode!r}")
        if self.beam_width not in LOBE_WIDTHS:
            raise LobeControlError(f"beamWidth must be one of {LOBE_WIDTHS}, got {self.beam_width!r}")
        if not (-180.0 <= float(self.main_angle_deg) <= 180.0):
            raise LobeControlError(f"mainAngleDeg must be in [-180, 180], got {self.main_angle_deg!r}")
        for n in self.nulls:
            if not (-180.0 <= float(n.angle_deg) <= 180.0):
                raise LobeControlError(f"null angleDeg must be in [-180, 180], got {n.angle_deg!r}")
        n_enabled = sum(1 for n in self.nulls if n.enabled)
        if n_enabled > int(self.safety.max_nulls):
            raise LobeControlError(
                f"too many enabled nulls ({n_enabled} > maxNulls={self.safety.max_nulls})")
        return self

    def effective_nulls(self) -> List[LobeNull]:
        """The enabled nulls actually applied — capped to ``maxNulls`` so the GUI/engine never exceed it."""
        enabled = [n for n in self.nulls if n.enabled]
        return enabled[: max(0, int(self.safety.max_nulls))]

    # ---- human-readable ----
    def _mode_phrase(self) -> str:
        if self.mode == "fixed":
            return f"fixed {float(self.main_angle_deg):.0f}°"
        if self.mode == "seat":
            return f"seat {self.target_seat_id}" if self.target_seat_id else "seat (none selected)"
        if self.mode == "follow":
            return "follow (auto-steer)"
        return "whole table"

    def summary(self, *, calibration_on: Optional[bool] = None,
                placement_status: Optional[str] = None) -> str:
        """A compact one-line lobe summary, e.g.
        ``Lobe: fixed 35° · width medium · null 180° · calibration ON · placement BAD warning``."""
        parts: List[str] = [f"Lobe: {self._mode_phrase()}", f"width {self.beam_width}"]
        en = self.effective_nulls()
        parts.append("null " + ", ".join(f"{float(n.angle_deg):.0f}°" for n in en) if en else "no null")
        if calibration_on is not None:
            parts.append(f"calibration {'ON' if calibration_on else 'OFF'}")
        if (placement_status or "").upper() == "BAD":
            parts.append("placement BAD warning")
        return " · ".join(parts)

    def warnings(self, *, calibration_on: bool, placement_status: Optional[str] = None) -> List[str]:
        """Honest operator warnings — never blocks the user, never promises a hard mute."""
        w: List[str] = []
        if self.effective_nulls():
            w.append("Suppress direction (null) reduces pickup from that direction but does NOT fully "
                     "mute it — it is a reduced-pickup zone, not a hard-mute zone.")
        if not calibration_on and self.safety.requires_calibration_recommended:
            w.append("Calibration is OFF — lobe direction may be less accurate.")
        if (placement_status or "").upper() == "BAD" and self.safety.warn_if_placement_bad:
            w.append("Placement is BAD — lobe/null control may underperform until physical noise is fixed.")
        return w

    # ---- JSON (camelCase on the wire) ----
    def to_dict(self) -> dict:
        return {
            "version": int(self.version),
            "enabled": bool(self.enabled),
            "mode": str(self.mode),
            "mainAngleDeg": float(self.main_angle_deg),
            "beamWidth": str(self.beam_width),
            "beamMode": str(self.beam_mode),
            "targetSeatId": (None if self.target_seat_id is None else str(self.target_seat_id)),
            "autoSteer": bool(self.auto_steer),
            "nulls": [{"angleDeg": float(n.angle_deg), "enabled": bool(n.enabled), "label": str(n.label)}
                      for n in self.nulls],
            "safety": {
                "requiresCalibrationRecommended": bool(self.safety.requires_calibration_recommended),
                "maxNulls": int(self.safety.max_nulls),
                "warnIfPlacementBad": bool(self.safety.warn_if_placement_bad),
            },
        }

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> "LobeControl":
        sf = dict(d.get("safety", {}) or {})
        seat = d.get("targetSeatId", None)
        return cls(
            version=int(d.get("version", LOBE_VERSION)),
            enabled=bool(d.get("enabled", True)),
            mode=str(d.get("mode", "table")),
            main_angle_deg=float(d.get("mainAngleDeg", 0.0)),
            beam_width=str(d.get("beamWidth", "medium")),
            beam_mode=str(d.get("beamMode", DEFAULT_BEAM_MODE)),
            target_seat_id=(None if seat is None else str(seat)),
            auto_steer=bool(d.get("autoSteer", False)),
            nulls=[LobeNull(angle_deg=float(n.get("angleDeg", 0.0)), enabled=bool(n.get("enabled", True)),
                            label=str(n.get("label", ""))) for n in (d.get("nulls", []) or [])],
            safety=LobeSafety(
                requires_calibration_recommended=bool(sf.get("requiresCalibrationRecommended", True)),
                max_nulls=int(sf.get("maxNulls", 2)),
                warn_if_placement_bad=bool(sf.get("warnIfPlacementBad", True))),
        )

    @classmethod
    def from_json(cls, text: str) -> "LobeControl":
        return cls.from_dict(json.loads(text))

    def to_json(self, *, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent)


def loading_for_width(beam_width: str) -> float:
    """The diagonal-loading value a width preset maps to (wide = robust/broad … narrow = directive). This
    reuses the engine's existing ``loading`` lever; it is NOT a continuous physical beamwidth."""
    return _WIDTH_LOADING.get(str(beam_width), _WIDTH_LOADING["medium"])


def default_lobe_for_mode(mode: str, *, target_seat_id: Optional[str] = None) -> LobeControl:
    """A safe default `LobeControl` for a LIVE listening-mode key (``table``/``follow``/``seat``/``clean``/
    ``manual``/``twokit``/``fixed``). Whole table is wide + no nulls; Follow/Clean auto-steer (medium);
    Lock-to-seat is fixed-to-seat (tolerates a missing seat); Manual stays neutral (the operator's own
    controls are the source of truth); Two-kits is whole-table-wide (combined-room lobe control is limited)."""
    m = str(mode)
    if m in ("follow", "clean"):
        return LobeControl(mode="follow", auto_steer=True, beam_width="medium")
    if m in ("seat", "lock"):
        return LobeControl(mode="seat", auto_steer=False, beam_width="medium", target_seat_id=target_seat_id)
    if m == "fixed":
        return LobeControl(mode="fixed", auto_steer=False, beam_width="medium")
    if m == "twokit":
        return LobeControl(mode="table", auto_steer=False, beam_width="wide")
    # table / manual / unknown → safe whole-table default
    return LobeControl(mode="table", auto_steer=False, beam_width="wide" if m == "table" else "medium")
