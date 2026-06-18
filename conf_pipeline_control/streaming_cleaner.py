"""Streaming voice-enhancement stages for the live beam (ports of OCTOVOX DSP).

Two real-time, pure-numpy stages that run at the post-beam seam (dereverb → denoise):
:class:`StreamingCleaner` (OM-LSA noise reduction) and :class:`StreamingDereverb`
(spectral late-reverb suppression). Both subclass :class:`_PostNoiseSuppressor` and
reuse its overlap-add STFT, warmup-passthrough and process/reset lock, overriding only
the per-bin gain law.

The conferencing engine already beamforms the 8-capsule POLARIS array down to one
mono voice in real time; this runs OCTOVOX's *cleaning* on that mono, frame by
frame, at the post-beam seam (:meth:`PolarisBeamformer.process_block` →
``_post_nr.process(mono, noise_gate)``). It is a **drop-in** for
:class:`~conf_pipeline_control.polaris_beamformer._PostNoiseSuppressor` — same
``process(block, noise_gate) -> block`` / ``reset()`` contract, the same Hann
50 %-overlap overlap-add STFT, the same minimum-statistics noise floor and
warmup-passthrough, and the same process/reset lock — but it swaps the gentle
single-pole Wiener gate for the decision-directed **OM-LSA** log-spectral-amplitude
gain (Ephraim–Malah / Cohen 2003) ported from New_OCTOVOX
``prod_pipeline.omlsa_vad_nr`` (and ``dd_wiener`` for the cheaper ``"wiener"`` mode).

**Why a port, not an import.** New_OCTOVOX is a Python 3.11 + numpy<2 + torch
Flask app; this host runs Python 3.14 + numpy 2.x, so the two cannot share an
interpreter, and the OCTOVOX functions are whole-clip anyway (they take a noise
percentile over the *entire* recording). So the gain law is re-implemented here
as numpy-only, frame-streaming code: the whole-clip noise percentile becomes the
inherited per-bin minimum-statistics floor (``_noise_mag``), and the per-frame
decision-directed a-priori-SNR recursion carries ``_prev_clean`` across blocks.
Pure numpy, no scipy / no torch — so it runs on the realtime audio thread (the
exponential integral E1 the LSA gain needs is a vendored numpy approximation,
:func:`_exp1`, instead of ``scipy.special.exp1``).

DeepFilterNet3 is deliberately NOT here: it needs 48 kHz + torch and has no
frame-streaming inference API, so it cannot run on the 44.1 kHz audio thread —
that stays an offline / out-of-process path (see :mod:`octovox_monitor`).
"""
from __future__ import annotations

from typing import Any

from .polaris_beamformer import (
    DEFAULT_DEREVERB_BETA,
    DEFAULT_DEREVERB_EARLY_MS,
    DEFAULT_DEREVERB_GMIN_DB,
    DEFAULT_DEREVERB_T60,
    DEFAULT_POST_NR_FRAME,
    DEFAULT_POST_NR_GAIN_ALPHA,
    DEFAULT_POST_NR_MINSTAT,
    DEFAULT_POST_NR_MINSTAT_BIAS,
    DEFAULT_POST_NR_MINSTAT_SUB,
    DEFAULT_POST_NR_MINSTAT_SUBLEN,
    DEFAULT_POST_NR_NOISE_ALPHA,
    DEFAULT_POST_NR_POWER_ALPHA,
    DEFAULT_POST_NR_WARMUP_FRAMES,
    _PostNoiseSuppressor,
)

# OM-LSA defaults — mirror New_OCTOVOX prod_pipeline.omlsa_vad_nr.
DEFAULT_CLEANER_MODE = "omlsa"          # "omlsa" | "wiener" | "gate"
DEFAULT_CLEANER_ALPHA = 0.985           # decision-directed a-priori-SNR smoothing (Ephraim–Malah)
DEFAULT_CLEANER_GMIN_DB = -18.0         # OM-LSA spectral floor Gmin (suppression depth, amplitude dB)
DEFAULT_CLEANER_GAMMA_THRESH = 2.0      # speech-presence logistic centre (on the a-posteriori SNR)
DEFAULT_CLEANER_NU_MIN = 1e-3           # ν clamp lo — the E1 boost explodes as ν→0
DEFAULT_CLEANER_NU_MAX = 500.0          # ν clamp hi


def _exp1(x: Any) -> Any:
    """Vectorised exponential integral E1(x) for x > 0 (pure numpy).

    A numpy stand-in for ``scipy.special.exp1`` so the OM-LSA log-spectral-amplitude
    lift ``exp(½·E1(ν))`` can run on the realtime audio thread without scipy. Uses
    the Abramowitz & Stegun rational approximations: 5.1.53 for 0 < x ≤ 1
    (|err| < 2e-7) and 5.1.56 for x > 1 (|err| < 5e-5). ν is clamped to
    ``[nu_min, nu_max]`` upstream, so x is always strictly positive.
    """
    import numpy as np

    x = np.asarray(x, dtype=float)
    small = x <= 1.0
    # --- 5.1.53 (0 < x ≤ 1): E1(x) = -ln x + Σ a_k x^k ---
    xs = np.where(small, x, 1.0)                      # placeholder for the large branch (overwritten below)
    a = (-0.57721566, 0.99999193, -0.24991055, 0.05519968, -0.00976004, 0.00107857)
    poly = a[0] + xs * (a[1] + xs * (a[2] + xs * (a[3] + xs * (a[4] + xs * a[5]))))
    e_small = -np.log(np.maximum(xs, 1e-300)) + poly
    # --- 5.1.56 (x > 1): E1(x) = e^-x / x · (x² + b1 x + b2)/(x² + c1 x + c2) ---
    xl = np.where(small, 2.0, x)                      # placeholder for the small branch (overwritten below)
    num = xl * xl + 2.334733 * xl + 0.250621
    den = xl * xl + 3.330657 * xl + 1.681534
    e_large = np.exp(-xl) / xl * (num / den)
    return np.where(small, e_small, e_large)


class StreamingCleaner(_PostNoiseSuppressor):
    """Drop-in for :class:`_PostNoiseSuppressor` with a decision-directed OM-LSA gain.

    Reuses the base class's overlap-add STFT, minimum-statistics noise floor,
    warmup-passthrough, and process/reset lock **verbatim**; only :meth:`_gain` is
    overridden (and ``_prev_clean`` state added). ``mode`` selects the gain law:

      · ``"omlsa"`` (default) — full Cohen OM-LSA: the LSA log-spectral-amplitude
        lift over the decision-directed Wiener gain, blended with a per-bin
        speech-presence probability so a natural ``Gmin`` bed is kept in the gaps.
      · ``"wiener"`` — the decision-directed Wiener gain only (cheaper, no E1).
      · ``"gate"`` — fall back to the base single-pole spectral gate.

    The noise PSD is the inherited per-bin ``_noise_mag`` (minimum statistics by
    default, so it tracks steady fans/AC without needing a VAD); ``noise_gate``
    is honoured exactly as the base class uses it (it feeds the legacy gated-EMA
    floor when ``minstat=False``). All the realtime/threading guarantees of the
    base class carry over unchanged.
    """

    def __init__(self, sample_rate: float, *, mode: str = DEFAULT_CLEANER_MODE,
                 alpha: float = DEFAULT_CLEANER_ALPHA, gmin_db: float = DEFAULT_CLEANER_GMIN_DB,
                 gamma_thresh: float = DEFAULT_CLEANER_GAMMA_THRESH,
                 nu_min: float = DEFAULT_CLEANER_NU_MIN, nu_max: float = DEFAULT_CLEANER_NU_MAX,
                 frame: int = DEFAULT_POST_NR_FRAME,
                 gain_alpha: float = DEFAULT_POST_NR_GAIN_ALPHA,
                 warmup_frames: int = DEFAULT_POST_NR_WARMUP_FRAMES,
                 noise_alpha: float = DEFAULT_POST_NR_NOISE_ALPHA,
                 minstat: bool = DEFAULT_POST_NR_MINSTAT,
                 minstat_sub: int = DEFAULT_POST_NR_MINSTAT_SUB,
                 minstat_sublen: int = DEFAULT_POST_NR_MINSTAT_SUBLEN,
                 minstat_bias: float = DEFAULT_POST_NR_MINSTAT_BIAS,
                 power_alpha: float = DEFAULT_POST_NR_POWER_ALPHA):
        self.mode = mode if mode in ("omlsa", "wiener", "gate") else "omlsa"
        self._alpha = min(1.0, max(0.0, float(alpha)))
        self._gamma_thresh = max(1e-6, float(gamma_thresh))
        self._nu_min = float(nu_min)
        self._nu_max = float(nu_max)
        self._xi_floor = 10.0 ** (float(gmin_db) / 10.0)     # power a-priori-SNR floor (mirrors omlsa_vad_nr)
        # Hand gmin_db to the base as floor_db so the inherited _g_floor IS the OM-LSA amplitude Gmin.
        super().__init__(
            sample_rate, frame=frame, floor_db=float(gmin_db), gain_alpha=gain_alpha,
            warmup_frames=warmup_frames, noise_alpha=noise_alpha, minstat=minstat,
            minstat_sub=minstat_sub, minstat_sublen=minstat_sublen,
            minstat_bias=minstat_bias, power_alpha=power_alpha)

    def _init_state(self) -> None:
        super()._init_state()
        self._prev_clean: Any = None   # per-bin clean-power estimate (decision-directed feedback); None until 1st frame

    def _gain(self, X: Any) -> Any:
        """Per-bin decision-directed OM-LSA gain (ported from omlsa_vad_nr / dd_wiener).

        Uses the inherited ``_noise_mag`` as the noise PSD, carries the a-priori
        SNR via ``_prev_clean``, then applies the same 3-tap frequency + temporal
        one-pole smoothing as the base gate. Gain is capped at 1.0 (suppression
        only — never boosts)."""
        if self.mode == "gate":
            return super()._gain(X)
        import numpy as np

        P = X.real * X.real + X.imag * X.imag                 # |X|² instantaneous power
        noise = self._noise_mag * self._noise_mag + 1e-20     # noise power (per bin)
        gamma = P / noise                                     # a-posteriori SNR
        gpost = np.maximum(gamma - 1.0, 0.0)
        if self._prev_clean is None:                          # first engaged frame: no history yet
            xi = gpost
        else:
            xi = self._alpha * (self._prev_clean / noise) + (1.0 - self._alpha) * gpost
        xi = np.maximum(xi, self._xi_floor)
        gw = xi / (1.0 + xi)                                  # decision-directed Wiener gain from smoothed ξ
        self._prev_clean = (gw * gw) * P                      # (gw·|X|)² fed back next frame
        if self.mode == "omlsa":
            nu = np.clip(gw * gamma, self._nu_min, self._nu_max)
            g_h1 = np.minimum(gw * np.exp(0.5 * _exp1(nu)), 1.0)   # LSA gain, hard-capped (E1 boost blows up as ν→0)
            spp = 1.0 / (1.0 + np.exp(-(np.log(gamma + 1e-20) - np.log(self._gamma_thresh))))  # speech-presence prob
            p = np.clip(spp, 0.0, 1.0)
            g = (g_h1 ** p) * (self._g_floor ** (1.0 - p))   # Cohen OM floor: keep a natural Gmin bed in the gaps
        else:                                                 # mode == "wiener"
            g = np.maximum(gw, self._g_floor)
        # Same smoothing as the base gate: 3-tap frequency smooth then a per-bin temporal one-pole.
        gs = g.copy()
        gs[1:-1] = 0.25 * g[:-2] + 0.5 * g[1:-1] + 0.25 * g[2:]
        gs = self._gain_alpha * gs + (1.0 - self._gain_alpha) * self._gain_prev
        self._gain_prev = gs
        return gs


class StreamingDereverb(_PostNoiseSuppressor):
    """Real-time single-channel **dereverberation** — a causal port of OCTOVOX's
    ``prod_pipeline.dereverb_spectral`` (Lebart 2001 / Habets statistical late-reverb
    suppression). A **drop-in** for :class:`_PostNoiseSuppressor` (same
    ``process(block, noise_gate) -> block`` / ``reset()`` contract and overlap-add STFT
    machinery), it estimates the LATE-reverberation power as a delayed, T60-decayed,
    one-pole-smoothed copy of the observed power and applies a spectral-subtraction gain
    ``G = max(1 − β·R/P, Gmin)``.

    Causal by construction: the offline version's delayed-power tap ``Pd[:, d:] = P[:, :-d]``
    reads PAST frames, so streaming only needs a ``d``-frame per-bin power-history ring plus
    the one-pole IIR state ``R`` — both carried across blocks. It runs BEFORE the noise
    reducer at the post-beam seam (dereverb → denoise → AGC). ``noise_gate`` is unused
    (dereverb is VAD-independent); engagement is unconditional after ``warmup_frames``
    (the inherited minstat counter advances every frame). Gain is floored at ``Gmin`` so it
    only REMOVES reverb energy and never hard-mutes.
    """

    def __init__(self, sample_rate: float, *, t60: float = DEFAULT_DEREVERB_T60,
                 beta: float = DEFAULT_DEREVERB_BETA, gmin_db: float = DEFAULT_DEREVERB_GMIN_DB,
                 early_ms: float = DEFAULT_DEREVERB_EARLY_MS,
                 frame: int = DEFAULT_POST_NR_FRAME,
                 gain_alpha: float = DEFAULT_POST_NR_GAIN_ALPHA,
                 warmup_frames: int = DEFAULT_POST_NR_WARMUP_FRAMES,
                 minstat: bool = DEFAULT_POST_NR_MINSTAT,
                 minstat_sub: int = DEFAULT_POST_NR_MINSTAT_SUB,
                 minstat_sublen: int = DEFAULT_POST_NR_MINSTAT_SUBLEN,
                 minstat_bias: float = DEFAULT_POST_NR_MINSTAT_BIAS,
                 power_alpha: float = DEFAULT_POST_NR_POWER_ALPHA):
        self._t60 = max(0.05, float(t60))
        self._beta = max(0.0, float(beta))
        self._early_ms = max(0.0, float(early_ms))
        # Hand gmin_db to the base as floor_db so the inherited _g_floor IS the dereverb gain floor.
        super().__init__(
            sample_rate, frame=frame, floor_db=float(gmin_db), gain_alpha=gain_alpha,
            warmup_frames=warmup_frames, minstat=minstat, minstat_sub=minstat_sub,
            minstat_sublen=minstat_sublen, minstat_bias=minstat_bias, power_alpha=power_alpha)

    def _init_state(self) -> None:
        super()._init_state()
        import numpy as np

        nb = self._F // 2 + 1
        # per-frame 60 dB decay pole + early-reflection delay (frames), derived from the frame/hop + sr
        self._a = float(np.exp(-13.8155 * self._H / (self._t60 * self._sr)))   # a = exp(-ln(1e6)·HOP/(t60·fs))
        self._d = max(1, int(round(self._early_ms / 1000.0 * self._sr / self._H)))
        self._R: Any = np.zeros(nb, dtype=float)                 # one-pole late-reverb PSD state
        self._phist: Any = np.zeros((self._d, nb), dtype=float)  # ring of the last d power frames (delayed tap)
        self._phist_idx = 0

    def _gain(self, X: Any) -> Any:
        """Per-bin late-reverb spectral-subtraction gain (ported from dereverb_spectral)."""
        import numpy as np

        P = X.real * X.real + X.imag * X.imag                 # |X|² instantaneous power
        Pd = self._phist[self._phist_idx].copy()              # power from d frames ago (zeros until the ring fills)
        self._phist[self._phist_idx] = P
        self._phist_idx = (self._phist_idx + 1) % self._d
        self._R = self._a * self._R + (1.0 - self._a) * Pd    # one-pole IIR → late-reverb PSD estimate
        g = np.maximum(1.0 - self._beta * self._R / (P + 1e-20), self._g_floor)
        # Same smoothing as the base gate: 3-tap frequency smooth then a per-bin temporal one-pole.
        gs = g.copy()
        gs[1:-1] = 0.25 * g[:-2] + 0.5 * g[1:-1] + 0.25 * g[2:]
        gs = self._gain_alpha * gs + (1.0 - self._gain_alpha) * self._gain_prev
        self._gain_prev = gs
        return gs
