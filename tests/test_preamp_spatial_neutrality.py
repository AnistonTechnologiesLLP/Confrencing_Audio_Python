"""Phase-0 #2 gate for the mic-input preamp: a uniform input gain is spatially neutral.

The preamp (manual or auto) multiplies every capsule by the SAME scalar ``g`` before the
beamformer. That scales the measured covariance by ``g²``. For the beam to be unchanged —
same look direction, same DOA, same null depth — the data-adaptive solve must be *scale
invariant*. This is the absolute-vs-relative-regularization bug class already caught twice in
this codebase, so it gets its own guard test independent of the preamp code.

Two checks, both hardware-free and deterministic (synthetic plane-wave covariances):

1. SRP-PHAT DOA is invariant to ``×g`` — PHAT whitening (``r/|r|``) and the peak-to-median VAD
   are pure ratios, so scaling the input can't move a detected talker or flip the VAD.
2. The live MVDR / LCMV beam weights (``_FreqDomainBeam`` fed a *measured* covariance) are
   invariant to ``×g`` — because the diagonal loading on the measured R is **trace-relative**
   (``rn + loading·tr·I``), the loaded R scales cleanly by ``g²`` and ``w = R⁻¹a/(aᴴR⁻¹a)``
   cancels it exactly. Identical weights ⇒ identical beam pattern ⇒ identical look gain and
   null depth.

If either fails, the preamp is NOT spatially neutral and must not ship until the offending
loading is made trace-relative (see the spec's Invariant B).
"""
import math

import pytest

np = pytest.importorskip("numpy")

import conf_pipeline_control as cc
from conf_pipeline_control import doa
import conf_pipeline_control.polaris_beamformer as pb


C = 343.0
SR = 44100.0
# DOA band used by the detector / beam overlay.
FREQS = np.linspace(doa.DEFAULT_F_LO_HZ, doa.DEFAULT_F_HI_HZ, 40)
# A representative sweep of uniform input gains, from a deep cut to a big boost.
SCALES = (0.01, 0.1, 0.5, 2.0, 10.0, 100.0)


def _unit(az_deg, off_nadir_deg=90.0):
    """Plane-wave direction unit vector — matches the production steering convention
    (azimuth 0° = +Y, clockwise; off-nadir 90° = horizontal)."""
    az = math.radians(az_deg)
    n = math.radians(off_nadir_deg)
    s = math.sin(n)
    return np.array([s * math.sin(az), s * math.cos(az), -math.cos(n)])


def _band_cov(freqs, geom, azimuths, *, amps=None, off_nadir=90.0, noise=1e-3):
    """A measured-style band covariance R(f) = noise·I + Σ aₘ·a aᴴ over plane-wave sources,
    evaluated at the given band frequencies. ``amps`` scales each source's power (defaults to
    unit). Shape (len(freqs), M, M)."""
    elems = np.array(geom.elements, dtype=float)
    M = geom.n_channels
    if amps is None:
        amps = [1.0] * len(azimuths)
    R = np.zeros((len(freqs), M, M), dtype=complex)
    for fi, f in enumerate(freqs):
        k = 2.0 * np.pi * f / C
        acc = noise * np.eye(M, dtype=complex)
        for az, amp in zip(azimuths, amps):
            a = np.exp(1j * k * (elems @ _unit(az, off_nadir)))
            acc += amp * np.outer(a, np.conj(a))
        R[fi] = acc
    return R


def _peaks(result):
    return sorted(round(d.azimuth_deg, 3) for d in result.detections)


def test_doa_is_invariant_to_uniform_input_scale():
    """A source + interferer: detected azimuths and the VAD flag don't move under ×g."""
    geom = cc.sensibel_8(radius_m=0.040)
    # A dominant talker at 30° plus a weaker interferer at 200°: the talker clears the VAD
    # peak-to-median floor and is the picked peak (two equal-power sources would flatten the
    # SRP map below the VAD floor — a detector property, unrelated to input scale).
    R = _band_cov(FREQS, geom, [30.0, 200.0], amps=[1.0, 0.25])

    ref = doa.detect(R, FREQS, geom)
    assert ref.active is True
    assert ref.detections, "expected at least one detected talker in the reference"

    for g in SCALES:
        scaled = doa.detect((g * g) * R, FREQS, geom)
        assert scaled.active == ref.active, f"VAD flipped at scale {g}"
        assert _peaks(scaled) == _peaks(ref), f"DOA peaks moved at scale {g}"


def _make_beam(geom, scale, R, bidx):
    """A live frequency-domain MVDR beam whose noise-cov provider returns ``scale·R``."""
    return pb._FreqDomainBeam(
        geom, SR, C,
        loading=pb.DEFAULT_SUPERDIRECTIVE_LOADING,
        noise_cov_provider=lambda: (scale * R, bidx),
    )


def test_mvdr_lcmv_weights_are_invariant_to_uniform_input_scale():
    """The data-adaptive beam weights (and therefore the look gain + null depth) are
    unchanged when the measured covariance is scaled by g² — proving the trace-relative
    loading on the measured R is scale invariant."""
    geom = cc.sensibel_8(radius_m=0.040)
    look_az, null_az = 30.0, 200.0

    # Band the beam will overlay the measured R onto (indices into its own rfft bins).
    probe = _make_beam(geom, 1.0, np.zeros((1, geom.n_channels, geom.n_channels), complex), [0])
    bidx = doa.band_indices(probe._freqs)
    band_freqs = probe._freqs[bidx]
    R = _band_cov(band_freqs, geom, [look_az, null_az])

    # Reference weights (unit scale), plain MVDR and LCMV-with-explicit-null.
    w_ref_mvdr = _make_beam(geom, 1.0, R, bidx).plan_look(look_az, 90.0, nulls=())
    w_ref_lcmv = _make_beam(geom, 1.0, R, bidx).plan_look(look_az, 90.0, nulls=[null_az])
    assert np.all(np.isfinite(w_ref_mvdr)) and np.all(np.isfinite(w_ref_lcmv))

    for g in SCALES:
        w_mvdr = _make_beam(geom, g * g, R, bidx).plan_look(look_az, 90.0, nulls=())
        w_lcmv = _make_beam(geom, g * g, R, bidx).plan_look(look_az, 90.0, nulls=[null_az])
        assert np.allclose(w_mvdr, w_ref_mvdr, rtol=1e-6, atol=1e-9), f"MVDR weights moved at scale {g}"
        assert np.allclose(w_lcmv, w_ref_lcmv, rtol=1e-6, atol=1e-9), f"LCMV weights moved at scale {g}"
