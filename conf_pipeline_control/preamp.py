"""Mic-input preamp — a uniform front-end gain on the raw multi-channel block, BEFORE the beamformer.

A manual dB gain on the captured voice, applied identically to every capsule at the very front of the
capture path. This is the SHARED core used by every live path; numpy is imported lazily (like
:mod:`agc`) so constructing one needs no runtime and the package root stays heavy-dep-free.

**Spatial neutrality.** The gain is the *same* scalar on all capsules, so it scales the array
covariance by ``g²`` and leaves DOA and beam weights unchanged (the data-adaptive loading is
trace-relative). Guarded by ``tests/test_preamp_spatial_neutrality.py``.

**SNR honesty.** A software multiply happens *after* the ADC, so it scales signal and noise together
(SNR-neutral) and the output :class:`~conf_pipeline_control.agc.TargetLoudnessAgc` divides it back out.
The manual software trim is for operator level / input metering — it does **not** improve SNR. Only a
true analog (pre-ADC) hardware gain does; that backend attaches via :class:`HwGain` (analog track).

The auto headroom stager and the hardware backend are forward-compatible hooks here: ``auto`` is inert
until a stager is injected, so the manual core ships on both tracks without a later signature change.
"""
from __future__ import annotations

import math
from typing import Any, Optional, Protocol, Tuple, runtime_checkable

from .control import GAIN_MAX_DB, GAIN_MIN_DB, _clamp

DEFAULT_PREAMP_GAIN_DB = 0.0   # manual front-end gain; 0 dB = bit-exact no-op (feature off)


def _db_to_lin(db: float) -> float:
    """dB → linear amplitude as a PLAIN Python float.

    Critically not a numpy float64: under NEP-50 a numpy float64 scalar is "strong" and would upcast a
    float32 block to float64 in :meth:`InputPreamp.process_block`, silently doubling memory and
    perturbing the covariance/beamformer."""
    return float(10.0 ** (float(db) / 20.0))


@runtime_checkable
class HwGain(Protocol):
    """A device's hardware (ideally analog, pre-ADC) input-gain control.

    Every method is best-effort and MUST NOT raise — an unavailable or unsupported backend reports
    ``available is False`` and returns ``None`` from the getters. The Windows Core Audio implementation
    lands on the analog track in ``hw_gain.py``; the preamp degrades to pure software when this is
    ``None`` or unavailable."""

    @property
    def available(self) -> bool: ...

    @property
    def status(self) -> str: ...

    def range_db(self) -> Optional[Tuple[float, float, float]]: ...   # (min, max, step) dB

    def get_db(self) -> Optional[float]: ...

    def set_db(self, db: float) -> Optional[float]: ...              # clamp+apply; returns achieved dB

    def close(self) -> None: ...


class InputPreamp:
    """Uniform front-end gain on a raw ``(N, M)`` mic block, applied BEFORE beamforming.

    Manual dB gain now; an auto headroom stager and a hardware backend attach on the analog track.
    Realtime-cheap: a single scalar multiply that allocates a new array (it never mutates the caller's
    buffer — PortAudio's ``indata``). A net unity gain is a **bit-exact no-op** (returns the input array
    unchanged), so an off preamp leaves the existing pipeline byte-identical."""

    def __init__(self, *, gain_db: float = DEFAULT_PREAMP_GAIN_DB, auto: bool = False,
                 hw_gain: Optional[HwGain] = None) -> None:
        self._gain_db: float = _clamp(float(gain_db), GAIN_MIN_DB, GAIN_MAX_DB)
        self._manual_lin: float = _db_to_lin(self._gain_db)
        self._auto: bool = bool(auto)
        self._hw_gain = hw_gain
        # Injected on the analog track (an envelope-follower with ``.update(block) -> float`` /
        # ``.reset()``). Until then ``auto`` is inert — manual gain alone is applied.
        self._auto_stager: Any = None

    @property
    def gain_db(self) -> float:
        """The clamped manual gain in dB."""
        return self._gain_db

    @property
    def auto(self) -> bool:
        return self._auto

    def set_gain_db(self, gain_db: float) -> None:
        """Set the manual front-end gain (dB), clamped to the controller gain range. A single atomic
        float write — the audio thread reads it lock-free, mirroring ``MicController.set_gain_db``."""
        self._gain_db = _clamp(float(gain_db), GAIN_MIN_DB, GAIN_MAX_DB)
        self._manual_lin = _db_to_lin(self._gain_db)

    def set_auto(self, on: bool) -> None:
        """Toggle the auto headroom stager (inert until a stager is wired on the analog track)."""
        self._auto = bool(on)

    def process_block(self, block: Any) -> Any:
        """Apply the uniform input gain to one ``(N, M)`` float32 block.

        Returns a new gained array, or — when the net gain is exactly unity — the input unchanged
        (no copy, no multiply). The scalar is a Python float, so a float32 block stays float32."""
        software_lin = self._manual_lin
        if self._auto and self._auto_stager is not None:
            software_lin *= float(self._auto_stager.update(block))
        if software_lin == 1.0:
            return block                                   # no-op: byte-identical, no allocation
        import numpy as np

        return np.asarray(block, dtype=np.float32) * software_lin

    def reset(self) -> None:
        """Drop any auto-stager streaming state (atomic rebind), mirroring ``TargetLoudnessAgc.reset``."""
        if self._auto_stager is not None:
            self._auto_stager.reset()


class PreampHost:
    """Mixin giving a live backend a mic-input preamp with a one-line front-end insert and
    realtime-safe setters reachable through the GUI's duck-typed ``_active_ctl`` surface.

    A host calls :meth:`_init_preamp` in its ``__init__`` and :meth:`_apply_preamp` at the FRONT of its
    per-block process method (before the beam and the covariance read, so DOA stays consistent with what
    is heard). The mixin defines no ``__init__`` and no state beyond ``_preamp`` (a ``None`` class
    default), so it composes with any backend with no MRO surprises. The analog track extends it with
    the debounced hardware-gain tick on the backend's worker thread."""

    _preamp: Optional[InputPreamp] = None     # class default → safe before _init_preamp / when off

    def _init_preamp(self, *, gain_db: float = DEFAULT_PREAMP_GAIN_DB, auto: bool = False,
                     hw_gain: Optional[HwGain] = None) -> None:
        """Build the preamp only when the feature is actually on, so the default path is a
        zero-overhead no-op and the existing suite stays byte-identical."""
        self._preamp = (InputPreamp(gain_db=gain_db, auto=auto, hw_gain=hw_gain)
                        if (gain_db != 0.0 or auto or hw_gain is not None) else None)

    def _apply_preamp(self, block: Any) -> Any:
        """Uniform front-end gain on the raw block; a no-op when the preamp is off."""
        p = self._preamp
        return p.process_block(block) if p is not None else block

    def set_preamp_gain_db(self, gain_db: float) -> None:
        """Set the manual mic-input gain (dB). Realtime-safe: a single atomic float write, or an atomic
        reference rebind when the preamp is lazily created on first use (the audio thread reads
        ``_preamp`` lock-free via the ``is not None`` guard)."""
        p = self._preamp
        if p is None:
            if float(gain_db) == 0.0:
                return                         # stay off — no allocation for a no-op set
            self._preamp = InputPreamp(gain_db=gain_db)
        else:
            p.set_gain_db(gain_db)

    def set_preamp_auto(self, on: bool) -> None:
        """Toggle the auto headroom stager (inert until the analog track wires a stager)."""
        p = self._preamp
        if p is None:
            if not on:
                return
            self._preamp = InputPreamp(auto=True)
        else:
            p.set_auto(on)
