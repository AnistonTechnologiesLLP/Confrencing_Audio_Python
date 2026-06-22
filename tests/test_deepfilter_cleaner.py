"""Real-time DeepFilterNet3 cleaner (``post_nr_engine="dfn3"``) — the streaming runtime around the
bundled ONNX model. The model + onnxruntime are the ``[dfn]`` extra, so the cleaner tests skip without
it; the streaming-resampler + contract checks run on numpy/scipy alone."""
from pathlib import Path

import pytest

np = pytest.importorskip("numpy")

import conf_pipeline_control.deepfilter_cleaner as dfc
from conf_pipeline_control.deepfilter_cleaner import StreamingDeepFilter, _StreamingResampler

_HAS_ORT = True
try:  # the [dfn] extra
    import onnxruntime  # noqa: F401
except Exception:
    _HAS_ORT = False
_HAS_MODEL = Path(dfc._DEFAULT_MODEL).exists()
needs_dfn = pytest.mark.skipif(not (_HAS_ORT and _HAS_MODEL),
                               reason="needs the [dfn] extra (onnxruntime) + the bundled DFN3 ONNX")


def _sine(f, n, sr):
    return (0.3 * np.sin(2 * np.pi * f * np.arange(n) / sr)).astype(np.float32)


# --------------------------------------------------------------------------- #
# streaming polyphase resampler (numpy + scipy only)
# --------------------------------------------------------------------------- #
def test_streaming_resampler_roundtrip_preserves_signal():
    pytest.importorskip("scipy")
    sr, bs = 44100, 1411
    x = _sine(1000.0, sr, sr)                                   # 1 s, 1 kHz
    to48 = _StreamingResampler(48000, 44100, np)               # 44.1k -> 48k
    back = _StreamingResampler(44100, 48000, np)               # 48k -> 44.1k
    out = [back.process(to48.process(x[i:i + bs])) for i in range(0, len(x) - bs, bs)]
    y = np.concatenate(out)
    assert abs(len(y) - (len(x) // bs) * bs) < 4 * bs           # net rate preserved (1:1 round-trip)
    mid = y[len(y) // 4:3 * len(y) // 4]
    freqs = np.fft.rfftfreq(len(mid), 1 / sr)
    peak = freqs[int(np.argmax(np.abs(np.fft.rfft(mid * np.hanning(len(mid))))))]
    assert abs(peak - 1000.0) < 5.0                            # tone survives the round-trip (low distortion)


def test_streaming_resampler_reset_clears_history():
    pytest.importorskip("scipy")
    r = _StreamingResampler(48000, 44100, np)
    r.process(_sine(500.0, 2048, 44100))
    r.reset()
    assert r._win.shape[0] == 0 and r._win_start == 0 and r._out_done == 0   # all streaming state cleared


def _roundtrip_thdn(f0, sr=44100, dur=4.0, blk=512):
    """44.1->48->44.1 round-trip THD+N (dB) of a pure tone, the resampler-distortion metric."""
    from scipy.signal.windows import blackmanharris
    n = int(sr * dur)
    x = (0.4 * np.sin(2 * np.pi * f0 * np.arange(n) / sr)).astype(np.float32)
    to48, back = _StreamingResampler(48000, sr, np), _StreamingResampler(sr, 48000, np)
    y = np.concatenate([back.process(to48.process(x[i:i + blk])) for i in range(0, n - blk + 1, blk)])
    seg = y[6000:len(y) - 6000].astype(np.float64)
    P = np.abs(np.fft.rfft(seg * blackmanharris(len(seg)))) ** 2
    k = int(np.argmin(np.abs(np.fft.rfftfreq(len(seg), 1 / sr) - f0)))
    fund = P[k - 8:k + 9].sum(); P[k - 8:k + 9] = 0.0; P[:8] = 0.0
    return 10 * np.log10(P.sum() / (fund + 1e-30))


def test_streaming_resampler_roundtrip_is_low_distortion():
    """The phase-coherent streamer must match a single-shot resample (≈−67..−80 dB across the speech band).
    The OLD overlap-save (phase-reset + integer-floor drift + emitted FIR tail) measured ≈−10 dB here —
    the dominant DFN3 voice-distortion source. Guard at −60 dB (huge margin over −10; below the −67.7 dB
    single-shot floor at 2 kHz)."""
    pytest.importorskip("scipy")
    for f0 in (500.0, 1000.0, 2000.0, 3000.0):
        thdn = _roundtrip_thdn(f0)
        assert thdn < -60.0, f"round-trip THD+N {thdn:.1f} dB @ {f0:.0f} Hz (resampler-distortion regression)"


def test_streaming_resampler_is_drift_free():
    """Output length must track the ideal rate within a CONSTANT startup deficit, independent of duration —
    the old integer-floor trim drifted ~+90 samples/s (a slowly worsening time-base error / pitch creep)."""
    pytest.importorskip("scipy")
    sr, blk = 44100, 512
    deficits = []
    for dur in (1.0, 2.0, 4.0):
        n = int(sr * dur)
        x = _sine(1000.0, n, sr)
        to48, back = _StreamingResampler(48000, sr, np), _StreamingResampler(sr, 48000, np)
        y = np.concatenate([back.process(to48.process(x[i:i + blk])) for i in range(0, n - blk + 1, blk)])
        whole = (n // blk) * blk
        deficits.append(whole - len(y))                       # 1:1 round-trip → deficit is pure startup latency
    assert max(deficits) - min(deficits) < blk, f"length deficit drifts with duration: {deficits}"


# --------------------------------------------------------------------------- #
# the cleaner — needs onnxruntime + the bundled model
# --------------------------------------------------------------------------- #
@pytest.mark.skipif(not _HAS_ORT, reason="needs onnxruntime to reach the model check")
def test_missing_model_raises_a_clear_error():
    with pytest.raises(RuntimeError) as e:
        StreamingDeepFilter(44100.0, model_path="C:/definitely/not/here/model.onnx")
    assert "model not found" in str(e.value).lower()


@needs_dfn
def test_cleaner_same_length_blocks_and_bounded_fifo():
    cl = StreamingDeepFilter(44100.0)
    bs = 1411
    rng = np.random.default_rng(0)
    fifo_max = 0
    for _ in range(120):
        blk = (0.1 * rng.standard_normal(bs)).astype(np.float32)
        y = cl.process(blk, False)
        assert y.shape[0] == bs and y.dtype == np.float32      # same-length contract
        fifo_max = max(fifo_max, len(cl._outq))
    assert fifo_max < 44100                                    # FIFO bounded (< 1 s) — no growth bug


@needs_dfn
def test_cleaner_reset_reprimes():
    cl = StreamingDeepFilter(44100.0)
    bs = 1411
    for _ in range(40):
        cl.process((0.1 * np.ones(bs, np.float32)), False)
    cl.reset()
    assert np.allclose(cl.process(np.zeros(bs, np.float32), False), 0.0)   # re-primed: first block is silence


@needs_dfn
def test_cleaner_48k_path_no_resampler_runs():
    cl = StreamingDeepFilter(48000.0)                          # sample_rate == DFN3_SR → no internal resampler
    assert cl._to48 is None and cl._from48 is None
    y = cl.process(_sine(300.0, 480 * 4, 48000), False)
    assert y.shape[0] == 480 * 4


@needs_dfn
def test_cleaner_mix_blends_original_back_in():
    """`mix` ("cleaning amount") < 1 blends the lag-aligned original back in, so the output differs
    from full-clean (and stays bounded/same-length) — the gentleness dial for muffling."""
    rng = np.random.default_rng(0)
    sig = ((0.2 * rng.standard_normal(48000)).astype(np.float32) + _sine(220.0, 48000, 48000))

    def run(mix):
        cl = StreamingDeepFilter(48000.0, mix=mix)
        bs = 480
        return np.concatenate([cl.process(sig[i:i + bs], False) for i in range(0, len(sig) - bs, bs)])

    full, half, dry = run(1.0), run(0.5), run(0.0)
    assert full.shape == half.shape == dry.shape
    assert np.all(np.isfinite(half)) and np.all(np.isfinite(dry))
    assert not np.allclose(full, half)                        # the mix actually changes the output
