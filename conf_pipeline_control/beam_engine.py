"""Unify the two POLARIS beamforming back-ends behind one shared input stream.

`BeamEngine` wraps the steered back-end (:class:`~conf_pipeline_control.polaris_beamformer.PolarisBeamformer`
— SRP-PHAT DOA + one delay-and-sum beam, reports an **angle**) and the grid back-end
(:class:`~conf_pipeline_control.virtual_mic_grid.VirtualMicGrid` — a fixed near-field grid,
loudest selected, reports an **(x, y)**) so the host app can A/B them live on one POLARIS board:
one input stream, runtime ``set_mode("steered"|"grid")``, a single mono output, a normalized
location report, and a glitch-free equal-power crossfade on switch.

**How it shares one device:** both back-ends each normally open their own ``sounddevice``
stream. Here they run in *external-feed* mode instead (``prepare_external()`` sets up their DSP
with **no device**), and BeamEngine owns the single :class:`sd.InputStream`, routing each block
to the active back-end via its ``process_block(block) -> mono`` seam. The device is opened
exactly once; the two back-ends never open it. Both stay prepared for the whole session, so a
mode switch is just a routing flip + a few equal-power-mixed blocks — the realtime callback never
joins a thread or does heavy allocation.

**This is a strategy comparison, not a quality ranking:** adaptive single beam vs. fixed dense
selection. Both share the same 40 mm-array physics — a ~5-6 kHz spatial-aliasing ceiling and
modest small-aperture resolution — so neither is "better"; they trade off differently.

numpy + sounddevice are the ``[control]`` extra, imported lazily.
"""
from __future__ import annotations

import math
import queue
import threading
from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional, Sequence, Tuple

from .audio import controls_available, list_input_devices
from .control import GAIN_MAX_DB, GAIN_MIN_DB, _clamp
from .polaris_beamformer import (
    DEFAULT_BLOCK_MS,
    POLARIS_N_MICS,
    POLARIS_RATE_HZ,
    DeviceConfigError,
    PolarisBeamformer,
    _install_hint,
)
from .virtual_mic_grid import VirtualMicGrid

_MODES = ("steered", "grid")
DEFAULT_CROSSFADE_BLOCKS = 6
_RESERVED_CFG = ("device", "sample_rate", "blocksize", "monitor", "output_callback")
_AUTO = object()   # "not specified" sentinel for the engine-level band-limit toggle


@dataclass(frozen=True)
class Location:
    """Normalized active-source report, identical shape across both back-ends.

    ``angle_deg`` is a compass bearing (0° = +Y, clockwise); ``xy`` is metres in the array
    frame. In steered mode ``angle_deg`` is primary and ``xy`` is derived only if an
    ``assumed_range_m`` was given; in grid mode ``xy`` is primary and ``angle_deg`` is derived
    as ``atan2(x, y)``. ``confidence`` is a rough 0..1 (DOA salience / grid peak margin)."""

    mode: str
    angle_deg: Optional[float]
    xy: Optional[Tuple[float, float]]
    confidence: float


def _clean_cfg(cfg: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Drop keys BeamEngine controls (device / sample_rate / blocksize / monitor /
    output_callback) so per-back-end config can't desync the shared stream."""
    return {k: v for k, v in (cfg or {}).items() if k not in _RESERVED_CFG}


class BeamEngine:
    """Single front-end over the steered + grid POLARIS back-ends; one shared stream."""

    def __init__(
        self,
        *,
        device: Optional[int] = None,
        fs: float = POLARIS_RATE_HZ,
        block_ms: float = DEFAULT_BLOCK_MS,
        blocksize: Optional[int] = None,
        mode: str = "steered",
        steered_cfg: Optional[Dict[str, Any]] = None,
        grid_cfg: Optional[Dict[str, Any]] = None,
        crossfade_blocks: int = DEFAULT_CROSSFADE_BLOCKS,
        output_callback: Optional[Callable[[Any], None]] = None,
        output_queue_size: int = 8,
        assumed_range_m: Optional[float] = None,
        beam_bandlimit_hz: Any = _AUTO,
        monitor: bool = False,
        output_device: Optional[int] = None,
    ):
        if mode not in _MODES:
            raise ValueError(f"unknown mode {mode!r}; expected one of {_MODES}")
        self.device = device
        self.fs = float(fs)
        self.blocksize = int(blocksize) if blocksize else int(round(self.fs * block_ms / 1000.0))
        self.crossfade_blocks = max(1, int(crossfade_blocks))
        self.assumed_range_m = assumed_range_m

        # One band-limit toggle for both back-ends (the plan's "consistent toggle"): when set,
        # override both; an explicit per-back-end cfg key still wins. Omit (the _AUTO default) to
        # let each back-end use its own default (ON at the array aliasing cutoff).
        scfg, gcfg = _clean_cfg(steered_cfg), _clean_cfg(grid_cfg)
        if beam_bandlimit_hz is not _AUTO:
            scfg = {"beam_bandlimit_hz": beam_bandlimit_hz, **scfg}
            gcfg = {"beam_bandlimit_hz": beam_bandlimit_hz, **gcfg}

        # Both back-ends share the SAME device(None)/fs/blocksize so a fed block fits both.
        self._steered = PolarisBeamformer(
            device=None, sample_rate=self.fs, blocksize=self.blocksize, monitor=False, **scfg)
        self._grid = VirtualMicGrid(
            device=None, sample_rate=self.fs, blocksize=self.blocksize, monitor=False, **gcfg)
        self._by_mode: Dict[str, Any] = {"steered": self._steered, "grid": self._grid}

        # Routing state, guarded by _lock (the audio callback reads it; set_mode writes it).
        self._lock = threading.Lock()
        self._mode = mode                       # target mode (what get_mode() reports)
        self._active: Any = self._by_mode[mode]
        self._incoming: Any = None
        self._outgoing: Any = None
        self._fading = False
        self._step = 0

        self._output_q: "queue.Queue[Any]" = queue.Queue(maxsize=max(1, output_queue_size))
        self._output_cb = output_callback
        self._level = 0.0
        self.error = ""
        self._sd: Any = None
        self._stream: Any = None
        # optional live monitor: play the unified mono on a SECOND (output) stream so the operator can
        # HEAR the A/B output. Two independent streams joined by a queue (can't assume a duplex device).
        # Mute/gain trim the MONITOR playback (and the meter) only — the host output_queue stays raw.
        self.monitor = bool(monitor)
        self.output_device = output_device
        self._muted = False
        self._gain_db = 0.0
        self._out_stream: Any = None
        self._monitor_q: Any = None
        self._out_channels = 0

    # ---- lifecycle ----
    def start(self) -> None:
        """Open the shared POLARIS stream once and begin feeding the active back-end."""
        if not controls_available():
            raise RuntimeError(_install_hint())
        self._validate_shared_device()
        # Prepare BOTH back-ends (device-free) and keep them prepared for the whole
        # session — so a mode switch never has to join a thread inside the audio callback.
        self._steered.prepare_external()
        self._grid.prepare_external()
        import sounddevice as sd

        self._sd = sd
        try:
            self._stream = sd.InputStream(
                samplerate=self.fs, channels=POLARIS_N_MICS, blocksize=self.blocksize,
                device=self.device, dtype="float32", callback=self._cb,
            )
            self._stream.start()
            self._open_monitor_stream()         # optional second (output) stream for headphone monitoring
        except Exception:
            self.stop()
            raise

    def stop(self) -> None:
        """Close the shared stream(s) and release both back-ends (idempotent)."""
        if self._out_stream is not None:         # output-first teardown (mirror PolarisBeamformer)
            try:
                self._out_stream.stop()
                self._out_stream.close()
            finally:
                self._out_stream = None
        self._monitor_q = None
        if self._stream is not None:
            try:
                self._stream.stop()
                self._stream.close()
            finally:
                self._stream = None
        self._steered.release_external()
        self._grid.release_external()
        self._level = 0.0

    def __enter__(self) -> "BeamEngine":
        self.start()
        return self

    def __exit__(self, *exc) -> None:
        self.stop()

    # ---- mode control ----
    def set_mode(self, mode: str) -> None:
        """Switch the active back-end with an equal-power crossfade (glitch-free)."""
        if mode not in _MODES:
            raise ValueError(f"unknown mode {mode!r}; expected one of {_MODES}")
        target = self._by_mode[mode]
        with self._lock:
            self._mode = mode
            if target is self._active and not self._fading:
                return                          # already there
            target.reset_transient()            # reset ONLY the newly-activated back-end
            self._outgoing = self._active
            self._incoming = target
            self._step = 0
            self._fading = True

    def get_mode(self) -> str:
        """The current (target) mode — flips immediately on ``set_mode``; the audio
        crossfades to it over the next few blocks."""
        with self._lock:
            return self._mode

    def set_nulls(self, bearings: Optional[Sequence[float]] = None) -> None:
        """Forward explicit interferer / room-aware-seat null bearings (array-relative deg) to the
        steered back-end (applied on its next DOA tick; frequency-domain steered modes only — the grid
        back-end has no nulls). ``None``/``[]`` clears them. See
        :meth:`conf_pipeline_control.polaris_beamformer.PolarisBeamformer.set_nulls`."""
        self._steered.set_nulls(bearings)

    @property
    def active_nulls(self) -> list:
        """The null bearings the steered back-end is **actually** applying this tick (after the M−1
        budget, the look-proximity filter, and the mode constraint — empty for a time-domain steered
        beam). For an honest readout, not the raw requested set."""
        return self._steered.active_nulls

    # ---- location ----
    @property
    def current_location(self) -> Location:
        """Normalized active-source report (the target mode during a crossfade)."""
        with self._lock:
            mode = self._mode
            backend = self._by_mode[mode]
        if mode == "steered":
            r = backend.reading()
            if r.azimuth_deg is None:
                return Location("steered", None, None, 0.0)
            conf = max(0.0, min(1.0, r.salience_db / 12.0))
            xy: Optional[Tuple[float, float]] = None
            if self.assumed_range_m is not None:        # approximate — planar array has no range
                a = math.radians(r.azimuth_deg)
                xy = (self.assumed_range_m * math.sin(a), self.assumed_range_m * math.cos(a))
            return Location("steered", r.azimuth_deg, xy, conf)
        # grid
        xy = backend.selected_xy
        if xy is None:
            return Location("grid", None, None, 0.0)
        ang = math.degrees(math.atan2(xy[0], xy[1])) % 360.0   # 0°=+Y clockwise (see steering.py)
        conf = 0.0
        sc = backend.scores()
        if sc is not None and getattr(sc, "size", 0) >= 2:
            top = float(sc.max())
            second = float(sorted(sc.tolist())[-2])
            conf = max(0.0, min(1.0, (top - second) / (top + 1e-12)))
        return Location("grid", ang, xy, conf)

    # ---- output ----
    @property
    def output_queue(self) -> "queue.Queue[Any]":
        """Drop-oldest queue of the unified mono output (always the active back-end)."""
        return self._output_q

    def read_level(self) -> float:
        """Current unified output level 0..1 — **post gain + mute**, so the meter matches the monitor."""
        if self._muted:
            return 0.0
        return min(1.0, self._level * (10.0 ** (self._gain_db / 20.0)))

    # ---- monitor mute / gain (trim the monitor playback + the meter; the host output_queue stays raw) ----
    @property
    def muted(self) -> bool:
        return self._muted

    @property
    def gain_db(self) -> float:
        return self._gain_db

    def set_mute(self, muted: bool) -> None:
        """Silence the monitor playback (and zero the meter). No effect on the host ``output_queue``."""
        self._muted = bool(muted)

    def set_gain_db(self, gain_db: float) -> None:
        """Trim the monitor playback level (dB, clamped to the standard range). Host queue unaffected."""
        self._gain_db = _clamp(float(gain_db), GAIN_MIN_DB, GAIN_MAX_DB)

    # ---- audio thread ----
    def _mix(self, mono_out: Any, mono_in: Any, step: int) -> Any:
        """Equal-power crossfade of the outgoing/incoming monos at fade ``step``.
        ``cos²+sin²=1`` keeps perceived loudness constant across the switch."""
        p = step / self.crossfade_blocks
        g_out = math.cos(p * math.pi / 2.0)
        g_in = math.sin(p * math.pi / 2.0)
        return g_out * mono_out + g_in * mono_in

    def _cb(self, indata, frames, time_info, status):  # pragma: no cover (needs hardware)
        # Copy the routing tuple out under the lock; run the (heavy) DSP lock-free so a
        # rare set_mode on the host thread never waits a full audio block.
        with self._lock:
            active, outgoing, incoming, fading, step = (
                self._active, self._outgoing, self._incoming, self._fading, self._step)
        if not fading:
            mono = active.process_block(indata)
        else:
            mono = self._mix(outgoing.process_block(indata), incoming.process_block(indata), step)
            step += 1
            with self._lock:
                self._step = step
                if step >= self.crossfade_blocks:    # fade done: flip active (NO teardown here)
                    self._active = incoming
                    self._incoming = None
                    self._outgoing = None
                    self._fading = False
        np = self._steered._np
        mono = mono.astype(np.float32) if np is not None else mono
        rms = float(np.sqrt(np.mean(mono * mono))) if (np is not None and mono.size) else 0.0
        self._level = min(1.0, rms)
        self._queue_put(self._output_q, mono)             # host consumer: raw (ungained) mono
        q = self._monitor_q                               # snapshot: stop() may null it mid-callback (TOCTOU)
        if q is not None:                                 # monitor playback: mute/gain applied here only
            if self._muted:
                mon = np.zeros_like(mono)
            elif self._gain_db != 0.0:
                mon = (mono * (10.0 ** (self._gain_db / 20.0))).astype(np.float32)
            else:
                mon = mono
            self._queue_put(q, mon)
        if self._output_cb is not None:
            try:
                self._output_cb(mono)         # NOTE: realtime audio thread — keep it cheap
            except Exception as exc:
                self.error = f"output_callback raised: {exc}"

    def _open_monitor_stream(self) -> None:
        """Open the monitor OUTPUT stream — a second, independent stream (we can't assume the input and
        output share a duplex device). No-op unless ``monitor``. The mono is fanned to all output
        channels in :meth:`_cb_output`. Use HEADPHONES — monitoring through room speakers feeds back into
        the array and howls."""
        if not self.monitor:
            return
        out_ch = 2
        try:
            info = self._sd.query_devices(self.output_device, "output")
            out_ch = max(1, min(2, int(info.get("max_output_channels", 2))))
        except Exception:
            out_ch = 2
        self._out_channels = out_ch
        self._monitor_q = queue.Queue(maxsize=8)
        self._out_stream = self._sd.OutputStream(
            samplerate=self.fs, channels=self._out_channels, blocksize=self.blocksize,
            device=self.output_device, dtype="float32", callback=self._cb_output,
        )
        self._out_stream.start()

    def _cb_output(self, outdata, frames, time_info, status):  # pragma: no cover (needs hardware)
        q = self._monitor_q
        try:
            blk = q.get_nowait() if q is not None else None
        except queue.Empty:
            blk = None
        if blk is not None and blk.shape[0] == outdata.shape[0]:
            outdata[:] = blk[:, None]         # mono → all output channels
        else:
            outdata.fill(0.0)                 # drop-oldest underflow → silence (no click)

    @staticmethod
    def _queue_put(q: "queue.Queue[Any]", item: Any) -> None:
        try:
            q.put_nowait(item)
        except queue.Full:                    # bound latency: drop oldest, keep newest
            try:
                q.get_nowait()
            except queue.Empty:
                pass
            try:
                q.put_nowait(item)
            except queue.Full:
                pass

    # ---- device validation (duplicated from the back-ends; single shared device) ----
    def _validate_shared_device(self) -> None:
        devs = list_input_devices()
        match = None
        if self.device is not None:
            match = next((d for d in devs if d.index == self.device), None)
            if match is None:
                raise ValueError(
                    f"input device index {self.device} not found; "
                    "run scripts/device_check.py to list devices"
                )
            if match.max_input_channels < POLARIS_N_MICS:
                raise DeviceConfigError(
                    f"device {self.device} ({match.name!r}) exposes {match.max_input_channels} "
                    f"input channels but POLARIS needs {POLARIS_N_MICS}; on Windows pick the "
                    "ASIO/WASAPI entry that surfaces all 8 (see scripts/device_check.py)"
                )
        import sounddevice as sd
        try:
            sd.check_input_settings(
                device=self.device, channels=POLARIS_N_MICS, samplerate=self.fs, dtype="float32")
        except Exception as exc:
            default_sr = match.default_samplerate if match is not None else self.fs
            raise DeviceConfigError(
                f"device {self.device} did not accept {self.fs:.0f} Hz @ {POLARIS_N_MICS}ch "
                f"(its default is {default_sr:.0f} Hz; POLARIS runs at {POLARIS_RATE_HZ:.0f}). "
                f"Original error: {exc}"
            ) from exc


# --------------------------------------------------------------------------- #
# Standalone demo: open POLARIS, start steered, print the live normalized
# location, and toggle mode every N seconds for hands-off A/B on identical audio.
# --------------------------------------------------------------------------- #
def _demo(argv: Optional[Any] = None) -> int:
    import argparse
    import sys
    import time

    ap = argparse.ArgumentParser(
        description="POLARIS BeamEngine A/B: steered vs virtual-mic-grid on one shared stream."
    )
    ap.add_argument("--device", type=int, default=None, help="input device index (omit to list devices)")
    ap.add_argument("--rate", type=float, default=POLARIS_RATE_HZ, help="sample rate (POLARIS=44100)")
    ap.add_argument("--block-ms", type=float, default=DEFAULT_BLOCK_MS, help="audio block size, ms")
    ap.add_argument("--start-mode", choices=_MODES, default="steered", help="initial mode")
    ap.add_argument("--toggle-seconds", type=float, default=6.0, help="auto-toggle mode every N s (0=off)")
    ap.add_argument("--crossfade-blocks", type=int, default=DEFAULT_CROSSFADE_BLOCKS, help="crossfade length")
    ap.add_argument("--no-bandlimit", action="store_true",
                    help="disable the beam band-limit on both back-ends (default: on at ~5.6 kHz)")
    args = ap.parse_args(argv)

    if not controls_available():
        sys.stderr.write(_install_hint() + "\n")
        return 2

    if args.device is None:
        print("Input devices (need one showing >= 8 channels @ 44100):")
        for d in list_input_devices():
            star = "*" if d.max_input_channels >= POLARIS_N_MICS else " "
            print(f" {star}[{d.index}] {d.name}  ({d.max_input_channels} ch, {d.default_samplerate:.0f} Hz)")
        print("\nRe-run with --device <idx>.")
        return 0

    eng = BeamEngine(
        device=args.device, fs=args.rate, block_ms=args.block_ms, mode=args.start_mode,
        crossfade_blocks=args.crossfade_blocks, assumed_range_m=2.0,
        beam_bandlimit_hz=(None if args.no_bandlimit else _AUTO),
    )
    print(f"BeamEngine A/B — steered vs grid on one POLARIS stream (strategy comparison, not a "
          f"quality ranking). Start: {args.start_mode}. Ctrl+C to stop.")
    if args.toggle_seconds > 0:
        print(f"Auto-toggling mode every {args.toggle_seconds:.0f}s.")
    try:
        eng.start()
        next_toggle = time.monotonic() + args.toggle_seconds
        while True:
            time.sleep(0.1)
            now = time.monotonic()
            if args.toggle_seconds > 0 and now >= next_toggle:
                eng.set_mode("grid" if eng.get_mode() == "steered" else "steered")
                next_toggle = now + args.toggle_seconds
            loc = eng.current_location
            if loc.mode == "steered" and loc.angle_deg is not None:
                where = f"{loc.angle_deg:5.0f}°"
            elif loc.xy is not None:
                where = f"({loc.xy[0]:+5.2f},{loc.xy[1]:+5.2f})m"
            else:
                where = "  --  "
            print(f"\r[{loc.mode:7s}] {where}  conf {loc.confidence:4.2f}  | lvl {eng.read_level():4.2f}    ",
                  end="", flush=True)
    except KeyboardInterrupt:
        print("\nStopping ...")
    finally:
        eng.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(_demo())
