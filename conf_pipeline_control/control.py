"""Controller abstraction for driving a real (or simulated) array microphone.

A :class:`MicController` is the uniform handle the GUI talks to: connect, mute,
set gain, read the live level, and load a :class:`~conf_pipeline_control.beamformer.BeamDesign`
(the steered/zoned pickup). The base class owns the mute/gain/connection
bookkeeping; backends implement only opening/closing the device and producing a
raw level.

:class:`SimulatedMicController` is a pure-stdlib backend (no hardware, no numpy)
so the whole control UI is exercisable without an array plugged in. The real
:class:`~conf_pipeline_control.live.LiveBeamController` lives in :mod:`live`
behind the ``[control]`` extra.
"""
from __future__ import annotations

import math
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional

from .beamformer import BeamDesign
from .geometry import ArrayGeometry

GAIN_MIN_DB = -60.0
GAIN_MAX_DB = 24.0


@dataclass(frozen=True)
class MicState:
    """Immutable snapshot of a controller, for the GUI to render."""

    connected: bool
    muted: bool
    gain_db: float
    level: float          # 0..1 post-gain, post-mute (what a meter shows)
    n_channels: int
    active_channels: int  # capsules in use (≤ n_channels)
    backend: str
    design_zones: int     # number of active pickup beams loaded (0 = none)


def _clamp(v: float, lo: float, hi: float) -> float:
    return lo if v < lo else hi if v > hi else v


class MicController(ABC):
    """Base controller. Subclass and implement :meth:`_open` / :meth:`_close` /
    :meth:`_raw_level`."""

    backend = "base"

    def __init__(self, geometry: Optional[ArrayGeometry] = None, *, n_channels: Optional[int] = None):
        self.geometry = geometry
        self.n_channels = n_channels if n_channels is not None else (geometry.n_channels if geometry else 1)
        self._connected = False
        self._muted = False
        self._gain_db = 0.0
        self._design: Optional[BeamDesign] = None

    # ---- state ----
    @property
    def connected(self) -> bool:
        return self._connected

    @property
    def muted(self) -> bool:
        return self._muted

    @property
    def gain_db(self) -> float:
        return self._gain_db

    @property
    def design(self) -> Optional[BeamDesign]:
        return self._design

    def state(self) -> MicState:
        return MicState(
            connected=self._connected,
            muted=self._muted,
            gain_db=self._gain_db,
            level=self.read_level(),
            n_channels=self.n_channels,
            active_channels=self.geometry.n_active if self.geometry is not None else self.n_channels,
            backend=self.backend,
            design_zones=len(self._design.beams) if self._design else 0,
        )

    # ---- lifecycle ----
    def connect(self) -> None:
        if not self._connected:
            self._open()
            self._connected = True

    def disconnect(self) -> None:
        if self._connected:
            self._close()
            self._connected = False

    def __enter__(self) -> "MicController":
        self.connect()
        return self

    def __exit__(self, *exc) -> None:
        self.disconnect()

    # ---- controls ----
    def set_mute(self, muted: bool) -> None:
        self._muted = bool(muted)
        self._on_mute(self._muted)

    def toggle_mute(self) -> bool:
        self.set_mute(not self._muted)
        return self._muted

    def set_gain_db(self, gain_db: float) -> None:
        self._gain_db = _clamp(float(gain_db), GAIN_MIN_DB, GAIN_MAX_DB)
        self._on_gain(self._gain_db)

    def apply_design(self, design: BeamDesign) -> None:
        """Load a beam design (steered/zoned pickup) into the controller."""
        if self.geometry is not None and design.geometry.n_channels != self.geometry.n_channels:
            raise ValueError(
                f"design is for {design.geometry.n_channels} capsules but controller has {self.geometry.n_channels}"
            )
        self._design = design
        self._on_design(design)

    # ---- metering ----
    def read_level(self) -> float:
        """Current output level 0..1 (post-gain, zeroed when muted)."""
        if self._muted or not self._connected:
            return 0.0
        lin = self._raw_level() * (10.0 ** (self._gain_db / 20.0))
        return _clamp(lin, 0.0, 1.0)

    # ---- backend hooks ----
    @abstractmethod
    def _open(self) -> None: ...

    @abstractmethod
    def _close(self) -> None: ...

    @abstractmethod
    def _raw_level(self) -> float:
        """Pre-gain RMS-ish level in 0..1 while connected."""

    def _on_mute(self, muted: bool) -> None:  # optional override
        pass

    def _on_gain(self, gain_db: float) -> None:  # optional override
        pass

    def _on_design(self, design: BeamDesign) -> None:  # optional override
        pass


class SimulatedMicController(MicController):
    """Hardware-free backend. Produces a smooth, **deterministic** level so the
    UI and tests run with no array attached. Each :meth:`read_level` advances an
    internal phase; a loaded design nudges the apparent level up slightly (more
    focused pickup), mirroring what real steering does."""

    backend = "simulated"

    def __init__(self, geometry: Optional[ArrayGeometry] = None, *, n_channels: Optional[int] = None, period: int = 24):
        super().__init__(geometry, n_channels=n_channels)
        self._t = 0
        self._period = max(2, period)

    def _open(self) -> None:
        self._t = 0

    def _close(self) -> None:
        pass

    def _raw_level(self) -> float:
        # Triangle-ish speech envelope, deterministic in self._t. Levels are
        # chosen to land mid-scale on the GUI's dB meter (≈ −24…−14 dB).
        phase = (self._t % self._period) / self._period
        env = 1.0 - abs(2.0 * phase - 1.0)           # 0→1→0
        base = 0.06 + 0.10 * env
        if self._design and self._design.beams:
            base = min(1.0, base + 0.03)
        self._t += 1
        return base
