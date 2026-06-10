"""Audio-device discovery for the live runtime (import-guarded).

numpy and sounddevice are **optional** (the ``[control]`` extra). Nothing here
imports them at module load; the guards let the rest of the app import this file
and ask :func:`controls_available` without the extra installed.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


def controls_available() -> bool:
    """True if both numpy and sounddevice can be imported."""
    try:
        import numpy  # noqa: F401
        import sounddevice  # noqa: F401
    except Exception:
        return False
    return True


def missing_dependencies() -> list[str]:
    """Which of the live-audio deps are absent (for a helpful message)."""
    missing = []
    for name in ("numpy", "sounddevice"):
        try:
            __import__(name)
        except Exception:
            missing.append(name)
    return missing


@dataclass(frozen=True)
class InputDevice:
    index: int
    name: str
    max_input_channels: int
    default_samplerate: float


def list_input_devices() -> list[InputDevice]:
    """Enumerate audio input devices. Empty list if the extra isn't installed."""
    try:
        import sounddevice as sd
    except Exception:
        return []
    out: list[InputDevice] = []
    try:
        for i, d in enumerate(sd.query_devices()):
            if int(d.get("max_input_channels", 0)) > 0:
                out.append(
                    InputDevice(
                        index=i,
                        name=str(d.get("name", f"device {i}")),
                        max_input_channels=int(d["max_input_channels"]),
                        default_samplerate=float(d.get("default_samplerate", 48000.0)),
                    )
                )
    except Exception:
        return []
    return out


def find_device_by_name(substr: str) -> Optional[InputDevice]:
    """First input device whose name contains ``substr`` (case-insensitive)."""
    s = substr.lower()
    for d in list_input_devices():
        if s in d.name.lower():
            return d
    return None


@dataclass(frozen=True)
class OutputDevice:
    index: int
    name: str
    max_output_channels: int
    default_samplerate: float


def list_output_devices() -> list[OutputDevice]:
    """Enumerate audio output devices (for live monitoring). Empty if no extra."""
    try:
        import sounddevice as sd
    except Exception:
        return []
    out: list[OutputDevice] = []
    try:
        for i, d in enumerate(sd.query_devices()):
            if int(d.get("max_output_channels", 0)) > 0:
                out.append(
                    OutputDevice(
                        index=i,
                        name=str(d.get("name", f"device {i}")),
                        max_output_channels=int(d["max_output_channels"]),
                        default_samplerate=float(d.get("default_samplerate", 48000.0)),
                    )
                )
    except Exception:
        return []
    return out
