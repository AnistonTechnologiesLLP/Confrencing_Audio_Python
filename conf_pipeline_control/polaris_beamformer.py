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
  ≤ 3.8 kHz (safe); the beam **output** is low-passed at the aliasing cutoff
  (~5.6 kHz, ``beam_bandlimit_hz``) **by default** — keeping speech up to where the
  array still focuses and dropping the aliased band. Pass ``beam_bandlimit_hz=None``
  to disable.
* **Delay model.** The default delay-and-sum (``mode="delaysum"``) uses integer-sample
  delays (one sample ≈ 7.78 mm of travel at 44.1 kHz vs the 80 mm aperture → up to a few
  degrees of pointing error from the ±0.5-sample rounding). ``mode="fracdelay"`` recovers
  the sub-sample remainder with a short Hann-windowed-sinc fractional-delay FIR per capsule
  (:class:`_FracDelaySumBeam`), tightening off-axis nulls for a flat ~(taps-1)/2-sample of
  common latency. ``mode="superdirective"`` switches to a frequency-domain diffuse-noise MVDR
  beam (:class:`_FreqDomainBeam`) that rejects isotropic background far better than
  delay-and-sum; ``mode="mvdr"`` makes that beam **data-adaptive** — it overlays a *measured*
  noise covariance (accumulated on noise-only frames, gated by :attr:`noise_only`) on the band
  bins to null the actual interferers, falling back to the analytic Γ out of band / cold start.
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
from .tracking import ExponentialTracker, Tracker
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

# Realtime processing bands — one source of truth, shared with the grid back-end:
#   * DOA scan / grid scoring stay ≤ doa.DEFAULT_F_HI_HZ (3.8 kHz), conservatively *below*
#     the aliasing cutoff so grating-lobe phantom peaks can't corrupt direction-finding;
#   * the beam *output* is low-passed at the aliasing cutoff (~5.6 kHz) by default — that
#     keeps speech brightness up to where the array still focuses and drops the higher band
#     where spatial filtering has broken down (off-axis HF leaks through grating lobes).
DEFAULT_BEAM_BANDLIMIT_HZ = ALIAS_CUTOFF_HZ
DEFAULT_BANDLIMIT_TAPS = 63       # Hann-windowed-sinc FIR length (forced odd → linear phase)
DEFAULT_FRACDELAY_TAPS = 15       # fractional-delay FIR length (odd); common latency = (taps-1)/2 ≈ 0.16ms
_STFT_FRAME = DEFAULT_NFFT        # frequency-domain beam STFT length (1024)
_STFT_HOP = _STFT_FRAME // 2      # 50% overlap → COLA-exact Hann overlap-add
DEFAULT_SUPERDIRECTIVE_LOADING = 0.05   # diagonal loading for the superdirective solve (robustness↔DI)

# --- target-loudness AGC (beam OUTPUT level normalization; OFF unless agc_target_db is set) ---
# Pulls the mono output toward a constant loudness so a near vs far talker lands at a consistent
# level. Control-pure: driven by the output RMS only (no room/distance coupling).
DEFAULT_AGC_MAX_GAIN_DB = 18.0    # clamp |applied gain| to ±this (no run-away boost of the noise floor)
DEFAULT_AGC_SLEW_ALPHA = 0.15     # per-block EMA on the gain — slow enough not to pump speech (~0.2s @ 32ms)
DEFAULT_AGC_SILENCE_DB = -55.0    # below this OUTPUT rms, HOLD the gain (don't ramp up near-silence)

# --- post-beam noise suppression (P3; single-channel spectral gate on the mono output; OFF by default) ---
# A LOCAL fallback for when the OCTOVOX cloud cleaning path isn't running: a gentle per-bin Wiener gate
# that learns the noise floor on noise-only frames and attenuates stationary background. Pure numpy.
DEFAULT_POST_NR_FRAME = 512        # STFT frame (hop = frame/2); 512 ≈ 12 ms added latency, decent freq res
DEFAULT_POST_NR_FLOOR_DB = -15.0   # residual gain floor / max suppression depth (never hard-mutes → no musical noise)
DEFAULT_POST_NR_OVERSUB = 1.5      # Wiener over-subtraction (1.0 = plain Wiener; >1 = stronger)
DEFAULT_POST_NR_GAIN_ALPHA = 0.5   # per-bin temporal one-pole on the gain (musical-noise control)
DEFAULT_POST_NR_WARMUP_FRAMES = 16 # gated noise frames before the gate engages (mirror _NOISE_WARMUP_FRAMES)
DEFAULT_POST_NR_NOISE_ALPHA = 0.05 # per-bin noise-floor EMA rate (matches _noise_cov_alpha)
# Minimum-statistics noise floor (DEFAULT): learn the per-bin floor as the running minimum over a sliding
# window, so STEADY noise (fans/AC/HVAC) is suppressed WITHOUT the VAD flagging silence — a steady
# directional source reads as a talker (keeps noise_gate False), so the gated EMA above never trains on it.
# Speech is preserved inherently (it sits ABOVE the per-bin minimum). post_nr_minstat=False ⇒ legacy gate.
DEFAULT_POST_NR_MINSTAT = True       # minimum statistics (VAD-independent) vs the gated EMA
DEFAULT_POST_NR_MINSTAT_SUB = 8      # sliding window = this many sub-windows ...
DEFAULT_POST_NR_MINSTAT_SUBLEN = 16  # ... × this many frames each (8×16 = 128 frames ≈ 0.7 s @ frame 512/256)
DEFAULT_POST_NR_MINSTAT_BIAS = 1.5   # the minimum under-estimates the mean noise power → lift toward it
DEFAULT_POST_NR_POWER_ALPHA = 0.8    # per-bin power-smoothing EMA feeding the minimum tracker

MODE_DELAYSUM = "delaysum"        # integer-sample time-domain delay-and-sum (default)
MODE_FRACDELAY = "fracdelay"      # sub-sample (fractional) delay-and-sum via windowed-sinc FIR
MODE_SUPERDIRECTIVE = "superdirective"  # frequency-domain diffuse-noise MVDR (fixed analytic Γ)
MODE_MVDR = "mvdr"                # frequency-domain data-adaptive MVDR (measured noise covariance)
_BEAM_MODES = (MODE_DELAYSUM, MODE_FRACDELAY, MODE_SUPERDIRECTIVE, MODE_MVDR)
_NOISE_WARMUP_FRAMES = 16         # gated noise frames before the measured covariance feeds the MVDR solve


class DeviceConfigError(ValueError):
    """The audio device is **present but cannot satisfy the request** — wrong input
    channel count or an unsupported sample rate. Distinct from a device that simply
    isn't there yet (a plain ``ValueError``): in ``wait_for_device`` mode the
    supervisor keeps retrying for *presence* but gives up immediately on this,
    because retrying a structural mismatch will never succeed. Subclasses
    ``ValueError`` so existing strict-mode callers catching ``ValueError`` still work.
    """


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
class _TalkerTracker(Tracker):
    """Dominant-talker hold/switch machine. Decouples the *steered* angle from the
    raw per-cycle DOA so the beam doesn't jitter: it holds the committed talker
    through brief silences and only switches when a new direction is more than
    ``switch_margin_deg`` away.

    This is the steered path's **domain tracker** (cf.
    :mod:`conf_pipeline_control.tracking`): it shares the :class:`~conf_pipeline_control.tracking.Tracker`
    lifecycle (``reset()``) but keeps its own richer ``update(observed_az, salience_db, t)``
    contract — arbitrating *which* discrete talker to follow, which a plain EMA on a wrapping
    azimuth can't do (it would smear across switches). For continuous *trajectory* smoothing of
    the committed angle, an :class:`~conf_pipeline_control.tracking.AlphaBetaTracker` (the
    constant-velocity Kalman hook) is the documented swap-in.

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

    def reset(self) -> None:
        """Drop the committed talker (Tracker lifecycle — wiped on a BeamEngine mode switch)."""
        self._az = None
        self._sal = 0.0
        self._last_seen_t = None

    def current(self) -> Optional[float]:
        return self._az


# --------------------------------------------------------------------------- #
# Delay-and-sum steering math (shared by the pure function + streaming strategy).
# --------------------------------------------------------------------------- #
def _steer_real_delays(geom: ArrayGeometry, azimuth_deg: float, off_nadir_deg: float,
                       sample_rate: float, speed_of_sound: float):
    """Real-valued (un-rounded) per-active-capsule steer delays in **samples** (≥ 0).

    For a plane wave from look direction ``u`` (array→source), capsule ``m`` at
    ``p_m`` leads the array centre by ``proj_m = p_m·u`` (a capsule *toward* the
    source is hit earlier). Aligning the wavefronts means delaying each capsule by
    ``(proj_m − min_k proj_k)/c`` — the capsule nearest the source is delayed most,
    so all channels line up on the farthest one. (The opposite sign would steer to
    the mirror azimuth; verified against the ``a_m = exp(+j·k·p·u)`` manifold used
    by :mod:`conf_pipeline_control.beamformer` / :mod:`conf_pipeline_control.doa`.)

    The integer ``_steer_delays`` rounds these; the fractional strategy
    (:class:`_FracDelaySumBeam`) keeps the sub-sample remainder. Returns
    ``(active_indices, delays_samples)``.
    """
    ux, uy, uz = _unit_from_az_offnadir(azimuth_deg, off_nadir_deg)
    idx = geom.active_indices()
    elems = geom.elements
    projs = [elems[m][0] * ux + elems[m][1] * uy + elems[m][2] * uz for m in idx]
    pmin = min(projs) if projs else 0.0
    delays = [(p - pmin) / speed_of_sound * sample_rate for p in projs]
    return idx, delays


def _steer_delays(geom: ArrayGeometry, azimuth_deg: float, off_nadir_deg: float,
                  sample_rate: float, speed_of_sound: float):
    """Integer per-active-capsule delays (samples, ≥ 0) to steer a delay-and-sum
    beam toward ``(azimuth_deg, off_nadir_deg)`` — :func:`_steer_real_delays` rounded.

    Returns ``(active_indices, delays_samples, max_delay)``.
    """
    idx, real = _steer_real_delays(geom, azimuth_deg, off_nadir_deg, sample_rate, speed_of_sound)
    delays = [int(round(d)) for d in real]
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
    """Steerable mono beamformer interface (``set_look`` / ``process`` / ``reset``).

    Three tiers ship behind this contract:

    * :class:`_DelaySumBeam` — time-domain, **integer-sample** delays (``mode="delaysum"``).
    * :class:`_FracDelaySumBeam` — time-domain, **sub-sample** delays via a windowed-sinc
      fractional-delay FIR (``mode="fracdelay"``), removing the integer-rounding pointing error.
    * :class:`_FreqDomainBeam` — frequency-domain per-FFT-bin MVDR weights, in two flavours: fixed
      **superdirective** against the analytic diffuse-field Γ (``mode="superdirective"``), and
      **data-adaptive** against a *measured* noise covariance gated on
      :attr:`PolarisBeamformer.noise_only` (``mode="mvdr"``) — the latter nulls the actual interferers.

    The next seam is **room-aware** steering (constrain the look to known seat coordinates).

    **Re-steer is split into ``plan_look`` + ``commit_look``** so the owner can do the heavy work
    OFF the audio-lock critical section: ``plan_look`` computes a steering plan from immutable state
    only (no shared mutation — safe to call WITHOUT ``_beam_lock``), and ``commit_look`` installs it
    cheaply (the part that must be serialised against :meth:`process`). For the time-domain tiers the
    plan is trivial; for :class:`_FreqDomainBeam` it is the multi-millisecond per-bin solve, which
    therefore never blocks the audio callback. ``set_look`` composes the two for synchronous callers
    (construction, tests, ``fixed_azimuth_deg``) where there is no concurrent ``process``.
    """

    def plan_look(self, azimuth_deg: float, off_nadir_deg: float = DEFAULT_OFF_NADIR_DEG,
                  nulls: Any = ()) -> Any:
        """Compute a steering plan (pure; reads only immutable state). Safe to call un-locked.

        ``nulls`` is an optional iterable of interferer azimuths (deg) to place spatial nulls on.
        Only the frequency-domain tiers honour it (LCMV constraints); the time-domain delay-sum tiers
        have no null degrees of freedom and ignore it."""
        raise NotImplementedError

    def commit_look(self, plan: Any) -> None:
        """Install a plan from :meth:`plan_look` (cheap; serialise against :meth:`process`)."""
        raise NotImplementedError

    def set_look(self, azimuth_deg: float, off_nadir_deg: float = DEFAULT_OFF_NADIR_DEG,
                 nulls: Any = ()) -> None:
        """Plan + commit in one call — for synchronous callers with no concurrent :meth:`process`."""
        self.commit_look(self.plan_look(azimuth_deg, off_nadir_deg, nulls))

    def process(self, block: Any) -> Any:
        raise NotImplementedError

    def reset(self) -> None:
        """Drop streaming history so the next block starts clean (re-steer / re-activate).
        A sub-millisecond transient is acceptable."""
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

    def plan_look(self, azimuth_deg: float, off_nadir_deg: float = DEFAULT_OFF_NADIR_DEG,
                  nulls: Any = ()) -> Any:
        return _steer_delays(self._geom, azimuth_deg, off_nadir_deg, self._sr, self._c)  # nulls: no DOF

    def commit_look(self, plan: Any) -> None:
        self._idx, self._delays, self._maxd = plan
        self.reset()   # a sub-millisecond transient on re-steer is acceptable

    def reset(self) -> None:
        self._hist = None

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


def _frac_delay_kernel(frac: float, numtaps: int = DEFAULT_FRACDELAY_TAPS) -> Any:
    """Hann-windowed-sinc **fractional-delay** FIR — delays by ``(numtaps-1)/2 + frac`` samples.

    Pure numpy (stays inside the ``[control]`` extra). ``frac`` is the sub-sample remainder in
    ``[0, 1)``; the kernel is an ideal-delay sinc centred at ``center + frac`` (``center =
    (taps-1)/2``) tapered by a Hann window and normalized to unity DC gain so the beam level is
    unchanged. ``numtaps`` is forced odd so ``center`` is integral. At ``frac == 0`` it reduces to a
    unit impulse at ``center`` (a pure ``center``-sample integer delay)."""
    import numpy as np

    # Floor at 5, not 3: np.hanning(3) == [0, 1, 0] zeros both edge taps, collapsing the kernel to a
    # unit impulse at centre (zero fractional delay) for any frac. 5 is the shortest length that keeps
    # a real sub-sample response. (DEFAULT_FRACDELAY_TAPS=15 is well clear of this.)
    taps = max(5, int(numtaps) | 1)
    center = (taps - 1) / 2.0
    n = np.arange(taps)
    h = np.sinc(n - center - float(frac)) * np.hanning(taps)  # ideal fractional delay × Hann
    return (h / h.sum()).astype(float)                        # unity DC → no level shift


class _FracDelaySumBeam(BeamStrategy):
    """Streaming delay-and-sum with **sub-sample** (fractional) steer delays.

    Each capsule's real steer delay ``d_m`` (samples) is split into an integer part read from a
    per-channel history ring (exactly as :class:`_DelaySumBeam`) and a fractional remainder applied
    by a short Hann-windowed-sinc fractional-delay FIR (:func:`_frac_delay_kernel`). This removes the
    up-to-±0.5-sample pointing error of integer rounding (one sample ≈ 7.78 mm of travel at 44.1 kHz
    vs the 80 mm aperture). The FIR adds a *common* ``center = (taps-1)/2``-sample latency to **every**
    capsule, so it shifts the whole mono output by ~0.16 ms but does not disturb the inter-capsule
    alignment. HF passband droop is immaterial here — the beam output is band-limited to the array's
    ~5.6 kHz aliasing cutoff anyway. Two persistent buffers keep the per-block convolution continuous
    across block boundaries: the integer ring ``_hist`` and the FIR continuity tail ``_frac_tail``."""

    def __init__(self, geom: ArrayGeometry, sample_rate: float, speed_of_sound: float,
                 *, numtaps: int = DEFAULT_FRACDELAY_TAPS):
        self._geom = geom
        self._sr = float(sample_rate)
        self._c = float(speed_of_sound)
        self._L = max(5, int(numtaps) | 1)               # see _frac_delay_kernel: 3 taps degenerate
        self._idx: tuple = ()
        self._delays_int: list = []
        self._kernels: list = []        # one length-L fractional FIR per active capsule
        self._maxd = 0
        self._hist: Any = None          # integer ring (maxd, M)
        self._frac_tail: Any = None     # FIR continuity tail (L-1, M)
        self.set_look(0.0)

    def plan_look(self, azimuth_deg: float, off_nadir_deg: float = DEFAULT_OFF_NADIR_DEG,
                  nulls: Any = ()) -> Any:                                    # nulls: no DOF in delay-sum
        import numpy as np

        idx, real = _steer_real_delays(self._geom, azimuth_deg, off_nadir_deg, self._sr, self._c)
        di = [int(np.floor(d)) for d in real]
        fr = [d - i for d, i in zip(real, di)]           # sub-sample remainder ∈ [0, 1)
        kernels = [_frac_delay_kernel(f, self._L) for f in fr]
        return idx, di, (max(di) if di else 0), kernels

    def commit_look(self, plan: Any) -> None:
        self._idx, self._delays_int, self._maxd, self._kernels = plan
        self.reset()

    def reset(self) -> None:
        self._hist = None
        self._frac_tail = None

    def process(self, block: Any) -> Any:
        import numpy as np

        x = np.asarray(block, dtype=float)
        n, M = x.shape
        D = self._maxd
        L1 = self._L - 1
        if self._hist is None or self._hist.shape != (D, M):
            self._hist = np.zeros((D, M), dtype=float)
        if self._frac_tail is None or self._frac_tail.shape != (L1, M):
            self._frac_tail = np.zeros((L1, M), dtype=float)
        ext = np.concatenate([self._hist, x], axis=0) if D else x
        out = np.zeros(n, dtype=float)
        new_tail = self._frac_tail.copy()
        for m, d, k in zip(self._idx, self._delays_int, self._kernels):
            start = D - d                                 # integer-aligned read (as _DelaySumBeam)
            aligned = ext[start:start + n, m]             # (n,)
            col = np.concatenate([self._frac_tail[:, m], aligned])      # (L1 + n,)
            out += np.convolve(col, k, mode="valid")      # (n,) fractional-delayed
            new_tail[:, m] = col[-L1:] if L1 else col[:0]              # carry last L1 aligned samples
        out /= max(1, len(self._idx))
        self._frac_tail = new_tail
        if D:
            self._hist = ext[-D:, :].copy()
        return out.astype(np.float32)


# Nulls within this of the look (or of an already-accepted null) are dropped: an LCMV constraint
# coincident with the look would null the target, and near-duplicate constraints make the (small)
# CᴴR⁻¹C system singular. The 40 mm array can't resolve finer than this anyway.
_NULL_LOOK_GUARD_DEG = 5.0


def _az_sep(a: float, b: float) -> float:
    """Wrap-aware absolute azimuth separation in [0, 180] degrees."""
    return abs((a - b + 180.0) % 360.0 - 180.0)


def _acceptable_nulls(nulls: Any, look_deg: float, max_count: int) -> list:
    """Filter requested null azimuths for a well-posed LCMV solve: drop any within
    ``_NULL_LOOK_GUARD_DEG`` of the look (would null the target) or of an already-accepted null
    (near-duplicate constraint), then cap to the ``M−1`` null budget. Returns a list of azimuths (deg),
    in request order."""
    if max_count <= 0:
        return []
    out: list = []
    for phi in nulls:
        phi = float(phi)
        if _az_sep(phi, look_deg) < _NULL_LOOK_GUARD_DEG:
            continue
        if any(_az_sep(phi, q) < _NULL_LOOK_GUARD_DEG for q in out):
            continue
        out.append(phi)
        if len(out) >= max_count:
            break
    return out


# Null-budget arbitration defaults. The look-free margin is conditioning-driven, not cosmetic: a few
# degrees is not enough on a 40 mm planar ring (front/back-ambiguous, coarse), so keep a generous
# margin around the look; the merge width is ~the beam's null width (coincident constraints are one null).
DEFAULT_NULL_MIN_SEP_DEG = 8.0
DEFAULT_NULL_MERGE_SEP_DEG = 6.0   # >= the beam's _NULL_LOOK_GUARD_DEG so the composed set survives it intact


def _near_any(x: float, items: Sequence[float], sep: float) -> bool:
    return any(_az_sep(x, q) < sep for q in items)


def _dedupe_az(items: Sequence[float], sep: float) -> list:
    """Drop near-duplicate bearings (within ``sep``), keeping the first occurrence (input order)."""
    out: list = []
    for x in items:
        if not _near_any(x, out, sep):
            out.append(x)
    return out


def compose_nulls(
    detected: Sequence[float],
    seats: Sequence[float],
    target_az: float,
    budget: int,
    *,
    min_sep_deg: float = DEFAULT_NULL_MIN_SEP_DEG,
    merge_sep_deg: float = DEFAULT_NULL_MERGE_SEP_DEG,
    seat_null_max_count: Optional[int] = None,
) -> list:
    """Merge two competing null sources into one budgeted, deterministic null list for the steered beam.

    ``detected`` are interferer bearings the DOA layer is seeing *right now* (#13 auto-null); ``seats``
    are *speculative* empty-seat bearings (room-aware). **Adaptive evidence beats static geometry:**
    detected nulls fill the ``budget`` (= M−1) first, seat nulls fill only what remains — a measured
    interferer is never crowded out by a speculative seat null. All bearings are array-relative
    azimuths (deg). The single owner of the final null set: both callers feed lists in here, neither
    pushes to the beam directly. Steps: (1) drop nulls within ``min_sep_deg`` of the look from BOTH
    lists *before* budgeting (so a near-look null can't consume budget — and would otherwise make the
    LCMV constraint matrix singular); (2) drop a seat within ``merge_sep_deg`` of a detected null
    (same null, one constraint); (3) fill detected (capped at budget), then seats — ordered
    nearest-to-look first (an empty seat acoustically close to the talker leaks most), optionally
    self-capped by ``seat_null_max_count`` to reserve headroom for live talkers."""
    if budget <= 0:
        return []
    det = _dedupe_az([float(d) for d in detected if _az_sep(float(d), target_az) >= min_sep_deg], merge_sep_deg)
    seat = [float(s) for s in seats if _az_sep(float(s), target_az) >= min_sep_deg]
    seat = [s for s in seat if not _near_any(s, det, merge_sep_deg)]    # cross-source dedupe
    seat = _dedupe_az(seat, merge_sep_deg)
    seat.sort(key=lambda s: _az_sep(s, target_az))                     # deterministic: nearest-to-look first
    if seat_null_max_count is not None:
        seat = seat[:max(0, int(seat_null_max_count))]
    final = list(det[:budget])                                        # detected win the budget
    for s in seat:
        if len(final) >= budget:
            break
        final.append(s)
    return final


class _FreqDomainBeam(BeamStrategy):
    """Frequency-domain **superdirective** (diffuse-noise MVDR) beamformer.

    A windowed overlap-add STFT (``_STFT_FRAME``=1024, ``_STFT_HOP``=512, symmetric Hann, 50%
    overlap → near-COLA, ~0.1% amplitude ripple, well below the array's own spatial errors) with one
    complex weight vector ``W(f)`` per rfft bin. The weights are the superdirective / diffuse-MVDR
    solution ``w = R⁻¹a / (aᴴR⁻¹a)`` with ``R = Γ(f) + loading·I`` and ``Γ_ij = sinc(k·d_ij)`` — the
    isotropic-noise coherence a small array minimises against, so it rejects room/background noise far
    better than delay-and-sum (this mirrors
    :func:`conf_pipeline_control.beamformer.superdirective_weights`, vectorised over bins). With
    ``noise_cov_provider=None`` it is a **fixed analytic** superdirective design
    (``mode="superdirective"``); given a provider it becomes **data-adaptive MVDR** (``mode="mvdr"``),
    overlaying a *measured* noise covariance on the DOA-band bins so it nulls the actual interferers
    (and falling back to the analytic Γ out of band and during cold start).

    **Realtime split:** the per-bin matrix solves (several milliseconds over 513 bins) run only in
    :meth:`plan_look`, which the owner calls **off the audio lock**; :meth:`commit_look` then publishes
    the result by a single atomic array assignment, snapshot once per :meth:`process`. So the audio
    callback is pure multiply-accumulate (``Y = Σ conj(W)·X``) and is never blocked behind a solve.
    An input/output FIFO adapts the caller's block size to the internal 512-hop framing; the round-trip
    latency is ≈ ``_STFT_FRAME + _STFT_HOP`` samples (~35 ms). Weights are full-length (zero on inactive
    capsules), so the MAC ignores a dead one.
    """

    def __init__(self, geom: ArrayGeometry, sample_rate: float, speed_of_sound: float,
                 *, loading: float = DEFAULT_SUPERDIRECTIVE_LOADING,
                 off_nadir_deg: float = DEFAULT_OFF_NADIR_DEG,
                 frame: int = _STFT_FRAME,
                 noise_cov_provider: Optional[Callable[[], Any]] = None):
        self._geom = geom
        self._sr = float(sample_rate)
        self._c = float(speed_of_sound)
        # The STFT frame MUST match the owner's DOA covariance nfft so a measured R's band indices map
        # to the beam's rfft bins (the owner passes frame=self.nfft); HOP = FRAME/2 keeps Hann near-COLA.
        self._F = int(frame)
        self._H = self._F // 2
        # Floor the loading: at exactly 0.0 the DC bin's Γ is the rank-1 all-ones matrix and the
        # solve is singular; 1e-9 is numerically ≈ "no loading" (max directivity) but stays solvable.
        self._loading = max(1e-9, float(loading))
        self._off = float(off_nadir_deg)
        # mode="mvdr": a callable returning ``(noise_cov (n_band, M, M), band_indices)`` or ``None``
        # (cold start). ``None`` provider ⇒ fixed superdirective (mode="superdirective").
        self._noise_cov_provider = noise_cov_provider
        self._W: Any = None            # (nbins, M) complex — published atomically, read by process()
        self._init_state()
        self.set_look(0.0, off_nadir_deg)

    def _init_state(self) -> None:
        import numpy as np

        M = self._geom.n_channels
        self._win = np.hanning(self._F).astype(float)
        self._inbuf = np.zeros((self._F, M), dtype=float)            # sliding STFT input frame
        self._ola = np.zeros(self._F, dtype=float)                   # overlap-add accumulator
        self._inq = np.zeros((0, M), dtype=float)                    # pending input samples (FIFO)
        self._outq = np.zeros(self._F, dtype=float)                  # primed with FRAME zeros = framing latency
        self._freqs = np.fft.rfftfreq(self._F, d=1.0 / self._sr)

    def reset(self) -> None:
        self._init_state()             # drop streaming history; weights (_W) stay valid

    def plan_look(self, azimuth_deg: float, off_nadir_deg: float = DEFAULT_OFF_NADIR_DEG,
                  nulls: Any = ()) -> Any:
        return self._compute_weights(azimuth_deg, off_nadir_deg, nulls)   # heavy solve — call OFF the lock

    def commit_look(self, plan: Any) -> None:
        self._W = plan                                              # atomic publish (single assignment)

    def _compute_weights(self, azimuth_deg: float, off_nadir_deg: float, nulls: Any = ()) -> Any:
        """Per-bin MVDR weights ``w = R⁻¹a / (aᴴR⁻¹a)``, vectorised over rfft bins (multi-ms; off-lock).

        ``R`` is the analytic diffuse-field coherence ``Γ(f) + loading·I`` (superdirective) for every
        bin. In ``mode="mvdr"`` the injected ``noise_cov_provider`` supplies a *measured* noise
        covariance for the DOA band bins, which **overlays** the analytic ``R`` there — so the beam
        nulls the actual interferers while bins outside the band (and the whole cold-start period,
        provider → ``None``) gracefully fall back to fixed superdirective.

        ``nulls`` (azimuths, deg) adds **explicit** spatial nulls on known interferers: the per-bin
        solve becomes LCMV (`w = R⁻¹C (CᴴR⁻¹C)⁻¹ g`, `C = [a(look), a(φ₁)…]`, `g = [1,0,…]`) — unit
        gain at the look, exact zeros at each φ. With no nulls it is the plain MVDR above (the K=0
        special case — bit-identical). The nulls compose with the measured-R overlay, so ``mode="mvdr"``
        can null both the measured interferer field *and* the supplied bearings at once."""
        import numpy as np

        geom = self._geom
        idx = list(geom.active_indices())
        na = len(idx)
        elems = np.array([geom.elements[i] for i in idx], dtype=float)        # (na, 3)
        diff = elems[:, None, :] - elems[None, :, :]
        d = np.sqrt(np.sum(diff * diff, axis=2))                             # (na, na) pairwise distances
        k = 2.0 * np.pi * self._freqs / self._c                             # (nb,) wavenumbers

        def manifold(az: float) -> Any:
            u = np.array(_unit_from_az_offnadir(az, off_nadir_deg), dtype=float)
            return np.exp(1j * k[:, None] * (elems @ u)[None, :])            # (nb, na) a(f) = exp(+jk·proj)

        a = manifold(azimuth_deg)                                            # (nb, na) look manifold
        arg = k[:, None, None] * d[None, :, :]                              # (nb, na, na)
        gamma = np.ones_like(arg)
        nz = arg != 0.0
        gamma[nz] = np.sin(arg[nz]) / arg[nz]                               # Γ_ij = sinc(k·d_ij) (unnormalised sinc)
        R = gamma.astype(complex) + self._loading * np.eye(na)[None, :, :]  # diagonal-loaded; DC → solvable

        snap = self._noise_cov_provider() if self._noise_cov_provider is not None else None
        if snap is not None:                                                # mvdr: overlay measured R on band bins
            noise_cov, band_idx = snap
            rn = np.asarray(noise_cov)[:, idx][:, :, idx]                   # (n_band, na, na) active submatrix
            tr = np.maximum(np.einsum("bii->b", rn).real / na, 1e-20)       # per-bin mean diagonal power
            rn = rn + (self._loading * tr)[:, None, None] * np.eye(na)[None, :, :]   # trace-relative loading
            R[np.asarray(band_idx)] = rn                                    # measured R where we have it

        phis = _acceptable_nulls(nulls, azimuth_deg, na - 1)
        if not phis:                                                        # K=0: plain MVDR (unchanged path)
            rinv_a = np.linalg.solve(R, a[:, :, None])[:, :, 0]            # (nb, na) = R⁻¹a
            denom = np.sum(np.conj(a) * rinv_a, axis=1)                     # (nb,) = aᴴR⁻¹a
            w_active = rinv_a / denom[:, None]                             # (nb, na) MVDR weights
        else:                                                              # K>0: LCMV — unit gain at look, null φ
            C = np.stack([a] + [manifold(p) for p in phis], axis=2)        # (nb, na, 1+K) constraints
            g = np.zeros((C.shape[0], C.shape[2]), dtype=complex)
            g[:, 0] = 1.0                                                  # gain 1 at look, 0 at each null
            rinv_C = np.linalg.solve(R, C)                                # (nb, na, 1+K) = R⁻¹C
            small = np.conj(np.transpose(C, (0, 2, 1))) @ rinv_C          # (nb, 1+K, 1+K) = CᴴR⁻¹C
            # At DC / very low frequency the manifolds collapse (k→0 ⇒ a(any dir)→all-ones), so C goes
            # rank-1 and CᴴR⁻¹C is singular — you cannot null where the array has no phase difference.
            # A tiny trace-relative ridge (diagonally-loaded LCMV) regularises those bins to finite
            # weights; it is negligible in-band, so the null stays exact where it is physically possible.
            ridge = 1e-10 * np.maximum(np.einsum("bii->b", small).real, 1e-30)   # (nb,)
            small = small + ridge[:, None, None] * np.eye(C.shape[2])[None, :, :]
            y = np.linalg.solve(small, g[:, :, None])[:, :, 0]            # (nb, 1+K); column-RHS form
            w_active = np.einsum("bak,bk->ba", rinv_C, y)                 # (nb, na) LCMV weights
        W = np.zeros((len(self._freqs), geom.n_channels), dtype=complex)
        W[:, idx] = w_active                                               # scatter; inactive capsules stay 0
        return W

    def process(self, block: Any) -> Any:
        import numpy as np

        x = np.asarray(block, dtype=float)
        n = x.shape[0]
        W = self._W                                                        # atomic snapshot for this block
        F, H = self._F, self._H
        self._inq = np.concatenate([self._inq, x], axis=0)
        while self._inq.shape[0] >= H:
            hop_in = self._inq[:H]
            self._inq = self._inq[H:]
            self._inbuf[:-H] = self._inbuf[H:]                            # slide frame left by one hop
            self._inbuf[-H:] = hop_in
            X = np.fft.rfft(self._inbuf * self._win[:, None], axis=0)     # (nb, M)
            Y = np.sum(np.conj(W) * X, axis=1)                           # (nb,) pure MAC — no solve here
            y = np.fft.irfft(Y, n=F)                                     # (F,)
            self._ola[:-H] = self._ola[H:]
            self._ola[-H:] = 0.0
            self._ola += y                                              # overlap-add
            self._outq = np.concatenate([self._outq, self._ola[:H].copy()])
        if self._outq.shape[0] >= n:
            out = self._outq[:n]
            self._outq = self._outq[n:]
        else:                                                            # startup underflow only (front-pad)
            out = np.concatenate([np.zeros(n - self._outq.shape[0]), self._outq])
            self._outq = self._outq[:0]
        return out.astype(np.float32)


def _lowpass_kernel(fc_hz: float, fs: float, numtaps: int = DEFAULT_BANDLIMIT_TAPS) -> Any:
    """Hann-windowed-sinc low-pass FIR — linear phase, unity DC gain, **pure numpy**.

    Built by hand (windowed ideal-lowpass) so the realtime path stays inside the
    ``[control]`` extra (numpy only, no scipy). ``numtaps`` is forced odd for an exact
    symmetric kernel; ``fc_hz`` is the cutoff. Used to band-limit the beam output to the
    array's spatial-aliasing cutoff — a real roll-off, unlike a crude moving average."""
    import numpy as np

    taps = max(3, int(numtaps) | 1)                          # odd ≥ 3 → linear phase
    fc = min(0.499, max(1e-3, float(fc_hz) / float(fs)))     # normalized cutoff (cycles/sample)
    n = np.arange(taps) - (taps - 1) / 2.0
    h = 2.0 * fc * np.sinc(2.0 * fc * n) * np.hanning(taps)  # ideal LP × Hann window
    return (h / h.sum()).astype(float)                       # normalize to unity DC gain


class _PostNoiseSuppressor:
    """Light single-channel **post-beam** spectral-gate noise suppressor (pure numpy) — a local fallback
    for when the OCTOVOX cloud cleaning path isn't running. Runs on the beamformed MONO output: a windowed
    overlap-add STFT (Hann, 50% hop, COLA-exact) whose per-bin gain is a gentle **single-pole Wiener**
    against a learned noise floor.

    Floor estimator (``minstat``, default ON): **minimum statistics** — the per-bin floor is the running
    minimum of the smoothed power over a sliding window (``minstat_sub`` × ``minstat_sublen`` frames),
    lifted by ``minstat_bias``. This learns STEADY noise (fans/AC/HVAC) **continuously, without a VAD**, and
    preserves speech inherently (speech sits ABOVE the per-bin minimum). ``minstat=False`` falls back to the
    legacy gated EMA that learns ONLY on noise-only frames (the SRP VAD's ``noise_gate``) — which can't
    train on a steady DIRECTIONAL source the VAD mistakes for a talker.

    Gentle by construction — no musical noise: ``G = g_floor + (1−g_floor)·P/(P + oversub·N²)`` is
    smooth and monotonic, bounded in ``[g_floor, 1]`` so it never hard-mutes; a 3-tap frequency smooth
    plus a per-bin temporal one-pole on the gain kill bin flicker.

    Warmup contract: until ``warmup_frames`` gated frames have been seen the floor isn't trusted, so the
    gate is **bypassed** — :meth:`process` returns the input block **byte-identical** (the STFT/OLA is not
    engaged, so there is no reconstruction ripple or latency during warmup). When it engages there is a
    one-time ≈one-frame transition seam (during noise, harmless — documented, like the crossfade race).

    Threading: :meth:`process` runs on the audio thread; :meth:`reset` on the control thread (a
    BeamEngine ``set_mode`` → ``reset_transient`` can overlap an in-flight ``process`` of the same
    steered back-end during a switch-back crossfade). The output LENGTH is derived from multiple reads of
    the ``_outq`` FIFO, so — unlike the fixed-length ``_lp_tail`` — a torn read could emit a wrong-length
    block (which would crash the BeamEngine crossfade mix). A small internal lock serializes ``process``
    vs ``reset`` to make that safe. Adds ``frame``-ish samples of latency once engaged (~12 ms at frame
    512); stacks on the freq-domain beam's ~35 ms. Knobs: ``floor_db`` (suppression depth, capped at 0 dB
    so the floor never boosts), ``oversub`` (strength), ``gain_alpha`` (temporal smoothing),
    ``warmup_frames``, ``noise_alpha`` (floor EMA rate)."""

    def __init__(self, sample_rate: float, *, frame: int = DEFAULT_POST_NR_FRAME,
                 floor_db: float = DEFAULT_POST_NR_FLOOR_DB, oversub: float = DEFAULT_POST_NR_OVERSUB,
                 gain_alpha: float = DEFAULT_POST_NR_GAIN_ALPHA,
                 warmup_frames: int = DEFAULT_POST_NR_WARMUP_FRAMES,
                 noise_alpha: float = DEFAULT_POST_NR_NOISE_ALPHA,
                 minstat: bool = DEFAULT_POST_NR_MINSTAT,
                 minstat_sub: int = DEFAULT_POST_NR_MINSTAT_SUB,
                 minstat_sublen: int = DEFAULT_POST_NR_MINSTAT_SUBLEN,
                 minstat_bias: float = DEFAULT_POST_NR_MINSTAT_BIAS,
                 power_alpha: float = DEFAULT_POST_NR_POWER_ALPHA):
        self._sr = float(sample_rate)
        self._F = max(2, (int(frame) // 2) * 2)                 # even ≥ 2 (Hann 50%-hop COLA needs even frame)
        self._H = self._F // 2
        self._g_floor = min(1.0, 10.0 ** (float(floor_db) / 20.0))   # ≤ 1: a floor caps suppression, never boosts
        self._oversub = max(0.0, float(oversub))
        self._gain_alpha = min(1.0, max(0.0, float(gain_alpha)))
        self._warmup = max(0, int(warmup_frames))
        self._noise_alpha = min(1.0, max(0.0, float(noise_alpha)))
        self._minstat = bool(minstat)
        self._minstat_sub = max(1, int(minstat_sub))
        self._minstat_sublen = max(1, int(minstat_sublen))
        self._minstat_bias = max(1.0, float(minstat_bias))
        self._power_alpha = min(1.0, max(0.0, float(power_alpha)))
        self._lock = threading.Lock()          # serialize process() (audio thread) vs reset() (control thread)
        self._init_state()

    def _init_state(self) -> None:
        import numpy as np

        nb = self._F // 2 + 1
        self._win = np.hanning(self._F).astype(float)
        self._inbuf = np.zeros(self._F, dtype=float)            # sliding STFT analysis frame
        self._ola = np.zeros(self._F, dtype=float)              # overlap-add synthesis accumulator
        self._inq = np.zeros(0, dtype=float)                    # pending input samples (FIFO)
        self._outq = np.zeros(self._F, dtype=float)             # synthesized output (FIFO; primed = fixed latency)
        self._noise_mag = np.zeros(nb, dtype=float)            # per-bin noise-floor magnitude (EMA or min-stat)
        self._gain_prev = np.ones(nb, dtype=float)             # per-bin gain (temporal smoothing state)
        self._freqs = np.fft.rfftfreq(self._F, d=1.0 / self._sr)
        self._noise_frames = 0                                 # gated frames seen (legacy-EMA warmup counter)
        self._engaged = False                                  # True once the floor is trusted
        # minimum-statistics state: a sliding window of per-bin power minima (VAD-independent floor)
        self._total_frames = 0                                 # all frames seen (min-stat warmup counter)
        self._p_smooth = np.zeros(nb, dtype=float)             # per-bin smoothed |X|² feeding the min tracker
        self._submin = np.full(nb, np.inf, dtype=float)        # current sub-window running minimum
        self._minbuf = np.full((self._minstat_sub, nb), np.inf, dtype=float)  # completed sub-window minima
        self._sub_frame = 0                                    # frame index within the current sub-window
        self._sub_idx = 0                                      # circular write index into _minbuf

    def reset(self) -> None:
        with self._lock:               # serialize vs an in-flight process() on the audio thread
            self._init_state()         # drop streaming + floor history; re-warms on the next gated frames

    def _gain(self, X: Any) -> Any:
        """Per-bin Wiener gain lifted to the floor, frequency- then temporally-smoothed."""
        import numpy as np

        P = X.real * X.real + X.imag * X.imag                  # |X|² instantaneous power
        n2 = self._noise_mag * self._noise_mag                 # noise power estimate
        wiener = P / (P + self._oversub * n2 + 1e-20)          # smooth, monotonic, in (0,1]
        g = self._g_floor + (1.0 - self._g_floor) * wiener     # lift to [g_floor, 1] — never hard-mutes
        gs = g.copy()
        gs[1:-1] = 0.25 * g[:-2] + 0.5 * g[1:-1] + 0.25 * g[2:]  # 3-tap frequency smooth (edges replicate)
        gs = self._gain_alpha * gs + (1.0 - self._gain_alpha) * self._gain_prev   # temporal one-pole
        self._gain_prev = gs
        return gs

    def process(self, block: Any, noise_gate: bool) -> Any:
        """One mono block in → one mono block out (same length). ``noise_gate`` True ⇒ a noise-only
        frame (feeds the floor). Byte-identical passthrough until ``warmup_frames`` gated frames seen.
        Holds the lock so a concurrent :meth:`reset` can't tear the FIFO length derivation."""
        import numpy as np

        with self._lock:
            x = np.asarray(block, dtype=float)
            n = x.shape[0]
            F, H = self._F, self._H
            self._inq = np.concatenate([self._inq, x])
            while self._inq.shape[0] >= H:
                hop_in = self._inq[:H]
                self._inq = self._inq[H:]
                self._inbuf[:-H] = self._inbuf[H:]             # slide analysis frame left by one hop
                self._inbuf[-H:] = hop_in
                X = np.fft.rfft(self._inbuf * self._win)       # (nb,) complex
                if self._minstat:                               # min-statistics floor — runs EVERY frame (no VAD)
                    P = X.real * X.real + X.imag * X.imag       # |X|² per bin
                    if not self._total_frames:                  # seed the EMA with the first power so the minimum
                        self._p_smooth = P.copy()               # tracker doesn't latch a cold-start 0-ramp under-floor
                    else:
                        self._p_smooth = self._power_alpha * self._p_smooth + (1.0 - self._power_alpha) * P
                    self._submin = np.minimum(self._submin, self._p_smooth)
                    self._sub_frame += 1
                    if self._sub_frame >= self._minstat_sublen:   # close the sub-window, start a fresh one
                        self._minbuf[self._sub_idx] = self._submin
                        self._sub_idx = (self._sub_idx + 1) % self._minstat_sub
                        self._submin = self._p_smooth.copy()
                        self._sub_frame = 0
                    p_min = np.minimum(self._submin, self._minbuf.min(axis=0))   # window minimum (finite after 1 frame)
                    self._noise_mag = np.sqrt(self._minstat_bias * p_min)        # → _gain squares it (n2 = noise_mag²)
                    self._total_frames += 1
                    if not self._engaged and self._total_frames >= self._warmup:
                        self._engaged = True                   # enough frames seen → start suppressing (no VAD needed)
                elif noise_gate:                                # legacy: learn the floor on noise-only frames only
                    a = self._noise_alpha
                    self._noise_mag *= (1.0 - a)
                    self._noise_mag += a * np.abs(X)           # symmetric per-bin EMA
                    if not self._engaged:
                        self._noise_frames += 1
                        if self._noise_frames >= self._warmup:
                            self._engaged = True               # floor trusted → start suppressing
                if self._engaged:
                    Y = self._gain(X) * X
                    y = np.fft.irfft(Y, n=F)
                    self._ola[:-H] = self._ola[H:]
                    self._ola[-H:] = 0.0
                    self._ola += y                             # overlap-add (Hann COLA preserves amplitude)
                    self._outq = np.concatenate([self._outq, self._ola[:H].copy()])
            if not self._engaged:
                return x.astype(np.float32)                    # WARMUP: byte-identical passthrough (no STFT delay)
            if self._outq.shape[0] >= n:                       # ENGAGED: drain the synthesis FIFO
                out = self._outq[:n]
                self._outq = self._outq[n:]
            else:                                              # one-time underflow at engagement (front-pad)
                out = np.concatenate([np.zeros(n - self._outq.shape[0]), self._outq])
                self._outq = self._outq[:0]
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

    By default the array must be present at ``start()`` (a clear error otherwise).
    With ``wait_for_device=True`` a supervisor thread instead waits for the array to
    appear and **auto-reconnects** if it drops mid-session; check :attr:`streaming`
    and ``error`` for status. Beamforming is continuous the whole time the stream is
    live — it does not stop after the first detection.
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
        superdirective_loading: float = DEFAULT_SUPERDIRECTIVE_LOADING,   # mode="superdirective" only
        steer_to_doa: bool = True,
        auto_null: bool = False,                   # null detected interferers (freq-domain modes only)
        auto_null_max: int = 2,                    # max auto-derived interferer nulls per tick
        null_min_sep_deg: float = DEFAULT_NULL_MIN_SEP_DEG,      # arbitration: look-free margin
        null_merge_sep_deg: float = DEFAULT_NULL_MERGE_SEP_DEG,  # arbitration: cross-source dedupe width
        seat_null_max_count: Optional[int] = None,              # cap seat nulls (reserve detected headroom)
        fixed_azimuth_deg: Optional[float] = None,
        beam_bandlimit_hz: Optional[float] = DEFAULT_BEAM_BANDLIMIT_HZ,   # None/0 disables
        agc_target_db: Optional[float] = None,                           # target output RMS (dBFS); None = AGC off
        agc_max_gain_db: float = DEFAULT_AGC_MAX_GAIN_DB,                 # clamp |applied AGC gain| to ±this
        agc_slew_alpha: float = DEFAULT_AGC_SLEW_ALPHA,                   # per-block EMA slew on the gain
        agc_silence_db: float = DEFAULT_AGC_SILENCE_DB,                   # hold gain below this output RMS
        post_nr: bool = False,                                           # post-beam spectral-gate NR (local fallback)
        post_nr_floor_db: float = DEFAULT_POST_NR_FLOOR_DB,              # NR residual floor / suppression depth
        post_nr_oversub: float = DEFAULT_POST_NR_OVERSUB,               # NR Wiener over-subtraction strength
        post_nr_gain_alpha: float = DEFAULT_POST_NR_GAIN_ALPHA,         # NR temporal gain smoothing
        post_nr_frame: int = DEFAULT_POST_NR_FRAME,                     # NR STFT frame (latency ↔ freq res)
        post_nr_warmup_frames: int = DEFAULT_POST_NR_WARMUP_FRAMES,     # frames before NR engages
        post_nr_minstat: bool = DEFAULT_POST_NR_MINSTAT,                # min-statistics floor (VAD-free) vs gated EMA
        output_callback: Optional[Callable[[Any], None]] = None,
        output_queue_size: int = 8,
        output_device: Optional[int] = None,
        monitor: bool = False,
        wait_for_device: bool = False,
        reconnect_interval_s: float = 1.0,
        device_stall_timeout_s: float = 2.0,
        device_timeout_s: Optional[float] = None,
    ):
        mask = _resolve_active_mask(active_mask, dead_capsule)
        geom = sensibel_8(radius_m=radius_m)
        if mask is not None:
            geom = with_active_channels(geom, mask)   # validates length / non-empty
        super().__init__(geom, n_channels=POLARIS_N_MICS)

        if mode not in _BEAM_MODES:
            raise ValueError(
                f"mode {mode!r} not implemented; choose one of {_BEAM_MODES} "
                "(data-adaptive MVDR is the next documented BeamStrategy seam, on the same STFT plumbing)"
            )
        self.mode = mode
        self.superdirective_loading = float(superdirective_loading)
        # mode="mvdr": gated noise covariance (allocated in _setup_runtime), set up here so the
        # provider injected into the strategy at _make_beam() can read them safely at construction.
        self._noise_cov: Any = None          # (n_band, M, M) EMA, gated on noise-only frames
        self._noise_frames = 0               # gated frames accumulated (warmup gate)
        # latest "noise-only" flag: the DOA thread writes (= not reading.active), the audio thread reads
        # lock-free. Starts FALSE (unknown ⇒ don't train) so neither the MVDR noise cov NOR the post-beam
        # NR floor learns from cold-start audio until the DOA thread confirms a noise-only frame.
        self._noise_gate = False
        self._noise_cov_alpha = 0.05

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
        # auto-null: feed detected interferer bearings (and any caller-supplied set_nulls bearings)
        # to the freq-domain beam as LCMV nulls. No-op for the time-domain modes (no null DOF).
        self.auto_null = bool(auto_null)
        self.auto_null_max = max(0, int(auto_null_max))
        self.null_min_sep_deg = float(null_min_sep_deg)
        self.null_merge_sep_deg = float(null_merge_sep_deg)
        self.seat_null_max_count = seat_null_max_count
        self._switch_margin_deg = float(switch_margin_deg)   # tracker hysteresis; bounds talker-exclusion
        self._explicit_nulls: list = []      # caller-supplied seat/manual bearings (set_nulls); DOA reads
        self._active_nulls: list = []        # bearings actually nulled this tick (telemetry)
        self.beam_bandlimit_hz = beam_bandlimit_hz
        # target-loudness AGC on the beam OUTPUT (control-pure: driven by output RMS, NOT distance/room).
        # OFF unless agc_target_db is set; sits below the user's set_gain_db (metering-only here), so manual
        # gain still trims on top. The applied gain is one scalar/block, EMA-slewed so it ramps (no pumping)
        # and clamped to ±agc_max_gain_db; near-silence HOLDS the gain so pauses don't amplify the floor.
        self.agc_target_db = agc_target_db
        self._agc_target_rms: Optional[float] = (
            10.0 ** (float(agc_target_db) / 20.0) if agc_target_db is not None else None)
        self._agc_gain_max = 10.0 ** (float(agc_max_gain_db) / 20.0)
        self._agc_gain_min = 10.0 ** (-float(agc_max_gain_db) / 20.0)
        self._agc_silence_rms = 10.0 ** (float(agc_silence_db) / 20.0)
        self._agc_alpha = float(agc_slew_alpha)
        # scalar EMA slewing the applied gain (pure stdlib). Rebound atomically in reset_transient,
        # mirroring _tracker, since the audio thread mutates it lock-free.
        self._agc_gain: Optional[ExponentialTracker] = (
            ExponentialTracker(self._agc_alpha) if self._agc_target_rms is not None else None)
        # post-beam noise suppression (P3): a light single-channel spectral gate on the mono output, a
        # LOCAL fallback for when the OCTOVOX cloud cleaning path isn't running. OFF unless post_nr; the
        # suppressor object is built in _setup_runtime (needs numpy) and reset in reset_transient.
        self.post_nr = bool(post_nr)
        self._post_nr_floor_db = float(post_nr_floor_db)
        self._post_nr_oversub = float(post_nr_oversub)
        self._post_nr_gain_alpha = float(post_nr_gain_alpha)
        self._post_nr_frame = int(post_nr_frame)
        self._post_nr_warmup_frames = int(post_nr_warmup_frames)
        self._post_nr_minstat = bool(post_nr_minstat)
        self._post_nr: Optional[_PostNoiseSuppressor] = None
        self.output_device = output_device
        self.monitor = monitor
        # device supervision (opt-in): wait for the array to appear, auto-reconnect if it drops
        self.wait_for_device = bool(wait_for_device)
        self.reconnect_interval_s = max(0.05, float(reconnect_interval_s))
        self.device_stall_timeout_s = max(0.1, float(device_stall_timeout_s))
        self.device_timeout_s = device_timeout_s

        self._tracker = _TalkerTracker(hold_seconds=hold_seconds, switch_margin_deg=switch_margin_deg)
        self._beam = self._make_beam(geom)
        self._steered_az: Optional[float] = None
        # Steering generation: bumped under _beam_lock whenever the steering INTENT changes (set_steering).
        # _doa_tick snapshots it before its off-lock solve and only commits if it's unchanged — so a stale
        # DOA-tick commit can't clobber a concurrently-applied seat lock (lost-update guard).
        self._steer_gen = 0
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
        self._supervisor_thread: Optional[threading.Thread] = None
        self._streaming = False                          # is the input stream currently open + live?
        self._device_fatal = False                       # supervisor hit a structural device error → gave up
        self._last_block_monotonic: Optional[float] = None  # watchdog: last audio block arrival
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

    def _make_beam(self, geom: ArrayGeometry) -> BeamStrategy:
        """Build the steering strategy for the configured ``mode``."""
        if self.mode in (MODE_SUPERDIRECTIVE, MODE_MVDR):
            provider = self._noise_cov_snapshot if self.mode == MODE_MVDR else None
            return _FreqDomainBeam(geom, self.sample_rate, self.speed_of_sound,
                                   loading=self.superdirective_loading, off_nadir_deg=self.off_nadir_deg,
                                   frame=self.nfft,   # share the DOA covariance bin grid (mvdr overlay alignment)
                                   noise_cov_provider=provider)
        if self.mode == MODE_FRACDELAY:
            return _FracDelaySumBeam(geom, self.sample_rate, self.speed_of_sound)
        return _DelaySumBeam(geom, self.sample_rate, self.speed_of_sound)

    def _noise_cov_snapshot(self) -> Any:
        """Provider for the MVDR strategy: a thread-safe ``(noise_cov, band_indices)`` snapshot, or
        ``None`` until the gated noise covariance has warmed up (cold start → analytic fallback)."""
        # Lock-free fast-out: these attrs exist before _cov_lock (set early in __init__ so the
        # provider is safe at construction, before _setup_runtime). Take the lock only to copy.
        if self._noise_cov is None or self._noise_frames < _NOISE_WARMUP_FRAMES:
            return None
        with self._cov_lock:
            if self._noise_cov is None:                  # re-check: a concurrent reset may have cleared it
                return None
            return self._noise_cov.copy(), self._cov_band

    # ---- public API ----
    def start(self) -> None:
        """Open the array and start DOA tracking + beam steering.

        With ``wait_for_device=False`` (default) the device must be present now —
        this raises a clear error otherwise. With ``wait_for_device=True`` it
        returns immediately and a supervisor thread opens the array as soon as it
        appears (and reopens it if it drops); poll :attr:`streaming` / ``error``.
        """
        self._stop.clear()
        self._device_fatal = False
        self.connect()                                  # _open(): open now, or hand off to the supervisor
        if self._doa_thread is None:
            self._doa_thread = threading.Thread(target=self._doa_loop, name="polaris-doa", daemon=True)
            self._doa_thread.start()

    def stop(self) -> None:
        """Stop DOA tracking + supervision and close the array (idempotent)."""
        self._stop.set()
        for attr in ("_doa_thread", "_supervisor_thread"):
            th = getattr(self, attr)
            if th is not None:
                th.join(timeout=2.0)
                setattr(self, attr, None)
        self.disconnect()

    @property
    def streaming(self) -> bool:
        """True while the input stream is open and delivering audio (False while
        waiting for / reconnecting the device in ``wait_for_device`` mode)."""
        return self._streaming

    @property
    def device_fatal(self) -> bool:
        """True if the supervisor gave up on a structural device error (wrong channel
        count / unsupported rate) — vs. still waiting for the device to appear. See
        ``error`` for the reason; ``start()`` clears it on the next attempt."""
        return self._device_fatal

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

    @property
    def noise_only(self) -> bool:
        """"Nobody is talking" flag — the inverse of the latest DOA-cycle SRP VAD
        (:attr:`DoaReading.active`). In ``mode="mvdr"`` this gates the noise-covariance EMA (via the
        internal ``_noise_gate`` cache) so the measured ``R`` captures the noise/interference field,
        not the talker; also exposed for parity with the grid back-end.
        Note: updated at the DOA rate (~10 Hz), not per audio block."""
        with self._state_lock:
            return not self._reading.active

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
        following the tracked talker when ``None``. Bumps the steering generation under ``_beam_lock`` so
        an in-flight DOA tick's stale commit can't clobber this (lost-update guard)."""
        if azimuth_deg is None:
            self.steer_to_doa = True
            with self._beam_lock:
                self._steer_gen += 1          # invalidate any in-flight DOA-tick commit built for the lock
            return
        self.steer_to_doa = False
        nulls = self._compose_nulls([], float(azimuth_deg))            # caller-supplied seat/manual nulls
        plan = self._beam.plan_look(float(azimuth_deg), self.off_nadir_deg, nulls)   # off the lock
        with self._beam_lock:
            self._steer_gen += 1              # this is the newest steering intent — DOA ticks must yield
            self._beam.commit_look(plan)
            self._steered_az = float(azimuth_deg)
        self._publish_active_nulls(nulls, float(azimuth_deg))          # telemetry = nulls actually applied

    def set_nulls(self, bearings: Optional[Sequence[float]] = None) -> None:
        """Set explicit interferer bearings (deg) to null — e.g. from the room-aware layer (non-target
        seats) or a manual control — applied on the next DOA tick. Frequency-domain modes only (the
        time-domain tiers have no null degrees of freedom). ``None``/``[]`` clears them."""
        self._explicit_nulls = [float(b) for b in (bearings or [])]

    @property
    def active_nulls(self) -> list:
        """The interferer bearings the beam is currently nulling (for display / telemetry)."""
        with self._state_lock:
            return list(self._active_nulls)

    def _freq_domain(self) -> bool:
        """The beam mode supports nulls (LCMV) only in the frequency-domain tiers."""
        return self.mode in (MODE_SUPERDIRECTIVE, MODE_MVDR)

    def _nulls_engaged(self) -> bool:
        """True when nulls can change tick-to-tick, so the look must be re-planned every tick."""
        return self._freq_domain() and (self.auto_null or bool(self._explicit_nulls))

    def _compose_nulls(self, dets: list, look_az: Optional[float]) -> list:
        """Final null set for this look (frequency-domain modes only, else empty). Routes BOTH null
        sources through the single :func:`compose_nulls` arbiter: detected interferers (``auto_null``)
        take priority for the M−1 budget; the caller-supplied seat/manual ``set_nulls`` bearings fill
        what remains. All dedupe / budget / ordering happens once, in the composer.

        Detected sources are pre-filtered to **exclude the tracked talker**: its raw SRP detection can
        sit up to ``switch_margin_deg`` from the COMMITTED look (the tracker holds the look until a move
        exceeds that margin), so drop detections within ``max(null_min_sep_deg, switch_margin)`` of the
        look — otherwise the beam would null the very source it is following. A genuine interferer is
        ``>= min_separation_deg`` away, so it survives. (Seats use the smaller ``null_min_sep_deg``
        conditioning margin in the composer; a non-target seat near the look is not the talker.)"""
        if not self._freq_domain() or look_az is None or self.geometry is None:
            return []
        detected: list = []
        if self.auto_null:
            talker_guard = max(self.null_min_sep_deg, self._switch_margin_deg)
            detected = [d.azimuth_deg for d in dets if _az_sep(d.azimuth_deg, look_az) >= talker_guard]
        return compose_nulls(
            detected, list(self._explicit_nulls), look_az, self.geometry.n_active - 1,
            min_sep_deg=self.null_min_sep_deg, merge_sep_deg=self.null_merge_sep_deg,
            seat_null_max_count=self.seat_null_max_count,
        )

    def _publish_active_nulls(self, nulls: list, look_az: Optional[float]) -> None:
        """Record the nulls the beam ACTUALLY applies (telemetry). Runs the *same* `_acceptable_nulls`
        filter the LCMV solve uses (drop within 5° of the look / near-duplicates, cap to the M−1
        budget), so `active_nulls` matches the committed beam rather than the raw request."""
        if look_az is not None and self.geometry is not None:
            applied = _acceptable_nulls(nulls, look_az, self.geometry.n_active - 1)
        else:
            applied = []
        with self._state_lock:
            self._active_nulls = applied

    # ---- DOA detection (pure-ish; covariance in, dominant azimuth out) ----
    def _detect_dominant(self, cov: Any, freqs: Any):
        """Run SRP-PHAT on a band covariance and return
        ``(dominant_az or None, salience_db, detections)``. ``cov`` must be the
        full ``(n_band, M, M)`` over **all** capsules — :func:`doa.detect` slices to
        the active ones via the geometry mask; pre-slicing mis-maps azimuth. With ``auto_null`` we
        ask for more peaks (dominant + interferers) so the secondary sources can be nulled."""
        assert self.geometry is not None
        res = doa.detect(
            cov, freqs, self.geometry,
            off_nadir_deg=self.off_nadir_deg,
            grid_step_deg=self.grid_step_deg,
            max_talkers=(1 + self.auto_null_max) if self.auto_null else 1,   # dominant (+ interferers)
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
        self._noise_gate = not reading.active     # gate the MVDR noise covariance (audio thread reads this)
        # Re-steer when the talker moves; in mvdr, or whenever nulls are engaged, re-solve EVERY tick so
        # the null tracks the evolving noise field / appearing-or-clearing interferers even when the
        # committed look angle is unchanged. The heavy solve stays off the audio lock.
        gen0 = self._steer_gen                          # snapshot steering intent before the off-lock solve
        target_az = reading.azimuth_deg if (self.steer_to_doa and reading.azimuth_deg is not None) \
            else self._steered_az
        nulls = self._compose_nulls(dets, target_az) if target_az is not None else []
        if target_az is not None and (
                target_az != self._steered_az or self.mode == MODE_MVDR or self._nulls_engaged()):
            plan = self._beam.plan_look(target_az, self.off_nadir_deg, nulls)   # heavy work off the lock
            with self._beam_lock:
                if self._steer_gen == gen0:             # no set_steering interleaved → safe to commit
                    self._beam.commit_look(plan)
                    self._steered_az = target_az
        self._publish_active_nulls(nulls, target_az)   # telemetry = the nulls actually applied

    def _snapshot_covariance(self):
        with self._cov_lock:
            if self._cov is None:
                return None, None
            return self._cov.copy(), self._cov_freqs

    # ---- lifecycle / backend hooks ----
    def _open(self) -> None:
        # A missing [control] extra is fatal in any mode (not "the device will appear").
        if not controls_available():
            raise RuntimeError(_install_hint())
        if self.wait_for_device:
            self._start_supervisor()      # supervisor (re)opens the stream as the device comes/goes
        else:
            self._open_stream()           # strict: open now, raise on any device error

    def _start_supervisor(self) -> None:
        if self._supervisor_thread is None:
            self._supervisor_thread = threading.Thread(
                target=self._supervise, name="polaris-supervisor", daemon=True)
            self._supervisor_thread.start()

    def _supervise(self) -> None:  # pragma: no cover (timing/thread)
        """Keep the input stream alive: open it once the device appears, and reopen
        it if it stalls/drops. ``device_timeout_s`` bounds only the FIRST appearance;
        reconnects after that are unbounded. Errors surface via ``self.error``."""
        waited = 0.0
        connected_once = False
        while not self._stop.is_set() and not self._device_fatal:
            if (not self._streaming and not connected_once and self.device_timeout_s is not None
                    and waited > self.device_timeout_s):
                self.error = f"device did not appear within {self.device_timeout_s:.0f}s"
                break
            self._supervise_once(time.monotonic())
            if self._streaming:
                connected_once = True
            interval = 0.25 if self._streaming else self.reconnect_interval_s
            waited += interval
            self._stop.wait(interval)
        self._close_stream()

    def _supervise_once(self, now: float) -> None:
        """One supervision step (thread/sleep-free, for testing): (re)open the stream
        when it is down, or reconnect it when the audio watchdog goes stale."""
        if not self._streaming:
            try:
                self._open_stream()
                self.error = ""
            except DeviceConfigError as exc:  # present but structurally wrong → give up, don't spin
                self.error = str(exc)
                self._device_fatal = True
            except Exception as exc:          # device not present/ready yet → retry next tick
                self.error = str(exc)
            return
        last = self._last_block_monotonic
        if last is not None and (now - last) > self.device_stall_timeout_s:
            self.error = "input stream stalled; reconnecting"
            self._close_stream()

    def _setup_runtime(self) -> None:
        """Allocate the numpy DSP state (Hann window, STFT buffer, band covariance,
        optional band-limit kernel) — **no device, no validation, no monitor**. Shared
        by :meth:`_open_stream` (standalone) and :meth:`prepare_external` (the shared-
        stream feed used by BeamEngine)."""
        import numpy as np

        self._np = np
        self._win = np.hanning(self.nfft).astype(float)
        self._stftbuf = np.zeros((0, self.n_channels), dtype=float)
        freqs_full = np.fft.rfftfreq(self.nfft, d=1.0 / self.sample_rate)
        self._cov_band = doa.band_indices(freqs_full, doa.DEFAULT_F_LO_HZ, doa.DEFAULT_F_HI_HZ)
        self._cov_freqs = freqs_full[self._cov_band]
        with self._cov_lock:
            self._cov = np.zeros((len(self._cov_band), self.n_channels, self.n_channels), dtype=complex)
            if self.mode == MODE_MVDR:                          # gated noise covariance for the MVDR solve
                self._noise_cov = np.zeros_like(self._cov)
        self._noise_frames = 0
        if self.beam_bandlimit_hz:
            # Anti-alias the beam output: a Hann-windowed-sinc low-pass at the band-limit
            # (default = the array's ~5.6 kHz spatial-aliasing cutoff). Dependency-free
            # (numpy only); pass beam_bandlimit_hz=None (or 0) to disable. The streaming
            # history ring (_lp_tail) keeps the per-block convolution continuous across
            # block boundaries (no edge click).
            self._lp_kernel = _lowpass_kernel(float(self.beam_bandlimit_hz), self.sample_rate)
            self._lp_tail = np.zeros(len(self._lp_kernel) - 1, dtype=float)
        else:
            self._lp_kernel = None
            self._lp_tail = None
        # post-beam noise suppressor (P3): built only when enabled (needs numpy); fresh per session.
        self._post_nr = _PostNoiseSuppressor(
            self.sample_rate, frame=self._post_nr_frame, floor_db=self._post_nr_floor_db,
            oversub=self._post_nr_oversub, gain_alpha=self._post_nr_gain_alpha,
            warmup_frames=self._post_nr_warmup_frames, minstat=self._post_nr_minstat) if self.post_nr else None

    # ---- external-feed seam (BeamEngine drives the DSP from a shared stream) ----
    def process_block(self, block: Any) -> Any:
        """Run one block of the steered DSP and **return** the mono (does not emit).

        Updates the beam level and accumulates the DOA covariance (so the async DOA
        thread keeps tracking). Used both by :meth:`_cb_input` (standalone) and by an
        external owner (BeamEngine) feeding a shared input stream — see
        :meth:`prepare_external`. Realtime-safe: no device, no thread joins, and only bounded
        per-block numpy work (a few small array allocations, like the rest of the DSP path)."""
        np = self._np
        with self._beam_lock:
            mono = self._beam.process(block)
        if self._post_nr is not None:
            mono = self._post_nr.process(mono, self._noise_gate)   # spectral-gate NR before AGC (stable floor)
        if self._agc_gain is not None:
            mono = self._apply_agc(mono)              # target-loudness normalize (before the linear band-limit)
        if self._lp_kernel is not None:
            mono = self._band_limit(mono)
        rms = float(np.sqrt(np.mean(mono * mono))) if mono.size else 0.0
        self._level = min(1.0, rms)
        self._accumulate_covariance(block)
        return mono

    def prepare_external(self) -> None:
        """Ready the DSP + DOA thread to be fed an external shared stream — **no
        device opened**. Pair with :meth:`process_block` per block and
        :meth:`release_external` to tear down. Do NOT also call :meth:`start`."""
        if not controls_available():
            raise RuntimeError(_install_hint())
        self._setup_runtime()
        self._stop.clear()
        if self._doa_thread is None:
            self._doa_thread = threading.Thread(target=self._doa_loop, name="polaris-doa", daemon=True)
            self._doa_thread.start()
        self._streaming = True

    def release_external(self) -> None:
        """Stop the DOA thread and free DSP state after external feeding (no device)."""
        self._stop.set()
        th = self._doa_thread
        if th is not None:
            th.join(timeout=2.0)
            self._doa_thread = None
        with self._cov_lock:
            self._cov = None
            self._noise_cov = None
        self._stftbuf = None
        self._level = 0.0
        self._streaming = False

    def reset_transient(self) -> None:
        """Wipe per-mode transient state so a re-activated beam doesn't replay stale
        audio/DOA (called on the newly-activated back-end by BeamEngine on switch)."""
        # Rebind a fresh tracker (atomic ref swap) rather than _tracker.reset() in place: the DOA
        # thread mutates _tracker lock-free in _doa_tick, so an in-place wipe here would race it.
        self._tracker = _TalkerTracker(
            hold_seconds=self._tracker.hold_seconds,
            switch_margin_deg=self._tracker.switch_margin_deg,
        )
        with self._cov_lock:
            if self._cov is not None:
                self._cov[...] = 0.0
            if self._noise_cov is not None:
                self._noise_cov[...] = 0.0
        self._noise_frames = 0
        self._noise_gate = False              # re-acquire on the next DOA tick (don't train on the switch transient)
        if self._stftbuf is not None and self._np is not None:
            self._stftbuf = self._np.zeros((0, self.n_channels), dtype=float)
        with self._state_lock:
            self._reading = DoaReading(None, 0.0, False, False)
        self._steered_az = None
        if self._lp_kernel is not None and self._np is not None:
            self._lp_tail = self._np.zeros(len(self._lp_kernel) - 1, dtype=float)  # flush FIR state
        if self._agc_gain is not None:
            self._agc_gain = ExponentialTracker(self._agc_alpha)   # fresh slew state (atomic rebind; audio reads lock-free)
        if self._post_nr is not None:
            self._post_nr.reset()          # drop NR streaming + floor history; its lock serializes vs an in-flight process()
        with self._beam_lock:
            self._beam.reset()             # drop the strategy's streaming history; rebuilt on next process

    def _open_stream(self) -> None:
        """Validate the device and open the input (+ optional monitor) stream, setting
        :attr:`streaming`. Raises on any device/rate problem; the supervisor catches
        and retries, strict mode lets it propagate."""
        self._validate_input_device()
        import sounddevice as sd
        self._sd = sd
        self._setup_runtime()

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

        try:
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
        except Exception:
            self._close_stream()              # tear down any partially-opened stream
            raise
        self._last_block_monotonic = time.monotonic()
        self._streaming = True

    def _validate_input_device(self) -> None:
        """Clear errors for a missing device, too few channels, or an unsupported
        rate — before opening the stream."""
        devs = list_input_devices()
        match = None
        if self.device is not None:
            match = next((d for d in devs if d.index == self.device), None)
            if match is None:
                # Not present (yet). Plain ValueError → in wait mode the supervisor retries.
                raise ValueError(
                    f"input device index {self.device} not found; "
                    "run scripts/device_check.py to list devices"
                )
            if match.max_input_channels < self.n_channels:
                # Present but structurally wrong → fatal even in wait mode.
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
            # A present device rejecting the rate/format is treated as structural (fatal).
            # (A rare transient — e.g. the device momentarily held by another app — would
            # also land here; wait mode then needs a restart rather than silent retrying.)
            default_sr = match.default_samplerate if match is not None else self.sample_rate
            raise DeviceConfigError(
                f"device {self.device} did not accept {self.sample_rate:.0f} Hz @ "
                f"{self.n_channels}ch (its default is {default_sr:.0f} Hz; POLARIS runs at "
                f"{POLARIS_RATE_HZ:.0f}). Original error: {exc}"
            ) from exc

    def _close(self) -> None:
        # If a supervisor is still running (e.g. disconnect() called directly without
        # stop()), wind it down first. The normal stop() path already joined it.
        th = self._supervisor_thread
        if th is not None and threading.current_thread() is not th:
            self._stop.set()
            th.join(timeout=2.0)
            self._supervisor_thread = None
        self._close_stream()

    def _close_stream(self) -> None:
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
        self._last_block_monotonic = None
        with self._cov_lock:
            self._cov = None              # don't let stale covariance drive DOA after a drop
            self._noise_cov = None

    def _raw_level(self) -> float:
        return self._level

    # ---- audio thread ----
    def _cb_input(self, indata, frames, time_info, status):  # pragma: no cover (needs hardware)
        self._last_block_monotonic = time.monotonic()       # watchdog: stream is alive
        self._emit(self.process_block(indata))              # DSP (beam + DOA cov) → emit

    def _apply_agc(self, mono: Any) -> Any:
        """Push the beam-output block RMS toward ``agc_target_db`` with one scalar gain, EMA-slewed so
        it ramps (no pumping) and clamped to ±``agc_max_gain_db``. Near-silence (output RMS below
        ``agc_silence_db``) HOLDS the current gain instead of ramping up — so pauses don't amplify the
        noise floor. Control-pure: driven by the output level only, never distance/room. No-op unless
        ``agc_target_db`` was set (guarded by the caller)."""
        np = self._np
        tr = self._agc_gain
        target = self._agc_target_rms
        if tr is None or target is None:                 # AGC off (also satisfies the type-checker)
            return mono
        rms = float(np.sqrt(np.mean(mono * mono))) if mono.size else 0.0
        if rms > self._agc_silence_rms:
            desired = min(self._agc_gain_max, max(self._agc_gain_min, target / rms))
        else:
            held = tr.value
            desired = held if held is not None else 1.0  # hold through silence (don't pump the floor)
        g = float(tr.update(desired))
        return (mono * g).astype(np.float32)

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
            gate = self._noise_gate                                  # mvdr: update noise R on noise-only frames
            with self._cov_lock:                                     # re-check under the lock: a concurrent
                if self._cov is not None:                            # release_external/drop may have nulled either
                    self._cov *= (1.0 - a)
                    self._cov += a * inst
                if gate and self._noise_cov is not None:
                    an = self._noise_cov_alpha
                    self._noise_cov *= (1.0 - an)
                    self._noise_cov += an * inst
                    self._noise_frames += 1
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
    ap.add_argument("--mode", choices=_BEAM_MODES, default=MODE_DELAYSUM,
                    help="beam strategy: delaysum | fracdelay | superdirective (fixed MVDR) | mvdr (adaptive MVDR)")
    ap.add_argument("--loading", type=float, default=DEFAULT_SUPERDIRECTIVE_LOADING,
                    help="superdirective diagonal loading (higher = more robust, lower DI)")
    ap.add_argument("--radius", type=float, default=POLARIS_RADIUS_M, help="capsule-circle radius m (POLARIS=0.040)")
    ap.add_argument("--rate", type=float, default=POLARIS_RATE_HZ, help="sample rate (POLARIS=44100)")
    ap.add_argument("--block-ms", type=float, default=DEFAULT_BLOCK_MS, help="audio block size, ms")
    ap.add_argument("--dead", type=int, default=-1,
                    help="dead capsule index to mask off (-1 = none; the real board's is 5)")
    ap.add_argument("--hold", type=float, default=DEFAULT_HOLD_SECONDS, help="talker-hold seconds")
    ap.add_argument("--switch-margin", type=float, default=DEFAULT_SWITCH_MARGIN_DEG, help="re-steer margin, deg")
    ap.add_argument("--bandlimit", type=float, default=None,
                    help="beam low-pass cutoff, Hz (default: array aliasing cutoff ~5500)")
    ap.add_argument("--no-bandlimit", action="store_true", help="disable the beam band-limit")
    ap.add_argument("--agc-target-db", type=float, default=None,
                    help="normalize output loudness to this RMS, dBFS (e.g. -20); enables output AGC")
    ap.add_argument("--post-nr", action="store_true",
                    help="post-beam spectral-gate noise suppression (local fallback for the OCTOVOX cloud path)")
    ap.add_argument("--post-nr-floor-db", type=float, default=DEFAULT_POST_NR_FLOOR_DB,
                    help=f"post-NR residual floor / suppression depth, dB (default {DEFAULT_POST_NR_FLOOR_DB:.0f})")
    ap.add_argument("--post-nr-oversub", type=float, default=DEFAULT_POST_NR_OVERSUB,
                    help=f"post-NR Wiener over-subtraction strength (default {DEFAULT_POST_NR_OVERSUB})")
    ap.add_argument("--no-post-nr-minstat", dest="post_nr_minstat", action="store_false",
                    help="use the legacy VAD-gated noise floor instead of minimum statistics (default: min-stat)")
    ap.add_argument("--auto-null", action="store_true",
                    help="null detected interferers (the non-dominant talkers); superdirective/mvdr only")
    ap.add_argument("--monitor", action="store_true", help="play the mono output (use HEADPHONES)")
    ap.add_argument("--output-device", type=int, default=None, help="monitor output device index")
    ap.add_argument("--wait", action="store_true",
                    help="wait for the array to appear and auto-reconnect if it drops")
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
    # omit beam_bandlimit_hz unless the user spoke → keep the class default (ON at ~5.6 kHz)
    bl_kw: dict[str, Any] = {}
    if args.no_bandlimit:
        bl_kw["beam_bandlimit_hz"] = None
    elif args.bandlimit is not None:
        bl_kw["beam_bandlimit_hz"] = args.bandlimit
    bf = PolarisBeamformer(
        device=args.device, radius_m=args.radius, sample_rate=args.rate, block_ms=args.block_ms,
        dead_capsule=dead, hold_seconds=args.hold, switch_margin_deg=args.switch_margin,
        mode=args.mode, superdirective_loading=args.loading, auto_null=args.auto_null,
        agc_target_db=args.agc_target_db,
        post_nr=args.post_nr, post_nr_floor_db=args.post_nr_floor_db,
        post_nr_oversub=args.post_nr_oversub, post_nr_minstat=args.post_nr_minstat,
        monitor=args.monitor, output_device=args.output_device,
        wait_for_device=args.wait, **bl_kw,
    )
    if args.auto_null and bf.mode not in (MODE_SUPERDIRECTIVE, MODE_MVDR):
        print(f"Note: --auto-null needs a frequency-domain mode; '{bf.mode}' has no null DOF (ignored).")
    assert bf.geometry is not None
    print(f"Array: {bf.geometry.n_active}/{POLARIS_N_MICS} capsules, aperture "
          f"{bf.geometry.aperture_m() * 100:.1f} cm.  Aliasing cutoff ~{ALIAS_CUTOFF_HZ / 1000:.1f} kHz.")
    _mode_desc = {
        MODE_DELAYSUM: "integer-sample delay-and-sum",
        MODE_FRACDELAY: "sub-sample windowed-sinc delay-and-sum",
        MODE_SUPERDIRECTIVE: f"frequency-domain superdirective (diffuse-noise MVDR, loading={bf.superdirective_loading})",
        MODE_MVDR: f"frequency-domain data-adaptive MVDR (measured noise cov, loading={bf.superdirective_loading})",
    }
    print(f"Beam strategy: {bf.mode}  ({_mode_desc.get(bf.mode, '')})")
    if bf.agc_target_db is not None:
        print(f"Output AGC: normalizing to {bf.agc_target_db:.0f} dBFS (±{DEFAULT_AGC_MAX_GAIN_DB:.0f} dB, holds in silence).")
    if bf.post_nr:
        print(f"Post-beam NR: spectral gate, floor {bf._post_nr_floor_db:.0f} dB "
              f"(frame {bf._post_nr_frame}; learns on noise-only frames). Local fallback for OCTOVOX.")
    if args.wait:
        print(f"Waiting for device {args.device} @ {args.rate:.0f} Hz (auto-reconnect on) ... Ctrl+C to stop.")
    else:
        print(f"Opening device {args.device} @ {args.rate:.0f} Hz ... Ctrl+C to stop.")
    if args.monitor:
        print("Monitoring live — wear HEADPHONES to avoid feedback.")
    try:
        bf.start()
        while True:
            time.sleep(0.1)
            if bf.device_fatal:                    # structural device error in wait mode → stop
                print(f"\nGiving up: {bf.error}")
                break
            r = bf.reading()
            az = "  --  " if r.azimuth_deg is None else f"{r.azimuth_deg:5.0f}°"
            if not bf.streaming:
                tag = "WAIT"                  # waiting for / reconnecting the device
            elif r.held:
                tag = "HOLD"
            elif r.active:
                tag = "LIVE"
            else:
                tag = "  · "
            err = f"  !{bf.error}" if bf.error else ""
            nulls = bf.active_nulls
            null_s = f"  null {','.join(f'{n:.0f}' for n in nulls)}°" if nulls else ""
            print(f"\r[{tag}] DOA {az}  sal {r.salience_db:4.1f}dB{null_s}  | lvl {bf.read_level():4.2f}{err}    ",
                  end="", flush=True)
    except KeyboardInterrupt:
        print("\nStopping ...")
    finally:
        bf.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(_demo())
