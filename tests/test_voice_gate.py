"""VoiceOnlyGate — speech-presence output gate (hardware-free, synthetic signals)."""
import math

import pytest

np = pytest.importorskip("numpy")

from conf_pipeline_control.voice_gate import VoiceOnlyGate

FS = 44100.0


def _rms(x):
    x = np.asarray(x, dtype=np.float64)
    return float(np.sqrt(np.mean(x * x))) if x.size else 0.0


def _stream(vg, x, blk=512):
    out = [vg.process(x[i:i + blk]) for i in range(0, len(x), blk)]
    return np.concatenate(out) if out else np.asarray([], np.float32)


def _syllabic(n, rng, amp=0.2, mod_hz=4.0):
    """Noise amplitude-modulated at a syllabic rate ≈ speech."""
    t = np.arange(n) / FS
    env = 0.5 + 0.5 * np.sin(2.0 * math.pi * mod_hz * t)
    return (amp * env * rng.standard_normal(n)).astype(np.float32)


def test_output_length_and_dtype():
    vg = VoiceOnlyGate(FS)
    out = vg.process((0.1 * np.ones(512)).astype(np.float32))
    assert out.shape[0] == 512 and out.dtype == np.float32


def test_speech_opens_the_gate():
    vg = VoiceOnlyGate(FS)
    x = _syllabic(int(1.5 * FS), np.random.default_rng(0))
    _stream(vg, x)
    assert vg.gate_open and vg._gain > 0.8           # syllabic speech → open


def test_steady_tone_closes_the_gate():
    """A steady hum/fan proxy (constant-amplitude tone, no syllabic modulation) ducks toward the floor."""
    vg = VoiceOnlyGate(FS)
    t = np.arange(int(1.5 * FS)) / FS
    x = (0.1 * np.sin(2.0 * math.pi * 200.0 * t)).astype(np.float32)
    _stream(vg, x)
    assert not vg.gate_open and vg._gain < 0.4       # non-speech → ducked


def test_onset_is_not_silenced():
    """A talker starting after a silence keeps the first syllable — the onset opens the gate fast, and
    the floor is a duck (never a mute), so it's recoverable (Invariant C)."""
    vg = VoiceOnlyGate(FS)
    sil = np.zeros(int(0.5 * FS), dtype=np.float32)
    talk = _syllabic(int(0.6 * FS), np.random.default_rng(2))
    x = np.concatenate([sil, talk])
    y = _stream(vg, x)
    onset = slice(len(sil), len(sil) + int(0.05 * FS))     # first 50 ms of speech
    assert _rms(y[onset]) > 0.3 * _rms(x[onset])            # not silenced
    # and it fully recovers shortly after
    body = slice(len(sil) + int(0.2 * FS), len(sil) + int(0.5 * FS))
    assert _rms(y[body]) > 0.75 * _rms(x[body])


def test_floor_is_a_duck_not_a_mute():
    vg = VoiceOnlyGate(FS, floor_db=-15.0)
    t = np.arange(int(1.5 * FS)) / FS
    x = (0.1 * np.sin(2.0 * math.pi * 200.0 * t)).astype(np.float32)
    _stream(vg, x)
    assert vg._gain > 0.1                              # floor ≈ 0.18 (-15 dB) — attenuated, not silenced


def test_reset_clears_state():
    vg = VoiceOnlyGate(FS)
    _stream(vg, _syllabic(int(0.5 * FS), np.random.default_rng(3)))
    vg.reset()
    assert vg._scorer is None and vg._gain == 1.0 and vg.gate_open is True


def test_wires_into_polaris_beamformer():
    from conf_pipeline_control.polaris_beamformer import PolarisBeamformer
    bf = PolarisBeamformer(voice_gate=True)
    bf._setup_runtime()
    assert bf._voice_gate is not None
    off = PolarisBeamformer()
    off._setup_runtime()
    assert off._voice_gate is None                    # off by default
