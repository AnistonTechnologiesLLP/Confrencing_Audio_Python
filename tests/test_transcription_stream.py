"""Tests for the transcription-ready clean stream (Phase 5).

Consumes Phase 4's 16 kHz ASR-ready mono int16, runs a deterministic energy VAD/chunker, and sends
speech chunks to a pluggable ``TranscriptionProvider`` (a mock here — no real ASR, no network). The
transcription layer never sees raw 8-channel audio and never touches the DSP pipeline.
"""
import pytest

from conf_pipeline_control.transcription import (
    AudioChunk,
    MockTranscriptionProvider,
    SpeechChunker,
    TranscriptionError,
    TranscriptionSession,
    TranscriptionStream,
    TranscriptResult,
)

SR = 16000


def _np():
    return pytest.importorskip("numpy")


def _silence(ms):
    np = _np()
    return np.zeros(int(SR * ms / 1000), dtype=np.int16)


def _speech(ms, amp=0.3, f=300.0):
    np = _np()
    n = int(SR * ms / 1000)
    t = np.arange(n) / SR
    return (amp * np.sin(2 * np.pi * f * t) * 32767).astype(np.int16)


# --------------------------------------------------------------------------- #
# Models + provider (stdlib)
# --------------------------------------------------------------------------- #
def test_transcript_result_roundtrip_is_camelcase():
    r = TranscriptResult(text="hi", segments=({"t": 0.0},), duration_seconds=3.2,
                         provider="mock", language="en")
    d = r.to_dict()
    assert "durationSeconds" in d and d["provider"] == "mock"
    assert TranscriptResult.from_dict(d) == r


def test_session_defaults():
    s = TranscriptionSession(session_id="x")
    assert s.sample_rate == 16000 and s.channels == 1
    assert s.encoding == "pcm_s16le" and s.status == "idle"


def test_transcription_exported_from_root():
    import conf_pipeline_control as cc
    assert cc.TranscriptionStream is TranscriptionStream
    assert cc.MockTranscriptionProvider is MockTranscriptionProvider


def test_mock_provider_lifecycle():
    np = _np()
    prov = MockTranscriptionProvider()
    prov.start_session(TranscriptionSession(session_id="x"))
    assert prov.started
    ch = AudioChunk(pcm16=_speech(300).astype("<i2").tobytes(), sample_rate=16000, channels=1,
                    start_time_seconds=0.0, duration_seconds=0.3, is_speech=True, energy_dbfs=-12.0)
    prov.send_audio_chunk(ch)
    res = prov.stop_session()
    assert prov.stopped and isinstance(res, TranscriptResult)
    assert res.text != "" and res.provider == "mock"
    assert prov.network_calls == 0


def test_mock_empty_session_returns_empty_text():
    prov = MockTranscriptionProvider()
    prov.start_session(TranscriptionSession(session_id="x"))
    res = prov.stop_session()
    assert res.text == "" and res.duration_seconds == 0.0


# --------------------------------------------------------------------------- #
# Stream session lifecycle
# --------------------------------------------------------------------------- #
def test_stream_session_start_stop():
    _np()
    prov = MockTranscriptionProvider()
    st = TranscriptionStream(prov)
    sess = st.start()
    assert sess.status == "running" and prov.started
    res = st.stop()
    assert st.session.status == "stopped" and isinstance(res, TranscriptResult)


def test_push_before_start_raises():
    _np()
    st = TranscriptionStream(MockTranscriptionProvider())
    with pytest.raises(TranscriptionError):
        st.push_pcm16(_speech(300))


# --------------------------------------------------------------------------- #
# VAD / chunking
# --------------------------------------------------------------------------- #
def test_speech_emits_chunks():
    _np()
    prov = MockTranscriptionProvider()
    st = TranscriptionStream(prov)
    st.start()
    st.push_pcm16(_silence(200))
    st.push_pcm16(_speech(800))
    st.push_pcm16(_silence(500))                  # hangover closes the chunk
    st.stop()
    assert len(prov.received) >= 1
    assert all(c.is_speech and c.sample_rate == 16000 and c.channels == 1 for c in prov.received)


def test_silence_emits_no_chunks():
    _np()
    prov = MockTranscriptionProvider()
    st = TranscriptionStream(prov)
    st.start()
    st.push_pcm16(_silence(2000))
    st.stop()
    assert prov.received == []
    assert st.session.chunks_sent == 0


def test_long_speech_splits_by_max_duration():
    _np()
    prov = MockTranscriptionProvider()
    st = TranscriptionStream(prov, preroll_ms=0, max_chunk_ms=500, hangover_ms=200, min_speech_ms=100)
    st.start()
    st.push_pcm16(_silence(200))
    st.push_pcm16(_speech(2000))                  # 2 s continuous → several ~0.5 s chunks
    st.push_pcm16(_silence(400))
    st.stop()
    assert len(prov.received) >= 3
    assert all(c.duration_seconds <= 0.6 for c in prov.received)


def test_short_burst_is_ignored():
    _np()
    prov = MockTranscriptionProvider()
    st = TranscriptionStream(prov)                 # default min_speech_ms = 200
    st.start()
    st.push_pcm16(_silence(200))
    st.push_pcm16(_speech(60))                     # 60 ms < min speech
    st.push_pcm16(_silence(500))
    st.stop()
    assert prov.received == []


def test_chunker_reset_clears_buffered_speech():
    _np()
    ch = SpeechChunker()
    ch.push(_silence(200))
    ch.push(_speech(300))                          # collecting
    ch.reset()
    assert ch.flush() == []                        # nothing pending after reset


def test_chunk_timestamps_and_durations_are_correct():
    _np()
    ch = SpeechChunker(preroll_ms=0, hangover_ms=100, min_speech_ms=100)
    out = ch.push(_silence(500))                   # 0.5 s silence
    out += ch.push(_speech(400))                   # speech begins ~0.5 s in
    out += ch.push(_silence(300))                  # hangover closes the chunk
    out += ch.flush()
    assert len(out) >= 1
    c = out[0]
    assert abs(c.start_time_seconds - 0.5) < 0.05
    assert c.duration_seconds > 0.3


# --------------------------------------------------------------------------- #
# Input validation / processed-only
# --------------------------------------------------------------------------- #
def test_wrong_sample_rate_is_rejected():
    _np()
    st = TranscriptionStream(MockTranscriptionProvider())
    st.start()
    with pytest.raises(TranscriptionError):
        st.push_pcm16(_speech(300), sample_rate=48000)


def test_raw_multichannel_is_rejected():
    np = _np()
    st = TranscriptionStream(MockTranscriptionProvider())
    st.start()
    with pytest.raises(TranscriptionError):
        st.push_pcm16(np.zeros((320, 8), dtype=np.int16))


def test_float_input_is_rejected():
    np = _np()
    st = TranscriptionStream(MockTranscriptionProvider())
    st.start()
    with pytest.raises(TranscriptionError):
        st.push_pcm16(np.zeros(320, dtype=np.float32))      # must come through the ASR-safe int16 path


def test_accepts_int16_bytes_and_array():
    _np()
    prov = MockTranscriptionProvider()
    st = TranscriptionStream(prov, min_speech_ms=100, hangover_ms=200)
    st.start()
    st.push_pcm16(_silence(200))
    st.push_pcm16(_speech(400))                                   # int16 array
    st.push_pcm16(_speech(400).astype("<i2").tobytes())          # int16 bytes
    st.push_pcm16(_silence(400))
    st.stop()
    assert len(prov.received) >= 1


def test_mock_receives_clean_16k_int16_chunks():
    np = _np()
    prov = MockTranscriptionProvider()
    st = TranscriptionStream(prov)
    st.start()
    st.push_pcm16(_silence(200))
    st.push_pcm16(_speech(600))
    st.push_pcm16(_silence(500))
    st.stop()
    assert prov.received
    for c in prov.received:
        assert c.sample_rate == 16000 and c.channels == 1
        arr = np.frombuffer(c.pcm16, dtype="<i2")
        assert arr.dtype == np.int16 and arr.size > 0


# --------------------------------------------------------------------------- #
# Error handling / integration / safety
# --------------------------------------------------------------------------- #
def test_provider_error_is_handled_safely():
    _np()
    prov = MockTranscriptionProvider(fail_on_chunk=0)
    st = TranscriptionStream(prov)
    st.start()
    st.push_pcm16(_silence(200))
    st.push_pcm16(_speech(600))
    with pytest.raises(TranscriptionError):
        st.push_pcm16(_silence(500))                  # closing the chunk → provider send fails
    assert st.session.status == "error"


def test_integration_with_egress_router():
    np = _np()
    import conf_pipeline_control as cc
    router = cc.EgressRouter(48000.0, asr_rate=16000)
    t = np.arange(48000) / 48000.0
    mono = (0.3 * np.sin(2 * np.pi * 300.0 * t)).astype(np.float32)        # 1 s processed mono "speech"
    for i in range(0, 48000, 512):
        router.push(mono[i:i + 512])
    prov = MockTranscriptionProvider()
    st = TranscriptionStream(prov, min_speech_ms=100, hangover_ms=200)
    st.start()
    st.pump_from_egress(router)                        # drain 16 kHz ASR PCM → push
    st.push_pcm16(_silence(400))                       # close the chunk
    st.stop()
    assert len(prov.received) >= 1


def test_no_network_call_by_default():
    _np()
    prov = MockTranscriptionProvider()
    st = TranscriptionStream(prov)
    st.start()
    st.push_pcm16(_silence(200))
    st.push_pcm16(_speech(600))
    st.push_pcm16(_silence(500))
    st.stop()
    assert prov.network_calls == 0


def test_transcription_does_not_change_pipeline_defaults():
    _np()
    st = TranscriptionStream(MockTranscriptionProvider())
    st.start()
    st.push_pcm16(_speech(300))
    st.stop()
    from conf_pipeline_control.polaris_beamformer import PolarisBeamformer
    bf = PolarisBeamformer(device=None)
    assert bf.pre_nr is False and bf._calib is None and bf.post_nr is False
