"""Real-time "virtual microphone grid" selection beamformer for the POLARIS 8-array.

A Nureva *Microphone Mist*-style design, and deliberately **NOT** a steered beamformer
(contrast :mod:`conf_pipeline_control.polaris_beamformer`, which estimates the talker's
direction and steers one beam). Here we pre-compute a dense grid of **fixed** virtual
microphones — each a stationary delay-and-sum beam focused on a fixed ``(x, y)`` point that
blankets a room region. Every block, **all** virtual mics run simultaneously; we score each
by speech-band energy and route the strongest (optionally a small top-k blend) as the mono
output. Nothing is steered or scanned — the focus points never move, the system only
*selects* among them. That eliminates beam-switching artifacts and the DOA-tracking /
hold-time problem entirely.

This module is **self-contained and optional**: it reuses only the shared core modules
(geometry / MicController / doa band helper / audio device discovery / tracking) and
duplicates the few small device-IO helpers inline, so it imports from and modifies the steered
sibling not at all. Delete this file (+ its test, the one ``__init__`` export line, the
optional console entry) to remove the feature cleanly.

numpy + sounddevice are the ``[control]`` extra, imported lazily inside :meth:`_open`.

Honest caveats (encoded below, not hidden):

* **Geometry is physical.** 8 capsules on a circle of radius 0.040 m, 45° apart, planar
  (z = 0). Radius/angles are constants; only the grid/focus configuration is tunable.
* **Small-aperture limit.** An 80 mm aperture has limited spatial resolution: near-field
  "focusing" on distant points is soft and adjacent virtual mics overlap heavily. With this
  array the grid gives **coarse zone selection, not Nureva-scale pinpoint** (Nureva uses a
  much larger physical array, far more virtual mics, and heavier DSP).
* **Coplanar grid degeneracy.** With ``focus_height_m = 0`` (focus points in the mic plane) a
  planar array barely separates points sharing a bearing/range — lift the grid off the plane
  for real separation. Documented, not "fixed."
* **Spatial aliasing > ~5.6 kHz** (adjacent spacing ``2·R·sin(π/8) ≈ 30.6 mm``): scoring is
  band-limited to ≤ 3.8 kHz, and the selected output is low-passed at the aliasing cutoff
  (~5.6 kHz, ``beam_bandlimit_hz``) by default — matching the steered sibling. Pass
  ``beam_bandlimit_hz=None`` to disable.
* **Cost = O(N_virtual_mics × N_phys_mics) per block** (plus ``O(N · nfft·log nfft)`` scoring).
  Steering delays are precomputed/cached once (focus points are fixed); per-block work is just
  apply-cached-delays + sum + score. **N is the scaling bottleneck** — true Nureva counts need
  a C/Numba kernel or an FFT-domain reformulation (out of scope for v1).
* **Integer-delay v1** and a **top-k blend** that can comb-filter decorrelated points: a
  fractional-delay focusing path and per-virtual-mic AGC / persistent-noise ("ignore noisy
  locations", IST-style) rejection are documented future upgrades.
"""
from __future__ import annotations

import queue
from typing import Any, Callable, List, Optional, Sequence, Tuple

from .audio import controls_available, list_input_devices, missing_dependencies
from .control import MicController
from .geometry import SOUND_SPEED_MPS, ArrayGeometry, sensibel_8, with_active_channels
from .tracking import ExponentialTracker, ValueSmoother
from . import doa

# --- POLARIS hardware constants (physical facts — do not "tune") ---
POLARIS_N_MICS = 8
POLARIS_RADIUS_M = 0.040
POLARIS_RATE_HZ = 44100.0
POLARIS_DEAD_CAPSULE = 5            # capsule 5 (index 4) is dead on the real board (opt-in mask)

# --- defaults ---
DEFAULT_BLOCK_MS = 32.0
DEFAULT_NFFT = 1024                 # scoring FFT length
DEFAULT_SCORE_LO_HZ = doa.DEFAULT_F_LO_HZ   # 300
DEFAULT_SCORE_HI_HZ = doa.DEFAULT_F_HI_HZ   # 3800 — below the aliasing cutoff
# adjacent spacing 2*R*sin(pi/8); aliasing cutoff c/(2*spacing). For R=40mm ≈ 5.6 kHz.
ALIAS_CUTOFF_HZ = SOUND_SPEED_MPS / (2.0 * 2.0 * POLARIS_RADIUS_M * 0.38268343236508984)
# Same convention as the steered sibling: scoring stays ≤ 3.8 kHz (anti-alias for selection);
# the selected output is low-passed at the aliasing cutoff (~5.6 kHz) by default.
DEFAULT_BEAM_BANDLIMIT_HZ = ALIAS_CUTOFF_HZ
DEFAULT_BANDLIMIT_TAPS = 63         # Hann-windowed-sinc FIR length (forced odd → linear phase)
# Selection VAD: a focused talker lifts its virtual mic well above the grid's median energy;
# diffuse noise/silence leaves the map flat (~0 dB peak-over-median). Gate re-selection on this
# so the grid HOLDS the last seat through silence instead of chasing HVAC/keyboard noise. Mirrors
# the steered path's doa.detect vad_floor_db. Set None/≤0 to disable (always re-select).
DEFAULT_VAD_FLOOR_DB = 3.0


# --------------------------------------------------------------------------- #
# Inline device-IO helpers (duplicated from the steered module on purpose, so
# this feature is a clean add/remove and touches nothing else).
# --------------------------------------------------------------------------- #
class DeviceConfigError(ValueError):
    """The audio device is present but cannot satisfy the request — wrong input channel
    count or unsupported sample rate (vs. simply not being there). Subclasses ``ValueError``."""


def _install_hint() -> str:
    miss = missing_dependencies()
    pkgs = " + ".join(miss) if miss else "numpy + sounddevice"
    return f"Live audio needs {pkgs}. Install the extra:\n    pip install -e \".[control]\""


def _resolve_active_mask(active_mask: Optional[Sequence[bool]], dead_capsule: Optional[int]):
    """All 8 active by default; an explicit ``active_mask`` wins; else mask one dead index."""
    if active_mask is not None:
        return tuple(bool(x) for x in active_mask)
    if dead_capsule is not None:
        return tuple(i != dead_capsule for i in range(POLARIS_N_MICS))
    return None


def _lowpass_kernel(fc_hz: float, fs: float, numtaps: int = DEFAULT_BANDLIMIT_TAPS) -> Any:
    """Hann-windowed-sinc low-pass FIR — linear phase, unity DC gain, **pure numpy** (no scipy).
    Duplicated from the steered sibling so this module stays self-contained/removable."""
    import numpy as np

    taps = max(3, int(numtaps) | 1)                          # odd ≥ 3 → linear phase
    fc = min(0.499, max(1e-3, float(fc_hz) / float(fs)))     # normalized cutoff (cycles/sample)
    n = np.arange(taps) - (taps - 1) / 2.0
    h = 2.0 * fc * np.sinc(2.0 * fc * n) * np.hanning(taps)
    return (h / h.sum()).astype(float)


# --------------------------------------------------------------------------- #
# Grid construction + focusing math (pure, stream-free → directly testable).
# --------------------------------------------------------------------------- #
def _linspace(a: float, b: float, n: int) -> List[float]:
    if n <= 1:
        return [(a + b) / 2.0]
    step = (b - a) / (n - 1)
    return [a + i * step for i in range(n)]


def build_grid(room_width_m: float, room_depth_m: float, grid_cols: int, grid_rows: int,
               focus_height_m: float = 0.0,
               array_origin_xy: Tuple[float, float] = (0.0, 0.0)) -> List[Tuple[float, float, float]]:
    """Fixed focus points blanketing the room region (pure stdlib — no numpy).

    ``grid_cols × grid_rows`` points span ``[-W/2, W/2] × [-D/2, D/2]`` (offset by
    ``array_origin_xy``, i.e. the array sits at the region centre by default) at height
    ``z = focus_height_m`` above the mic plane. Row-major: index ``n = r*cols + c``. The points
    never move — this is the whole point of the design. Returns ``(x, y, z)`` tuples.
    """
    if grid_cols < 1 or grid_rows < 1:
        raise ValueError("grid_cols and grid_rows must be >= 1")
    ox, oy = array_origin_xy
    xs = [ox + x for x in _linspace(-room_width_m / 2.0, room_width_m / 2.0, grid_cols)]
    ys = [oy + y for y in _linspace(-room_depth_m / 2.0, room_depth_m / 2.0, grid_rows)]
    return [(x, y, float(focus_height_m)) for y in ys for x in xs]


def build_grid_delays(geom: ArrayGeometry, points, sample_rate: float,
                      speed_of_sound: float = SOUND_SPEED_MPS):
    """Precompute the per-physical-mic integer focus delays for every grid point.

    Near-field (spherical) model: for focus point ``P`` and physical mic ``m`` at ``p_m``, the
    time of flight is ``τ_m = |P − p_m| / c``. To focus a source AT ``P`` we delay each mic by
    ``D_m = round((max_k τ_k − τ_m)·fs)`` (≥ 0): the mic **nearest** the point is delayed most,
    aligning all channels on the **farthest** mic. (This is the near-field analogue of the
    steered module's plane-wave ``round((proj_m − min proj)/c·fs)`` — same "delay the
    early-arriving mic" physics; the matched-filter test is the empirical guard.)

    Focus points are fixed, so this is a one-time startup cost. Returns
    ``(delays int (N, M), maxd)`` over **all** M physical mics (inactive columns are unused).
    """
    import numpy as np

    P = np.asarray(points, dtype=float)               # (N, 3)
    pm = np.asarray(geom.elements, dtype=float)        # (M, 3)
    dist = np.linalg.norm(P[:, None, :] - pm[None, :, :], axis=2)   # (N, M)
    tau = dist / float(speed_of_sound)                 # (N, M) seconds
    delays = np.rint((tau.max(axis=1, keepdims=True) - tau) * sample_rate).astype(np.int64)
    return delays, int(delays.max()) if delays.size else 0


def delay_and_sum_grid(ext: Any, delays: Any, active_cols, maxd: int, B: int) -> Any:
    """Delay-and-sum **every** virtual mic for one block, vectorized over the grid.

    ``ext`` = ``concat(history (maxd, M), block (B, M))``; ``delays`` = cached ``(N, M)`` focus
    delays (0..maxd). Returns ``monos (N, B)`` float32. We loop only over the ≤ 8 physical mics
    and gather across all N virtual mics at once: output row ``i`` of vmic ``v`` reads
    ``ext[maxd + i − delays[v, m], m]``. Index range is ``[0, maxd+B−1]`` (in-bounds because
    ``delays ≤ maxd``). Only ``active_cols`` contribute; the sum is divided by their count.

    Cost: M gathers of ``N×B`` each → ``O(N·M)`` ops/block (the scaling bottleneck).
    """
    import numpy as np

    out = np.zeros((delays.shape[0], B), dtype=np.float64)
    base = maxd + np.arange(B)                          # (B,) output→ext row map
    for m in active_cols:
        rows = base[None, :] - delays[:, m][:, None]    # (N, B), in [0, maxd+B-1]
        out += ext[rows, m]
    n_active = max(1, len(active_cols))
    out /= n_active
    return out.astype(np.float32)


def score_grid(monos: Any, win: Any, band_idx: Any, nfft: int) -> Any:
    """Speech-band energy of every virtual mic (batched). Returns ``(N,)``.

    Windows the most recent ``nfft`` samples of each mono, one batched rfft, sums
    ``|X|²`` over ``band_idx`` (the speech band, kept below the aliasing cutoff)."""
    import numpy as np

    n, B = monos.shape
    if B >= nfft:
        seg = monos[:, -nfft:]
    else:
        seg = np.zeros((n, nfft), dtype=monos.dtype)
        seg[:, -B:] = monos
    X = np.fft.rfft(seg * win[None, :], axis=1)         # (N, nbins)
    Xb = X[:, band_idx]
    return np.sum((Xb * np.conj(Xb)).real, axis=1)      # (N,) real band energy


# --------------------------------------------------------------------------- #
# The module
# --------------------------------------------------------------------------- #
class VirtualMicGrid(MicController):
    """Live virtual-microphone-grid selection beamformer for the POLARIS 8-array.

    Subclasses :class:`~conf_pipeline_control.control.MicController` for lifecycle, mute/gain,
    metering, and ``state()``. ``start()`` / ``stop()`` open/close the array (no worker thread —
    everything happens in the audio callback). Read :attr:`selected_xy` for the currently
    selected focus point and :meth:`scores` for the per-virtual-mic energy map (e.g. to draw a
    heatmap on the host's room/seating map).
    """

    backend = "vmic-grid"

    def __init__(
        self,
        *,
        device: Optional[int] = None,
        radius_m: float = POLARIS_RADIUS_M,
        active_mask: Optional[Sequence[bool]] = None,
        dead_capsule: Optional[int] = None,        # default: all 8 active
        sample_rate: float = POLARIS_RATE_HZ,
        block_ms: float = DEFAULT_BLOCK_MS,
        blocksize: Optional[int] = None,
        room_width_m: float = 4.0,
        room_depth_m: float = 3.0,
        grid_cols: int = 13,
        grid_rows: int = 9,
        focus_height_m: float = 0.0,
        array_origin_xy: Tuple[float, float] = (0.0, 0.0),
        speed_of_sound: float = SOUND_SPEED_MPS,
        score_band: Tuple[float, float] = (DEFAULT_SCORE_LO_HZ, DEFAULT_SCORE_HI_HZ),
        nfft: int = DEFAULT_NFFT,
        top_k: int = 1,
        selection_smoothing: float = 0.5,
        tracker: Optional[ValueSmoother] = None,   # swap the selection smoother (default: EMA)
        vad_floor_db: Optional[float] = DEFAULT_VAD_FLOOR_DB,   # hold seat through silence; None/≤0 off
        beam_bandlimit_hz: Optional[float] = DEFAULT_BEAM_BANDLIMIT_HZ,   # None/0 disables
        output_callback: Optional[Callable[[Any], None]] = None,
        output_queue_size: int = 8,
        output_device: Optional[int] = None,
        monitor: bool = False,
    ):
        import threading

        mask = _resolve_active_mask(active_mask, dead_capsule)
        geom = sensibel_8(radius_m=radius_m)
        if mask is not None:
            geom = with_active_channels(geom, mask)    # validates length / non-empty
        super().__init__(geom, n_channels=POLARIS_N_MICS)

        if top_k < 1:
            raise ValueError("top_k must be >= 1")
        if not (0.0 <= selection_smoothing <= 1.0):
            raise ValueError("selection_smoothing must be in [0, 1]")
        if score_band[0] >= score_band[1]:
            raise ValueError("score_band must be (lo, hi) with lo < hi")

        self.device = device
        self.sample_rate = float(sample_rate)
        self.blocksize = int(blocksize) if blocksize else int(round(self.sample_rate * block_ms / 1000.0))
        self.speed_of_sound = float(speed_of_sound)
        self.nfft = int(nfft)
        self.score_band = (float(score_band[0]), float(score_band[1]))
        self.top_k = int(top_k)
        self.selection_smoothing = float(selection_smoothing)
        # Selection smoother behind the unified ValueSmoother interface — swap in any
        # ValueSmoother (e.g. tracking.AlphaBetaTracker) via tracker=; default is the EMA used
        # inline before.
        self._selection_tracker: ValueSmoother = tracker or ExponentialTracker(self.selection_smoothing)
        self.vad_floor_db = vad_floor_db
        self.beam_bandlimit_hz = beam_bandlimit_hz
        self.output_device = output_device
        self.monitor = monitor

        # Fixed focus grid (stdlib — so grid_points() works without numpy). Delays (numpy) are
        # precomputed once in _open(). The grid never moves.
        self._points_xyz = build_grid(room_width_m, room_depth_m, grid_cols, grid_rows,
                                      focus_height_m=focus_height_m, array_origin_xy=array_origin_xy)
        self._points_xy: List[Tuple[float, float]] = [(x, y) for (x, y, _z) in self._points_xyz]
        self.grid_cols = int(grid_cols)
        self.grid_rows = int(grid_rows)

        # output delivery: drop-oldest queue + optional realtime callback
        self._output_q: "queue.Queue[Any]" = queue.Queue(maxsize=max(1, output_queue_size))
        self._output_cb = output_callback

        self._state_lock = threading.Lock()
        self._selected: Optional[int] = None
        self._scores: Any = None                # latest smoothed per-vmic energies (N,)
        self._active = False                    # VAD: is a talker focused this block?
        self._last_order: Any = None            # held selection order (reused while not active)
        self._last_smoothed: Any = None
        self.error = ""

        # lazily bound in _open() (numpy / sounddevice) — Any so the module imports + type-checks
        # without the [control] extra, like the steered sibling.
        self._np: Any = None
        self._sd: Any = None
        self._stream: Any = None
        self._out_stream: Any = None
        self._monitor_q: Any = None
        self._out_channels = 0
        self._streaming = False
        self._level = 0.0
        self._delays: Any = None                # (N, M) int cached focus delays
        self._maxd = 0
        self._active_cols: tuple = ()
        self._hist: Any = None                  # (maxd, M) history ring
        self._win: Any = None
        self._score_band_idx: Any = None
        self._lp_kernel: Any = None             # optional output band-limit (windowed-sinc FIR)
        self._lp_tail: Any = None

    # ---- public API ----
    def start(self) -> None:
        """Open the array and begin grid selection (alias for connect())."""
        self.connect()

    def stop(self) -> None:
        """Close the array (alias for disconnect())."""
        self.disconnect()

    @property
    def streaming(self) -> bool:
        """True while the input stream is open and delivering audio."""
        return self._streaming

    @property
    def speech_active(self) -> bool:
        """Selection VAD for the latest block: True if a talker was localized, False on a flat
        (silence/diffuse-noise) energy map. False means the selection is being **held**."""
        with self._state_lock:
            return self._active

    @property
    def noise_only(self) -> bool:
        """Inverse of :attr:`speech_active` — a per-block "nobody is talking" flag a downstream
        adaptive beamformer (MVDR, item 2) can use to update its noise covariance on clean
        noise-only frames."""
        with self._state_lock:
            return not self._active

    @property
    def selected_xy(self) -> Optional[Tuple[float, float]]:
        """``(x, y)`` of the currently selected virtual mic, or ``None`` before the first block.
        For the host to overlay on its room/seating map."""
        with self._state_lock:
            if self._selected is None:
                return None
            return self._points_xy[self._selected]

    def grid_points(self) -> List[Tuple[float, float]]:
        """The fixed ``(x, y)`` focus points (row-major), for the host to draw the grid."""
        return list(self._points_xy)

    def scores(self) -> Any:
        """Copy of the latest smoothed per-virtual-mic energies ``(N,)``, or ``None`` before the
        first block. Lets the host render a selection heatmap over the grid."""
        with self._state_lock:
            return None if self._scores is None else self._scores.copy()

    @property
    def output_queue(self) -> "queue.Queue[Any]":
        """Drop-oldest queue of mono output blocks for a host consumer to drain."""
        return self._output_q

    @property
    def grid_size(self) -> int:
        return len(self._points_xy)

    # ---- selection (pure-ish; testable without a stream) ----
    def _is_active(self, score: Any) -> bool:
        """Selection VAD: True if the per-vmic energy map has a localized peak (a talker),
        False on a flat map (silence / diffuse noise). Peak-over-median in dB vs
        ``vad_floor_db`` — the grid analogue of the steered path's SRP peak/median gate.
        ``vad_floor_db`` None or ≤ 0 disables the gate (always active → always re-select)."""
        if self.vad_floor_db is None or self.vad_floor_db <= 0.0:
            return True
        np = self._np
        med = float(np.median(score))
        peak = float(np.max(score))
        if peak <= 0.0 or med <= 0.0:
            return False                                  # no in-band energy → treat as silence
        return (10.0 * float(np.log10(peak / med))) >= self.vad_floor_db

    def _update_selection(self, score: Any):
        """Fold a raw per-vmic score into the smoothed selection: push it through the selection
        :class:`~conf_pipeline_control.tracking.ValueSmoother` (default a one-pole EMA), then
        select ``argmax``. Returns ``(selected, order, smoothed)``. The smoother update + state
        writes run under ``_state_lock`` so a host-thread :meth:`reset_transient` (a BeamEngine
        mode switch) can't race the audio thread's smoother state. The argsort is light
        bookkeeping, not the heavy DSP (which ran lock-free before this)."""
        import numpy as np

        with self._state_lock:
            smoothed = self._selection_tracker.update(score)
            order = np.argsort(smoothed)[::-1]
            selected = int(order[0])
            self._scores = smoothed
            self._selected = selected
        return selected, order, smoothed

    def _mix_output(self, monos: Any, order: Any, ema: Any) -> Any:
        """Top-1 (default) or score-weighted top-k blend of the selected virtual mics."""
        import numpy as np

        if self.top_k <= 1:
            return monos[order[0]].astype(np.float32)
        k = min(self.top_k, monos.shape[0])
        top = order[:k]
        w = np.asarray(ema[top], dtype=float)
        w = w / (w.sum() + 1e-12)                       # NOTE: blending decorrelated points can
        return (w[:, None] * monos[top]).sum(axis=0).astype(np.float32)  # comb-filter — see caveats

    # ---- external-feed seam (BeamEngine drives the DSP from a shared stream) ----
    def _setup_runtime(self) -> None:
        """Allocate the numpy DSP state (cached focus delays, history ring, window,
        score band) — **no device, no validation, no monitor**. Shared by :meth:`_open`
        (standalone) and :meth:`prepare_external` (the shared-stream feed)."""
        import numpy as np

        self._np = np
        assert self.geometry is not None
        # Precompute + cache the focus delays once (the grid is fixed).
        self._delays, self._maxd = build_grid_delays(
            self.geometry, self._points_xyz, self.sample_rate, self.speed_of_sound)
        assert self._maxd == int(self._delays.max())     # guards the gather index bounds
        self._active_cols = self.geometry.active_indices()
        self._hist = np.zeros((self._maxd, self.n_channels), dtype=float)
        self._win = np.hanning(self.nfft).astype(float)
        freqs_full = np.fft.rfftfreq(self.nfft, d=1.0 / self.sample_rate)
        self._score_band_idx = doa.band_indices(freqs_full, self.score_band[0], self.score_band[1])
        self._selection_tracker.reset()
        if self.beam_bandlimit_hz:
            # Same anti-alias FIR as the steered sibling, applied to the *selected* output.
            self._lp_kernel = _lowpass_kernel(float(self.beam_bandlimit_hz), self.sample_rate)
            self._lp_tail = np.zeros(len(self._lp_kernel) - 1, dtype=float)
        else:
            self._lp_kernel = None
            self._lp_tail = None

    def process_block(self, block: Any) -> Any:
        """Run one block of the grid DSP and **return** the mono (does not emit).

        Updates the selection (and `selected_xy`/`scores()`) and level. Used both by
        :meth:`_cb_input` (standalone) and by an external owner (BeamEngine) feeding a
        shared input stream — see :meth:`prepare_external`."""
        np = self._np
        x = np.asarray(block, dtype=float)                   # (B, M)
        B = x.shape[0]
        ext = np.concatenate([self._hist, x], axis=0) if self._maxd else x
        monos = delay_and_sum_grid(ext, self._delays, self._active_cols, self._maxd, B)
        if self._maxd:
            self._hist = ext[-self._maxd:, :].copy()
        score = score_grid(monos, self._win, self._score_band_idx, self.nfft)
        active = self._is_active(score)
        # Snapshot the hold-state atomically — a host-thread reset_transient (BeamEngine switch)
        # can null _last_* mid-block; the None-guard below then re-selects instead of crashing.
        with self._state_lock:
            self._active = active
            held_order, held_ema = self._last_order, self._last_smoothed
            have_selection = self._selected is not None
        if active or not have_selection or held_order is None:   # speak / first block / no held seat
            _selected, order, ema = self._update_selection(score)
            with self._state_lock:
                self._last_order, self._last_smoothed = order, ema
        else:                                                    # silence/noise → HOLD the last seat
            order, ema = held_order, held_ema
        mono = self._mix_output(monos, order, ema)
        if self._lp_kernel is not None:
            mono = self._band_limit(mono)
        rms = float(np.sqrt(np.mean(mono * mono))) if mono.size else 0.0
        self._level = min(1.0, rms)
        return mono

    def _band_limit(self, mono: Any) -> Any:
        """Streaming windowed-sinc low-pass with a history ring (continuous across blocks)."""
        np = self._np
        ext = np.concatenate([self._lp_tail, mono])
        y = np.convolve(ext, self._lp_kernel, mode="valid")     # (len(mono),)
        self._lp_tail = ext[-(len(self._lp_kernel) - 1):]
        return y.astype(np.float32)

    def prepare_external(self) -> None:
        """Ready the DSP to be fed an external shared stream — **no device opened**.
        Pair with :meth:`process_block` per block. Do NOT also call :meth:`start`."""
        if not controls_available():
            raise RuntimeError(_install_hint())
        self._setup_runtime()
        self._streaming = True

    def release_external(self) -> None:
        """Tear down after external feeding (no device, no thread)."""
        self._level = 0.0
        self._streaming = False

    def reset_transient(self) -> None:
        """Wipe per-mode transient state so a re-activated grid doesn't carry a stale selection
        (called on the newly-activated back-end by BeamEngine on switch). The smoother + selection
        + hold state are reset under ``_state_lock`` so this host-thread call can't race the audio
        thread's :meth:`process_block` / :meth:`_update_selection`."""
        with self._state_lock:
            self._selection_tracker.reset()
            self._selected = None
            self._scores = None
            self._active = False
            self._last_order = None
            self._last_smoothed = None
        if self._hist is not None:
            self._hist[...] = 0.0
        if self._lp_kernel is not None and self._np is not None:
            # FIR flush races _band_limit on the audio thread, but benignly: both the old and new
            # tails are valid length-(L-1) arrays, and the BeamEngine crossfade masks a one-block
            # flush miss. (Can't lock _band_limit — that would hold a lock across the convolution.)
            self._lp_tail = self._np.zeros(len(self._lp_kernel) - 1, dtype=float)

    # ---- lifecycle / backend hooks ----
    def _open(self) -> None:
        if not controls_available():
            raise RuntimeError(_install_hint())
        self._validate_input_device()
        import sounddevice as sd
        self._sd = sd
        self._setup_runtime()

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

        try:
            self._stream = sd.InputStream(
                samplerate=self.sample_rate, channels=self.n_channels,
                blocksize=self.blocksize, device=self.device, dtype="float32",
                callback=self._cb_input,
            )
            self._stream.start()
            if self.monitor:
                self._out_stream = sd.OutputStream(
                    samplerate=self.sample_rate, channels=self._out_channels,
                    blocksize=self.blocksize, device=self.output_device, dtype="float32",
                    callback=self._cb_output,
                )
                self._out_stream.start()
        except Exception:
            self._close()
            raise
        self._streaming = True

    def _validate_input_device(self) -> None:
        """Clear errors for a missing device (retryable ValueError), too few channels, or an
        unsupported rate (DeviceConfigError) — before opening the stream."""
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
                raise DeviceConfigError(
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
            raise DeviceConfigError(
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
        self._streaming = False

    def _raw_level(self) -> float:
        return self._level

    # ---- audio thread ----
    def _cb_input(self, indata, frames, time_info, status):  # pragma: no cover (needs hardware)
        self._emit(self.process_block(indata))

    def _emit(self, mono: Any) -> None:
        """Deliver a mono block: drop-oldest into the host queue, the optional realtime
        callback, and the monitor stream. Never blocks the audio thread."""
        self._queue_put(self._output_q, mono)
        if self._output_cb is not None:
            try:
                self._output_cb(mono)        # NOTE: runs on the realtime audio thread — keep it cheap
            except Exception as exc:
                self.error = f"output_callback raised: {exc}"
        if self._monitor_q is not None:
            self._queue_put(self._monitor_q, mono)

    @staticmethod
    def _queue_put(q: "queue.Queue[Any]", item: Any) -> None:
        try:
            q.put_nowait(item)
        except queue.Full:                   # bound latency: drop oldest, keep newest
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
            outdata[:] = blk[:, None]
        else:
            outdata.fill(0.0)


# --------------------------------------------------------------------------- #
# Standalone demo: build the grid, run the audio, print the live selected (x, y).
# --------------------------------------------------------------------------- #
def _demo(argv: Optional[Sequence[str]] = None) -> int:
    import argparse
    import sys
    import time

    ap = argparse.ArgumentParser(
        description="POLARIS virtual-mic-grid selection beamformer (Nureva Mist-style)."
    )
    ap.add_argument("--device", type=int, default=None, help="input device index (omit to list devices)")
    ap.add_argument("--radius", type=float, default=POLARIS_RADIUS_M, help="capsule-circle radius m (POLARIS=0.040)")
    ap.add_argument("--rate", type=float, default=POLARIS_RATE_HZ, help="sample rate (POLARIS=44100)")
    ap.add_argument("--block-ms", type=float, default=DEFAULT_BLOCK_MS, help="audio block size, ms")
    ap.add_argument("--room-width", type=float, default=4.0, help="grid width, m")
    ap.add_argument("--room-depth", type=float, default=3.0, help="grid depth, m")
    ap.add_argument("--cols", type=int, default=13, help="grid columns")
    ap.add_argument("--rows", type=int, default=9, help="grid rows")
    ap.add_argument("--focus-height", type=float, default=0.0, help="focus-plane height above the mics, m")
    ap.add_argument("--dead", type=int, default=-1, help="dead capsule index to mask (-1=none; real board=5)")
    ap.add_argument("--score-lo", type=float, default=DEFAULT_SCORE_LO_HZ, help="score band low, Hz")
    ap.add_argument("--score-hi", type=float, default=DEFAULT_SCORE_HI_HZ, help="score band high, Hz")
    ap.add_argument("--top-k", type=int, default=1, help="blend the top-k virtual mics (1 = pure select)")
    ap.add_argument("--smoothing", type=float, default=0.5, help="selection EMA factor (0..1)")
    ap.add_argument("--vad-floor", type=float, default=DEFAULT_VAD_FLOOR_DB,
                    help="selection VAD peak/median floor dB; hold seat through silence (0=disable)")
    ap.add_argument("--no-bandlimit", action="store_true",
                    help="disable the output band-limit (default: on at the array aliasing cutoff)")
    ap.add_argument("--monitor", action="store_true", help="play the selected mono (use HEADPHONES)")
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
    bl_kw: dict[str, Any] = {"beam_bandlimit_hz": None} if args.no_bandlimit else {}  # else default (ON)
    vmg = VirtualMicGrid(
        device=args.device, radius_m=args.radius, sample_rate=args.rate, block_ms=args.block_ms,
        dead_capsule=dead, room_width_m=args.room_width, room_depth_m=args.room_depth,
        grid_cols=args.cols, grid_rows=args.rows, focus_height_m=args.focus_height,
        score_band=(args.score_lo, args.score_hi), top_k=args.top_k,
        selection_smoothing=args.smoothing, vad_floor_db=args.vad_floor,
        monitor=args.monitor, output_device=args.output_device,
        **bl_kw,
    )
    assert vmg.geometry is not None
    print(f"Grid: {args.cols}×{args.rows} = {vmg.grid_size} virtual mics over "
          f"{args.room_width:.1f}×{args.room_depth:.1f} m at z={args.focus_height:.2f} m.")
    print(f"Array: {vmg.geometry.n_active}/{POLARIS_N_MICS} capsules, aperture "
          f"{vmg.geometry.aperture_m() * 100:.1f} cm.  Aliasing cutoff ~{ALIAS_CUTOFF_HZ / 1000:.1f} kHz "
          f"(soft, coarse-zone selection — not Nureva-scale pinpoint).")
    print(f"Opening device {args.device} @ {args.rate:.0f} Hz ... Ctrl+C to stop.")
    if args.monitor:
        print("Monitoring live — wear HEADPHONES to avoid feedback.")
    try:
        vmg.start()
        while True:
            time.sleep(0.1)
            xy = vmg.selected_xy
            sel = "  --  " if xy is None else f"({xy[0]:+5.2f}, {xy[1]:+5.2f}) m"
            tag = "LIVE" if vmg.speech_active else "HOLD"      # HOLD = VAD silence, seat frozen
            err = f"  !{vmg.error}" if vmg.error else ""
            print(f"\r[{tag}] selected {sel}  | lvl {vmg.read_level():4.2f}{err}    ", end="", flush=True)
    except KeyboardInterrupt:
        print("\nStopping ...")
    finally:
        vmg.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(_demo())
