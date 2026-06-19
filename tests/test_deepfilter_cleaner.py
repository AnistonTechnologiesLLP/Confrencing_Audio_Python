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
    assert r._hist.shape[0] == 0


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
