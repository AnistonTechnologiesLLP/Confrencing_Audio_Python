"""Real-time SRP-PHAT DOA + delay-and-sum beam for the sensiBel POLARIS 8-array.

Self-contained hybrid runtime: this module owns its own ``sounddevice`` input
stream and a **time-domain delay-and-sum** beamformer, and reuses the
SRP-PHAT direction-finding (:mod:`conf_pipeline_control.doa`) and circular
geometry (:mod:`conf_pipeline_control.geometry`) already in this package. It
estimates the dominant talker's azimuth and emits a single mono beam steered at
them — *active-speaker isolation, not source separation*.

It deliberately does **not** wrap :class:`~conf_pipeline_control.live.LiveBeamController`
(that one is a frequency-domain, zone-driven runtime); the public surface here is
the small, host-agnostic class the embedding app asked for: ``start()`` /
``stop()``, a mono-output queue + optional callback, and a ``current_doa_deg``
property the host can display/log alongside its room geometry.

numpy + sounddevice are the ``[control]`` extra; they are imported lazily inside
:meth:`PolarisBeamformer._open`, so importing this module (and the whole app)
works without the extra.

Array facts and honest caveats (encoded below, not hidden):

* **Geometry is physical, not tunable.** 8 capsules on a circle of radius
  ``POLARIS_RADIUS_M`` (40 mm), 45° apart, planar (z = 0). The radius and angles
  are constants; only the *look* direction is steered.
* **Planar array → elevation is unresolvable.** A circular array resolves the full
  360° of azimuth with no front/back ambiguity, but it cannot tell a source above
  the plane from one below it. ``off_nadir_deg`` is therefore fixed at 90°
  (horizontal) for both the DOA scan and the beam.
* **Spatial aliasing.** Adjacent-capsule spacing is ``2·R·sin(π/8) ≈ 30.6 mm`` →
  reliable beamforming up to ``c/(2·spacing) ≈ 5.6 kHz``; high-frequency
  selectivity degrades above that (grating lobes). The DOA scan is band-limited to
  ≤ 3.8 kHz (safe); the beam offers an *optional* gentle band-limit.
* **Integer-delay v1.** The delay-and-sum uses integer-sample delays (one sample
  ≈ 7.78 mm of travel at 44.1 kHz vs the 80 mm aperture → up to a few degrees of
  pointing error). A fractional-delay / MVDR strategy is a documented seam
  (:class:`BeamStrategy`).
* **Dead capsule.** The real POLARIS eval board's capsule 5 (index 4) is dead.
  By product decision this module **defaults to all 8 active**; pass
  ``dead_capsule=5`` or an explicit ``active_mask`` to exclude it on the real board.
"""
from __future__ import annotations

import queue
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Optional, Sequence

from .audio import controls_available, list_input_devices, missing_dependencies
from .beamformer import _unit_from_az_offnadir
from .control import MicController
from .geometry import SOUND_SPEED_MPS, ArrayGeometry, sensibel_8, with_active_channels
from . import doa

# --- POLARIS hardware constants (physical facts — do not "tune") ---
POLARIS_N_MICS = 8
POLARIS_RADIUS_M = 0.040          # capsule-circle radius (NOT the 0.05 sensibel_8 placeholder)
POLARIS_RATE_HZ = 44100.0         # the board's USB default sample rate
POLARIS_DEAD_CAPSULE = 5          # capsule 5 (index 4) is dead on the real board (see module note)

# --- defaults ---
DEFAULT_BLOCK_MS = 32.0           # audio-callback cadence
DEFAULT_NFFT = 1024               # DOA STFT length (the beam path is FFT-free)
DEFAULT_GRID_STEP_DEG = 2.0       # SRP-PHAT azimuth resolution
DEFAULT_OFF_NADIR_DEG = 90.0      # horizontal — planar array, elevation unresolvable
DEFAULT_HOLD_SECONDS = 0.4        # talker-hold across VAD dropouts (0.3-0.5)
DEFAULT_SWITCH_MARGIN_DEG = 20.0  # only re-steer past this angular move (anti-jitter)
DEFAULT_DOA_UPDATE_HZ = 10.0      # DOA control-thread rate (NOT the audio block rate)
# adjacent spacing 2*R*sin(pi/8); aliasing cutoff c/(2*spacing). For R=40mm ≈ 5.6 kHz.
ALIAS_CUTOFF_HZ = SOUND_SPEED_MPS / (2.0 * 2.0 * POLARIS_RADIUS_M * 0.38268343236508984)

MODE_DELAYSUM = "delaysum"


@dataclass(frozen=True)
class DoaReading:
    """Snapshot of the dominant-talker tracker, for the host/GUI/demo."""

    azimuth_deg: Optional[float]   # committed dominant talker; None on silence past the hold
    salience_db: float             # SRP peak height over the map median; 0.0 when azimuth is None
    held: bool                     # True while coasting through a brief VAD dropout
    active: bool                   # SRP VAD flag this cycle


# --------------------------------------------------------------------------- #
# Pure talker-hold smoothing — no threads, no numpy, no audio (unit-testable).
# --------------------------------------------------------------------------- #
class _TalkerTracker:
    """Dominant-talker hold/switch machine. Decouples the *steered* angle from the
    raw per-cycle DOA so the beam doesn't jitter: it holds the committed talker
    through brief silences and only switches when a new direction is more than
    ``switch_margin_deg`` away.

    Time is supplied by the caller (monotonic seconds) so it is deterministic and
    testable without a clock or hardware.
    """

    def __init__(self, *, hold_seconds: float = DEFAULT_HOLD_SECONDS,
                 switch_margin_deg: float = DEFAULT_SWITCH_MARGIN_DEG):
        self.hold_seconds = float(hold_seconds)
        self.switch_margin_deg = float(switch_margin_deg)
        self._az: Optional[float] = None       # currently committed talker
        self._sal: float = 0.0
        self._last_seen_t: Optional[float] = None

    def update(self, observed_az: Optional[float], salience_db: float, t: float) -> DoaReading:
        """Fold one DOA cycle into the committed reading.

        ``observed_az`` is this cycle's strongest in-band azimuth, or ``None`` when
        the VAD says nobody is talking. Returns the committed :class:`DoaReading`.
        """
        if observed_az is None:
            # Silence: coast on the committed talker until the hold elapses, then drop.
            if (self._az is not None and self._last_seen_t is not None
                    and (t - self._last_seen_t) <= self.hold_seconds):
                return DoaReading(self._az, self._sal, held=True, active=False)
            self._az = None
            self._sal = 0.0
            self._last_seen_t = None
            return DoaReading(None, 0.0, held=False, active=False)

        # Have a DOA: acquire if none held, or switch only past the margin.
        if self._az is None or doa._circular_sep(observed_az, self._az) >= self.switch_margin_deg:
            self._az = observed_az
        # Within the margin: keep the committed angle (ignore jitter), refresh liveness.
        self._sal = float(salience_db)
        self._last_seen_t = t
        return DoaReading(self._az, self._sal, held=False, active=True)

    def current(self) -> Optional[float]:
        return self._az


# --------------------------------------------------------------------------- #
# Delay-and-sum steering math (shared by the pure function + streaming strategy).
# --------------------------------------------------------------------------- #
def _steer_delays(geom: ArrayGeometry, azimuth_deg: float, off_nadir_deg: float,
                  sample_rate: float, speed_of_sound: float):
    """Integer per-active-capsule delays (samples, ≥ 0) to steer a delay-and-sum
    beam toward ``(azimuth_deg, off_nadir_deg)``.

    For a plane wave from look direction ``u`` (array→source), capsule ``m`` at
    ``p_m`` leads the array centre by ``proj_m = p_m·u`` (a capsule *toward* the
    source is hit earlier). Aligning the wavefronts means delaying each capsule by
    ``(proj_m − min_k proj_k)/c`` — the capsule nearest the source is delayed most,
    so all channels line up on the farthest one. (The opposite sign would steer to
    the mirror azimuth; verified against the ``a_m = exp(+j·k·p·u)`` manifold used
    by :mod:`conf_pipeline_control.beamformer` / :mod:`conf_pipeline_control.doa`.)

    Returns ``(active_indices, delays_samples, max_delay)``.
    """
    ux, uy, uz = _unit_from_az_offnadir(azimuth_deg, off_nadir_deg)
    idx = geom.active_indices()
    elems = geom.elements
    projs = [elems[m][0] * ux + elems[m][1] * uy + elems[m][2] * uz for m in idx]
    pmin = min(projs) if projs else 0.0
    delays = [int(round((p - pmin) / speed_of_sound * sample_rate)) for p in projs]
    maxd = max(delays) if delays else 0
    return idx, delays, maxd


def delay_and_sum_block(block: Any, geom: ArrayGeometry, azimuth_deg: float,
                        off_nadir_deg: float = DEFAULT_OFF_NADIR_DEG, *,
                        sample_rate: float, speed_of_sound: float = SOUND_SPEED_MPS) -> Any:
    """Steer a multichannel block toward ``(azimuth_deg, off_nadir_deg)`` and sum.

    ``block`` is ``(n, M)`` float (channels in capsule order). Returns mono ``(n,)``
    float32. Pure and stateless — the streaming path uses :class:`_DelaySumBeam`
    (a history ring) so integer delays pull real previous samples across block
    boundaries; this function zero-pads the leading ``delay`` samples and is meant
    for analysis/tests. Only active capsules (``geom.active_indices()``) contribute;
    the sum is divided by the active count.
    """
    import numpy as np

    x = np.asarray(block, dtype=float)
    if x.ndim != 2:
        raise ValueError("block must be 2-D (samples, channels)")
    n = x.shape[0]
    idx, delays, _ = _steer_delays(geom, azimuth_deg, off_nadir_deg, sample_rate, speed_of_sound)
    out = np.zeros(n, dtype=float)
    for m, d in zip(idx, delays):
        if d <= 0:
            out += x[:, m]
        elif d < n:
            out[d:] += x[:-d, m]        # delay channel m by d samples (zero-pad the head)
    out /= max(1, len(idx))
    return out.astype(np.float32)


class BeamStrategy:
    """Steerable mono beamformer interface.

    v1 ships :class:`_DelaySumBeam` (time-domain, integer-sample). The seam for
    later tiers, behind this same ``set_look`` / ``process`` contract:

    * a fractional-delay strategy (per-bin FFT phase ramp ``exp(-j·2π·f·delay_m)``)
      to remove the integer-rounding pointing error, and
    * an MVDR / superdirective strategy reusing
      :func:`conf_pipeline_control.beamformer.delay_and_sum_weights` /
      ``superdirective_weights`` (frequency-domain, per FFT bin).
    """

    def set_look(self, azimuth_deg: float, off_nadir_deg: float = DEFAULT_OFF_NADIR_DEG) -> None:
        raise NotImplementedError

    def process(self, block: Any) -> Any:
        raise NotImplementedError


class _DelaySumBeam(BeamStrategy):
    """Streaming integer-sample delay-and-sum with a per-channel history ring.

    Naive ``np.roll`` wraps end-of-block samples to the start (a periodic click);
    instead we keep the last ``max_delay`` samples of every channel and index back
    into them, so each delayed read is a real previous sample.
    """

    def __init__(self, geom: ArrayGeometry, sample_rate: float, speed_of_sound: float):
        self._geom = geom
        self._sr = float(sample_rate)
        self._c = float(speed_of_sound)
        self._idx: tuple = ()
        self._delays: list = []
        self._maxd = 0
        self._hist: Any = None
        self.set_look(0.0)

    def set_look(self, azimuth_deg: float, off_nadir_deg: float = DEFAULT_OFF_NADIR_DEG) -> None:
        self._idx, self._delays, self._maxd = _steer_delays(
            self._geom, azimuth_deg, off_nadir_deg, self._sr, self._c
        )
        self._hist = None   # reset; a sub-millisecond transient on re-steer is acceptable

    def process(self, block: Any) -> Any:
        import numpy as np

        x = np.asarray(block, dtype=float)
        n, M = x.shape
        D = self._maxd
        if self._hist is None or self._hist.shape != (D, M):
            self._hist = np.zeros((D, M), dtype=float)
        ext = np.concatenate([self._hist, x], axis=0) if D else x
        out = np.zeros(n, dtype=float)
        for m, d in zip(self._idx, self._delays):
            start = D - d                       # output row i ↔ ext row D+i; delayed read at D+i-d
            out += ext[start:start + n, m]
        out /= max(1, len(self._idx))
        if D:
            self._hist = ext[-D:, :].copy()
        return out.astype(np.float32)


def _install_hint() -> str:
    miss = missing_dependencies()
    pkgs = " + ".join(miss) if miss else "numpy + sounddevice"
    return f"Live audio needs {pkgs}. Install the extra:\n    pip install -e \".[control]\""


def _resolve_active_mask(active_mask: Optional[Sequence[bool]], dead_capsule: Optional[int]):
    """All 8 active by default; an explicit ``active_mask`` wins; else mask one
    dead index. Returns a mask tuple or ``None`` (all active)."""
    if active_mask is not None:
        return tuple(bool(x) for x in active_mask)
    if dead_capsule is not None:
        return tuple(i != dead_capsule for i in range(POLARIS_N_MICS))
    return None


class PolarisBeamformer(MicController):
    """Live delay-and-sum beam + dominant-talker DOA for the POLARIS 8-array.

    Subclasses :class:`~conf_pipeline_control.control.MicController` for lifecycle,
    mute/gain, metering, and ``state()``. ``start()`` / ``stop()`` are the public
    entry points (they manage the audio stream *and* the DOA worker thread);
    ``connect()`` / ``disconnect()`` alone open the stream but leave DOA idle, so
    prefer ``start()`` / ``stop()`` (and ``with PolarisBeamformer(...) as bf``).
    """

    backend = "polaris"

    def __init__(
        self,
        *,
        device: Optional[int] = None,
        radius_m: float = POLARIS_RADIUS_M,
        active_mask: Optional[Sequence[bool]] = None,
        dead_capsule: Optional[int] = None,        # default: all 8 active (per product decision)
        sample_rate: float = POLARIS_RATE_HZ,
        block_ms: float = DEFAULT_BLOCK_MS,
        blocksize: Optional[int] = None,
        nfft: int = DEFAULT_NFFT,
        speed_of_sound: float = SOUND_SPEED_MPS,
        grid_step_deg: float = DEFAULT_GRID_STEP_DEG,
        off_nadir_deg: float = DEFAULT_OFF_NADIR_DEG,
        hold_seconds: float = DEFAULT_HOLD_SECONDS,
        switch_margin_deg: float = DEFAULT_SWITCH_MARGIN_DEG,
        doa_update_hz: float = DEFAULT_DOA_UPDATE_HZ,
        min_salience_db: float = 3.0,
        vad_floor_db: float = 3.0,
        min_separation_deg: float = 40.0,
        mode: str = MODE_DELAYSUM,
        steer_to_doa: bool = True,
        fixed_azimuth_deg: Optional[float] = None,
        beam_bandlimit_hz: Optional[float] = None,
        output_callback: Optional[Callable[[Any], None]] = None,
        output_queue_size: int = 8,
        output_device: Optional[int] = None,
        monitor: bool = False,
    ):
        mask = _resolve_active_mask(active_mask, dead_capsule)
        geom = sensibel_8(radius_m=radius_m)
        if mask is not None:
            geom = with_active_channels(geom, mask)   # validates length / non-empty
        super().__init__(geom, n_channels=POLARIS_N_MICS)

        if mode != MODE_DELAYSUM:
            raise ValueError(
                f"mode {mode!r} not implemented in v1; only {MODE_DELAYSUM!r} ships today "
                "(MVDR / fractional-delay are a documented BeamStrategy seam)"
            )

        self.device = device
        self.sample_rate = float(sample_rate)
        self.blocksize = int(blocksize) if blocksize else int(round(self.sample_rate * block_ms / 1000.0))
        self.nfft = int(nfft)
        self.hop = self.nfft // 2
        self.speed_of_sound = float(speed_of_sound)
        self.grid_step_deg = float(grid_step_deg)
        self.off_nadir_deg = float(off_nadir_deg)
        self.min_salience_db = float(min_salience_db)
        self.vad_floor_db = float(vad_floor_db)
        self.min_separation_deg = float(min_separation_deg)
        self.doa_update_hz = max(1.0, float(doa_update_hz))
        self.steer_to_doa = bool(steer_to_doa)
        self.beam_bandlimit_hz = beam_bandlimit_hz
        self.output_device = output_device
        self.monitor = monitor

        self._tracker = _TalkerTracker(hold_seconds=hold_seconds, switch_margin_deg=switch_margin_deg)
        self._beam = _DelaySumBeam(geom, self.sample_rate, self.speed_of_sound)
        self._steered_az: Optional[float] = None
        if fixed_azimuth_deg is not None:
            self._beam.set_look(fixed_azimuth_deg, self.off_nadir_deg)
            self._steered_az = float(fixed_azimuth_deg)

        # output delivery: a drop-oldest queue + an optional realtime callback
        self._output_q: "queue.Queue[Any]" = queue.Queue(maxsize=max(1, output_queue_size))
        self._output_cb = output_callback

        # locks/state shared across the audio thread, DOA thread, and host
        self._beam_lock = threading.Lock()
        self._cov_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._stop = threading.Event()
        self._doa_thread: Optional[threading.Thread] = None
        self._reading = DoaReading(None, 0.0, False, False)
        self._detections: list = []
        self.error = ""

        # lazily bound in _open() (numpy / sounddevice) — Any so the module imports
        # and type-checks without the [control] extra, like live.py
        self._np: Any = None
        self._sd: Any = None
        self._stream: Any = None
        self._out_stream: Any = None
        self._monitor_q: Any = None
        self._out_channels = 0
        self._level = 0.0                 # written by the audio thread, read via read_level()
        self._win: Any = None             # Hann window for the DOA STFT
        self._stftbuf: Any = None         # rolling multichannel buffer feeding the STFT
        self._cov: Any = None             # EMA band covariance (n_band, M, M)
        self._cov_freqs: Any = None
        self._cov_band: Any = None
        self._cov_alpha = 0.05
        self._lp_kernel: Any = None       # optional beam band-limit (moving-average FIR)
        self._lp_tail: Any = None

    # ---- public API ----
    def start(self) -> None:
        """Open the array and start DOA tracking + beam steering."""
        self.connect()                                  # _open(): validate + open the stream
        if self._doa_thread is None:
            self._stop.clear()
            self._doa_thread = threading.Thread(target=self._doa_loop, name="polaris-doa", daemon=True)
            self._doa_thread.start()

    def stop(self) -> None:
        """Stop DOA tracking and close the array (idempotent)."""
        self._stop.set()
        if self._doa_thread is not None:
            self._doa_thread.join(timeout=2.0)
            self._doa_thread = None
        self.disconnect()

    def __enter__(self) -> "PolarisBeamformer":
        self.start()
        return self

    def __exit__(self, *exc) -> None:
        self.stop()

    @property
    def current_doa_deg(self) -> Optional[float]:
        """Latest committed dominant-talker azimuth (degrees), or ``None`` on silence."""
        with self._state_lock:
            return self._reading.azimuth_deg

    def reading(self) -> DoaReading:
        """Full latest :class:`DoaReading` snapshot (thread-safe)."""
        with self._state_lock:
            return self._reading

    def detections(self) -> list:
        """Copy of the latest raw SRP detections (parity with AutoSteerController)."""
        with self._state_lock:
            return list(self._detections)

    @property
    def output_queue(self) -> "queue.Queue[Any]":
        """Drop-oldest queue of mono beam blocks for a host consumer to drain."""
        return self._output_q

    def set_steering(self, azimuth_deg: Optional[float] = None) -> None:
        """Pin the beam to ``azimuth_deg`` (disables DOA-follow), or resume
        following the tracked talker when ``None``."""
        if azimuth_deg is None:
            self.steer_to_doa = True
            return
        self.steer_to_doa = False
        with self._beam_lock:
            self._beam.set_look(float(azimuth_deg), self.off_nadir_deg)
            self._steered_az = float(azimuth_deg)

    # ---- DOA detection (pure-ish; covariance in, dominant azimuth out) ----
    def _detect_dominant(self, cov: Any, freqs: Any):
        """Run SRP-PHAT on a band covariance and return
        ``(dominant_az or None, salience_db, detections)``. ``cov`` must be the
        full ``(n_band, M, M)`` over **all** capsules — :func:`doa.detect` slices to
        the active ones via the geometry mask; pre-slicing mis-maps azimuth."""
        assert self.geometry is not None
        res = doa.detect(
            cov, freqs, self.geometry,
            off_nadir_deg=self.off_nadir_deg,
            grid_step_deg=self.grid_step_deg,
            max_talkers=1,                       # single dominant talker
            min_separation_deg=self.min_separation_deg,
            min_salience_db=self.min_salience_db,
            vad_floor_db=self.vad_floor_db,
        )
        if res.active and res.detections:
            d = res.detections[0]
            return d.azimuth_deg, d.salience_db, res.detections
        return None, 0.0, res.detections

    # ---- DOA control thread (decoupled from the audio block rate) ----
    def _doa_loop(self) -> None:  # pragma: no cover (timing/thread)
        period = 1.0 / self.doa_update_hz
        while not self._stop.is_set():
            try:
                self._doa_tick()
            except Exception as exc:             # keep the thread alive; surface the error
                self.error = str(exc)
            self._stop.wait(period)

    def _doa_tick(self) -> None:
        cov, freqs = self._snapshot_covariance()
        if cov is None:
            return
        dominant, salience, dets = self._detect_dominant(cov, freqs)
        reading = self._tracker.update(dominant, salience, time.monotonic())
        with self._state_lock:
            self._reading = reading
            self._detections = dets
        if self.steer_to_doa and reading.azimuth_deg is not None \
                and reading.azimuth_deg != self._steered_az:
            with self._beam_lock:
                self._beam.set_look(reading.azimuth_deg, self.off_nadir_deg)
                self._steered_az = reading.azimuth_deg

    def _snapshot_covariance(self):
        with self._cov_lock:
            if self._cov is None:
                return None, None
            return self._cov.copy(), self._cov_freqs

    # ---- lifecycle / backend hooks ----
    def _open(self) -> None:
        if not controls_available():
            raise RuntimeError(_install_hint())
        self._validate_input_device()
        import numpy as np
        import sounddevice as sd

        self._np = np
        self._sd = sd
        self._win = np.hanning(self.nfft).astype(float)
        self._stftbuf = np.zeros((0, self.n_channels), dtype=float)
        freqs_full = np.fft.rfftfreq(self.nfft, d=1.0 / self.sample_rate)
        self._cov_band = doa.band_indices(freqs_full, doa.DEFAULT_F_LO_HZ, doa.DEFAULT_F_HI_HZ)
        self._cov_freqs = freqs_full[self._cov_band]
        with self._cov_lock:
            self._cov = np.zeros((len(self._cov_band), self.n_channels, self.n_channels), dtype=complex)

        if self.beam_bandlimit_hz:
            # Gentle, dependency-free anti-alias: an L-tap moving average whose first
            # null sits near the band-limit (L ≈ fs / fc). Not a brickwall — for a
            # steeper filter, swap in a biquad behind a fractional/MVDR BeamStrategy.
            taps = max(2, int(round(self.sample_rate / float(self.beam_bandlimit_hz))))
            self._lp_kernel = np.ones(taps, dtype=float) / taps
            self._lp_tail = np.zeros(taps - 1, dtype=float)

        # Monitoring uses two independent streams joined by a queue (a single duplex
        # stream needs both devices on one host API; we can't assume that). Mirrors
        # live.py.
        if self.monitor:
            out_ch = 2
            try:
                info = sd.query_devices(self.output_device, "output")
                out_ch = max(1, min(2, int(info.get("max_output_channels", 2))))
            except Exception:
                out_ch = 2
            self._out_channels = out_ch
            self._monitor_q = queue.Queue(maxsize=8)
        else:
            self._out_channels = 0
            self._monitor_q = None

        self._stream = sd.InputStream(
            samplerate=self.sample_rate,
            channels=self.n_channels,
            blocksize=self.blocksize,
            device=self.device,
            dtype="float32",
            callback=self._cb_input,
        )
        self._stream.start()
        if self.monitor:
            self._out_stream = sd.OutputStream(
                samplerate=self.sample_rate,
                channels=self._out_channels,
                blocksize=self.blocksize,
                device=self.output_device,
                dtype="float32",
                callback=self._cb_output,
            )
            self._out_stream.start()

    def _validate_input_device(self) -> None:
        """Clear errors for a missing device, too few channels, or an unsupported
        rate — before opening the stream."""
        devs = list_input_devices()
        match = None
        if self.device is not None:
            match = next((d for d in devs if d.index == self.device), None)
            if match is None:
                raise ValueError(
                    f"input device index {self.device} not found; "
                    "run scripts/device_check.py to list devices"
                )
            if match.max_input_channels < self.n_channels:
                raise ValueError(
                    f"device {self.device} ({match.name!r}) exposes {match.max_input_channels} "
                    f"input channels but POLARIS needs {self.n_channels}; on Windows pick the "
                    "ASIO/WASAPI entry that surfaces all 8 (see scripts/device_check.py)"
                )
        import sounddevice as sd
        try:
            sd.check_input_settings(
                device=self.device, channels=self.n_channels,
                samplerate=self.sample_rate, dtype="float32",
            )
        except Exception as exc:
            default_sr = match.default_samplerate if match is not None else self.sample_rate
            raise ValueError(
                f"device {self.device} did not accept {self.sample_rate:.0f} Hz @ "
                f"{self.n_channels}ch (its default is {default_sr:.0f} Hz; POLARIS runs at "
                f"{POLARIS_RATE_HZ:.0f}). Original error: {exc}"
            ) from exc

    def _close(self) -> None:
        if self._out_stream is not None:
            try:
                self._out_stream.stop()
                self._out_stream.close()
            finally:
                self._out_stream = None
        if self._stream is not None:
            try:
                self._stream.stop()
                self._stream.close()
            finally:
                self._stream = None
        self._monitor_q = None
        self._level = 0.0

    def _raw_level(self) -> float:
        return self._level

    # ---- audio thread ----
    def _cb_input(self, indata, frames, time_info, status):  # pragma: no cover (needs hardware)
        np = self._np
        # (a) BEAM PATH — time-domain delay-and-sum, FFT-free, low latency
        with self._beam_lock:
            mono = self._beam.process(indata)
        if self._lp_kernel is not None:
            mono = self._band_limit(mono)
        rms = float(np.sqrt(np.mean(mono * mono))) if mono.size else 0.0
        self._level = min(1.0, rms)
        self._emit(mono)

        # (b) DOA PATH — accumulate a band covariance from a sliding Hann STFT
        self._accumulate_covariance(indata)

    def _band_limit(self, mono: Any) -> Any:
        np = self._np
        ext = np.concatenate([self._lp_tail, mono])
        y = np.convolve(ext, self._lp_kernel, mode="valid")     # (len(mono),)
        self._lp_tail = ext[-(len(self._lp_kernel) - 1):]
        return y.astype(np.float32)

    def _accumulate_covariance(self, indata) -> None:
        np = self._np
        self._stftbuf = np.concatenate([self._stftbuf, np.asarray(indata, dtype=float)], axis=0)
        while self._stftbuf.shape[0] >= self.nfft:
            frame = self._stftbuf[:self.nfft]
            X = np.fft.rfft(frame * self._win[:, None], axis=0)     # (n_bins, M)
            xb = X[self._cov_band, :]                                # (n_band, M)
            inst = xb[:, :, None] * np.conj(xb[:, None, :])          # (n_band, M, M)
            a = self._cov_alpha
            with self._cov_lock:
                self._cov *= (1.0 - a)
                self._cov += a * inst
            self._stftbuf = self._stftbuf[self.hop:]

    def _emit(self, mono: Any) -> None:
        """Deliver a mono block: drop-oldest into the host queue, the optional
        realtime callback, and the monitor stream. Never blocks the audio thread."""
        self._queue_put(self._output_q, mono)
        if self._output_cb is not None:
            try:
                self._output_cb(mono)         # NOTE: runs on the realtime audio thread — keep it cheap
            except Exception as exc:
                self.error = f"output_callback raised: {exc}"
        if self._monitor_q is not None:
            self._queue_put(self._monitor_q, mono)

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

    def _cb_output(self, outdata, frames, time_info, status):  # pragma: no cover (needs hardware)
        q = self._monitor_q
        try:
            blk = q.get_nowait() if q is not None else None
        except queue.Empty:
            blk = None
        if blk is not None and blk.shape[0] == outdata.shape[0]:
            outdata[:] = blk[:, None]         # mono → all output channels
        else:
            outdata.fill(0.0)


# --------------------------------------------------------------------------- #
# Standalone demo: list devices, run the beam, print the live DOA.
# --------------------------------------------------------------------------- #
def _demo(argv: Optional[Sequence[str]] = None) -> int:
    import argparse
    import sys

    ap = argparse.ArgumentParser(
        description="POLARIS live delay-and-sum beam + dominant-talker DOA (SRP-PHAT)."
    )
    ap.add_argument("--device", type=int, default=None, help="input device index (omit to list devices)")
    ap.add_argument("--radius", type=float, default=POLARIS_RADIUS_M, help="capsule-circle radius m (POLARIS=0.040)")
    ap.add_argument("--rate", type=float, default=POLARIS_RATE_HZ, help="sample rate (POLARIS=44100)")
    ap.add_argument("--block-ms", type=float, default=DEFAULT_BLOCK_MS, help="audio block size, ms")
    ap.add_argument("--dead", type=int, default=-1,
                    help="dead capsule index to mask off (-1 = none; the real board's is 5)")
    ap.add_argument("--hold", type=float, default=DEFAULT_HOLD_SECONDS, help="talker-hold seconds")
    ap.add_argument("--switch-margin", type=float, default=DEFAULT_SWITCH_MARGIN_DEG, help="re-steer margin, deg")
    ap.add_argument("--bandlimit", type=float, default=None, help="optional beam low-pass cutoff, Hz (~5500)")
    ap.add_argument("--monitor", action="store_true", help="play the mono output (use HEADPHONES)")
    ap.add_argument("--output-device", type=int, default=None, help="monitor output device index")
    args = ap.parse_args(argv)

    if not controls_available():
        sys.stderr.write(_install_hint() + "\n")
        return 2

    if args.device is None:
        print("Input devices (need one showing >= 8 channels @ 44100):")
        for d in list_input_devices():
            star = "*" if d.max_input_channels >= POLARIS_N_MICS else " "
            print(f" {star}[{d.index}] {d.name}  ({d.max_input_channels} ch, {d.default_samplerate:.0f} Hz)")
        print("\nRe-run with --device <idx>.  (The real board's capsule 5 is dead: add --dead 5.)")
        return 0

    dead = None if args.dead is None or args.dead < 0 else args.dead
    bf = PolarisBeamformer(
        device=args.device, radius_m=args.radius, sample_rate=args.rate, block_ms=args.block_ms,
        dead_capsule=dead, hold_seconds=args.hold, switch_margin_deg=args.switch_margin,
        beam_bandlimit_hz=args.bandlimit, monitor=args.monitor, output_device=args.output_device,
    )
    assert bf.geometry is not None
    print(f"Array: {bf.geometry.n_active}/{POLARIS_N_MICS} capsules, aperture "
          f"{bf.geometry.aperture_m() * 100:.1f} cm.  Aliasing cutoff ~{ALIAS_CUTOFF_HZ / 1000:.1f} kHz.")
    print(f"Opening device {args.device} @ {args.rate:.0f} Hz ... Ctrl+C to stop.")
    if args.monitor:
        print("Monitoring live — wear HEADPHONES to avoid feedback.")
    try:
        bf.start()
        while True:
            time.sleep(0.1)
            r = bf.reading()
            az = "  --  " if r.azimuth_deg is None else f"{r.azimuth_deg:5.0f}°"
            tag = "HOLD" if r.held else ("LIVE" if r.active else "  · ")
            err = f"  !{bf.error}" if bf.error else ""
            print(f"\r[{tag}] DOA {az}  sal {r.salience_db:4.1f}dB  | lvl {bf.read_level():4.2f}{err}    ",
                  end="", flush=True)
    except KeyboardInterrupt:
        print("\nStopping ...")
    finally:
        bf.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(_demo())
