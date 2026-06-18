"""A/B measurement harness — make the steering concept **audible and measurable**.

Record (or pass in) a raw 8-channel clip, run it through several beamformers
offline, and get back mono signals + a dB report. This is the evidence: you can
*listen* to omni vs delay-and-sum vs superdirective vs nulled, and read the
directivity index / white-noise gain / per-talker leakage for each.

Needs the ``[control]`` extra (numpy; ``record_clip`` also needs sounddevice).
Offline whole-clip processing — no latency concerns, highest quality.
"""
from __future__ import annotations

import wave
from dataclasses import dataclass
from typing import Optional

from .beamformer import (
    MODE_DELAYSUM,
    MODE_SUPERDIRECTIVE,
    design_from_bearings,
    design_zone_beams,
    talker_leakage_db,
)
from .geometry import SOUND_SPEED_MPS, ArrayGeometry
from .live import LiveBeamController

_AB_FRAME = 2048
_AB_HOP = 1024


def omni_reference(y8, np):
    """The 'no beamforming' baseline: plain average of the capsules."""
    return y8.mean(axis=0).astype(np.float32)


def apply_design_offline(design, geom: ArrayGeometry, y8, sr: int, np):
    """Beamform a whole recorded clip with a :class:`BeamDesign` (per-FFT-bin,
    Hann overlap-add). Returns mono float32. Same math as the live runtime, run
    offline on the whole signal."""
    M = geom.n_channels
    elems = np.array(geom.elements, dtype=float)
    active = np.array(geom.active_indices(), dtype=int)
    na = max(1, len(active))
    n_bins = _AB_FRAME // 2 + 1
    freqs = np.fft.rfftfreq(_AB_FRAME, d=1.0 / sr)
    looks = [b.look for b in design.beams]
    nulls = list(design.null_dirs or design.exclusion_dirs)
    superd = design.mode == MODE_SUPERDIRECTIVE
    loading = float(design.loading)
    ap = elems[active]
    dist = np.sqrt(((ap[:, None, :] - ap[None, :, :]) ** 2).sum(-1))

    if not looks:                                   # no pickup zone → omni
        return omni_reference(y8, np)

    W = np.zeros((n_bins, M), dtype=complex)
    for bi, f in enumerate(freqs):
        if f <= 0:
            W[bi, active] = 1.0 / na
            continue
        k = 2.0 * np.pi * f / SOUND_SPEED_MPS
        R = (np.sinc(k * dist / np.pi) + loading * np.eye(na)) if superd else None
        acc = np.zeros(M, dtype=complex)
        for look in looks:
            acc += LiveBeamController._bin_weights(np, elems, k, look, nulls, M, active, R)
        W[bi, :] = acc / max(1, len(looks))

    win = np.hanning(_AB_FRAME)
    nT = y8.shape[1]
    out = np.zeros(nT + _AB_FRAME)
    norm = np.zeros(nT + _AB_FRAME)
    for start in range(0, max(1, nT - _AB_FRAME), _AB_HOP):
        block = y8[:, start:start + _AB_FRAME] * win
        X = np.fft.rfft(block, axis=1).T            # (n_bins, M)
        Y = np.sum(np.conj(W) * X, axis=1)          # (n_bins,)
        y = np.fft.irfft(Y, n=_AB_FRAME)
        out[start:start + _AB_FRAME] += y * win
        norm[start:start + _AB_FRAME] += win ** 2
    norm[norm < 1e-6] = 1.0
    return (out / norm)[:nT].astype(np.float32)


@dataclass
class ABVariant:
    name: str
    mono: object          # numpy float32
    di_db: float
    wng_db: float
    note: str = ""


@dataclass
class ABReport:
    sr: int
    variants: list
    talker_leakage: list  # [(label, gain_db, in_pickup), …] for the main beam
    summary: str


def ab_compare(
    config,
    array_id: str,
    geom: ArrayGeometry,
    y8,
    sr: int,
    *,
    freq_hz: float = 1500.0,
    safe_loading: float = 0.05,
    aggressive_loading: float = 0.005,
) -> ABReport:
    """Process a recorded clip every way and return mono signals + metrics.

    Variants: ``omni`` (no beamforming), ``delaysum``, ``superdirective`` (safe),
    ``superdirective_aggressive`` (low loading — for the low-noise studio mics),
    and ``nulled`` (superdirective + null out-of-zone talkers). Metrics are at
    ``freq_hz``."""
    import numpy as np

    from .steering import look_direction  # noqa

    def design(mode, loading, suppress=False):
        return design_zone_beams(
            config, array_id, geom, freq_hz=freq_hz, mode=mode, loading=loading,
            suppress_outside_talkers=suppress,
        )

    specs = [
        ("omni", None, None, False, "no beamforming (capsule average)"),
        ("delaysum", MODE_DELAYSUM, 0.0, False, "delay-and-sum"),
        ("superdirective", MODE_SUPERDIRECTIVE, safe_loading, False, "superdirective (safe loading)"),
        ("superdirective_aggressive", MODE_SUPERDIRECTIVE, aggressive_loading, False,
         "superdirective aggressive (exploits low-noise mics)"),
        ("nulled", MODE_SUPERDIRECTIVE, safe_loading, True, "superdirective + null out-of-zone talkers"),
    ]
    variants: list[ABVariant] = []
    main_design = None
    for name, mode, loading, suppress, note in specs:
        if name == "omni":
            mono = omni_reference(y8, np)
            variants.append(ABVariant(name, mono, 0.0, 0.0, note))
            continue
        d = design(mode, loading, suppress)
        if not d.beams:
            note = "no pickup zone — omni"
            variants.append(ABVariant(name, omni_reference(y8, np), 0.0, 0.0, note))
            continue
        if main_design is None or name == "nulled":
            main_design = d
        mono = apply_design_offline(d, geom, y8, sr, np)
        b = d.beams[0]
        variants.append(ABVariant(name, mono, b.di_db, b.wng_db, note))

    leak = []
    if main_design and main_design.beams and config.talkers:
        for _tid, label, gain, in_pk in talker_leakage_db(
            config, array_id, geom, list(main_design.beams[0].weights), freq_hz
        ):
            leak.append((label, gain, in_pk))

    # summary text
    def rms_db(x):
        import numpy as _np
        return 20.0 * _np.log10(float(_np.sqrt((x ** 2).mean())) + 1e-12)

    lines = [f"A/B beamformer comparison @ {freq_hz:.0f} Hz "
             f"({geom.n_active}/{geom.n_channels} capsules, aperture {geom.aperture_m()*100:.0f} cm)", ""]
    omni_db = rms_db(variants[0].mono)
    lines.append(f"{'variant':30s}  out-level   DI     WNG")
    for v in variants:
        lines.append(f"{v.name:30s}  {rms_db(v.mono) - omni_db:+5.1f} dB  {v.di_db:+4.1f}  {v.wng_db:+5.1f} dB")
    if leak:
        lines.append("")
        lines.append("Per-talker pickup (main beam):")
        for label, gain, in_pk in sorted(leak, key=lambda r: -r[1]):
            lines.append(f"  {label}: {gain:+.0f} dB  [{'pickup' if in_pk else 'OUTSIDE'}]")
    return ABReport(sr=sr, variants=variants, talker_leakage=leak, summary="\n".join(lines))


@dataclass
class NullDepthReport:
    """How much a steered beam's spatial null at ``null_az_deg`` suppresses energy coming FROM that
    direction, and whether the look at ``look_az_deg`` is preserved. Measured by beamforming a clip
    BOTH ways — look-only vs look+null — and comparing output power. On a small (≈40 mm) array the
    broadband depth is modest (a few dB), so this is the honest *spatial* figure to set against the
    single-channel cleaner's deeper cut."""
    look_az_deg: float
    null_az_deg: float
    null_depth_db: float            # interferer-direction power, look+null vs look-only (negative = suppressed)
    look_change_db: Optional[float]  # look-direction power change (≈0 = preserved), if a look clip is given
    summary: str


def measure_null_depth(geom: ArrayGeometry, interferer_y8, sr: int, look_az_deg: float, null_az_deg: float,
                       *, talker_y8=None, off_nadir_deg: float = 90.0, freq_hz: float = 1500.0,
                       loading: float = 0.02, mode: str = MODE_SUPERDIRECTIVE) -> "NullDepthReport":
    """Measure the null depth a steered beam achieves on a SECOND source. ``interferer_y8`` is a raw
    ``(M, samples)`` clip dominated by the interferer at ``null_az_deg`` (e.g. a fan with nobody talking);
    ``talker_y8`` is an optional clip from ``look_az_deg``. Beams each clip look-only vs look+null (LCMV)
    and reports the dB change — the interferer should drop, the talker should stay. Pure offline math
    (reuses :func:`apply_design_offline`); hardware-free and deterministic."""
    import numpy as np

    look = (float(look_az_deg), float(off_nadir_deg))
    nullb = (float(null_az_deg), float(off_nadir_deg))
    d_off = design_from_bearings(geom, look, nulls=(), freq_hz=freq_hz, mode=mode, loading=loading)
    d_on = design_from_bearings(geom, look, nulls=[nullb], freq_hz=freq_hz, mode=mode, loading=loading)

    def _rms(d, y8) -> float:
        m = apply_design_offline(d, geom, y8, sr, np)
        return float(np.sqrt(np.mean(m[_AB_FRAME:] ** 2)) + 1e-20)   # skip the first frame (OLA edge)

    null_db = 20.0 * float(np.log10(_rms(d_on, interferer_y8) / _rms(d_off, interferer_y8)))
    look_db = None
    if talker_y8 is not None:
        look_db = 20.0 * float(np.log10(_rms(d_on, talker_y8) / _rms(d_off, talker_y8)))
    lines = [f"Null at {null_az_deg:.0f}° (look {look_az_deg:.0f}°): interferer {null_db:+.1f} dB"]
    if look_db is not None:
        lines.append(f"talker at look preserved {look_db:+.1f} dB")
    return NullDepthReport(float(look_az_deg), float(null_az_deg), null_db, look_db, " · ".join(lines))


def save_ab_report(report: ABReport, out_dir: str) -> list:
    """Write each variant to ``<out_dir>/<name>.wav`` + ``report.txt``. Returns the
    list of written paths."""
    import os

    import numpy as np

    os.makedirs(out_dir, exist_ok=True)
    written = []
    for v in report.variants:
        path = os.path.join(out_dir, f"{v.name}.wav")
        x = np.clip(v.mono, -1.0, 1.0)
        with wave.open(path, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(int(report.sr))
            w.writeframes((x * 32767.0).astype("<i2").tobytes())
        written.append(path)
    rp = os.path.join(out_dir, "report.txt")
    with open(rp, "w", encoding="utf-8") as f:
        f.write(report.summary + "\n")
    written.append(rp)
    return written


def record_clip(device, sr: int, seconds: float, channels: int = 8):
    """Record a raw multi-channel clip from the array → ``(channels, samples)``
    float32. Needs sounddevice."""
    import numpy as np
    import sounddevice as sd

    rec = sd.rec(int(seconds * sr), samplerate=sr, channels=channels, device=device, dtype="float32")
    sd.wait()
    return np.ascontiguousarray(rec.T.astype(np.float32))
