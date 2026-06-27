"""Tests for the clean-mono egress layer (Phase 4).

The egress router receives ONLY the final processed clean mono and routes it to the engine-rate
(48 kHz) PCM, a 16 kHz ASR-ready int16 path, a mono WAV sink, and optional external sinks. It reuses
the existing int16 WAV pattern and the existing resampler, and it refuses raw multichannel audio so
the 8-channel input can never leak out as the "clean" output.
"""
import wave

import pytest

from conf_pipeline_control.egress import (
    EgressError,
    EgressRouter,
    pcm16_bytes,
    resample_mono,
    to_pcm16,
)


def _np():
    return pytest.importorskip("numpy")


def _mono(n=480, f=1000.0, sr=48000.0, amp=0.3):
    np = _np()
    t = np.arange(n) / sr
    return (amp * np.sin(2 * np.pi * f * t)).astype(np.float32)


# --------------------------------------------------------------------------- #
# Conversion utilities
# --------------------------------------------------------------------------- #
def test_to_pcm16_is_int16_and_saturates_safely():
    np = _np()
    pcm = to_pcm16(np.array([2.0, -2.0, 0.0, 0.5], dtype=np.float32))
    assert pcm.dtype == np.int16
    assert pcm[0] == 32767 and pcm[1] == -32767 and pcm[2] == 0     # clip, no wrap-around
    assert abs(int(pcm[3]) - int(0.5 * 32767)) <= 1


def test_egress_int16_matches_existing_wav_pattern():
    np = _np()
    x = np.array([0.5, -0.5, 1.5, -1.5], dtype=np.float32)
    expected = (np.clip(x, -1.0, 1.0) * 32767.0).astype("<i2")      # the ab_capture / live pattern
    assert np.array_equal(to_pcm16(x).astype("<i2"), expected)


def test_resample_48k_to_16k_length_is_correct():
    np = _np()
    y = resample_mono(_mono(n=48000), 48000, 16000)
    assert abs(y.shape[0] - 16000) <= 1                            # 48000 * 16/48 = 16000


def test_resample_44100_to_16k_length_is_correct():
    np = _np()
    y = resample_mono(_mono(n=44100, sr=44100.0), 44100, 16000)
    assert abs(y.shape[0] - 16000) <= 1


def test_silence_stays_silence():
    np = _np()
    z = np.zeros(4800, dtype=np.float32)
    assert np.all(to_pcm16(z) == 0)
    assert np.all(resample_mono(z, 48000, 16000) == 0.0)


def test_tone_frequency_survives_resample_to_16k():
    np = _np()
    x = _mono(n=48000, f=1000.0, sr=48000.0, amp=0.5)
    y = resample_mono(x, 48000, 16000)
    sp = np.abs(np.fft.rfft(y * np.hanning(len(y))))
    freqs = np.fft.rfftfreq(len(y), 1.0 / 16000.0)
    assert abs(float(freqs[int(np.argmax(sp))]) - 1000.0) < 20.0


def test_resample_rejects_multichannel():
    np = _np()
    with pytest.raises(EgressError):
        resample_mono(np.zeros((100, 8), dtype=np.float32), 48000, 16000)


# --------------------------------------------------------------------------- #
# EgressRouter
# --------------------------------------------------------------------------- #
def test_router_accepts_clean_mono():
    np = _np()
    r = EgressRouter(48000.0)
    x = _mono()
    r.push(x)
    assert r.frames_pushed == x.shape[0]
    assert r.latest_mono().shape == x.shape


def test_router_accepts_column_mono():
    np = _np()
    r = EgressRouter(48000.0)
    r.push(np.zeros((480, 1), dtype=np.float32))
    assert r.latest_mono().ndim == 1


def test_router_rejects_raw_multichannel_as_clean_output():
    np = _np()
    r = EgressRouter(48000.0)
    with pytest.raises(EgressError):
        r.push(np.zeros((480, 8), dtype=np.float32))               # raw 8ch must never be the clean output


def test_router_noop_paths_are_safe():
    np = _np()
    r = EgressRouter(48000.0)
    assert r.latest_mono() is None
    assert r.drain_asr_pcm16() == b""
    assert r.pending_seconds() == 0.0
    r.reset()
    r.close()                                                      # safe with nothing pushed / no wav


def test_latest_pcm16_is_int16_at_engine_rate():
    np = _np()
    r = EgressRouter(48000.0)
    x = _mono()
    r.push(x)
    arr = np.frombuffer(r.latest_pcm16(), dtype="<i2")
    assert arr.shape[0] == x.shape[0]


def test_drain_asr_is_int16_at_16k():
    np = _np()
    r = EgressRouter(48000.0, asr_rate=16000)
    r.push(_mono(n=4800))                                          # 0.1 s @ 48k
    arr = r.drain_asr_array()
    assert arr.dtype == np.int16
    assert abs(arr.shape[0] - 1600) <= 2                          # 4800 * 16/48
    assert r.pending_seconds() == 0.0                             # drain clears the buffer


def test_drain_asr_silence_stays_silence():
    np = _np()
    r = EgressRouter(48000.0)
    r.push(np.zeros(4800, dtype=np.float32))
    assert np.all(r.drain_asr_array() == 0)


def test_wav_sink_writes_processed_mono_and_reads_back(tmp_path):
    np = _np()
    p = str(tmp_path / "out.wav")
    r = EgressRouter(48000.0, wav_path=p)
    x = _mono(amp=0.5)
    r.push(x)
    r.close()
    with wave.open(p, "rb") as w:
        assert w.getnchannels() == 1 and w.getsampwidth() == 2 and w.getframerate() == 48000
        n = w.getnframes()
        back = np.frombuffer(w.readframes(n), dtype="<i2")
    assert n == x.shape[0]
    assert np.array_equal(back, to_pcm16(x).astype("<i2"))        # the written audio IS the pushed processed mono


def test_external_sink_receives_engine_rate_pcm16():
    np = _np()

    class _Spy:
        def __init__(self):
            self.chunks = []

        def write(self, pcm16, sample_rate):
            self.chunks.append((pcm16, sample_rate))

        def close(self):
            pass

    spy = _Spy()
    r = EgressRouter(48000.0, sinks=[spy])
    r.push(_mono())
    assert spy.chunks and spy.chunks[0][1] == 48000
    assert isinstance(spy.chunks[0][0], (bytes, bytearray))


def test_reset_clears_router_state():
    np = _np()
    r = EgressRouter(48000.0)
    r.push(_mono(n=4800))
    assert r.pending_seconds() > 0
    r.reset()
    assert r.pending_seconds() == 0.0
    assert r.latest_mono() is None
    assert r.drain_asr_pcm16() == b""


def test_engine_latency_unchanged_by_egress():
    np = _np()
    from conf_pipeline_control.polaris_beamformer import PolarisBeamformer
    bf = PolarisBeamformer(device=None)
    bf._setup_runtime()
    base = bf.estimated_latency_ms
    r = EgressRouter(bf.sample_rate)
    out = bf.process_block(np.zeros((bf.blocksize, 8), dtype=np.float32))
    r.push(out)
    assert bf.estimated_latency_ms == base                        # egress is downstream of the engine
    assert r.algorithmic_latency_ms == 0.0                        # 48k passthrough route is zero-latency


def test_router_works_as_engine_output_callback():
    np = _np()
    from conf_pipeline_control.polaris_beamformer import PolarisBeamformer
    r = EgressRouter(44100.0)
    bf = PolarisBeamformer(device=None, output_callback=r.push)   # the engine accepts the router as its emit sink
    bf._setup_runtime()
    out = bf.process_block(np.zeros((bf.blocksize, 8), dtype=np.float32))
    r.push(out)                                                   # the emit seam hands the final processed mono here
    assert r.frames_pushed == out.shape[0]


def test_egress_exported_from_package_root():
    import conf_pipeline_control as cc
    assert cc.EgressRouter is EgressRouter
    assert cc.resample_mono is resample_mono
    assert cc.to_pcm16 is to_pcm16
