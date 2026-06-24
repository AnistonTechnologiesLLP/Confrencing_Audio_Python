# RTF-MVDR Beamformer Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an opt-in `MODE_RTF_MVDR` beam mode to `PolarisBeamformer` that steers the existing measured-noise MVDR with a data-estimated relative transfer function (RTF) instead of the plane-wave manifold, gated and validated by the existing SRP-PHAT DOA.

**Architecture:** A new pure module (`rtf_mvdr.py`) estimates the per-band RTF via the principal generalized eigenvector of `(R_target, R_noise)` (GEVD / max-SNR). `PolarisBeamformer` grows a second gated covariance EMA (`_target_cov`, trained on confident-talker frames, mirroring the existing `_noise_cov` trained on `noise_only` frames) and a snapshot provider. `_FreqDomainBeam._compute_weights` gets an RTF branch: when a snapshot is available it replaces the plane-wave manifold `a` on the DOA-band bins with the estimated RTF (a unit-norm cross-check falls back to plane-wave per band when the RTF disagrees with the SRP-PHAT azimuth), then runs the *existing* vectorized solve unchanged. Out-of-band bins and cold start fall back to the current behaviour, so the mode is never worse than today's beam.

**Tech Stack:** Python, numpy, scipy (`scipy.linalg.eigh` generalized eigendecomposition — already a `[control]` dependency), pytest. Realtime split: covariance EMA updates are cheap rank-1 accumulations on the audio thread under the existing lock; the GEVD/solve runs in `plan_look` off the audio lock; weights publish via the existing single atomic assignment.

## Global Constraints

- **Opt-in, default-OFF, bit-exact pass-through when off.** `mode != "rtf_mvdr"` must be byte-identical to today (the established stage recipe).
- **Realtime-safety:** no heavy DSP (GEVD/solve) under a lock or in the audio callback; covariance EMA updates stay cheap; weights rebound by a single atomic assignment (`self._W = plan`); shared state rebound atomically, never reset in place.
- **DSP conventions:** azimuth 0° = +Y clockwise; off-nadir 90° = horizontal; manifold `a(f) = exp(+jk·proj)`; DOA band 300–3800 Hz; beam output low-passed at the ~5.6 kHz aliasing ceiling.
- **Two parallel implementations exist** (`PolarisBeamformer` and `LiveBeamController`). This plan implements **only `PolarisBeamformer`** (v1, per the spec); the `LiveBeamController` port with per-sector RTFs is explicit follow-on, out of scope.
- **venv:** `./.venv/Scripts/python.exe` (NOT `.venv311`). Prefix bash with `cd /c/Work/conferencing-audio-pipeline-py &&`.
- **Tests are hardware-free:** synthetic plane-wave / covariance fixtures, stubbed providers.
- Branch `feat/rtf-mvdr` already exists with the design spec committed.

---

### Task 1: Pure RTF math module (`rtf_mvdr.py`)

**Files:**
- Create: `conf_pipeline_control/rtf_mvdr.py`
- Test: `tests/test_rtf_mvdr.py`

**Interfaces:**
- Produces:
  - `estimate_rtf_gevd(r_target, r_noise, *, loading: float = 1e-3) -> "np.ndarray"` — inputs `(B, M, M)` complex Hermitian band covariances; returns `(B, M)` complex, **unit-norm per band** RTF (principal generalized eigenvector mapped through `R_noise`).
  - `rtf_cosine_to_manifold(h, a) -> "np.ndarray"` — `h` `(B, M)`, `a` `(B, M)` plane-wave manifold (same bins); returns `(B,)` real cosine similarity `|hᴴa| / (‖h‖‖a‖)` in `[0, 1]`, the SRP-PHAT cross-check score.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_rtf_mvdr.py
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
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd /c/Work/conferencing-audio-pipeline-py && ./.venv/Scripts/python.exe -m pytest -q tests/test_rtf_mvdr.py`
Expected: FAIL — `ModuleNotFoundError: No module named 'conf_pipeline_control.rtf_mvdr'`

- [ ] **Step 3: Write the module**

```python
# conf_pipeline_control/rtf_mvdr.py
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
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd /c/Work/conferencing-audio-pipeline-py && ./.venv/Scripts/python.exe -m pytest -q tests/test_rtf_mvdr.py`
Expected: PASS (3 passed)

- [ ] **Step 5: Type-check**

Run: `cd /c/Work/conferencing-audio-pipeline-py && ./.venv/Scripts/python.exe -m mypy`
Expected: `Success: no issues found`

- [ ] **Step 6: Commit**

```bash
git add conf_pipeline_control/rtf_mvdr.py tests/test_rtf_mvdr.py
git commit -m "feat(rtf-mvdr): pure GEVD RTF estimator + DOA cross-check"
```

---

### Task 2: `MODE_RTF_MVDR` constant + config recognition

**Files:**
- Modify: `conf_pipeline_control/polaris_beamformer.py` (near `MODE_MVDR` at line ~153; `_BEAM_MODES` at ~154)
- Test: `tests/test_polaris_beamformer.py` (add one test)

**Interfaces:**
- Produces: `MODE_RTF_MVDR = "rtf_mvdr"` constant; included in `_BEAM_MODES`; accepted by `PolarisBeamformer(mode="rtf_mvdr")`.

- [ ] **Step 1: Write the failing test**

```python
# add to tests/test_polaris_beamformer.py
def test_rtf_mvdr_mode_is_accepted():
    from conf_pipeline_control.polaris_beamformer import (
        PolarisBeamformer, MODE_RTF_MVDR, _BEAM_MODES,
    )
    assert MODE_RTF_MVDR == "rtf_mvdr"
    assert MODE_RTF_MVDR in _BEAM_MODES
    bf = PolarisBeamformer(device=None, mode=MODE_RTF_MVDR)   # constructs without raising
    assert bf.mode == MODE_RTF_MVDR
```

- [ ] **Step 2: Run it to verify it fails**

Run: `cd /c/Work/conferencing-audio-pipeline-py && ./.venv/Scripts/python.exe -m pytest -q tests/test_polaris_beamformer.py::test_rtf_mvdr_mode_is_accepted`
Expected: FAIL — `ImportError: cannot import name 'MODE_RTF_MVDR'`

- [ ] **Step 3: Add the constant**

In `conf_pipeline_control/polaris_beamformer.py`, after the `MODE_MVDR` line (~153):

```python
MODE_RTF_MVDR = "rtf_mvdr"        # data-adaptive MVDR steered by a data-estimated RTF (not plane-wave a0)
```

And extend `_BEAM_MODES` (~154) to include it:

```python
_BEAM_MODES = (MODE_DELAYSUM, MODE_FRACDELAY, MODE_SUPERDIRECTIVE, MODE_MVDR, MODE_RTF_MVDR)
```

- [ ] **Step 4: Run it to verify it passes**

Run: `cd /c/Work/conferencing-audio-pipeline-py && ./.venv/Scripts/python.exe -m pytest -q tests/test_polaris_beamformer.py::test_rtf_mvdr_mode_is_accepted`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add conf_pipeline_control/polaris_beamformer.py tests/test_polaris_beamformer.py
git commit -m "feat(rtf-mvdr): add MODE_RTF_MVDR constant"
```

---

### Task 3: Target covariance EMA + three-way gating

**Files:**
- Modify: `conf_pipeline_control/polaris_beamformer.py` — `__init__` (~1148-1158, beside `_noise_cov`), `_setup_runtime` (~1634), `reset_transient` noise-cov clear (~1921), `_close`/teardown noise-cov drop (~1820/2073), and the covariance-update block in `process_block` (~2122-2130).
- Test: `tests/test_polaris_beamformer.py`

**Interfaces:**
- Consumes: `MODE_RTF_MVDR` (Task 2); the existing `self.noise_only` property; the per-frame band cross-spectrum `inst` and gate already computed at ~2122.
- Produces: `self._target_cov` `(n_band, M, M)` EMA + `self._target_frames` counter, trained on **confident-talker** frames; `self._target_cov_alpha` (reuse `0.05`). The existing `_noise_cov` continues training on `noise_only` frames. A frame that is neither (ambiguous) trains neither.

- [ ] **Step 1: Write the failing test**

```python
# add to tests/test_polaris_beamformer.py
def test_target_and_noise_covariances_are_gated_separately():
    import numpy as np
    from conf_pipeline_control.polaris_beamformer import PolarisBeamformer, MODE_RTF_MVDR
    bf = PolarisBeamformer(device=None, mode=MODE_RTF_MVDR)
    bf._setup_runtime()                                  # device-free allocation
    assert bf._target_cov is not None and bf._noise_cov is not None
    assert bf._target_frames == 0
    nb = bf._target_cov.shape[0]; M = bf._target_cov.shape[1]
    inst = np.tile(np.eye(M, dtype=complex), (nb, 1, 1))
    bf._accumulate_rtf_covariance(inst, target_present=True)    # talker frame
    assert bf._target_frames == 1
    bf._accumulate_rtf_covariance(inst, target_present=False, noise_only=True)   # noise frame
    assert bf._noise_frames >= 1 and bf._target_frames == 1
    bf._accumulate_rtf_covariance(inst, target_present=False, noise_only=False)  # ambiguous → neither
    assert bf._target_frames == 1
```

- [ ] **Step 2: Run it to verify it fails**

Run: `cd /c/Work/conferencing-audio-pipeline-py && ./.venv/Scripts/python.exe -m pytest -q tests/test_polaris_beamformer.py::test_target_and_noise_covariances_are_gated_separately`
Expected: FAIL — `AttributeError: 'PolarisBeamformer' object has no attribute '_target_cov'`

- [ ] **Step 3: Add the slots + allocation + a small accumulate helper**

In `__init__` (beside `self._noise_cov` ~1152), add:

```python
        self._target_cov: Any = None         # (n_band, M, M) EMA, gated on confident-talker frames (RTF-MVDR)
        self._target_frames = 0
        self._target_cov_alpha = 0.05
```

In `_setup_runtime` (~1634, where `_noise_cov` is allocated for `MODE_MVDR`), broaden to RTF-MVDR and add the target cov:

```python
            if self.mode in (MODE_MVDR, MODE_RTF_MVDR):        # gated noise covariance for the MVDR solve
                self._noise_cov = np.zeros_like(self._cov)
            if self.mode == MODE_RTF_MVDR:                     # gated target covariance for the RTF estimate
                self._target_cov = np.zeros_like(self._cov)
                self._target_frames = 0
```

Add the accumulate helper (next to `_noise_cov_snapshot`, ~1325). It centralises the gating so the audio-thread block and the test share one path:

```python
    def _accumulate_rtf_covariance(self, inst: Any, *, target_present: bool,
                                   noise_only: bool = False) -> None:
        """Three-way gated EMA update (audio thread, holds the cov lock at the call site).

        ``inst`` is the per-band instantaneous cross-spectrum ``(n_band, M, M)``. A confident-talker
        frame trains ``_target_cov``; a ``noise_only`` frame trains ``_noise_cov`` (today's path);
        an ambiguous frame trains neither, so neither covariance is contaminated."""
        if target_present and self._target_cov is not None:
            a = self._target_cov_alpha
            self._target_cov *= (1.0 - a)
            self._target_cov += a * inst
            self._target_frames += 1
        elif noise_only and self._noise_cov is not None:
            an = self._noise_cov_alpha
            self._noise_cov *= (1.0 - an)
            self._noise_cov += an * inst
            self._noise_frames += 1
```

In the `process_block` covariance-update block (~2122-2130), route RTF-MVDR through the new helper. Replace the existing noise-only gate body so that when `mode == MODE_RTF_MVDR` it calls `_accumulate_rtf_covariance(inst, target_present=not self.noise_only, noise_only=self.noise_only)`; the `MODE_MVDR` path keeps its existing `_noise_cov` update unchanged. (Both run under the same `_state_lock`/cov lock already held there.)

In `reset_transient` (~1921, where `_noise_cov` is zeroed) and teardown (`_close` ~2073 / ~1820 where `_noise_cov = None`), mirror for `_target_cov` (`self._target_cov[...] = 0.0` on reset; `self._target_cov = None` on teardown) and reset `self._target_frames = 0`.

- [ ] **Step 4: Run it to verify it passes**

Run: `cd /c/Work/conferencing-audio-pipeline-py && ./.venv/Scripts/python.exe -m pytest -q tests/test_polaris_beamformer.py::test_target_and_noise_covariances_are_gated_separately`
Expected: PASS

- [ ] **Step 5: Run the full beamformer suite (no regressions)**

Run: `cd /c/Work/conferencing-audio-pipeline-py && ./.venv/Scripts/python.exe -m pytest -q tests/test_polaris_beamformer.py`
Expected: PASS (all)

- [ ] **Step 6: Commit**

```bash
git add conf_pipeline_control/polaris_beamformer.py tests/test_polaris_beamformer.py
git commit -m "feat(rtf-mvdr): gated target covariance EMA + three-way accumulate"
```

---

### Task 4: RTF snapshot provider (warmup gate)

**Files:**
- Modify: `conf_pipeline_control/polaris_beamformer.py` — add `_rtf_cov_snapshot` next to `_noise_cov_snapshot` (~1325-1335).
- Test: `tests/test_polaris_beamformer.py`

**Interfaces:**
- Consumes: `_target_cov`/`_noise_cov`/`_target_frames`/`_noise_frames` (Task 3); `_NOISE_WARMUP_FRAMES`; `self._cov_band`.
- Produces: `_rtf_cov_snapshot() -> tuple | None` returning `(target_cov_copy, noise_cov_copy, band_indices)` once **both** covariances pass `_NOISE_WARMUP_FRAMES`, else `None`. This is the callable injected into `_FreqDomainBeam` (Task 5/6).

- [ ] **Step 1: Write the failing test**

```python
# add to tests/test_polaris_beamformer.py
def test_rtf_cov_snapshot_none_until_both_warm():
    import numpy as np
    from conf_pipeline_control.polaris_beamformer import (
        PolarisBeamformer, MODE_RTF_MVDR, _NOISE_WARMUP_FRAMES,
    )
    bf = PolarisBeamformer(device=None, mode=MODE_RTF_MVDR)
    bf._setup_runtime()
    assert bf._rtf_cov_snapshot() is None                       # cold
    nb, M = bf._target_cov.shape[0], bf._target_cov.shape[1]
    inst = np.tile(np.eye(M, dtype=complex), (nb, 1, 1))
    for _ in range(_NOISE_WARMUP_FRAMES + 1):
        bf._accumulate_rtf_covariance(inst, target_present=True)
        bf._accumulate_rtf_covariance(inst, target_present=False, noise_only=True)
    snap = bf._rtf_cov_snapshot()
    assert snap is not None
    tcov, ncov, band = snap
    assert tcov.shape == bf._target_cov.shape and ncov.shape == bf._noise_cov.shape
    assert len(band) == tcov.shape[0]
```

- [ ] **Step 2: Run it to verify it fails**

Run: `cd /c/Work/conferencing-audio-pipeline-py && ./.venv/Scripts/python.exe -m pytest -q tests/test_polaris_beamformer.py::test_rtf_cov_snapshot_none_until_both_warm`
Expected: FAIL — `AttributeError: ... '_rtf_cov_snapshot'`

- [ ] **Step 3: Add the provider**

Next to `_noise_cov_snapshot` (~1325), mirroring its lock + warmup + re-check pattern:

```python
    def _rtf_cov_snapshot(self) -> Any:
        """Thread-safe snapshot for RTF-MVDR: ``(target_cov, noise_cov, band_indices)`` once BOTH the
        target and noise covariances have passed warmup, else ``None`` (cold start → plane-wave
        fallback in the beam). Copies under the cov lock; the band indices map to the beam's rfft bins."""
        with self._state_lock:
            if (self._target_cov is None or self._noise_cov is None
                    or self._target_frames < _NOISE_WARMUP_FRAMES
                    or self._noise_frames < _NOISE_WARMUP_FRAMES):
                return None
            return self._target_cov.copy(), self._noise_cov.copy(), self._cov_band
```

(Use the same lock object `_noise_cov_snapshot` uses — match its `with` exactly.)

- [ ] **Step 4: Run it to verify it passes**

Run: `cd /c/Work/conferencing-audio-pipeline-py && ./.venv/Scripts/python.exe -m pytest -q tests/test_polaris_beamformer.py::test_rtf_cov_snapshot_none_until_both_warm`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add conf_pipeline_control/polaris_beamformer.py tests/test_polaris_beamformer.py
git commit -m "feat(rtf-mvdr): warmup-gated RTF covariance snapshot provider"
```

---

### Task 5: `_FreqDomainBeam` RTF branch (steering swap + cross-check fallback)

**Files:**
- Modify: `conf_pipeline_control/polaris_beamformer.py` — `_FreqDomainBeam.__init__` (~624-645) and `_compute_weights` (~668-732).
- Test: `tests/test_polaris_beamformer.py`

**Interfaces:**
- Consumes: `estimate_rtf_gevd`, `rtf_cosine_to_manifold` (Task 1); a new `rtf_cov_provider` callable returning `(target_cov, noise_cov, band_idx) | None` (Task 4 supplies it).
- Produces: weights identical to today when `rtf_cov_provider` is `None` or returns `None`; otherwise, on the band bins, the manifold `a` is replaced by the GEVD RTF (unit-norm, scattered to active capsules) **except** where the per-band cross-check `rtf_cosine_to_manifold(h, a_band) < RTF_DOA_MIN_COS` (default 0.5), which keeps the plane-wave `a` for that bin. The measured noise covariance overlays `R` on the band exactly as the existing `MODE_MVDR` path.

- [ ] **Step 1: Write the failing test**

```python
# add to tests/test_polaris_beamformer.py
def test_freqdomain_rtf_branch_nulls_interferer_better_than_planewave():
    import numpy as np
    from conf_pipeline_control.geometry import sensibel_8
    from conf_pipeline_control.polaris_beamformer import _FreqDomainBeam

    geom = sensibel_8()
    # measured covariances on the beam's band bins: target at az0, interferer at az1, + diffuse.
    beam = _FreqDomainBeam(geom, 44100.0, 343.0)
    band = np.arange(20, 60)                       # a slice of in-band bins
    M = geom.n_channels
    # build synthetic (n_band, M, M) target/noise covs from manifolds at two azimuths
    def manifold_band(az):
        idx = list(geom.active_indices()); el = np.array([geom.elements[i] for i in idx])
        from conf_pipeline_control.beamformer import _unit_from_az_offnadir
        u = np.array(_unit_from_az_offnadir(az, 90.0))
        k = 2 * np.pi * beam._freqs[band] / 343.0
        a = np.zeros((len(band), M), complex)
        a[:, idx] = np.exp(1j * k[:, None] * (el @ u)[None, :])
        return a
    at, ai = manifold_band(20.0), manifold_band(80.0)
    ncov = np.einsum("bi,bj->bij", ai, ai.conj()) * 4.0 + np.eye(M)[None] * 1.0
    tcov = np.einsum("bi,bj->bij", at, at.conj()) * 10.0 + ncov
    full_t = np.zeros((len(beam._freqs), M, M), complex); full_t[band] = tcov
    full_n = np.zeros((len(beam._freqs), M, M), complex); full_n[band] = ncov
    beam._rtf_cov_provider = lambda: (full_t, full_n, band)

    W = beam._compute_weights(20.0, 90.0, ())     # RTF branch active
    # response of the beam to the interferer manifold should be well below the target response
    resp_t = np.abs(np.sum(np.conj(W[band]) * at, axis=1))
    resp_i = np.abs(np.sum(np.conj(W[band]) * ai, axis=1))
    assert np.median(resp_t) > np.median(resp_i) * 3.0          # interferer suppressed vs target


def test_freqdomain_rtf_provider_none_is_identical_to_planewave():
    import numpy as np
    from conf_pipeline_control.geometry import sensibel_8
    from conf_pipeline_control.polaris_beamformer import _FreqDomainBeam
    geom = sensibel_8()
    a = _FreqDomainBeam(geom, 44100.0, 343.0)                   # no rtf provider
    b = _FreqDomainBeam(geom, 44100.0, 343.0)
    b._rtf_cov_provider = lambda: None                         # cold start → fallback
    Wa = a._compute_weights(33.0, 90.0, ())
    Wb = b._compute_weights(33.0, 90.0, ())
    assert np.allclose(Wa, Wb)                                  # byte-equivalent fallback
```

- [ ] **Step 2: Run it to verify it fails**

Run: `cd /c/Work/conferencing-audio-pipeline-py && ./.venv/Scripts/python.exe -m pytest -q tests/test_polaris_beamformer.py -k freqdomain_rtf`
Expected: FAIL — `_FreqDomainBeam` has no `_rtf_cov_provider` / RTF branch.

- [ ] **Step 3: Add the constructor param + the RTF branch**

In `_FreqDomainBeam.__init__` signature (~628), add a parameter and store it (default `None` keeps today's behaviour):

```python
                 noise_cov_provider: Optional[Callable[[], Any]] = None,
                 rtf_cov_provider: Optional[Callable[[], Any]] = None):
```
```python
        self._noise_cov_provider = noise_cov_provider
        self._rtf_cov_provider = rtf_cov_provider          # mode="rtf_mvdr": (target_cov, noise_cov, band)|None
```

Add a module-level constant near the other beam constants (~155):

```python
RTF_DOA_MIN_COS = 0.5            # per-band RTF↔plane-wave cosine below this → keep plane-wave (cross-check)
```

In `_compute_weights`, **after** the analytic `R` is built and `a = manifold(azimuth_deg)` (~696) and after the existing measured-noise overlay block (~703-709), insert the RTF branch. It overlays both the measured noise covariance (so `R` on the band is the measured noise) and swaps the steering `a` on the band bins to the estimated RTF, with the per-band cross-check fallback:

```python
        rtf_snap = self._rtf_cov_provider() if self._rtf_cov_provider is not None else None
        if rtf_snap is not None:
            import numpy as np
            target_cov, noise_cov, band_idx = rtf_snap
            band_idx = np.asarray(band_idx)
            # active-capsule submatrices on the DOA-band bins
            tt = np.asarray(target_cov)[band_idx][:, idx][:, :, idx]      # (n_band, na, na)
            nn = np.asarray(noise_cov)[band_idx][:, idx][:, :, idx]       # (n_band, na, na)
            from .rtf_mvdr import estimate_rtf_gevd, rtf_cosine_to_manifold
            h = estimate_rtf_gevd(tt, nn, loading=self._loading)         # (n_band, na) unit-norm RTF
            a_band = a[band_idx]                                          # plane-wave manifold on the band
            cos = rtf_cosine_to_manifold(h, a_band)                      # (n_band,) cross-check
            use_rtf = cos >= RTF_DOA_MIN_COS                             # per-band: trust the RTF or not
            a_new = a_band.copy()
            a_new[use_rtf] = h[use_rtf]
            a[band_idx] = a_new                                          # steer with RTF where it agrees
            # overlay the measured NOISE covariance as R on the band (trace-relative loading, as MODE_MVDR)
            tr = np.maximum(np.einsum("bii->b", nn).real / na, 1e-20)
            R[band_idx] = nn + (self._loading * tr)[:, None, None] * np.eye(na)[None, :, :]
```

The rest of `_compute_weights` (the `phis`/solve at ~711-732) runs unchanged on the modified `a` and `R`.

- [ ] **Step 4: Run it to verify it passes**

Run: `cd /c/Work/conferencing-audio-pipeline-py && ./.venv/Scripts/python.exe -m pytest -q tests/test_polaris_beamformer.py -k freqdomain_rtf`
Expected: PASS (2 passed)

- [ ] **Step 5: Type-check + full beamformer suite**

Run: `cd /c/Work/conferencing-audio-pipeline-py && ./.venv/Scripts/python.exe -m mypy && ./.venv/Scripts/python.exe -m pytest -q tests/test_polaris_beamformer.py`
Expected: mypy clean; all beamformer tests pass.

- [ ] **Step 6: Commit**

```bash
git add conf_pipeline_control/polaris_beamformer.py tests/test_polaris_beamformer.py
git commit -m "feat(rtf-mvdr): _FreqDomainBeam RTF steering branch with DOA cross-check fallback"
```

---

### Task 6: Wire the mode end-to-end (`_make_beam` + re-plan trigger)

**Files:**
- Modify: `conf_pipeline_control/polaris_beamformer.py` — `_make_beam` (~1313-1325), the control-thread re-plan condition (~1552), the freq-domain-mode predicate(s) (~1460, ~1899).
- Test: `tests/test_polaris_beamformer.py`

**Interfaces:**
- Consumes: `_FreqDomainBeam(..., rtf_cov_provider=...)` (Task 5), `_rtf_cov_snapshot` (Task 4).
- Produces: `PolarisBeamformer(mode="rtf_mvdr")` builds a `_FreqDomainBeam` whose `rtf_cov_provider` is `_rtf_cov_snapshot` and whose `noise_cov_provider` is `_noise_cov_snapshot`; the control loop re-plans every tick in RTF mode (to pick up the evolving covariances), mirroring `MODE_MVDR`.

- [ ] **Step 1: Write the failing test**

```python
# add to tests/test_polaris_beamformer.py
def test_make_beam_wires_rtf_providers():
    from conf_pipeline_control.polaris_beamformer import PolarisBeamformer, MODE_RTF_MVDR
    bf = PolarisBeamformer(device=None, mode=MODE_RTF_MVDR)
    beam = bf._make_beam(bf.geometry)
    assert beam._rtf_cov_provider is not None
    assert beam._noise_cov_provider is not None        # measured noise overlay still active
```

- [ ] **Step 2: Run it to verify it fails**

Run: `cd /c/Work/conferencing-audio-pipeline-py && ./.venv/Scripts/python.exe -m pytest -q tests/test_polaris_beamformer.py::test_make_beam_wires_rtf_providers`
Expected: FAIL — RTF mode not handled in `_make_beam` (beam has no `_rtf_cov_provider` set).

- [ ] **Step 3: Wire `_make_beam` and the predicates**

In `_make_beam` (~1313), handle the new mode alongside the freq-domain modes:

```python
        if self.mode in (MODE_SUPERDIRECTIVE, MODE_MVDR, MODE_RTF_MVDR):
            provider = self._noise_cov_snapshot if self.mode in (MODE_MVDR, MODE_RTF_MVDR) else None
            rtf_provider = self._rtf_cov_snapshot if self.mode == MODE_RTF_MVDR else None
            return _FreqDomainBeam(geom, self.sample_rate, self.speed_of_sound,
                                   loading=self.loading, off_nadir_deg=self.off_nadir_deg,
                                   frame=self.nfft,
                                   noise_cov_provider=provider,
                                   rtf_cov_provider=rtf_provider)
```
(Match the exact existing keyword args of the current `_FreqDomainBeam(...)` call at ~1317-1320; only add `rtf_cov_provider=rtf_provider`.)

Update the two freq-domain-mode predicates to include RTF-MVDR:
- `~1460`: `return self.mode in (MODE_SUPERDIRECTIVE, MODE_MVDR, MODE_RTF_MVDR)`
- `~1899`: `if self.mode in (MODE_SUPERDIRECTIVE, MODE_MVDR, MODE_RTF_MVDR):`

Update the per-tick re-plan condition (~1552) so RTF mode re-plans every tick like MVDR:
```python
                target_az != self._steered_az or self.mode in (MODE_MVDR, MODE_RTF_MVDR) or self._nulls_engaged()):
```

- [ ] **Step 4: Run it to verify it passes**

Run: `cd /c/Work/conferencing-audio-pipeline-py && ./.venv/Scripts/python.exe -m pytest -q tests/test_polaris_beamformer.py::test_make_beam_wires_rtf_providers`
Expected: PASS

- [ ] **Step 5: Full suite + mypy (no regressions; bit-exact-off holds)**

Run: `cd /c/Work/conferencing-audio-pipeline-py && QT_QPA_PLATFORM=offscreen ./.venv/Scripts/python.exe -m pytest -q --ignore=tests/test_gui_calibrate_front.py --ignore=tests/test_gui_coverage.py --ignore=tests/test_gui_live_seat.py --ignore=tests/test_gui_smoke.py --ignore=tests/test_gui_twokit.py && ./.venv/Scripts/python.exe -m mypy`
Expected: all pass; mypy clean.

- [ ] **Step 6: Commit**

```bash
git add conf_pipeline_control/polaris_beamformer.py tests/test_polaris_beamformer.py
git commit -m "feat(rtf-mvdr): wire MODE_RTF_MVDR through _make_beam + per-tick re-plan"
```

---

### Task 7: Expose RTF-MVDR in the A/B engine + GUI mode combo

**Files:**
- Modify: `conf_pipeline_control/beam_engine.py` (it forwards `mode` into the steered cfg — ensure `"rtf_mvdr"` passes through; it already forwards arbitrary `mode`, so verify, no code change expected) — confirm via test.
- Modify: `conf_pipeline_gui/panels/live.py` — the A/B-engine mode combo `live_beameng_mode` (the steered-engine mode selector).
- Test: `tests/test_gui_autosteer_sectors.py` (construct-and-poke; or a new tiny GUI test file `tests/test_gui_rtf_mode.py`).

**Interfaces:**
- Consumes: `MODE_RTF_MVDR` (Task 2).
- Produces: a `live_beameng_mode` combo entry mapping its `currentData()` to `cc.MODE_RTF_MVDR`, so connecting the A/B engine in that mode builds the RTF beam.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_gui_rtf_mode.py
import os
import pytest
pytest.importorskip("PySide6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import conf_pipeline as cp
from conf_pipeline.model import Point2D
import conf_pipeline_control as cc


@pytest.fixture(scope="module")
def qapp():
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


def test_beameng_mode_combo_has_rtf_mvdr(qapp):
    from conf_pipeline_gui.panels.live import LivePanel
    from conf_pipeline_gui.state import AppState
    c = cp.create_config("rt", "2026-01-01T00:00:00Z")
    c = cp.add_device(c, cp.create_microphone_array("A", "Array", position=Point2D(0.0, 0.0)))
    st = AppState(); st.set_config(c)
    p = LivePanel(st)
    modes = [p.live_beameng_mode.itemData(i) for i in range(p.live_beameng_mode.count())]
    assert cc.MODE_RTF_MVDR in modes
    p.deleteLater()
```

- [ ] **Step 2: Run it to verify it fails**

Run: `cd /c/Work/conferencing-audio-pipeline-py && QT_QPA_PLATFORM=offscreen ./.venv/Scripts/python.exe -m pytest -q tests/test_gui_rtf_mode.py`
Expected: FAIL — `MODE_RTF_MVDR` not in the combo (or `live_beameng_mode` lacks the entry).

- [ ] **Step 3: Add the combo entry**

Find where `live_beameng_mode` is populated in `conf_pipeline_gui/panels/live.py` (the steered-engine mode selector — grep `live_beameng_mode.addItem`). Add an entry after the existing modes:

```python
        self.live_beameng_mode.addItem("RTF-MVDR (learns the talker's signature)", cc.MODE_RTF_MVDR)
```

Confirm `cc.MODE_RTF_MVDR` is exported from `conf_pipeline_control/__init__.py`; if not, add it to the export list next to `MODE_MVDR`.

- [ ] **Step 4: Run it to verify it passes**

Run: `cd /c/Work/conferencing-audio-pipeline-py && QT_QPA_PLATFORM=offscreen ./.venv/Scripts/python.exe -m pytest -q tests/test_gui_rtf_mode.py`
Expected: PASS

- [ ] **Step 5: Verify the engine forwards the mode (no code change expected)**

```python
# add to tests/test_beam_engine.py
def test_beam_engine_forwards_rtf_mode():
    from conf_pipeline_control.beam_engine import BeamEngine
    from conf_pipeline_control.polaris_beamformer import MODE_RTF_MVDR
    eng = BeamEngine(device=None, mode="steered", steered_cfg={"mode": MODE_RTF_MVDR})
    assert eng._steered.mode == MODE_RTF_MVDR
```
Run: `cd /c/Work/conferencing-audio-pipeline-py && ./.venv/Scripts/python.exe -m pytest -q tests/test_beam_engine.py::test_beam_engine_forwards_rtf_mode`
Expected: PASS (if it fails, ensure `mode` is not in `_RESERVED_CFG` stripping — it is a per-back-end cfg key and should pass through; adjust only if needed).

- [ ] **Step 6: Commit**

```bash
git add conf_pipeline_gui/panels/live.py conf_pipeline_control/__init__.py tests/test_gui_rtf_mode.py tests/test_beam_engine.py
git commit -m "feat(rtf-mvdr): expose RTF-MVDR in the A/B engine mode combo"
```

---

### Task 8: Docs (README + CHANGELOG)

**Files:**
- Modify: `README.md` (the real-time beamforming + IntelliMix-comparison sections — add RTF-MVDR as a beam mode), `CHANGELOG.md` (`[Unreleased]`).

**Interfaces:** none (docs only).

- [ ] **Step 1: Update CHANGELOG `[Unreleased] / Added`**

```markdown
- **RTF-MVDR beam mode (`mode="rtf_mvdr"`).** A data-adaptive MVDR steered by a measured **relative
  transfer function** (GEVD / max-SNR over a target vs noise covariance) instead of the plane-wave
  manifold — captures real reverb / near-field / per-capsule mismatch. SRP-PHAT stays for DOA/UI and
  gates the target-vs-noise frames + cross-checks the RTF direction; warmup + cross-check + loading
  fall back to plane-wave MVDR so it is never worse than the existing beam. Opt-in (Beam → Mode),
  default unchanged, bit-exact when off. `PolarisBeamformer` / A-B-engine path (v1).
```

- [ ] **Step 2: Update the README beam-mode list + IntelliMix table**

In the real-time beamforming section, add `rtf_mvdr` to the beam-mode list with one line; in the "Noise reduction / steering" comparison, note RTF-MVDR as the data-driven steering option. (Match the surrounding prose style.)

- [ ] **Step 3: Commit**

```bash
git add README.md CHANGELOG.md
git commit -m "docs(rtf-mvdr): README beam mode + CHANGELOG entry"
```

---

## Final verification (after all tasks)

- [ ] Full suite + mypy:
  `cd /c/Work/conferencing-audio-pipeline-py && QT_QPA_PLATFORM=offscreen ./.venv/Scripts/python.exe -m pytest -q --ignore=tests/test_gui_calibrate_front.py --ignore=tests/test_gui_coverage.py --ignore=tests/test_gui_live_seat.py --ignore=tests/test_gui_smoke.py --ignore=tests/test_gui_twokit.py && ./.venv/Scripts/python.exe -m mypy`
- [ ] `dsp-realtime-reviewer` over the diff (realtime-callback safety, atomic rebind, no per-block alloc, bit-exact-off).
- [ ] **Manual live validation (POLARIS kit, all 8 capsules healthy):** connect the A/B engine in RTF-MVDR mode, run **Capture A/B proof** head-to-head vs `MODE_MVDR`, record the measured SINR / noise-suppression dB. Document the number in the CHANGELOG entry.

## Spec-coverage note: the "min target dwell" rung

The spec's robustness ladder lists *min target dwell* alongside warmup / cross-check / loading /
unit-norm. It is **realized here by the cumulative warmup (`_target_frames >= _NOISE_WARMUP_FRAMES`,
Task 4) + the covariance EMA slew + the per-band cross-check (Task 5)** — not a separate consecutive
counter. A strict consecutive-dwell counter would mis-fire on this design, because target and noise
frames naturally alternate during speech pauses, so it would reset constantly and rarely arm. The
EMA means a brief false detection contributes only `alpha` (≈5%) weight and is washed out, and the
cross-check rejects an RTF that disagrees with the SRP-PHAT azimuth — together covering the intent.

## Notes / follow-on (out of scope)

- Port to `LiveBeamController` (auto-steer) with **per-sector RTFs** for the multi-sector use case.
- Capsule-health / per-capsule calibration (separate feature; RTF-MVDR depends on a healthy array).
- Optional `learning / locked / fallback` status read-out in the GUI (per-stage-meter style).
