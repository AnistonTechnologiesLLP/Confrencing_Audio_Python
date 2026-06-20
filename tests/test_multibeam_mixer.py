"""Multi-beam realtime mixer — NOM automix + per-beam steering (hardware-free, synthetic plane waves)."""
import math

import pytest

np = pytest.importorskip("numpy")

import conf_pipeline_control as cc
from conf_pipeline_control.multibeam import BeamSlot, MultiBeamMixer, nom_automix

C = 343.0
SR = 44100.0


def _unit(az_deg, off_nadir_deg=90.0):
    a = math.radians(az_deg)
    n = math.radians(off_nadir_deg)
    s = math.sin(n)
    return np.array([s * math.sin(a), s * math.cos(a), -math.cos(n)])


def _plane_wave(geom, az_deg, n, *, tones=(900.0, 1500.0, 2500.0), env=1.0):
    """An (n, M) in-band plane wave from az_deg (each capsule advanced by its propagation lead)."""
    elems = np.array(geom.elements, dtype=float)
    proj = elems @ _unit(az_deg)
    t = np.arange(n) / SR
    sig = env * sum(np.sin(2 * np.pi * f * t) for f in tones)
    x = np.zeros((n, geom.n_channels), dtype=float)
    for m in range(geom.n_channels):
        x[:, m] = np.roll(sig, -int(round(proj[m] / C * SR)))
    return x.astype(np.float32)


# --------------------------------------------------------------------------- nom_automix
def test_nom_automix_one_open_is_unity():
    ones = np.ones(16, dtype=np.float32)
    out = nom_automix([1.0, 0.0], [ones, np.zeros(16, dtype=np.float32)])
    assert np.allclose(out, ones)


def test_nom_automix_two_open_applies_nom_attenuation():
    ones = np.ones(16, dtype=np.float32)
    out = nom_automix([1.0, 1.0], [ones, ones])
    assert np.allclose(out, 2.0 / math.sqrt(2.0))            # √2 ≈ -3 dB vs a naive 2x sum
    assert out.dtype == np.float32


def test_nom_automix_nothing_open_is_silence():
    out = nom_automix([0.0, 0.0], [np.ones(8, dtype=np.float32), np.ones(8, dtype=np.float32)])
    assert out.shape == (8,) and not np.any(out)


# --------------------------------------------------------------------------- MultiBeamMixer
def test_mixer_steers_each_beam_to_its_slot():
    """Two beams aimed at 0° and 90°: a source FROM 0° lands far louder in beam-0 than in beam-1
    (which looks at 90° and nulls 0°). Validates per-beam steering + cross-nulls."""
    geom = cc.sensibel_8(radius_m=0.040)
    mix = MultiBeamMixer(geom, SR, C, n_beams=2)
    mix.set_slots([BeamSlot(0, 0.0, None, True, False), BeamSlot(1, 90.0, None, True, False)])
    blk = _plane_wave(geom, 0.0, 2048)
    for _ in range(5):                                       # prime the STFT/OLA FIFOs
        _mixed, monos, _gates = mix.process_block(blk)
    e0 = float(np.mean(np.asarray(monos[0]) ** 2))
    e1 = float(np.mean(np.asarray(monos[1]) ** 2))
    assert e0 > 4.0 * e1                                     # the 0° beam wins by a wide margin


def test_mixer_idle_slot_is_gated_out():
    geom = cc.sensibel_8(radius_m=0.040)
    mix = MultiBeamMixer(geom, SR, C, n_beams=2)
    mix.set_slots([BeamSlot(0, 0.0, None, True, False), BeamSlot(1, None, None, False, False)])
    _mixed, _monos, gates = mix.process_block(_plane_wave(geom, 0.0, 1024))
    assert gates[1] == 0.0                                   # an idle slot never opens its mic


def test_mixer_live_gate_opens_for_syllabic_input_not_steady():
    """A live beam's gate (the fan-proof scorer) opens for a ~4 Hz amplitude-modulated source and stays
    low for a steady one — so the automix only sums beams that actually carry speech."""
    geom = cc.sensibel_8(radius_m=0.040)
    block = 2048
    dur = block / SR

    def run(modulated):
        mix = MultiBeamMixer(geom, SR, C, n_beams=1, hop_seconds=dur)
        mix.set_slots([BeamSlot(0, 0.0, None, True, False)])
        late = []                                            # steady-state, after the onset transient settles
        for b in range(90):
            env = 0.5 * (1.0 + math.sin(2 * math.pi * 4.0 * b * dur)) if modulated else 1.0
            _m, _mo, gates = mix.process_block(_plane_wave(geom, 0.0, block, env=env))
            if b >= 60:
                late.append(gates[0])
        return max(late)

    assert run(modulated=True) > run(modulated=False) + 0.2  # sustained syllabic keeps the gate open
    assert run(modulated=True) > 0.15
