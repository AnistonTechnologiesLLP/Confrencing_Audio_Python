"""Room-wide **capture everyone** — combine N POLARIS arrays into ONE multi-talker capture.

Where `MultiKitController` *switches* between kits (one active talker) and `MultiBeamController` captures
everyone on ONE array, this runs N `MultiBeamController`s (one per kit/array) and **sums** their feeds
into a single room feed — so several arrays cover a whole room (more seats/talkers) at once, plus a
per-person track for every talker in the room.

Two hard rules from the dual-kit work shape it:

- **Independent USB clocks** → no inter-kit sample alignment is possible → the cross-kit combine is
  strictly **volume-domain** (`nom_automix` of the per-kit mono feeds — like the 2-kit cross-fade but
  *summing*, with number-of-open-mics attenuation so kits don't stack their noise floors).
- **Seat ownership** → each room seat is handled by its **nearest** array (`seats_owned_by_array`), and
  each kit's `MultiBeamController` is restricted to its owned seats — so the *same* talker is captured by
  ONE kit (best SNR) and never summed twice (which would comb-filter). Ownership needs snap-to-seats ON,
  every array posed, and room seats defined; otherwise the combine is **best-effort** (an overlapping
  talker may be double-captured) and says so via status.

Mirrors `MultiKitController`'s realtime invariants (copy-on-write tap, watchdog, ONE combined AGC + master
mute/gain, one output stream, no-throw callback, distinct-device guard, N kits) and is stub-injectable
(`kit_factory`/`output_stream_factory`/`time_fn`) so the combine is fully hardware-free testable.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, List, Optional, Sequence

from conf_pipeline.seat_mapper import seats_owned_by_array

from .agc import (
    DEFAULT_AGC_MAX_GAIN_DB,
    DEFAULT_AGC_SILENCE_DB,
    DEFAULT_AGC_SLEW_ALPHA,
    TargetLoudnessAgc,
)
from .multibeam import DEFAULT_N_BEAMS, nom_automix
from .multikit import DEFAULT_WATCHDOG_BLOCKS, SpeechPresenceScorer

_RESERVED_KIT_CFG = ("agc_target_db", "output_callback", "device", "sample_rate", "blocksize",
                     "radius_m", "n_beams", "snap", "owned_seats")


@dataclass
class RoomKitSpec:
    """One array's binding in a multi-array room: its OS input device + modeled array (id/position/seats
    from the config) + per-kit capture-everyone config."""

    device: Optional[int]
    array_id: Optional[str] = None
    radius_m: float = 0.04
    cfg: dict = field(default_factory=dict)


@dataclass(frozen=True)
class RoomKitStatus:
    """One kit's published state for the GUI: which array, its level/health, the seats it owns, and the
    per-beam 'who's talking' detail from its `MultiBeamController`."""

    index: int
    array_id: Optional[str]
    level: float
    dead: bool
    error: Optional[str]
    owned_seats: List[str]
    beams: List[Any]                                          # list[BeamStatus]


def _default_kit_factory(config: object, spec: RoomKitSpec, owned_seats: Optional[Sequence[str]],
                         tap: Callable[[Any], None], ctrl: "MultiRoomController") -> Any:
    """Build a real `MultiBeamController` for a kit: no per-kit AGC (the room owns the ONE AGC), restricted
    to its owned seats, feeding the room combiner via its output_callback."""
    from .multibeam import MultiBeamController

    cfg = {k: v for k, v in dict(spec.cfg).items() if k not in _RESERVED_KIT_CFG}
    return MultiBeamController(
        config, spec.array_id, device=spec.device, radius_m=spec.radius_m, sample_rate=ctrl.sample_rate,
        block_ms=ctrl._block_ms, n_beams=ctrl._n_beams, snap=ctrl._snap, owned_seats=owned_seats,
        agc_target_db=None, output_callback=tap, **cfg)


def _default_room_output_stream_factory(ctrl: "MultiRoomController") -> Any:
    import sounddevice as sd

    return sd.OutputStream(samplerate=ctrl.sample_rate, channels=1, blocksize=ctrl.blocksize,
                           device=ctrl.output_device, dtype="float32", callback=ctrl._cb_output)


class MultiRoomController:
    """Run N kits (each a `MultiBeamController`) and NOM-automix their feeds into one room-wide capture."""

    def __init__(self, config: object, kits: Sequence[RoomKitSpec], *, output_device: Optional[int] = None,
                 sample_rate: float = 44100.0, block_ms: float = 32.0, blocksize: Optional[int] = None,
                 n_beams: int = DEFAULT_N_BEAMS, snap: bool = True, plan_hz: float = 8.0,
                 agc_target_db: Optional[float] = None, agc_max_gain_db: float = DEFAULT_AGC_MAX_GAIN_DB,
                 agc_slew_alpha: float = DEFAULT_AGC_SLEW_ALPHA, agc_silence_db: float = DEFAULT_AGC_SILENCE_DB,
                 own_seats: bool = True, watchdog_blocks: int = DEFAULT_WATCHDOG_BLOCKS,
                 kit_factory: Optional[Callable[..., Any]] = None,
                 output_stream_factory: Optional[Callable[..., Any]] = None,
                 time_fn: Optional[Callable[[], float]] = None):
        self.kits = list(kits)
        n = len(self.kits)
        if n < 1:
            raise ValueError("need at least one kit")
        devs = [k.device for k in self.kits if k.device is not None]
        if len(devs) != len(set(devs)):                       # distinct device per kit (Invariant F)
            raise ValueError("each kit needs a DISTINCT input device (N POLARIS = N devices)")
        self._config = config
        self.sample_rate = float(sample_rate)
        self._block_ms = float(block_ms)
        self.blocksize = int(blocksize) if blocksize else max(1, int(round(self.sample_rate * block_ms / 1000.0)))
        self._hop_s = self.blocksize / self.sample_rate
        self._watchdog_s = float(watchdog_blocks) * self._hop_s
        self._n_beams = int(n_beams)
        self._snap = bool(snap)
        self.output_device = output_device
        self._time = time_fn or time.monotonic
        self._kit_factory = kit_factory or _default_kit_factory
        self._output_stream_factory = output_stream_factory or _default_room_output_stream_factory
        self._agc: Optional[TargetLoudnessAgc] = (
            TargetLoudnessAgc(target_db=agc_target_db, max_gain_db=agc_max_gain_db,
                              slew_alpha=agc_slew_alpha, silence_db=agc_silence_db)
            if agc_target_db is not None else None)
        # seat ownership: a kit captures only the seats nearest its array (None = unrestricted/best-effort)
        self._owned: List[Optional[List[str]]] = [
            (seats_owned_by_array(config, s.array_id) if (own_seats and snap and s.array_id) else None)  # type: ignore[arg-type]
            for s in self.kits]
        self._scorers = [SpeechPresenceScorer(hop_seconds=self._hop_s) for _ in range(n)]
        self._lock = threading.Lock()
        self._stores: List[Optional[Any]] = [None] * n
        self._scores = [0.0] * n
        self._levels = [0.0] * n
        self._last_emit: List[Optional[float]] = [None] * n
        self._dead = [False] * n
        self._errors: List[Optional[str]] = [None] * n
        self._kit_mute = [False] * n
        self._kit_gain_db = [0.0] * n
        self._engines: List[Any] = [None] * n
        self._recorders: List[Any] = [None] * n
        self._room_rec: List[Any] = []                        # room-mixed buffer while recording
        self._recording = False
        self._muted = False
        self._gain_db = 0.0
        self._level = 0.0
        self._streaming = False
        self.error: Optional[str] = None
        self._np: Any = None
        self._out_stream: Any = None

    # ---- introspection ----
    @property
    def n_kits(self) -> int:
        return len(self.kits)

    @property
    def streaming(self) -> bool:
        return self._streaming

    def read_level(self) -> float:
        with self._lock:
            return self._level

    def owned_seats(self, kit: int) -> List[str]:
        o = self._owned[kit]
        return list(o) if o is not None else []

    def status(self) -> List[RoomKitStatus]:
        with self._lock:
            levels, dead, errs = list(self._levels), list(self._dead), list(self._errors)
        out: List[RoomKitStatus] = []
        for k in range(self.n_kits):
            eng = self._engines[k]
            beams = eng.status() if eng is not None else []
            out.append(RoomKitStatus(index=k, array_id=self.kits[k].array_id, level=levels[k],
                                     dead=dead[k], error=errs[k], owned_seats=self.owned_seats(k), beams=beams))
        return out

    # ---- master + per-kit mute/gain (duck-typed for the GUI transport) ----
    def set_mute(self, muted: bool, *, kit: Optional[int] = None) -> None:
        with self._lock:
            if kit is None:
                self._muted = bool(muted)
            else:
                self._kit_mute[kit] = bool(muted)

    def set_gain_db(self, gain_db: float, *, kit: Optional[int] = None) -> None:
        g = float(gain_db)
        with self._lock:
            if kit is None:
                self._gain_db = g
            else:
                self._kit_gain_db[kit] = g

    @property
    def muted(self) -> bool:
        return self._muted

    @property
    def gain_db(self) -> float:
        return self._gain_db

    # ---- audio: per-kit tap (Invariant C) + room combiner ----
    def _lazy_np(self) -> Any:
        if self._np is None:
            import numpy as np
            self._np = np
        return self._np

    def _silence(self) -> Any:
        np = self._lazy_np()
        return np.zeros(self.blocksize, dtype=np.float32)

    def _make_tap(self, k: int) -> Callable[[Any], None]:
        def _tap(mono: Any) -> None:
            self._on_kit_feed(k, mono)
        return _tap

    def _on_kit_feed(self, k: int, mono: Any) -> None:
        """Kit k's combined (already gated + automixed) feed, on its own audio thread. Copy, score (so a
        silent kit doesn't inflate the room NOM count), and store the latest."""
        np = self._lazy_np()
        blk = np.asarray(mono, dtype=np.float32).copy()       # Invariant C: copy on write
        rms = float(np.sqrt(np.mean(blk * blk))) if blk.size else 0.0
        score = self._scorers[k].update(rms)
        now = self._time()
        with self._lock:
            self._stores[k] = blk
            self._scores[k] = score
            self._levels[k] = rms
            self._last_emit[k] = now
            self._dead[k] = False
            self._errors[k] = None

    def _produce(self, t: float) -> Any:
        """Build one room block: watchdog → NOM-automix the live kit feeds (volume-domain) → ONE room AGC
        → master mute/gain. The testable core (no streams, no threads)."""
        np = self._lazy_np()
        n = self.n_kits
        with self._lock:
            stores = list(self._stores)
            scores = list(self._scores)
            last_emit = list(self._last_emit)
            dead_in = list(self._dead)
            kit_mute = list(self._kit_mute)
            kit_gain = list(self._kit_gain_db)
            muted, gain_db, recording = self._muted, self._gain_db, self._recording
        dead, stalled = [], []
        for k in range(n):
            le = last_emit[k]
            is_stale = le is not None and (t - le) > self._watchdog_s
            dead.append(dead_in[k] or is_stale)
            stalled.append(is_stale)
        monos, gates = [], []
        for k in range(n):
            s = stores[k]
            if s is None or dead[k] or kit_mute[k]:
                monos.append(self._silence())
                gates.append(0.0)
            else:
                g = 10.0 ** (kit_gain[k] / 20.0)
                monos.append(s * g if g != 1.0 else s)
                gates.append(scores[k])                       # NOM weight: a silent kit (~0) doesn't attenuate
        mono = nom_automix(gates, monos)                      # volume-domain combine (independent clocks)
        if self._agc is not None:
            mono = self._agc.process(mono)                    # the ONE room AGC (Invariant B)
        if muted:
            mono = self._silence()
        elif gain_db != 0.0:
            mono = mono * (10.0 ** (gain_db / 20.0))
        mono = np.asarray(mono, dtype=np.float32)             # fresh array
        out_rms = float(np.sqrt(np.mean(mono * mono))) if mono.size else 0.0
        with self._lock:
            self._level = out_rms
            for k in range(n):
                if stalled[k] and not self._dead[k]:
                    self._dead[k] = True
                    if self._errors[k] is None:
                        self._errors[k] = "kit stalled (no audio)"
            if recording:
                self._room_rec.append(mono.copy())            # bounded buffer; no file I/O on the audio thread
        return mono

    def _cb_output(self, outdata: Any, frames: int, time_info: Any, status: Any) -> None:  # pragma: no cover (needs hardware)
        try:
            mono = self._produce(self._time())
            if mono is not None and mono.shape[0] == outdata.shape[0]:
                outdata[:] = mono[:, None]
            else:
                outdata.fill(0.0)
        except Exception as exc:                              # never propagate out of a PortAudio callback
            self.error = f"output callback: {exc}"
            try:
                outdata.fill(0.0)
            except Exception:
                pass

    # ---- per-person recording (one MultiTrackRecorder per kit + the room feed) ----
    def record_tracks(self, on: bool) -> None:
        """Arm/disarm per-person recording: one `MultiTrackRecorder` per kit (fed by that kit's own
        clock-correct audio thread via its set_recorder hook) + the combined room feed."""
        from .multibeam import MultiTrackRecorder

        if on:
            for k, eng in enumerate(self._engines):
                rec = MultiTrackRecorder(self._n_beams, self.sample_rate)
                rec.start()
                self._recorders[k] = rec
                if eng is not None:
                    eng.set_recorder(rec)
            with self._lock:
                self._room_rec = []
                self._recording = True
        else:
            with self._lock:
                self._recording = False
            for k, eng in enumerate(self._engines):
                if eng is not None:
                    eng.set_recorder(None)

    def write_tracks(self, out_dir: str, *, prefix: str = "room") -> List[str]:
        """Write each kit's per-person tracks (namespaced by array id, so a seat never collides) + the
        combined room feed. Returns the written paths."""
        import os
        import wave

        np = self._lazy_np()
        written: List[str] = []
        for k, rec in enumerate(self._recorders):
            if rec is None:
                continue
            rec.stop()
            aid = self.kits[k].array_id or f"kit{k + 1}"
            written.extend(rec.write(out_dir, prefix=f"{prefix}_{aid}"))
        with self._lock:
            buf = list(self._room_rec)
            self._room_rec = []
        if buf:
            os.makedirs(out_dir, exist_ok=True)
            path = os.path.join(out_dir, f"{prefix}_mixed.wav")
            x = np.clip(np.concatenate(buf), -1.0, 1.0)
            with wave.open(path, "wb") as w:
                w.setnchannels(1)
                w.setsampwidth(2)
                w.setframerate(int(round(self.sample_rate)))
                w.writeframes((x * 32767.0).astype("<i2").tobytes())
            written.append(path)
        self._recorders = [None] * self.n_kits
        return written

    # ---- lifecycle ----
    def start(self) -> None:
        """Bring up each kit (independent — one failing leaves the others running) then the room output stream."""
        started = 0
        for k, spec in enumerate(self.kits):
            try:
                eng = self._kit_factory(self._config, spec, self._owned[k], self._make_tap(k), self)
                self._engines[k] = eng
                eng.start()
                started += 1
            except Exception as exc:
                with self._lock:
                    self._errors[k] = f"kit {k} failed to start: {exc}"
                    self._dead[k] = True
                self._engines[k] = None
        if started == 0:
            raise RuntimeError("no kit could start: " + "; ".join(e for e in self._errors if e))
        try:
            self._out_stream = self._output_stream_factory(self)
            starter = getattr(self._out_stream, "start", None)
            if callable(starter):
                starter()
        except Exception as exc:
            self.error = f"output stream failed: {exc}"
            self.stop()
            raise
        self._streaming = True

    def stop(self) -> None:
        self._streaming = False
        if self._out_stream is not None:
            for meth in ("stop", "close"):
                fn = getattr(self._out_stream, meth, None)
                if callable(fn):
                    try:
                        fn()
                    except Exception:
                        pass
            self._out_stream = None
        for k, eng in enumerate(self._engines):
            if eng is not None:
                try:
                    eng.stop()
                except Exception:
                    pass
                self._engines[k] = None

    def __enter__(self) -> "MultiRoomController":
        self.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.stop()
