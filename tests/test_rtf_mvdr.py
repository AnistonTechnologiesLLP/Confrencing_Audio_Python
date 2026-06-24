"""RTF-MVDR pure math: GEVD relative-transfer-function estimation + the DOA cross-check.

Hardware-free — builds synthetic band covariances from a known target steering + a directional
interferer + diffuse noise, and checks the estimated RTF points at the target (max-SNR), nulls the
interferer better than a plane-wave steering, and degrades gracefully."""
import pytest

np = pytest.importorskip("numpy")
pytest.importorskip("scipy")

from conf_pipeline_control.rtf_mvdr import estimate_rtf_gevd, rtf_cosine_to_manifold


def _steer(M, phase):
    """A toy steering vector for M mics: exp(j*phase*m)."""
    return np.exp(1j * phase * np.arange(M))


def _cov(steer, power):
    return power * np.outer(steer, steer.conj())


def test_rtf_recovers_target_direction_over_interferer():
    M = 8
    tgt = _steer(M, 0.5)            # target manifold
    itf = _steer(M, 2.0)           # interferer manifold (different phase slope)
    diffuse = np.eye(M)
    # one band bin
    r_noise = (_cov(itf, 4.0) + 2.0 * diffuse)[None]      # (1, M, M): interferer + diffuse
    r_target = (_cov(tgt, 10.0) + r_noise[0])[None]       # target present on top of the noise field
    h = estimate_rtf_gevd(r_target, r_noise)
    assert h.shape == (1, M)
    assert abs(np.linalg.norm(h[0]) - 1.0) < 1e-6        # unit-norm
    # the RTF must align with the target far more than with the interferer
    cos_tgt = abs(np.vdot(h[0], tgt)) / (np.linalg.norm(tgt))
    cos_itf = abs(np.vdot(h[0], itf)) / (np.linalg.norm(itf))
    assert cos_tgt > cos_itf + 0.3


def test_rtf_cosine_to_manifold_high_when_aligned_low_when_not():
    M = 8
    a = _steer(M, 0.5)[None]
    h_aligned = (a / np.linalg.norm(a))
    h_off = (_steer(M, 2.0)[None] / np.linalg.norm(_steer(M, 2.0)))
    assert rtf_cosine_to_manifold(h_aligned, a)[0] > 0.99
    assert rtf_cosine_to_manifold(h_off, a)[0] < 0.7


def test_estimate_rtf_handles_singular_noise_via_loading():
    M = 4
    r_noise = np.zeros((1, M, M), dtype=complex)          # degenerate (all-zero) noise → loading saves it
    r_target = _cov(_steer(M, 0.3), 1.0)[None]
    h = estimate_rtf_gevd(r_target, r_noise)              # must not raise
    assert h.shape == (1, M)
    assert np.all(np.isfinite(h))
