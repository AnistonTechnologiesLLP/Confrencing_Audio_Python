"""Voice-cleaning **level preservation** + **cleaning amount** — the engine-agnostic makeup that stops
any post-beam cleaner from making the talker sound *weak*, plus the per-bin "amount" gentleness dial.

``_LevelPreservingCleaner`` is tested over a STUB inner cleaner (a fixed-gain attenuator), so these need
no model / ``[dfn]`` extra; the ``amount`` blend is tested on the real ``_PostNoiseSuppressor`` gate.
"""
import pytest

np = pytest.importorskip("numpy")

from conf_pipeline_control.polaris_beamformer import (  # noqa: E402
    _POST_NR_MAKEUP_MAX_GAIN_DB,
    _LevelPreservingCleaner,
    _PostNoiseSuppressor,
)

_GAIN_CAP = 10.0 ** (_POST_NR_MAKEUP_MAX_GAIN_DB / 20.0)


class _Atten:
    """Stub cleaner: scales every block by a fixed gain (and counts ``reset``s)."""

    def __init__(self, gain: float):
        self.gain = float(gain)
        self.resets = 0

    def process(self, block, noise_gate):
        return (np.asarray(block, dtype=np.float32) * self.gain).astype(np.float32)

    def reset(self):
        self.resets += 1


def _drive(w, n=400, bs=512, gate=False, level=0.2, seed=0):
    rng = np.random.default_rng(seed)
    last = None
    for _ in range(n):
        last = w.process((level * rng.standard_normal(bs)).astype(np.float32), gate)
    return last


# --------------------------------------------------------------------------- #
# _LevelPreservingCleaner — the speech-gated makeup
# --------------------------------------------------------------------------- #
def test_makeup_restores_the_level_the_cleaner_removed():
    w = _LevelPreservingCleaner(_Atten(0.5))          # inner drops 6 dB (×0.5)
    _drive(w)                                          # speech frames (noise_gate False)
    assert abs(float(w._slew.value) - 2.0) < 0.1       # makeup converged to +6 dB (×2) → output ≈ input level


def test_makeup_same_length_and_finite():
    w = _LevelPreservingCleaner(_Atten(0.5))
    y = w.process(np.zeros(300, np.float32), False)
    assert y.shape[0] == 300 and y.dtype == np.float32 and np.all(np.isfinite(y))


def test_makeup_is_boost_only_never_attenuates():
    w = _LevelPreservingCleaner(_Atten(2.0))          # inner BOOSTS (+6 dB) — makeup must not pull it back down
    _drive(w)
    assert abs(float(w._slew.value) - 1.0) < 1e-6      # clamped to unity (a cleaner only ever attenuates)


def test_makeup_held_through_silence_no_floor_pumping():
    w = _LevelPreservingCleaner(_Atten(0.5))
    _drive(w)                                          # learn +6 dB on speech
    g0 = float(w._slew.value)
    for _ in range(400):                               # a long pause: noise_gate True, near-silent input
        w.process((1e-4 * np.ones(512, np.float32)), noise_gate=True)
    assert abs(float(w._slew.value) - g0) < 1e-3       # gain HELD at the speech value (not ramped up on silence)


def test_makeup_capped_at_the_max_gain():
    w = _LevelPreservingCleaner(_Atten(0.001))        # an absurd 60 dB drop — makeup must saturate, not run away
    _drive(w)
    assert float(w._slew.value) <= _GAIN_CAP + 1e-6


def test_reset_clears_state_and_resets_inner():
    inner = _Atten(0.5)
    w = _LevelPreservingCleaner(inner)
    _drive(w)
    w.reset()
    assert inner.resets == 1 and w._target == 1.0 and w._slew.value is None


def test_process_never_throws_into_the_audio_callback():
    class _Bad:                                        # an inner whose level read could divide oddly
        def process(self, b, ng):
            return np.asarray(b, dtype=np.float32)     # returns the input → ratio 1, no boost
        def reset(self):
            pass

    w = _LevelPreservingCleaner(_Bad())
    y = w.process(np.ones(256, np.float32), False)      # must not raise
    assert y.shape[0] == 256 and np.all(np.isfinite(y))


# --------------------------------------------------------------------------- #
# the "cleaning amount" gain blend on the shared base cleaner
# --------------------------------------------------------------------------- #
def test_amount_one_suppresses_amount_zero_passes_through():
    """``amount`` blends the cleaner's per-bin gain toward unity: 1.0 = full suppression, 0.0 = passthrough."""
    sr = 44100
    n = sr
    rng = np.random.default_rng(0)
    sig = (0.15 * rng.standard_normal(n)).astype(np.float32)   # pure noise → the gate wants to suppress it

    def run(amount):
        g = _PostNoiseSuppressor(sr, amount=amount, warmup_frames=4)
        bs = 512
        return np.concatenate([g.process(sig[i:i + bs], True) for i in range(0, n - bs, bs)])

    full, zero = run(1.0), run(0.0)
    L = min(len(full), len(zero))
    rms = lambda y: float(np.sqrt(np.mean(y[:L] ** 2)))
    assert rms(full) < 0.8 * rms(zero)                        # amount=1 cuts the noise; amount=0 leaves it
