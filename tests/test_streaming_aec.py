"""Hardware-free tests for the streaming partitioned-block AEC.

Validates the contract + DSP behaviour without a device: pass-through with no
reference, convergence/ERLE on a synthetic echo path, the far-end-only adaptation
gate (no divergence on near-end-only / double-talk), block-size invariance and
process/reset length-safety. numpy is required and skipped if absent.
"""
import threading

import pytest

np = pytest.importorskip("numpy")

from conf_pipeline_control.streaming_aec import StreamingAec


def _rms(x):
    return float(np.sqrt(np.mean(x * x))) if x.size else 0.0


def _delayed(x, d, scale):
    """A causal delayed + scaled copy (a simple synthetic echo path)."""
    y = np.zeros_like(x)
    if d < len(x):
        y[d:] = x[:len(x) - d]
    return scale * y


def _run_blocks(aec, mic, ref, near_end_active=False, block=512):
    out = []
    for i in range(0, len(mic), block):
        out.append(aec.process(mic[i:i + block], None if ref is None else ref[i:i + block],
                               near_end_active=near_end_active))
    return np.concatenate(out)


def test_aec_passthrough_without_reference():
    rng = np.random.default_rng(0)
    mic = (0.1 * rng.standard_normal(8192)).astype(float)
    aec = StreamingAec(44100.0, frame=512)
    out = _run_blocks(aec, mic, None)
    # no reference ⇒ nothing to cancel: energy is preserved (COLA reconstruction) and ERLE stays 0
    assert out.shape == mic.shape and bool(np.all(np.isfinite(out)))
    assert 0.7 * _rms(mic) < _rms(out) < 1.3 * _rms(mic)
    assert aec.erle_db == 0.0


def test_aec_converges_and_cancels_synthetic_echo():
    rng = np.random.default_rng(1)
    ref = (0.3 * rng.standard_normal(88200)).astype(float)      # ~2 s of far-end (white)
    mic = _delayed(ref, 128, 0.6)                               # echo = 0.6 × ref delayed 128 samples
    aec = StreamingAec(44100.0, frame=512, n_taps=12)
    out = _run_blocks(aec, mic, ref, near_end_active=False)
    assert bool(np.all(np.isfinite(out)))
    # Frequency-domain windowed block AEC gives MODEST ERLE on a pure delay (the Hann overlap-add breaks the
    # clean delay→per-bin-phase relation); ~5 dB here. It is a real, opt-in first stage — proper
    # linear-convolution / per-mic AEC is the upgrade path (see the module docstring).
    assert aec.erle_db > 4.0                                    # meaningful echo-return-loss enhancement
    h = len(out) // 2
    assert _rms(out[h:]) < 0.75 * _rms(mic[h:])                 # cancelled output's late half is reduced


def test_aec_freezes_adaptation_when_near_end_active():
    """near_end_active=True must FREEZE the update — the filter never trains on near-end speech, so the
    weights stay at zero (no divergence) and the output is the mic passed through."""
    rng = np.random.default_rng(2)
    ref = (0.3 * rng.standard_normal(16384)).astype(float)
    mic = _delayed(ref, 128, 0.6) + (0.2 * rng.standard_normal(16384)).astype(float)   # echo + near-end
    aec = StreamingAec(44100.0, frame=512)
    out = _run_blocks(aec, mic, ref, near_end_active=True)
    assert bool(np.all(np.isfinite(out)))
    assert float(np.max(np.abs(aec._W))) == 0.0                 # frozen: never adapted
    assert 0.7 * _rms(mic) < _rms(out) < 1.3 * _rms(mic)        # ≈ pass-through


def test_aec_double_talk_does_not_diverge():
    """Adapt on far-end-only, then hit double-talk: weights stay bounded (leaky + ±10 clip) and the
    output stays finite — the canceller must not blow up when near-end speech appears."""
    rng = np.random.default_rng(3)
    ref = (0.3 * rng.standard_normal(44100)).astype(float)
    aec = StreamingAec(44100.0, frame=512)
    _run_blocks(aec, _delayed(ref, 128, 0.6), ref, near_end_active=False)              # converge
    near = (0.5 * rng.standard_normal(44100)).astype(float)
    mic_dt = _delayed(ref, 128, 0.6) + near
    out = _run_blocks(aec, mic_dt, ref, near_end_active=True)                          # double-talk (frozen)
    assert bool(np.all(np.isfinite(out)))
    assert float(np.max(np.abs(aec._W))) <= 10.0 + 1e-9         # magnitude clamp holds


def test_aec_block_size_invariance():
    rng = np.random.default_rng(4)
    ref = (0.3 * rng.standard_normal(32768)).astype(float)
    mic = _delayed(ref, 96, 0.5)
    a = StreamingAec(44100.0, frame=512)
    b = StreamingAec(44100.0, frame=512)
    out_a = _run_blocks(a, mic, ref, block=512)
    out_b = _run_blocks(b, mic, ref, block=1411)
    L = min(len(out_a), len(out_b))
    assert L > 8192 and np.allclose(out_a[1024:L], out_b[1024:L], atol=1e-6)


def test_aec_process_reset_length_safe_under_concurrency():
    aec = StreamingAec(44100.0, frame=512)
    rng = np.random.default_rng(5)
    mblk = [(0.1 * rng.standard_normal(1411)).astype(float) for _ in range(160)]
    rblk = [(0.1 * rng.standard_normal(1411)).astype(float) for _ in range(160)]
    stop = threading.Event()

    def resetter():
        while not stop.is_set():
            aec.reset()

    t = threading.Thread(target=resetter, daemon=True)
    t.start()
    try:
        for m, r in zip(mblk, rblk):
            assert aec.process(m, r).shape == (1411,)
    finally:
        stop.set()
        t.join(timeout=2.0)


def test_aec_reset_clears_weights():
    rng = np.random.default_rng(6)
    ref = (0.3 * rng.standard_normal(16384)).astype(float)
    aec = StreamingAec(44100.0, frame=512)
    _run_blocks(aec, _delayed(ref, 128, 0.6), ref)
    assert float(np.max(np.abs(aec._W))) > 0.0 and aec._n_obs > 0
    aec.reset()
    assert float(np.max(np.abs(aec._W))) == 0.0 and aec._n_obs == 0 and aec.erle_db == 0.0
