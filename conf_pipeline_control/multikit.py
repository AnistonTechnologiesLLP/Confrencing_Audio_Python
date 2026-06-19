"""Dual-POLARIS **cross-array automix** — run two kits, output the active one.

Two POLARIS kits in one room, each auto-steering to its local talker. A selector
picks whichever kit currently has the active *talker* and the output cross-fades to
it; the result is one mono stream (coverage by zone-switching, **not** simultaneity —
double-talk across zones still yields one stream, following the active speaker).

This module is split so the **decision logic is pure and hardware-free**:

* :func:`crossfade_gains` — the equal-power cos/sin ramp (same math as
  ``BeamEngine._mix``), so a kit-to-kit switch is glitch-free.
* :class:`SpeechPresenceScorer` — the **fan-proof** speech-presence metric. A steady
  directional source (a fan/AC at a fixed bearing) reads as a talker to a DOA VAD,
  which is exactly the bug the minimum-statistics post-NR floor exists to dodge. The
  selector therefore must **not** key off the SRP-PHAT voice flag; it keys off
  *syllabic envelope modulation* (~3-8 Hz, which speech has and a fan does not),
  scaled by the kit's own level. Fan → ~0; speech → high; quiet speech still beats a
  loud fan because level is in the denominator, not the numerator.
* :class:`KitSelector` — hysteresis/hold across kits (modeled on
  ``polaris_beamformer._TalkerTracker``): switch only past a score ``switch_margin``,
  hold the active kit through brief pauses, and on a fan-only / silent room **hold the
  last-active kit and report no speaker** (never grab the fan).

The live :class:`MultiKitController` (two ``PolarisBeamformer`` engines + a single
combined output stream) is built on top of these; it lives behind the ``[control]``
extra. The pure pieces above need no numpy and no sounddevice.
"""
from __future__ import annotations

import math
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional, Sequence

from .agc import (
    DEFAULT_AGC_MAX_GAIN_DB,
    DEFAULT_AGC_SILENCE_DB,
    DEFAULT_AGC_SLEW_ALPHA,
    TargetLoudnessAgc,
)

DEFAULT_CROSSFADE_BLOCKS = 6       # equal-power ramp length on a kit switch (~0.2 s @ 32 ms)
DEFAULT_WATCHDOG_BLOCKS = 5        # a kit silent for this many hops is declared dead and leaves contention

# Selection / hold defaults (mirrors the steered tracker's tunables, in score units).
DEFAULT_HOP_SECONDS = 0.032        # ~32 ms block (engine default); the score update cadence
DEFAULT_SWITCH_MARGIN = 0.12       # a challenger must beat the incumbent's score by this to switch
DEFAULT_HOLD_SECONDS = 0.4         # coast the "active speaker" through pauses this long
DEFAULT_SPEECH_THRESHOLD = 0.15    # a score above this counts as speech present

# Speech-presence scorer defaults (syllabic-modulation band ≈ 3-8 Hz via a difference of EMAs).
DEFAULT_TAU_FAST = 0.03            # s — fast envelope EMA (upper syllabic corner)
DEFAULT_TAU_SLOW = 0.15            # s — slow envelope EMA (DC / lower corner)
DEFAULT_TAU_MOD = 0.30            # s — smoothing of the rectified band-passed envelope
DEFAULT_MOD_REF = 0.25            # modulation depth that maps to a full (1.0) speech score
_LEVEL_FLOOR = 1e-4              # guards the modulation-depth denominator at silence


def crossfade_gains(step: int, total: int) -> tuple[float, float]:
    """Equal-power crossfade gains ``(g_out, g_in)`` at fade ``step`` of ``total``.

    ``cos²+sin²=1`` keeps perceived loudness constant across a kit switch. ``step`` is
    clamped to ``[0, total]`` so callers can over-run without a special case. The
    cross-fade mixes two **uncorrelated** sources (different talkers in different
    zones) — equal-power is exactly right and there is deliberately **no** inter-kit
    sample/phase alignment (it would be meaningless across two independent clocks)."""
    if total <= 0:
        return (0.0, 1.0)
    s = min(max(step, 0), total)
    p = s / total
    return (math.cos(p * math.pi / 2.0), math.sin(p * math.pi / 2.0))


class SpeechPresenceScorer:
    """Per-kit, fan-proof speech-presence score from the kit's output level envelope.

    Feed it the per-hop output RMS (a scalar); it returns a score in ``[0, 1]`` that is
    high for syllabically-modulated speech and ~0 for a steady source — *regardless of
    level*. The metric is a difference-of-EMAs band-pass on the envelope (≈3-8 Hz, the
    syllabic band) divided by the kit's own slow level: a fan is near-DC so the
    band-passed energy is ~0; a louder fan does not help because level is the
    denominator. Pure (no numpy); deterministic given the hop cadence."""

    def __init__(self, *, hop_seconds: float = DEFAULT_HOP_SECONDS,
                 tau_fast: float = DEFAULT_TAU_FAST, tau_slow: float = DEFAULT_TAU_SLOW,
                 tau_mod: float = DEFAULT_TAU_MOD, mod_ref: float = DEFAULT_MOD_REF):
        self._a_fast = _alpha(hop_seconds, tau_fast)
        self._a_slow = _alpha(hop_seconds, tau_slow)
        self._a_mod = _alpha(hop_seconds, tau_mod)
        self._mod_ref = max(1e-6, float(mod_ref))
        self._fast = 0.0
        self._slow = 0.0
        self._mod = 0.0

    def update(self, rms: float, *, noise_floor: float = 0.0) -> float:
        """Fold one hop's output RMS in and return the speech-presence score.

        ``noise_floor`` is the kit's own (minimum-statistics) noise estimate when the
        engine exposes it — it lifts the denominator so sub-floor wandering does not
        read as modulation. Reuse the existing floor; do not stand up a second one."""
        env = rms if rms > 0.0 else 0.0
        self._fast += self._a_fast * (env - self._fast)
        self._slow += self._a_slow * (env - self._slow)
        bp = self._fast - self._slow                       # band-passed envelope (~syllabic)
        self._mod += self._a_mod * (abs(bp) - self._mod)   # smoothed modulation energy
        level = max(self._slow, noise_floor, _LEVEL_FLOOR)
        mod_depth = self._mod / level
        return min(1.0, mod_depth / self._mod_ref)

    def reset(self) -> None:
        self._fast = self._slow = self._mod = 0.0


@dataclass(frozen=True)
class SelectionState:
    """The selector's per-hop decision."""

    active: int                    # index of the kit being output
    switching: bool                # True on the hop a switch was just committed (start a cross-fade)
    speech_present: bool           # is anyone actually talking (coasted through brief pauses)?
    scores: tuple[float, ...]      # the per-kit speech-presence scores this hop


class KitSelector:
    """Hysteresis/hold selection across kits, driven by the speech-presence score.

    Models ``_TalkerTracker``: hold the active kit, switch to a challenger only when it
    beats the incumbent by ``switch_margin`` *and* clears ``speech_threshold`` (fast
    attack to a new speaker), and on a silent / fan-only room **hold the last-active
    kit and report no speaker** — never switch *to* a non-talker. Time is caller-
    supplied (monotonic seconds) so it is deterministic and testable."""

    def __init__(self, *, n_kits: int = 2, switch_margin: float = DEFAULT_SWITCH_MARGIN,
                 hold_seconds: float = DEFAULT_HOLD_SECONDS,
                 speech_threshold: float = DEFAULT_SPEECH_THRESHOLD):
        if n_kits < 1:
            raise ValueError("n_kits must be >= 1")
        self.n_kits = int(n_kits)
        self.switch_margin = float(switch_margin)
        self.hold_seconds = float(hold_seconds)
        self.speech_threshold = float(speech_threshold)
        self._active = 0
        self._last_speech_t: float | None = None

    def update(self, scores: Sequence[float], t: float) -> SelectionState:
        sc = [float(s) for s in scores]
        if len(sc) != self.n_kits:
            raise ValueError(f"expected {self.n_kits} scores, got {len(sc)}")
        best = max(range(self.n_kits), key=lambda i: sc[i])
        inc_score = sc[self._active]
        any_speech = sc[best] > self.speech_threshold

        switching = False
        if (any_speech and best != self._active
                and sc[best] >= inc_score + self.switch_margin):
            self._active = best                            # fast attack to a clearly-louder speaker
            switching = True

        if any_speech:
            self._last_speech_t = t                        # someone is talking right now
        present = any_speech or (
            self._last_speech_t is not None and (t - self._last_speech_t) <= self.hold_seconds)

        return SelectionState(active=self._active, switching=switching,
                              speech_present=present, scores=tuple(sc))

    def reset(self) -> None:
        self._active = 0
        self._last_speech_t = None


def _alpha(hop_seconds: float, tau_seconds: float) -> float:
    """One-pole EMA coefficient for a time constant ``tau`` at the given hop cadence."""
    if tau_seconds <= 0.0:
        return 1.0
    return 1.0 - math.exp(-float(hop_seconds) / float(tau_seconds))


# --------------------------------------------------------------------------- #
# Live controller: two PolarisBeamformer engines → select → cross-fade → one out.
# --------------------------------------------------------------------------- #
@dataclass
class KitSpec:
    """One kit's binding: which OS input device, which modeled room array (id +
    bearing live in the config, used by the GUI/overlay), and the per-kit
    PolarisBeamformer config (post_nr / dereverb / aec / mode / …). Per-kit AGC is
    forced OFF by the controller — Invariant B puts one AGC on the combined output."""

    device: Optional[int]
    array_id: Optional[str] = None
    radius_m: float = 0.04
    cfg: dict = field(default_factory=dict)


@dataclass(frozen=True)
class KitStatus:
    """Per-kit telemetry snapshot for the GUI (meter, DOA readout, error chip)."""

    index: int
    active: bool                   # is this the kit currently being output?
    doa_deg: Optional[float]
    level: float                   # the kit's output RMS (0..1)
    score: float                   # its speech-presence score (0..1)
    dead: bool                     # watchdog: stalled / failed → out of contention
    error: Optional[str]


def _default_engine_factory(spec: "KitSpec", tap: Callable[[Any], None],
                            ctrl: "MultiKitController") -> Any:
    """Build a real :class:`PolarisBeamformer` for a kit: no self-monitor (the controller
    owns the single output) and per-kit AGC stripped (Invariant B)."""
    from .polaris_beamformer import PolarisBeamformer

    cfg = dict(spec.cfg)
    for k in ("agc_target_db", "monitor", "output_callback", "device", "sample_rate", "blocksize"):
        cfg.pop(k, None)
    return PolarisBeamformer(device=spec.device, radius_m=spec.radius_m,
                             sample_rate=ctrl.sample_rate, blocksize=ctrl.blocksize,
                             monitor=False, output_callback=tap, **cfg)


def _default_output_stream_factory(ctrl: "MultiKitController") -> Any:
    import sounddevice as sd

    return sd.OutputStream(samplerate=ctrl.sample_rate, channels=2, blocksize=ctrl.blocksize,
                           device=ctrl.output_device, dtype="float32", callback=ctrl._cb_output)


class MultiKitController:
    """Run N POLARIS kits, output the active one with a glitch-free cross-fade.

    Each kit is an independent :class:`PolarisBeamformer` (its own clock domain, its own
    auto-steer + per-kit cleaning); the controller taps each kit's processed mono
    (``output_callback``, **copied** — Invariant C), scores each for speech presence
    (fan-proof — Invariant A), selects with hysteresis (:class:`KitSelector`),
    cross-fades on a switch, applies **one** combined AGC (Invariant B) + master
    mute/gain, and drives a single output stream. A stalled/lost kit is watch-dogged out
    of contention and never throws into the audio callback (Invariant D).

    Engines and the output stream are injected via factories so the whole thing is
    testable with stubs (no sounddevice, no hardware): drive :meth:`_produce` directly.
    """

    def __init__(self, kits: Sequence[KitSpec], *, output_device: Optional[int] = None,
                 sample_rate: float = 44100.0, block_ms: float = 32.0,
                 blocksize: Optional[int] = None, switch_margin: float = DEFAULT_SWITCH_MARGIN,
                 hold_seconds: float = DEFAULT_HOLD_SECONDS,
                 speech_threshold: float = DEFAULT_SPEECH_THRESHOLD,
                 crossfade_blocks: int = DEFAULT_CROSSFADE_BLOCKS,
                 watchdog_blocks: int = DEFAULT_WATCHDOG_BLOCKS, agc_target_db: Optional[float] = None,
                 agc_max_gain_db: float = DEFAULT_AGC_MAX_GAIN_DB,
                 agc_slew_alpha: float = DEFAULT_AGC_SLEW_ALPHA,
                 agc_silence_db: float = DEFAULT_AGC_SILENCE_DB,
                 engine_factory: Optional[Callable[..., Any]] = None,
                 output_stream_factory: Optional[Callable[..., Any]] = None,
                 time_fn: Optional[Callable[[], float]] = None):
        self.kits = list(kits)
        n = len(self.kits)
        if n < 1:
            raise ValueError("need at least one kit")
        devs = [k.device for k in self.kits if k.device is not None]
        if len(devs) != len(set(devs)):                       # Invariant F: distinct devices, hard guard
            raise ValueError("each kit needs a DISTINCT input device (two POLARIS = two devices)")
        self.sample_rate = float(sample_rate)
        self.blocksize = int(blocksize) if blocksize else max(1, int(round(self.sample_rate * block_ms / 1000.0)))
        self._hop_s = self.blocksize / self.sample_rate
        self._watchdog_s = float(watchdog_blocks) * self._hop_s
        self.crossfade_blocks = max(1, int(crossfade_blocks))
        self.output_device = output_device
        self._time = time_fn or time.monotonic
        self._engine_factory = engine_factory or _default_engine_factory
        self._output_stream_factory = output_stream_factory or _default_output_stream_factory
        self._agc: Optional[TargetLoudnessAgc] = (
            TargetLoudnessAgc(target_db=agc_target_db, max_gain_db=agc_max_gain_db,
                              slew_alpha=agc_slew_alpha, silence_db=agc_silence_db)
            if agc_target_db is not None else None)
        self._selector = KitSelector(n_kits=n, switch_margin=switch_margin,
                                     hold_seconds=hold_seconds, speech_threshold=speech_threshold)
        self._scorers = [SpeechPresenceScorer(hop_seconds=self._hop_s) for _ in range(n)]
        self._lock = threading.Lock()
        self._stores: list[Optional[Any]] = [None] * n
        self._scores = [0.0] * n
        self._levels = [0.0] * n
        self._last_emit: list[Optional[float]] = [None] * n
        self._dead = [False] * n
        self._errors: list[Optional[str]] = [None] * n
        self._kit_mute = [False] * n
        self._kit_gain_db = [0.0] * n
        self._engines: list[Any] = [None] * n
        self._active = 0
        self._fading = False
        self._fade_step = 0
        self._fade_from = 0
        self._muted = False
        self._gain_db = 0.0
        self._level = 0.0
        self._out_stream: Any = None
        self._streaming = False
        self.error: Optional[str] = None
        self._np: Any = None

    # ---- introspection ----
    @property
    def n_kits(self) -> int:
        return len(self.kits)

    @property
    def streaming(self) -> bool:
        return self._streaming

    @property
    def active_kit(self) -> int:
        with self._lock:
            return self._active

    def read_level(self) -> float:
        with self._lock:
            return self._level

    def status(self) -> list[KitStatus]:
        with self._lock:
            active, scores, levels = self._active, list(self._scores), list(self._levels)
            dead, errs = list(self._dead), list(self._errors)
        out = []
        for k in range(self.n_kits):
            eng = self._engines[k]
            doa = getattr(eng, "current_doa_deg", None) if eng is not None else None
            out.append(KitStatus(index=k, active=(k == active), doa_deg=doa,
                                 level=levels[k], score=scores[k], dead=dead[k], error=errs[k]))
        return out

    # ---- mute / gain (master, or per-kit) ----
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

    # ---- mic-INPUT preamp (per-kit input staging — distinct from the single combined-output AGC) ----
    def set_preamp_gain_db(self, gain_db: float, *, kit: Optional[int] = None) -> None:
        """Set a kit's mic-INPUT preamp gain (dB), or all kits' (``kit=None``). Forwarded to each
        kit's beamformer; this is input gain staging, NOT the combined-output gain (``set_gain_db``).
        Snapshots the engine list under the lock and calls the engines' realtime-safe setters outside
        it (so the controller lock is never held across a per-block-thread-visible write)."""
        with self._lock:
            engines = list(self._engines) if kit is None else [self._engines[kit]]
        for eng in engines:
            if eng is not None:
                eng.set_preamp_gain_db(gain_db)

    def set_preamp_auto(self, on: bool, *, kit: Optional[int] = None) -> None:
        """Toggle the auto headroom stager on a kit's engine, or all kits' (inert until the analog
        track wires a stager)."""
        with self._lock:
            engines = list(self._engines) if kit is None else [self._engines[kit]]
        for eng in engines:
            if eng is not None:
                eng.set_preamp_auto(on)

    # ---- audio: per-kit tap (Invariant C) + combined producer ----
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
            self._on_kit_output(k, mono)
        return _tap

    def _on_kit_output(self, k: int, mono: Any) -> None:
        """Kit ``k``'s processed mono hop (on its audio thread). Copy it (the engine hands
        the same object to several consumers), score it, and store the latest."""
        np = self._lazy_np()
        blk = np.asarray(mono, dtype=np.float32).copy()       # Invariant C: copy on write
        rms = float(np.sqrt(np.mean(blk * blk))) if blk.size else 0.0
        score = self._scorers[k].update(rms)                  # scorer[k] is touched only on this thread
        now = self._time()
        with self._lock:
            self._stores[k] = blk
            self._scores[k] = score
            self._levels[k] = rms
            self._last_emit[k] = now
            self._dead[k] = False
            self._errors[k] = None

    def _produce(self, t: float) -> Any:
        """Build one combined output block at time ``t``: watchdog → select → cross-fade →
        single AGC → master mute/gain. The testable core (no streams, no threads)."""
        np = self._lazy_np()
        n = self.n_kits
        with self._lock:
            stores = list(self._stores)
            scores = list(self._scores)
            last_emit = list(self._last_emit)
            dead_in = list(self._dead)
            kit_mute = list(self._kit_mute)
            kit_gain = list(self._kit_gain_db)
            muted, gain_db = self._muted, self._gain_db
            active, fading, step, fade_from = self._active, self._fading, self._fade_step, self._fade_from
        # watchdog (Invariant D): a kit that emitted then went stale is out of contention
        dead, stalled = [], []
        for k in range(n):
            le = last_emit[k]
            is_stale = le is not None and (t - le) > self._watchdog_s
            dead.append(dead_in[k] or is_stale)
            stalled.append(is_stale)
        eff = [0.0 if (dead[k] or kit_mute[k]) else scores[k] for k in range(n)]
        state = self._selector.update(eff, t)
        if state.switching:
            fade_from = active                                # cross-fade from what we were playing
            active = state.active
            fading = True
            step = 0

        def gained(idx: int) -> Any:
            s = stores[idx]
            if s is None:
                return self._silence()
            g = 10.0 ** (kit_gain[idx] / 20.0)
            return s * g if g != 1.0 else s

        if not fading:
            mono = gained(active)
        else:
            g_out, g_in = crossfade_gains(step, self.crossfade_blocks)
            mono = g_out * gained(fade_from) + g_in * gained(active)
            step += 1
            if step >= self.crossfade_blocks:
                fading = False
        if self._agc is not None:
            mono = self._agc.process(mono)                    # the ONE AGC (Invariant B)
        if muted:
            mono = self._silence()
        elif gain_db != 0.0:
            mono = mono * (10.0 ** (gain_db / 20.0))
        mono = mono.astype(np.float32)                        # fresh array (never aliases a stored block)
        out_rms = float(np.sqrt(np.mean(mono * mono))) if mono.size else 0.0
        with self._lock:
            self._active, self._fading, self._fade_step, self._fade_from = active, fading, step, fade_from
            self._level = out_rms
            for k in range(n):
                if stalled[k] and not self._dead[k]:
                    self._dead[k] = True
                    if self._errors[k] is None:
                        self._errors[k] = "kit stalled (no audio)"
        return mono

    def _cb_output(self, outdata: Any, frames: int, time_info: Any, status: Any) -> None:  # pragma: no cover (needs hardware)
        # Invariant D: an exception out of a PortAudio callback aborts the stream — never propagate.
        try:
            mono = self._produce(self._time())
            if mono is not None and mono.shape[0] == outdata.shape[0]:
                outdata[:] = mono[:, None]
            else:
                outdata.fill(0.0)
        except Exception as exc:
            self.error = f"output callback: {exc}"
            try:
                outdata.fill(0.0)
            except Exception:
                pass

    # ---- lifecycle ----
    def start(self) -> None:
        """Bring up each kit (independent — one failing leaves the others running) then the
        single output stream."""
        started = 0
        for k, spec in enumerate(self.kits):
            try:
                eng = self._engine_factory(spec, self._make_tap(k), self)
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
            if eng is None:
                continue
            try:
                eng.stop()
            except Exception as exc:
                self._errors[k] = f"kit {k} stop: {exc}"
            self._engines[k] = None
