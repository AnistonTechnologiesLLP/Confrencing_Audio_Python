# Honest Aperture-Aware Coverage — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the coverage simulation reflect a small array's real (aperture-limited) directivity so a POLARIS table layout can't be authored with seats the array physically cannot separate.

**Architecture:** A new pure-stdlib `conf_pipeline/directivity.py` computes a calibrated aperture-aware beamwidth; the placement scorer (`sim/scoring.py`) and the coverage report (`coverage_sim.py`) use it instead of the fixed 35° when the array's profile declares an `aperture_m`. Aperture lives in the profile catalog (code, not serialized). Separability + grating-lobe warnings attach to the existing `RoomCoverage.caveats`. Opt-in: arrays without `aperture_m` keep the legacy 35° (zero regression).

**Tech Stack:** Python 3.10+, pure stdlib `math` in `conf_pipeline` (numpy-free); numpy only in the `[control]`-gated calibration test.

## Global Constraints

- `conf_pipeline` stays **numpy-free** — `directivity.py`, `profiles.py`, `sim/scoring.py`, `coverage_sim.py` import only stdlib. numpy is allowed only in the `[control]`-gated calibration test.
- **No schema change:** `aperture_m`/`element_spacing_m` are profile-catalog constants (configs persist only `profileId`). Do NOT bump `CONFIG_VERSION`, add a migration, or touch the TS sibling.
- **No regression:** add `aperture_m` ONLY to the new `polaris-8` profile. Leave `generic-ceiling-array`/`generic-table-array` with `aperture_m=None` → they keep the legacy 35°. Existing tests must pass untouched.
- **Azimuth/angle convention:** 0° = +Y, clockwise; off-nadir 0° = straight down, 90° = horizontal.
- venv: `./.venv/Scripts/python.exe` (NOT `.venv311`). GUI tests need `QT_QPA_PLATFORM=offscreen`.
- Git: branch `feat/aperture-aware-coverage` (already created). Commit per task; **do not push** (push/PR only when the user asks). End commit messages with the `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>` trailer.
- After the suite-affecting tasks, run the **green-gate** agent; **docs-maintainer** for the final docs task.

---

### Task 1: `directivity.py` — the aperture-aware beamwidth model

**Files:**
- Create: `conf_pipeline/directivity.py`
- Test: `tests/test_directivity.py`

**Interfaces:**
- Produces:
  - `steered_beamwidth_deg(aperture_m: Optional[float], freq_hz: float, steer_deg: float) -> float` — 3 dB main-lobe **half-angle** (deg).
  - `alias_ceiling_hz(element_spacing_m: Optional[float]) -> float` — spatial-aliasing ceiling (Hz); `inf` when unknown.
  - `separable(sep_deg: float, beamwidth_half_deg: float, factor: float = 1.5) -> bool`.
  - Constants `SOUND_SPEED_MPS = 343.0`, `SIM_SPEECH_FREQ_HZ = 1500.0`, `NEAR_OMNI_HALF_DEG = 90.0`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_directivity.py
import math
from conf_pipeline.directivity import (
    steered_beamwidth_deg, alias_ceiling_hz, separable,
    NEAR_OMNI_HALF_DEG, SIM_SPEECH_FREQ_HZ,
)

POLARIS_AP = 0.08      # 40 mm radius → ~80 mm aperture
POLARIS_SP = 0.0306    # adjacent capsule spacing 2*R*sin(pi/8)

def test_none_or_zero_aperture_is_near_omni():
    assert steered_beamwidth_deg(None, 1500.0, 0.0) == NEAR_OMNI_HALF_DEG
    assert steered_beamwidth_deg(0.0, 1500.0, 0.0) == NEAR_OMNI_HALF_DEG

def test_higher_freq_is_narrower():
    lo = steered_beamwidth_deg(POLARIS_AP, 800.0, 0.0)
    hi = steered_beamwidth_deg(POLARIS_AP, 3400.0, 0.0)
    assert hi < lo  # more directive up high

def test_larger_aperture_is_narrower():
    small = steered_beamwidth_deg(0.08, 2000.0, 0.0)
    big = steered_beamwidth_deg(0.40, 2000.0, 0.0)
    assert big < small

def test_off_broadside_is_wider():
    broad = steered_beamwidth_deg(POLARIS_AP, 3400.0, 0.0)
    endfire = steered_beamwidth_deg(POLARIS_AP, 3400.0, 90.0)
    assert endfire >= broad

def test_polaris_is_near_omni_low_and_coarse_high():
    # 40 mm array: essentially omni at low speech freq, still coarse (>30 deg half) up high
    assert steered_beamwidth_deg(POLARIS_AP, 700.0, 90.0) >= 80.0
    assert steered_beamwidth_deg(POLARIS_AP, 3400.0, 0.0) >= 30.0

def test_ceiling_reference_aperture_is_about_35_deg():
    # a ~0.10 m aperture at the speech centre lands near the legacy 35 deg half-angle
    h = steered_beamwidth_deg(0.10, SIM_SPEECH_FREQ_HZ, 0.0)
    assert 28.0 <= h <= 45.0

def test_alias_ceiling_polaris_about_5p6k():
    assert 5200.0 <= alias_ceiling_hz(POLARIS_SP) <= 6000.0
    assert alias_ceiling_hz(None) == float("inf")

def test_separable_boundary():
    assert separable(120.0, 30.0)        # 120 >= 1.5*2*30? no -> use factor on half: 1.5*30=45 -> 120>=45 True
    assert not separable(20.0, 60.0)     # 20 < 1.5*60=90 -> not separable
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./.venv/Scripts/python.exe -m pytest -q tests/test_directivity.py`
Expected: FAIL — `ModuleNotFoundError: No module named 'conf_pipeline.directivity'`.

- [ ] **Step 3: Write minimal implementation**

```python
# conf_pipeline/directivity.py
"""Aperture-aware beamwidth model (pure stdlib, numpy-free).

Honest directivity for the coverage simulation: a small array (e.g. the 40 mm
POLARIS) is near-omni at low speech frequencies and only mildly directive up
high, with a spatial-aliasing ceiling set by element spacing. The constants are
calibrated to the measured sensibel_8 beam (see tests/test_directivity_calibration.py).
"""
from __future__ import annotations

import math
from typing import Optional

SOUND_SPEED_MPS = 343.0
SIM_SPEECH_FREQ_HZ = 1500.0   # representative speech-band centre for the single-narrowband sim
NEAR_OMNI_HALF_DEG = 90.0     # a half-angle of 90 deg = no usable directivity in the look plane

# Calibration constants (refined in tests/test_directivity_calibration.py).
_BW_K = 0.886                 # 3 dB FULL beamwidth ~= _BW_K * lambda / aperture (radians)
_ENDFIRE_WIDEN = 1.6          # extra widening as the look tilts toward endfire (off-nadir 90 deg)


def steered_beamwidth_deg(aperture_m: Optional[float], freq_hz: float, steer_deg: float) -> float:
    """3 dB main-lobe HALF-angle (deg) of the steered beam.

    ``steer_deg`` is the look angle off the array's broadside reference (0 = broadside,
    90 = endfire); the beam widens toward endfire. Returns ``NEAR_OMNI_HALF_DEG`` when the
    aperture/frequency are unknown or when the wavelength dwarfs the aperture (no directivity).
    """
    if not aperture_m or aperture_m <= 0.0 or freq_hz <= 0.0:
        return NEAR_OMNI_HALF_DEG
    lam = SOUND_SPEED_MPS / freq_hz
    half_deg = math.degrees(_BW_K * lam / aperture_m) / 2.0          # broadside half-angle
    widen = 1.0 + (_ENDFIRE_WIDEN - 1.0) * (min(abs(steer_deg), 90.0) / 90.0)
    return min(NEAR_OMNI_HALF_DEG, max(2.0, half_deg * widen))


def alias_ceiling_hz(element_spacing_m: Optional[float]) -> float:
    """Spatial-aliasing ceiling (Hz) = c / (2 * spacing); ``inf`` when unknown."""
    if not element_spacing_m or element_spacing_m <= 0.0:
        return float("inf")
    return SOUND_SPEED_MPS / (2.0 * element_spacing_m)


def separable(sep_deg: float, beamwidth_half_deg: float, factor: float = 1.5) -> bool:
    """Two looks resolve when their angular separation exceeds ``factor`` x the half-beamwidth."""
    return sep_deg >= factor * beamwidth_half_deg
```

- [ ] **Step 4: Run test to verify it passes**

Run: `./.venv/Scripts/python.exe -m pytest -q tests/test_directivity.py`
Expected: PASS (8 tests). If `test_separable_boundary` fails, re-check the `factor*half` arithmetic in the assertions above (120 ≥ 45 True; 20 ≥ 90 False).

- [ ] **Step 5: Commit**

```bash
git add conf_pipeline/directivity.py tests/test_directivity.py
git commit -m "$(printf 'feat(coverage): aperture-aware beamwidth model (directivity.py)\n\nCo-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>')"
```

---

### Task 2: Add `aperture_m` / `element_spacing_m` to the profile catalog + a `polaris-8` profile

**Files:**
- Modify: `conf_pipeline/profiles.py:39-48` (`DeviceCapabilities`), `:60-64` (`_cap`), `:67-81` (`DEVICE_PROFILES`)
- Test: `tests/test_profiles.py` (create if absent)

**Interfaces:**
- Consumes: nothing.
- Produces: `DeviceCapabilities.aperture_m: Optional[float]`, `.element_spacing_m: Optional[float]` (both default `None`); new profile id `"polaris-8"` with `aperture_m=0.08`, `element_spacing_m=0.0306`, `coverage_angle=150.0`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_profiles.py
from conf_pipeline.profiles import get_device_profile, DEVICE_PROFILES

def test_polaris_profile_has_aperture():
    cap = get_device_profile("polaris-8").capabilities
    assert cap.aperture_m == 0.08
    assert abs(cap.element_spacing_m - 0.0306) < 1e-6
    assert cap.max_coverage_zones == 8

def test_existing_profiles_have_no_aperture():
    # no-regression: legacy profiles keep aperture_m None -> legacy 35 deg scoring
    for pid in ("generic-ceiling-array", "generic-table-array"):
        assert get_device_profile(pid).capabilities.aperture_m is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./.venv/Scripts/python.exe -m pytest -q tests/test_profiles.py`
Expected: FAIL — `polaris-8` not in `DEVICE_PROFILES` (and/or `aperture_m` attribute missing).

- [ ] **Step 3: Write minimal implementation**

In `conf_pipeline/profiles.py`, add two fields to `DeviceCapabilities` (after `coverage_angle_deg`, line 46):

```python
    coverage_angle_deg: Optional[float] = None  # full pickup cone (arrays only); None = no coverage geometry
    aperture_m: Optional[float] = None          # physical array aperture (m); enables honest beamwidth. None = legacy 35 deg
    element_spacing_m: Optional[float] = None    # adjacent-capsule spacing (m); sets the spatial-aliasing ceiling
    camera: Optional[CameraSpec] = None         # FOV/range (camera profiles only)
    speaker: Optional[SpeakerSpec] = None       # dispersion/range (loudspeaker profiles)
```

Extend `_cap` (line 60) to accept and pass them:

```python
def _cap(aec, automix, mute, blocks, zones, coverage_angle=None, aperture_m=None,
         element_spacing_m=None, camera=None, speaker=None):
    return DeviceCapabilities(
        aec=aec, automix=automix, mute=mute, supported_blocks=list(blocks),
        max_coverage_zones=zones, coverage_angle_deg=coverage_angle,
        aperture_m=aperture_m, element_spacing_m=element_spacing_m, camera=camera, speaker=speaker,
    )
```

Add the `polaris-8` profile to `DEVICE_PROFILES` (after `generic-table-array`, line 69):

```python
    "polaris-8": DeviceProfile("polaris-8", "sensiBel POLARIS (8-capsule, 40 mm)", ["microphoneArray"], _cap(True, True, True, _MIC, 8, coverage_angle=150.0, aperture_m=0.08, element_spacing_m=0.0306), {"danteOutputs": 1}),
```

- [ ] **Step 4: Run test to verify it passes**

Run: `./.venv/Scripts/python.exe -m pytest -q tests/test_profiles.py && ./.venv/Scripts/python.exe -m mypy`
Expected: PASS; mypy clean.

- [ ] **Step 5: Commit**

```bash
git add conf_pipeline/profiles.py tests/test_profiles.py
git commit -m "$(printf 'feat(coverage): add aperture_m/element_spacing_m + polaris-8 profile\n\nCatalog-only (not serialized); no schema/TS-parity change.\n\nCo-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>')"
```

---

### Task 3: Use aperture-aware beamwidth in the placement scorer

**Files:**
- Modify: `conf_pipeline/sim/scoring.py` — `direct_level_db:139`, `coverage_score:158`, `talker_quality:295`
- Test: `tests/test_aperture_scoring.py`

**Interfaces:**
- Consumes: `directivity.steered_beamwidth_deg`, `SIM_SPEECH_FREQ_HZ`; `profiles.device_capabilities`.
- Produces: `effective_halfwidth_deg(array, off_nadir_deg, params) -> float`; `direct_level_db`/`coverage_score` gain an optional `halfwidth_deg: Optional[float] = None` arg (defaults to `params.lobe_halfwidth_deg` — legacy).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_aperture_scoring.py
from conf_pipeline.sim.scoring import effective_halfwidth_deg, coverage_score
from conf_pipeline.sim.types import SimParams
from conf_pipeline.model import MicrophoneArray

def _array(profile_id):
    return MicrophoneArray(id="a1", label="A", ports=[], coverage_mode="automatic",
                           zones=[], aec=None, profile_id=profile_id)

def test_polaris_halfwidth_is_wider_than_legacy_when_horizontal():
    p = SimParams()
    polaris = effective_halfwidth_deg(_array("polaris-8"), off_nadir_deg=90.0, params=p)
    legacy = effective_halfwidth_deg(_array("generic-ceiling-array"), off_nadir_deg=90.0, params=p)
    assert legacy == p.lobe_halfwidth_deg          # no aperture -> legacy 35
    assert polaris > legacy                         # 40 mm at table range is coarser

def test_coverage_score_drops_for_polaris_off_axis():
    p = SimParams()
    # a talker 25 deg off the look: a tight (legacy) beam still scores it; the coarse
    # POLARIS beam is so wide the off-axis penalty is smaller -> but a competing close
    # seat is no longer separable (covered in Task 5). Here: same off-axis angle, the
    # POLARIS wide beam yields a HIGHER lobe weight (wider main lobe), proving the
    # halfwidth feeds coverage_score.
    legacy = coverage_score(25.0, True, False, p, halfwidth_deg=35.0)
    coarse = coverage_score(25.0, True, False, p, halfwidth_deg=80.0)
    assert coarse > legacy
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./.venv/Scripts/python.exe -m pytest -q tests/test_aperture_scoring.py`
Expected: FAIL — `ImportError: cannot import name 'effective_halfwidth_deg'` and `coverage_score()` has no `halfwidth_deg` kwarg.

- [ ] **Step 3: Write minimal implementation**

In `conf_pipeline/sim/scoring.py`, add imports near the top (after line 30):

```python
from ..directivity import SIM_SPEECH_FREQ_HZ, steered_beamwidth_deg
from ..profiles import device_capabilities
```

Add a helper (after `off_axis_deg`, ~line 67):

```python
def effective_halfwidth_deg(array: MicrophoneArray, off_nadir_deg: float, params: SimParams) -> float:
    """Main-lobe half-angle to use for ``array``: the aperture-aware value when the array's
    profile declares an aperture, else the legacy fixed ``params.lobe_halfwidth_deg``."""
    cap = device_capabilities(array)
    if cap.aperture_m is None:
        return params.lobe_halfwidth_deg
    return steered_beamwidth_deg(cap.aperture_m, SIM_SPEECH_FREQ_HZ, off_nadir_deg)
```

Give `direct_level_db` and `coverage_score` an optional override (replace lines 139-145 and 158-163):

```python
def direct_level_db(distance_m: float, off_axis_angle_deg: float, params: SimParams,
                    halfwidth_deg: Optional[float] = None) -> float:
    """Relative direct-path level (dB) vs ``ref_distance``: spreading + directivity."""
    hw = params.lobe_halfwidth_deg if halfwidth_deg is None else halfwidth_deg
    d = max(distance_m, 0.25)
    spread_db = -20.0 * math.log10(d / params.ref_distance_m)
    x = off_axis_angle_deg / hw
    dir_db = -3.0 * (x * x)
    return spread_db + dir_db


def coverage_score(off_axis_angle_deg: float, in_pickup: bool, in_exclusion: bool, params: SimParams,
                   halfwidth_deg: Optional[float] = None) -> float:
    hw = params.lobe_halfwidth_deg if halfwidth_deg is None else halfwidth_deg
    if in_exclusion:
        return 0.0
    lobe = math.exp(-0.5 * (off_axis_angle_deg / hw) ** 2)
    zone_factor = 1.0 if in_pickup else 0.6
    return _clamp01(lobe * zone_factor)
```

Thread the array's effective halfwidth through `talker_quality` (replace lines 315-322):

```python
    oa = off_axis_deg(ang, steer_off_nadir_deg, steer_az_deg)
    hw = effective_halfwidth_deg(array, ang.off_nadir_deg, params)
    level_db = direct_level_db(ang.distance, oa, params, halfwidth_deg=hw)
    drr_value = drr_db(ang.distance, critical_distance_m)
    in_pickup, in_exclusion = zone_membership(array, talker_pos)

    snr = snr_score(level_db, params)
    drr = drr_score(drr_value, params)
    cov = coverage_score(oa, in_pickup, in_exclusion, params, halfwidth_deg=hw)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `./.venv/Scripts/python.exe -m pytest -q tests/test_aperture_scoring.py tests/test_coverage*.py && ./.venv/Scripts/python.exe -m mypy`
Expected: PASS, including the existing coverage tests (legacy path unchanged for non-aperture arrays). If an existing test changed value, it used `polaris-8` — investigate; ceiling/table arrays must be byte-identical.

- [ ] **Step 5: Commit**

```bash
git add conf_pipeline/sim/scoring.py tests/test_aperture_scoring.py
git commit -m "$(printf 'feat(coverage): aperture-aware beamwidth in the placement scorer\n\nCo-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>')"
```

---

### Task 4: Use aperture-aware wedge half-angle in the coverage report

**Files:**
- Modify: `conf_pipeline/coverage_sim.py` — `mic_coverage:305-358` (the per-zone `CoverageWedge`, line 327)
- Test: `tests/test_coverage_sim_aperture.py`

**Interfaces:**
- Consumes: `directivity.steered_beamwidth_deg`, `SIM_SPEECH_FREQ_HZ`; `device_capabilities` (already imported in `coverage_sim.py:40`).
- Produces: per-zone wedge `h_half_deg`/`v_half_deg` = aperture-aware value when the profile has `aperture_m`, else `DEFAULT_PICKUP_BEAM_HALF_DEG`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_coverage_sim_aperture.py
from conf_pipeline.coverage_sim import mic_coverage, Target, DEFAULT_PICKUP_BEAM_HALF_DEG
from conf_pipeline.model import MicrophoneArray, CoverageZone, RectShape, Point2D, Room

def _cfg_and_array(profile_id):
    arr = MicrophoneArray(
        id="a1", label="A", ports=[], coverage_mode="automatic",
        zones=[CoverageZone(id="z1", type="dedicated", shape=RectShape(Point2D(0.5, 0.5), 1.0, 1.0),
                            always_on=False, label="seat")],
        aec=None, profile_id=profile_id, position=Point2D(0.0, 0.0), elevation=0.85)
    from conf_pipeline.model import SystemConfig
    cfg = SystemConfig(devices=[arr], room=Room(vertices=[Point2D(-3,-3),Point2D(3,-3),Point2D(3,3),Point2D(-3,3)], height=3.0))
    return cfg, arr

def test_polaris_wedge_is_wider_than_legacy():
    cfg, arr = _cfg_and_array("polaris-8")
    mc = mic_coverage(cfg, arr, targets=[])
    assert mc.wedges[0].h_half_deg > DEFAULT_PICKUP_BEAM_HALF_DEG  # honest, coarse

def test_legacy_array_wedge_unchanged():
    cfg, arr = _cfg_and_array("generic-ceiling-array")
    mc = mic_coverage(cfg, arr, targets=[])
    assert mc.wedges[0].h_half_deg == DEFAULT_PICKUP_BEAM_HALF_DEG
```

(Adjust the `MicrophoneArray`/`Room`/`SystemConfig`/`CoverageZone` constructor kwargs to match `conf_pipeline/model.py` exactly — read the dataclasses first; the field names above mirror the spec's map.)

- [ ] **Step 2: Run test to verify it fails**

Run: `./.venv/Scripts/python.exe -m pytest -q tests/test_coverage_sim_aperture.py`
Expected: FAIL — `polaris-8` wedge half-angle still equals 35°.

- [ ] **Step 3: Write minimal implementation**

In `conf_pipeline/coverage_sim.py`, add the import (near line 40, after `from .profiles import device_capabilities`):

```python
from .directivity import SIM_SPEECH_FREQ_HZ, steered_beamwidth_deg
```

Inside `mic_coverage`, after `cap = device_capabilities(array)` (line 311), compute the array's half-angle once and use it for the per-zone wedges (replace the wedge build at lines 321-329):

```python
    def _half_for(off_nadir_deg: float) -> float:
        if cap.aperture_m is None:
            return DEFAULT_PICKUP_BEAM_HALF_DEG
        return steered_beamwidth_deg(cap.aperture_m, SIM_SPEECH_FREQ_HZ, off_nadir_deg)

    if pickup_zones:
        src = Point3D(center.x, center.y, elev)
        for z in pickup_zones:
            cen = _zone_centroid(z)
            sa = steering_angles(src, Point3D(cen.x, cen.y, SEATED_HEAD_M))
            reach = circ_radius if circ_radius > 0 else max(sa.horizontal_distance, 1.0)
            half = _half_for(sa.downtilt_deg)
            wedges.append(CoverageWedge(
                apex=center, apex_elev_m=elev, azimuth_deg=sa.azimuth_deg, tilt_deg=sa.downtilt_deg,
                h_half_deg=half, v_half_deg=half, range_m=reach,
            ))
```

(The target-hit loop at lines 343-351 already uses `wd.h_half_deg`, so coverage % follows the honest beam automatically.)

- [ ] **Step 4: Run test to verify it passes**

Run: `./.venv/Scripts/python.exe -m pytest -q tests/test_coverage_sim_aperture.py tests/test_coverage*.py && ./.venv/Scripts/python.exe -m mypy`
Expected: PASS; existing coverage tests unchanged (legacy arrays still 35°).

- [ ] **Step 5: Commit**

```bash
git add conf_pipeline/coverage_sim.py tests/test_coverage_sim_aperture.py
git commit -m "$(printf 'feat(coverage): honest aperture-aware mic wedge in the coverage report\n\nCo-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>')"
```

---

### Task 5: Separability + grating-lobe caveats on `RoomCoverage`

**Files:**
- Modify: `conf_pipeline/coverage_sim.py` — the aggregator that builds `RoomCoverage` (search for where `RoomCoverage(...)` / `caveats` is assembled, ~line 366+) + a new helper.
- Test: `tests/test_coverage_warnings.py`

**Interfaces:**
- Consumes: `directivity.separable`, `alias_ceiling_hz`, `steered_beamwidth_deg`, `SIM_SPEECH_FREQ_HZ`; `device_capabilities`; `is_pickup_zone`, `_zone_centroid`, `bearing_to_deg`, `angular_separation_deg`.
- Produces: `coverage_caveats(config) -> list[str]` (separability + grating-lobe lines), appended to `RoomCoverage.caveats`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_coverage_warnings.py
from conf_pipeline.coverage_sim import coverage_caveats
from conf_pipeline.model import (MicrophoneArray, CoverageZone, RectShape, Point2D, Room, SystemConfig)

def _two_close_seats(profile_id):
    z = lambda i, x: CoverageZone(id=f"z{i}", type="dedicated", shape=RectShape(Point2D(x, 1.2), 0.5, 0.5),
                                  always_on=False, label=f"seat{i}")
    arr = MicrophoneArray(id="a1", label="A", ports=[], coverage_mode="automatic",
                          zones=[z(1, -0.3), z(2, 0.3)], aec=None, profile_id=profile_id,
                          position=Point2D(0.0, 0.0), elevation=0.85)
    return SystemConfig(devices=[arr], room=Room(vertices=[Point2D(-3,-3),Point2D(3,-3),Point2D(3,3),Point2D(-3,3)], height=3.0))

def test_polaris_close_seats_flag_unseparable():
    caveats = coverage_caveats(_two_close_seats("polaris-8"))
    assert any("separat" in c.lower() for c in caveats)
    assert any("5" in c and ("kHz" in c or "khz" in c.lower()) for c in caveats)  # grating-lobe note

def test_legacy_array_no_aperture_warnings():
    caveats = coverage_caveats(_two_close_seats("generic-ceiling-array"))
    assert not any("separat" in c.lower() for c in caveats)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./.venv/Scripts/python.exe -m pytest -q tests/test_coverage_warnings.py`
Expected: FAIL — `ImportError: cannot import name 'coverage_caveats'`.

- [ ] **Step 3: Write minimal implementation**

Add to `conf_pipeline/coverage_sim.py` (after `mic_coverage`):

```python
def coverage_caveats(config: SystemConfig) -> list[str]:
    """Honesty warnings for aperture-limited arrays: pickup-zone pairs the array cannot
    separate, and a grating-lobe note when its spatial-aliasing ceiling is in the speech band.
    Empty for arrays whose profile declares no aperture (legacy)."""
    from .directivity import alias_ceiling_hz, separable, steered_beamwidth_deg, SIM_SPEECH_FREQ_HZ
    out: list[str] = []
    for array in (d for d in config.devices if d.type == "microphoneArray"):
        cap = device_capabilities(array)
        if cap.aperture_m is None or array.position is None:
            continue
        zones = [z for z in array.zones if is_pickup_zone(z)]
        elev = _device_elev(config, array)
        looks = []  # (zone_label, bearing_deg, half_deg)
        for z in zones:
            cen = _zone_centroid(z)
            sa = steering_angles(Point3D(array.position.x, array.position.y, elev),
                                 Point3D(cen.x, cen.y, SEATED_HEAD_M))
            half = steered_beamwidth_deg(cap.aperture_m, SIM_SPEECH_FREQ_HZ, sa.downtilt_deg)
            looks.append((z.label, sa.azimuth_deg, half))
        for i in range(len(looks)):
            for j in range(i + 1, len(looks)):
                sep = angular_separation_deg(looks[i][1], looks[j][1])
                if not separable(sep, max(looks[i][2], looks[j][2])):
                    out.append(f"{array.label}: zones '{looks[i][0]}' and '{looks[j][0]}' are "
                               f"{sep:.0f} deg apart but this array's beam is ~{max(looks[i][2], looks[j][2]):.0f} deg "
                               "half-width — it cannot separate them.")
        ceil = alias_ceiling_hz(cap.element_spacing_m)
        if ceil < 8000.0:
            out.append(f"{array.label}: directivity degrades above ~{ceil/1000.0:.1f} kHz "
                       "(spatial aliasing / grating lobes).")
    return out
```

Then append these in the aggregator that builds `RoomCoverage` (where `caveats=` / `_GEOMETRIC_CAVEATS` is used). Read that function and add: `caveats = list(_GEOMETRIC_CAVEATS) + coverage_caveats(config)` (or `rc.caveats.extend(coverage_caveats(config))` after construction). Add a test asserting the assembled `RoomCoverage.caveats` contains a separability line for a `polaris-8` close-seat config.

- [ ] **Step 4: Run test to verify it passes**

Run: `./.venv/Scripts/python.exe -m pytest -q tests/test_coverage_warnings.py tests/test_coverage*.py && ./.venv/Scripts/python.exe -m mypy`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add conf_pipeline/coverage_sim.py tests/test_coverage_warnings.py
git commit -m "$(printf 'feat(coverage): separability + grating-lobe caveats for small arrays\n\nCo-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>')"
```

---

### Task 6: Calibration test — analytic model vs measured `sensibel_8` beam (`[control]`, numpy)

**Files:**
- Test: `tests/test_directivity_calibration.py`
- Possibly modify: `conf_pipeline/directivity.py` constants (`_BW_K`, `_ENDFIRE_WIDEN`) if the fit is off.

**Interfaces:**
- Consumes: `conf_pipeline_control.geometry.sensibel_8`, `conf_pipeline_control.beamformer` (`design_from_bearings`/`response_db`), `directivity.steered_beamwidth_deg`.

- [ ] **Step 1: Write the test (skip-guarded on the [control] extra)**

```python
# tests/test_directivity_calibration.py
import math
import pytest

np = pytest.importorskip("numpy")  # skip when [control] isn't installed
from conf_pipeline.directivity import steered_beamwidth_deg

def _measured_half_deg(geom, freq_hz, look_az_deg, off_nadir_deg=90.0):
    """3 dB main-lobe half-width (deg) of the delay-sum beam toward (look_az, off_nadir)."""
    import conf_pipeline_control as cc
    from conf_pipeline_control.beamformer import design_from_bearings
    look = (float(look_az_deg), float(off_nadir_deg))
    d = design_from_bearings(geom, look, nulls=(), freq_hz=freq_hz, mode=cc.MODE_DELAYSUM, loading=0.0)
    w = list(d.beams[0].weights)
    on = cc.response_db(w, geom, _unit(look_az_deg, off_nadir_deg), freq_hz)
    for dphi in range(0, 181):
        r = cc.response_db(w, geom, _unit(look_az_deg + dphi, off_nadir_deg), freq_hz)
        if r <= on - 3.0:
            return float(dphi)
    return 180.0

def _unit(az_deg, off_nadir_deg):
    on, az = math.radians(off_nadir_deg), math.radians(az_deg)
    s = math.sin(on)
    return (s * math.sin(az), s * math.cos(az), -math.cos(on))

def test_analytic_matches_measured_sensibel8_within_tolerance():
    from conf_pipeline_control.geometry import sensibel_8
    geom = sensibel_8(radius_m=0.040)
    aperture = 0.08
    worst = 0.0
    for f in (800.0, 1500.0, 3000.0):
        meas = _measured_half_deg(geom, f, look_az_deg=0.0, off_nadir_deg=90.0)
        pred = steered_beamwidth_deg(aperture, f, 90.0)
        # both clamp toward near-omni at low freq; compare the un-clamped regime
        worst = max(worst, abs(min(pred, 90.0) - min(meas, 90.0)))
    assert worst <= 15.0, f"analytic vs measured half-width off by {worst:.1f} deg"
```

- [ ] **Step 2: Run it**

Run: `./.venv/Scripts/python.exe -m pytest -q tests/test_directivity_calibration.py -v`
Expected: PASS, or FAIL printing the gap. If FAIL: adjust `_BW_K` (scales overall width) and `_ENDFIRE_WIDEN` (off-nadir widening) in `directivity.py` to minimise `worst`, re-run Task 1 tests (qualitative — should still pass), then re-run this. If the analytic form cannot get within 15° across the grid, switch to **Approach B** (a small per-profile half-width lookup table keyed by frequency in `profiles.py`, read by `steered_beamwidth_deg`) — same public signature; note the decision in the spec's Risks section.

- [ ] **Step 3: Commit**

```bash
git add tests/test_directivity_calibration.py conf_pipeline/directivity.py
git commit -m "$(printf 'test(coverage): calibrate aperture beamwidth model to measured sensibel_8 beam\n\nCo-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>')"
```

---

### Task 7: Surface the caveats in the SIMULATE panel (GUI; CI-verified)

**Files:**
- Modify: `conf_pipeline_gui/panels/simulate.py` (add a read-only "Coverage warnings" list fed by `coverage_caveats`/`RoomCoverage.caveats`)
- Test: `tests/test_gui_simulate_warnings.py` (construct-and-poke probe, offscreen)

**Interfaces:**
- Consumes: `RoomCoverage.caveats` (already includes the new lines from Task 5). The honest wedge already renders on the canvas via `MicCoverage.wedges[].h_half_deg` (Task 4) — no canvas change needed.

- [ ] **Step 1: Write the failing probe test**

```python
# tests/test_gui_simulate_warnings.py
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import pytest
pytest.importorskip("PySide6")
from PySide6.QtWidgets import QApplication
from conf_pipeline_gui.state import AppState
from conf_pipeline_gui.panels.simulate import SimulatePanel  # confirm the class name when implementing

def test_simulate_panel_lists_coverage_caveats():
    app = QApplication.instance() or QApplication([])
    state = AppState()  # build a polaris-8 two-close-seat config as in test_coverage_warnings
    panel = SimulatePanel(state)
    panel.refresh()  # confirm the actual refresh entry point
    text = panel.warnings_text()  # a method that returns the joined caveat lines (add it)
    assert "separat" in text.lower()
```

- [ ] **Step 2: Run to verify it fails**

Run: `QT_QPA_PLATFORM=offscreen ./.venv/Scripts/python.exe -m pytest -q tests/test_gui_simulate_warnings.py`
Expected: FAIL (no `warnings_text`/no caveat surface).

- [ ] **Step 3: Implement the panel addition**

Read `conf_pipeline_gui/panels/simulate.py` first to match its construction pattern. Add a `QGroupBox("Coverage warnings")` containing a `QListWidget`; populate it from `RoomCoverage.caveats` wherever the panel already computes/holds the coverage result (it drives the heatmap, so a `RoomCoverage` is available or one extra `simulate_room_coverage(config)` call). Add `warnings_text()` returning the joined lines for the probe. **Match the file's existing widget/layout idiom exactly** (per CLAUDE.md: full `MainWindow` hangs locally — verify with this construct-and-poke probe + `pytest --collect-only`; full GUI behaviour is verified in CI offscreen-Linux).

- [ ] **Step 4: Run probe + collect**

Run: `QT_QPA_PLATFORM=offscreen ./.venv/Scripts/python.exe -m pytest -q tests/test_gui_simulate_warnings.py && ./.venv/Scripts/python.exe -m pytest --collect-only -q && ./.venv/Scripts/python.exe -m mypy`
Expected: probe PASS; collection clean; mypy clean.

- [ ] **Step 5: Commit**

```bash
git add conf_pipeline_gui/panels/simulate.py tests/test_gui_simulate_warnings.py
git commit -m "$(printf 'feat(gui): show coverage separability/grating caveats in SIMULATE\n\nCo-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>')"
```

---

### Task 8: Docs + green-gate

**Files:**
- Modify: `README.md` (coverage section — note honest aperture-aware sim + the `polaris-8` profile), `CHANGELOG.md` (new entry).

- [ ] **Step 1:** Use the **docs-maintainer** agent to draft the README/CHANGELOG additions: a short "Honest coverage for small arrays" note (aperture-aware beamwidth, separability + grating-lobe warnings, `polaris-8` profile, opt-in/no-regression), updating the test count.
- [ ] **Step 2:** Run the **green-gate** agent: `QT_QPA_PLATFORM=offscreen ./.venv/Scripts/python.exe -m pytest -q` + `./.venv/Scripts/python.exe -m mypy`. Expected: all green. (schema-parity-guard is N/A — no schema change — but a confirming pass is fine since `profiles.py` changed.)
- [ ] **Step 3: Commit**

```bash
git add README.md CHANGELOG.md
git commit -m "$(printf 'docs(coverage): honest aperture-aware coverage + polaris-8 profile\n\nCo-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>')"
```

---

## Self-Review

**Spec coverage:** ✓ aperture-aware beamwidth (T1,T3,T4) · ✓ calibration (T6) · ✓ separability + grating warnings (T5) · ✓ aperture_m in profile catalog, no schema/TS change (T2) · ✓ numpy-free conf_pipeline (T1-T5; numpy only in T6) · ✓ no-regression / opt-in (aperture only on `polaris-8`; legacy fallback asserted in T2-T5) · ✓ GUI surface incl. true wedge (T4 wedge + T7 caveats) · ✓ headless tests (T1-T5,T7) · Approach-B fallback noted (T6).

**Placeholder scan:** code is concrete in every code step. T4/T5/T7 explicitly say "read the exact constructor/panel idiom first and match it" — that's a *real instruction*, not a deferral (the model.py dataclass kwargs and the Qt panel idiom must be confirmed against the live files, which the implementer has). No "TODO/TBD/similar-to".

**Type consistency:** `steered_beamwidth_deg(aperture_m, freq_hz, steer_deg)`, `alias_ceiling_hz(element_spacing_m)`, `separable(sep_deg, half_deg, factor=1.5)`, `effective_halfwidth_deg(array, off_nadir_deg, params)`, the `halfwidth_deg` override on `direct_level_db`/`coverage_score`, and `coverage_caveats(config)` are used consistently across T1→T7. `DeviceCapabilities.aperture_m/element_spacing_m` and the `polaris-8` id match across T2→T6.
