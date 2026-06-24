"""RTF-MVDR: data-estimated steering (relative transfer function) for the live beam.

The existing MVDR aims a plane-wave manifold ``a0(az)`` (from SRP-PHAT) into a measured noise
covariance. RTF-MVDR instead estimates the target's **relative transfer function** ``h`` from the
data — the real source->mic transfer (reverberation, near-field, per-capsule gain/phase mismatch) —
and uses ``h`` as the steering vector. Per band, ``h`` is the principal generalized eigenvector of
``(R_target, R_noise)`` (the max-SNR / GEVD solution) mapped through the noise covariance:
``h = R_noise · v``. The caller feeds ``h`` into the existing per-bin MVDR solve
``w = R_noise^{-1} h / (hᴴ R_noise^{-1} h)``.

Pure numpy + scipy (no streams): fully unit-testable. The per-band ``M×M`` GEVD runs on the control
thread (off the audio callback), like the rest of the weight computation.
"""
from __future__ import annotations

from typing import Any


def estimate_rtf_gevd(r_target: Any, r_noise: Any, *, loading: float = 1e-3) -> Any:
    """Per-band RTF via the principal generalized eigenvector of ``(R_target, R_noise)``.

    ``r_target`` / ``r_noise`` are ``(B, M, M)`` complex Hermitian band covariances. Returns ``h``
    ``(B, M)`` complex, **unit-norm per band** (no fixed reference capsule, so a dead/hot capsule
    cannot break the estimate). ``R_noise`` is trace-relatively diagonally loaded for a
    positive-definite generalized problem; a degenerate band falls back to a trivial unit vector.
    """
    import numpy as np
    from scipy.linalg import eigh

    rt = np.asarray(r_target)
    rn = np.asarray(r_noise)
    B, M, _ = rt.shape
    eye = np.eye(M)
    h = np.zeros((B, M), dtype=complex)
    for b in range(B):
        load = loading * (float(np.trace(rn[b]).real) / M + 1e-20)
        Rn = rn[b] + load * eye                                   # PD by construction
        try:
            _evals, V = eigh(rt[b], Rn)                           # ascending generalized eigenvalues
            v = V[:, -1]                                          # principal = max generalized eigenvalue
        except Exception:
            v = np.zeros(M, dtype=complex); v[0] = 1.0            # degenerate → trivial
        hb = Rn @ v                                               # RTF from GEVD
        nrm = float(np.linalg.norm(hb))
        h[b] = hb / nrm if nrm > 1e-20 else 0.0
    return h


def rtf_cosine_to_manifold(h: Any, a: Any) -> Any:
    """Per-band cosine similarity ``|hᴴa| / (‖h‖‖a‖)`` in ``[0, 1]`` — the SRP-PHAT cross-check.

    ``h`` and ``a`` are both ``(B, M)`` (the estimated RTF and the plane-wave manifold at the
    detected azimuth, on the same band bins). A low score means the RTF locked onto something other
    than the detected talker; the caller then falls back to the plane-wave steering for that band.
    """
    import numpy as np

    h = np.asarray(h); a = np.asarray(a)
    num = np.abs(np.sum(np.conj(h) * a, axis=1))
    den = np.linalg.norm(h, axis=1) * np.linalg.norm(a, axis=1) + 1e-20
    return num / den
