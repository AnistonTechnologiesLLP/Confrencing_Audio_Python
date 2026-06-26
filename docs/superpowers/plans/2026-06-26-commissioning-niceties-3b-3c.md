# Live Commissioning Niceties #3b + #3c — Implementation Plan

> **For agentic workers:** executed in-session with implementer subagents. Per the user's instruction
> ("first do all steps and then commit"), implementers **implement + test but DO NOT git-commit**; the
> controller commits **once per feature at the end**, after all steps pass + the green-gate is clean.
> Steps use `- [ ]`.

**Goal:** Two POLARIS commissioning niceties — **#3c learn-array-orientation** (infer `bearing_deg`
from a DOA measurement of a reference at a known seat/point) and **#3b apply a zone's `gain_db` in the
live beam** (post-AGC, opt-in).

**Architecture:** Pure, fully-testable cores in `conf_pipeline`/`conf_pipeline_control` (a bearing-solve
geometry helper; an active-zone-gain lookup; a post-AGC trim stage) + thin live wiring that reuses
existing infrastructure (the "calibrate front" capture worker; the auto-steer tick). Everything new is
**opt-in / default-OFF / bit-exact when off** so nothing changes until enabled. Live A/B is validated
by the user at the kit; here we verify logic headless.

**Tech Stack:** Python 3.10+ (`conf_pipeline` pure stdlib; `conf_pipeline_control` may use numpy), PySide6, pytest, mypy. venv `./.venv/Scripts/python.exe`.

## Global Constraints

- `conf_pipeline` stays **numpy-free** (the new `seat_mapper` helpers are pure stdlib). `conf_pipeline_control` may use numpy.
- **No schema change.** `bearing_deg`, `zone.gain_db` are already serialized at `CONFIG_VERSION = 5`. No version bump, no migration, no TS-sibling edit.
- **Realtime-callback safety** (anything reached from `process_block`/`_process_block`): no lock across heavy DSP, no throw into the callback, scalars rebound atomically (never `.reset()` in place). New DSP needs **hardware-free tests** (synthetic blocks).
- **Opt-in recipe** for the #3b live stage: a cfg key **default-OFF**, a real off/None escape hatch, **bit-exact pass-through when off** (return the same array object / skip the multiply), built where the other optional stages are built, applied in BOTH `PolarisBeamformer.process_block` AND `LiveBeamController._process_block`, dropped in `reset_transient`, fanned out via `BeamEngine` + `AutoSteerController`, GUI checkbox.
- **Commit discipline (user instruction):** implementers do NOT commit. Controller commits per feature at the end. Trailer `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`. Branch `feat/commissioning-niceties` (off master). No push. Bash resets cwd → prefix `cd /c/Work/conferencing-audio-pipeline-py &&`. The plan doc itself is committed with Feature A's commit.
- DSP conventions: azimuth `0° = +Y, clockwise`; DOA/scoring band 300–3800 Hz; the array DOA frame is array-relative.

---

## FEATURE A — #3c learn-array-orientation

### Task A1: pure `learn_bearing` geometry + export

**Files:**
- Modify: `conf_pipeline/seat_mapper.py` (add after `azimuth_for_array_point`, ~line 213)
- Modify: `conf_pipeline/__init__.py` (export `learn_bearing`)
- Test: `tests/test_learn_bearing.py`

**Interfaces:**
- Consumes: `conf_pipeline.model.bearing_to_deg`, `conf_pipeline.seat_mapper._norm_bearing`, `Point2D`.
- Produces: `learn_bearing(array_pos: Point2D, ref_point: Point2D, measured_az_deg: float) -> float` — the array `bearing_deg` that makes a reference at `ref_point` read as `measured_az_deg` in the array's DOA frame. Inverse of `_array_relative_azimuth`. Exported `cp.learn_bearing`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_learn_bearing.py
import conf_pipeline as cp
from conf_pipeline.model import Point2D, bearing_to_deg
from conf_pipeline.seat_mapper import _array_relative_azimuth, learn_bearing


def test_learn_bearing_is_inverse_of_array_relative_azimuth():
    array = Point2D(0.0, 0.0)
    ref = Point2D(2.0, 1.0)
    for true_bearing in (0.0, 37.0, 90.0, 200.0, 359.0):
        measured = _array_relative_azimuth(array, true_bearing, ref)   # what DOA would report
        learned = learn_bearing(array, ref, measured)
        # learned bearing must reproduce the same azimuth (mod 360, within float eps)
        assert abs(((learned - true_bearing + 180.0) % 360.0) - 180.0) < 1e-6


def test_learn_bearing_normalizes_into_0_360():
    b = learn_bearing(Point2D(0.0, 0.0), Point2D(0.0, 1.0), 350.0)   # ref due +Y (bearing_to_deg=0)
    assert 0.0 <= b < 360.0
    assert abs(b - 10.0) < 1e-6     # 0 - 350 = -350 → +10


def test_reference_due_plus_y_zero_measured_gives_zero_bearing():
    # ref straight ahead (+Y), DOA reads 0° → array faces +Y → bearing 0
    b = learn_bearing(Point2D(0.0, 0.0), Point2D(0.0, 2.0), 0.0)
    assert abs(b) < 1e-6
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd /c/Work/conferencing-audio-pipeline-py && ./.venv/Scripts/python.exe -m pytest -q tests/test_learn_bearing.py`
Expected: FAIL — `ImportError: cannot import name 'learn_bearing'`.

- [ ] **Step 3: Implement**

Add to `conf_pipeline/seat_mapper.py` (it already imports `bearing_to_deg`, `_norm_bearing`, `Point2D`):

```python
def learn_bearing(array_pos: Point2D, ref_point: Point2D, measured_az_deg: float) -> float:
    """Infer the array's ``bearing_deg`` (0°=+Y, CW) from a DOA measurement.

    A reference at ``ref_point`` is observed at ``measured_az_deg`` in the array's
    DOA / steering frame. Since ``measured_az = bearing_to_deg(array, ref) − bearing``
    (see :func:`_array_relative_azimuth`), the array bearing is the inverse::

        bearing = bearing_to_deg(array_pos, ref_point) − measured_az_deg   (mod 360)

    Pure geometry — no hardware. The caller supplies ``measured_az_deg`` from a live
    DOA capture (e.g. the calibrate-front worker)."""
    return _norm_bearing(bearing_to_deg(array_pos, ref_point) - measured_az_deg)
```

Export `learn_bearing` from `conf_pipeline/__init__.py` (read the file; add alongside the other `seat_mapper` exports / any `__all__`).

- [ ] **Step 4: Run to verify it passes**

Run: `cd /c/Work/conferencing-audio-pipeline-py && ./.venv/Scripts/python.exe -m pytest -q tests/test_learn_bearing.py && ./.venv/Scripts/python.exe -m mypy`
Expected: PASS; mypy clean.

- [ ] **Step 5:** Do NOT commit (controller commits Feature A at the end). Report DONE.

---

### Task A2: CLI `learn_bearing.py` + DESIGN "Learn bearing…" button

**Files:**
- Create: `scripts/learn_bearing.py`
- Modify: `conf_pipeline_gui/panels/design.py` (a "Learn bearing from a reference…" button next to the existing "Bearing (°)" spin, ~line 340)
- Test: `tests/test_gui_learn_bearing.py`

**Interfaces:**
- Consumes: `cp.learn_bearing` (A1); `cp.set_array_bearing(config, device_id, bearing_deg)`; the array's `position`; a reference room point; the existing capture worker `_CalibWorker` (live.py) / `cc.detect_offline` (the CLI uses the same path as `scripts/calibrate_front.py`).
- Produces: a CLI that records a short clip, runs DOA, solves bearing, prints it; a GUI handler `_learn_bearing(self)` that captures a DOA reading and applies `learn_bearing` → `set_array_bearing`.

- [ ] **Step 1: Write the failing GUI probe**

The live DOA capture is hardware — the probe tests the **solve wiring** with an injected measured azimuth (no audio). Read `conf_pipeline_gui/panels/design.py` first: confirm the array's `position`, how the bearing spin/`_set_bearing` works, the selected-array accessor, and the panel's toast. The handler must take the measured azimuth + a reference point and apply the solve; structure it so the test can call a pure-ish apply path without real audio (e.g. `_apply_learned_bearing(array_id, ref_point, measured_az)` that the capture callback also calls).

```python
# tests/test_gui_learn_bearing.py
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import pytest

pytest.importorskip("PySide6")
from PySide6.QtWidgets import QApplication

import conf_pipeline as cp
from conf_pipeline.model import Point2D
from conf_pipeline_gui.state import AppState
from conf_pipeline_gui.panels.design import DesignPanel


@pytest.fixture(scope="module")
def qapp():
    yield QApplication.instance() or QApplication([])


def test_apply_learned_bearing_sets_array_bearing(qapp):
    cfg = cp.create_config("T", "2026-01-01T00:00:00Z")
    arr = cp.create_microphone_array("a1", "Array", position=Point2D(0.0, 0.0))
    cfg = cp.add_device(cfg, arr)
    st = AppState(); st.set_config(cfg); st.select({"kind": "device", "id": "a1"})
    panel = DesignPanel(st); panel.refresh()
    # reference 2m straight ahead (+Y); DOA measured it at 30° → bearing should be -30 → 330
    panel._apply_learned_bearing("a1", Point2D(0.0, 2.0), 30.0)
    arr2 = next(d for d in st.config.devices if d.id == "a1")
    assert abs(((arr2.bearing_deg - 330.0 + 180.0) % 360.0) - 180.0) < 1e-6
    panel.deleteLater()
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd /c/Work/conferencing-audio-pipeline-py && QT_QPA_PLATFORM=offscreen ./.venv/Scripts/python.exe -m pytest -q tests/test_gui_learn_bearing.py`
Expected: FAIL — no `_apply_learned_bearing`.

- [ ] **Step 3: Implement**

GUI (`design.py`): add `_apply_learned_bearing(self, array_id, ref_point, measured_az_deg)` that does
`self.state.set_config(cp.set_array_bearing(self.state.config, array_id, cp.learn_bearing(array_pos, ref_point, measured_az_deg)))` (one undo), reading `array_pos` from the array's `position` (guard if `position is None` → toast "place the array first"). Add a "Learn bearing from a reference…" button next to the bearing spin whose click opens a small flow: pick a reference (a seat from `cp.room_seats`, or the array→a clicked/typed point) + run a short DOA capture (reuse the live-panel `_CalibWorker` pattern / `cc.detect_offline`), then on the measured azimuth call `_apply_learned_bearing`. Match the panel idiom; the capture is hardware (not unit-tested), the solve is.

CLI (`scripts/learn_bearing.py`): mirror `scripts/calibrate_front.py` — record a short clip from the device, run `cc.detect_offline` to get the dominant azimuth, take `--array-x/--array-y` + `--ref-x/--ref-y` args, print `cp.learn_bearing(...)`. Honest `--help`: "records a reference talker at a KNOWN point, infers the array bearing."

- [ ] **Step 4: Run probe + collect + mypy**

Run: `cd /c/Work/conferencing-audio-pipeline-py && QT_QPA_PLATFORM=offscreen ./.venv/Scripts/python.exe -m pytest -q tests/test_gui_learn_bearing.py && QT_QPA_PLATFORM=offscreen ./.venv/Scripts/python.exe -m pytest --collect-only -q 2>&1 | tail -2 && ./.venv/Scripts/python.exe -m mypy`
Expected: probe PASS; collection clean; mypy clean.

- [ ] **Step 5:** Do NOT commit. Report DONE.

---

## FEATURE B — #3b apply per-zone `gain_db` live (scoped, opt-in)

### Task B1: pure `active_zone_gain_db` lookup

**Files:**
- Modify: `conf_pipeline/seat_mapper.py`
- Modify: `conf_pipeline/__init__.py` (export)
- Test: `tests/test_active_zone_gain.py`

**Interfaces:**
- Consumes: `azimuth_in_pickup_zone`/the zone geometry already in seat_mapper; `is_pickup_zone`; the array's zones.
- Produces: `active_zone_gain_db(config, array_id, azimuth_deg, *, margin_deg=8.0) -> Optional[float]` — the `gain_db` of the pickup zone whose area the given **array-relative** azimuth points into (the same matching `azimuth_in_pickup_zone` uses), else `None` (no zone / no gain set / unposed). Exported `cp.active_zone_gain_db`.

- [ ] **Step 1: Write the failing test**

Read `seat_mapper.azimuth_in_pickup_zone` first to mirror its zone-matching (centroid azimuth + margin). The test builds a posed `polaris`-style array with a pickup zone that has `gain_db` set, and asserts the azimuth pointing into it returns that gain; an azimuth elsewhere returns `None`; a zone with `gain_db=None` returns `None`.

```python
# tests/test_active_zone_gain.py
import conf_pipeline as cp
from conf_pipeline.model import Point2D, RectShape
from conf_pipeline.seat_mapper import active_zone_gain_db, azimuth_for_array_point


def _posed():
    cfg = cp.create_config("T", "2026-01-01T00:00:00Z")
    arr = cp.create_microphone_array("a1", "Array", position=Point2D(0.0, 0.0))
    arr.bearing_deg = 0.0
    cfg = cp.add_device(cfg, arr)
    return cfg


def test_azimuth_into_zone_returns_its_gain():
    cfg = _posed()
    cfg = cp.add_coverage_zone(cfg, "a1", cp.dynamic_zone("z1", "Z", RectShape(Point2D(0.5, 1.5), 1.0, 1.0)))
    cfg = cp.set_zone_gain_db(cfg, "a1", "z1", -6.0)
    az = azimuth_for_array_point(cfg, "a1", Point2D(1.0, 2.0))   # a point inside the zone
    assert abs(active_zone_gain_db(cfg, "a1", az) - (-6.0)) < 1e-9


def test_azimuth_outside_any_zone_returns_none():
    cfg = _posed()
    cfg = cp.add_coverage_zone(cfg, "a1", cp.dynamic_zone("z1", "Z", RectShape(Point2D(0.5, 1.5), 1.0, 1.0)))
    cfg = cp.set_zone_gain_db(cfg, "a1", "z1", -6.0)
    assert active_zone_gain_db(cfg, "a1", 180.0) is None   # behind → not in the zone


def test_zone_without_gain_returns_none():
    cfg = _posed()
    cfg = cp.add_coverage_zone(cfg, "a1", cp.dynamic_zone("z1", "Z", RectShape(Point2D(0.5, 1.5), 1.0, 1.0)))
    az = azimuth_for_array_point(cfg, "a1", Point2D(1.0, 2.0))
    assert active_zone_gain_db(cfg, "a1", az) is None       # gain_db unset
```

- [ ] **Step 2: Run to verify it fails** — `ImportError: cannot import name 'active_zone_gain_db'`.

- [ ] **Step 3: Implement** — in `seat_mapper.py`, iterate the array's pickup zones; for the one whose area contains `azimuth_deg` (reuse the same containment test `azimuth_in_pickup_zone` uses — match by the zone whose centroid azimuth is within `margin_deg`, or the existing point/sector test), return its `gain_db` (which may be `None`). Return `None` if unposed / no match. Export from `__init__.py`.

- [ ] **Step 4: Run** — `./.venv/Scripts/python.exe -m pytest -q tests/test_active_zone_gain.py && ./.venv/Scripts/python.exe -m mypy` → PASS, clean.

- [ ] **Step 5:** Do NOT commit. Report DONE.

### Task B2: post-AGC zone-gain trim stage in BOTH live impls (opt-in, default-off)

**Files:**
- Modify: `conf_pipeline_control/polaris_beamformer.py` (`__init__` flag + `_zone_gain_lin` scalar + apply post-AGC + `reset_transient`)
- Modify: `conf_pipeline_control/live.py` (`LiveBeamController` same)
- Modify: `conf_pipeline_control/beam_engine.py` (pass the `live_zone_gain` cfg key through)
- Test: `tests/test_zone_gain_stage.py`

**Interfaces:**
- Produces: a cfg key `live_zone_gain: bool = False` (default OFF) on both engines; a `set_zone_gain_lin(self, lin: Optional[float])` setter (rebind a scalar atomically; `None`/`1.0` = no trim); the trim applied **after AGC, before band-limit/voice-gate** in both `process_block`/`_process_block`, gated on the flag; bit-exact pass-through when off (skip the multiply when `not self._zone_gain or self._zone_gain_lin is None`).

- [ ] **Step 1: Write the failing test** (synthetic blocks; no hardware)

```python
# tests/test_zone_gain_stage.py
import numpy as np
import pytest
pytest.importorskip("numpy")
from conf_pipeline_control.agc import _apply_zone_gain   # pure helper extracted in Step 3


def test_zone_gain_off_is_bit_identical():
    x = np.linspace(-0.5, 0.5, 256).astype(np.float32)
    y = _apply_zone_gain(x, enabled=False, lin=0.5)
    assert y is x or np.array_equal(y, x)


def test_zone_gain_on_scales():
    x = np.ones(128, dtype=np.float32) * 0.5
    y = _apply_zone_gain(x, enabled=True, lin=0.5)   # -6 dB ≈ 0.501
    assert np.allclose(y, 0.25, atol=1e-6)


def test_zone_gain_none_lin_is_noop():
    x = np.ones(64, dtype=np.float32) * 0.3
    y = _apply_zone_gain(x, enabled=True, lin=None)
    assert y is x or np.array_equal(y, x)
```

- [ ] **Step 2: Run to verify it fails** — import error on `_apply_zone_gain`.

- [ ] **Step 3: Implement**

In `conf_pipeline_control/agc.py` (or a small shared spot both impls import), add a pure realtime-safe helper:

```python
def _apply_zone_gain(mono, *, enabled: bool, lin):
    """Post-AGC per-zone trim. Bit-exact pass-through when disabled or no trim set
    (returns the SAME array object). Realtime-safe: one multiply, no alloc when off."""
    if not enabled or lin is None or lin == 1.0:
        return mono
    return (mono * float(lin)).astype(mono.dtype)
```

In BOTH `PolarisBeamformer.process_block` (after `_apply_agc`, before `_band_limit`) and `LiveBeamController._process_block` (after the AGC line, before the voice gate): `mono = _apply_zone_gain(mono, enabled=self._zone_gain, lin=self._zone_gain_lin)`. Add `self._zone_gain = bool(live_zone_gain)` (default False) + `self._zone_gain_lin = None` in `__init__`; a `set_zone_gain_lin(self, lin)` that rebinds the scalar (atomic — plain assignment); leave `_zone_gain_lin` untouched in `reset_transient` (it's a steering-derived scalar, not session state) OR reset to None — match how `_active_nulls` is handled. Thread `live_zone_gain` through `BeamEngine` (pass-through cfg key like `post_nr`).

- [ ] **Step 4: Run** — `./.venv/Scripts/python.exe -m pytest -q tests/test_zone_gain_stage.py && ./.venv/Scripts/python.exe -m mypy` → PASS, clean. Also run `tests/test_polaris_beamformer.py`/`tests/test_live*.py` if present to confirm the off-path stays byte-identical.

- [ ] **Step 5:** Do NOT commit. Report DONE.

### Task B3: wire active-zone gain into auto-steer + GUI checkbox

**Files:**
- Modify: `conf_pipeline_control/autosteer.py` (in `_tick`, after resolving the active in-sector azimuth, set `self.ctrl.set_zone_gain_lin(...)` from `active_zone_gain_db` when the feature is enabled)
- Modify: `conf_pipeline_gui/panels/live.py` (a "Apply per-zone gain (live)" checkbox near the AGC toggle, default off, wired to the `live_zone_gain` cfg)
- Test: extend `tests/test_zone_gain_stage.py` (autosteer wiring with a stub) + a headless live-panel probe if the panel constructs standalone

**Interfaces:**
- Consumes: `active_zone_gain_db` (B1), `set_zone_gain_lin` (B2). In `autosteer._tick`: when `self.zone_gain_enabled` and a dominant in-sector azimuth exists, `g = active_zone_gain_db(self._config, self._array_id, az); self.ctrl.set_zone_gain_lin(10**(g/20) if g is not None else None)`.

- [ ] **Step 1: Write the failing test** — a stub-engine autosteer test: configure a zone with `gain_db`, feed a covariance/DOA that resolves into that zone, assert the controller's `set_zone_gain_lin` was called with `10**(gain_db/20)`. (Use the existing autosteer stub-injection pattern — read `tests/test_autosteer*.py` for how engines are faked.)

- [ ] **Step 2: Run to verify it fails.**

- [ ] **Step 3: Implement** the `_tick` wiring (guarded by an enable flag, default off; no-throw; if `active_zone_gain_db` returns None set lin None) + the GUI checkbox (default off, fans the `live_zone_gain` flag to the engine like the other live toggles). Auto-steer only in v1; pure zone/lock-to-seat mode may set it at connect from the primary zone (optional — only if it falls out cleanly). Document in the README note that this is auto-steer-scoped and post-AGC.

- [ ] **Step 4: Run** — the new test + `pytest --collect-only` + mypy → green.

- [ ] **Step 5:** Do NOT commit. Report DONE.

---

## Docs (folded into the final commits, not a separate task)

- **CHANGELOG.md** `[Unreleased] → Added`: two bullets — learn-array-orientation (`cp.learn_bearing` + DESIGN button + CLI) and per-zone live gain (`cp.active_zone_gain_db` + opt-in post-AGC trim, auto-steer-scoped). Both note: opt-in, no schema change, live A/B pending at the kit.
- **README.md**: short notes in the coverage/live sections; update the test-count line from the live `--collect-only` count. State #3b is auto-steer-scoped + post-AGC and #3c's capture reuses the calibrate-front worker.

## Controller commit plan (after ALL steps + green-gate)

1. Green-gate: `QT_QPA_PLATFORM=offscreen … pytest -q -p no:cacheprovider --ignore=<the 5 MainWindow files>` + mypy.
2. Commit **Feature A** (#3c): `conf_pipeline/seat_mapper.py` + `__init__.py` (learn_bearing) + `scripts/learn_bearing.py` + `conf_pipeline_gui/panels/design.py` (button) + the A tests + this plan doc + the CHANGELOG/README A bullet.
3. Commit **Feature B** (#3b): the B backend/control/GUI files + B tests + the CHANGELOG/README B bullet.
4. Final whole-branch review + finishing-a-development-branch.

## Self-Review (writing-plans)

**Spec coverage:** ✓ #3c pure inverse solve + export + tests (A1) · ✓ #3c CLI + DESIGN button reusing calibrate-front capture (A2) · ✓ #3b active-zone-gain lookup (B1) · ✓ #3b opt-in post-AGC trim in BOTH impls, bit-exact-off (B2) · ✓ #3b auto-steer wiring + GUI (B3) · ✓ docs + commit plan. **Placeholder scan:** code concrete; the GUI/autosteer "read the real accessor/stub pattern" notes are real instructions (Qt/stub specifics must match live files). **Type consistency:** `learn_bearing(array_pos, ref_point, measured_az_deg)`, `active_zone_gain_db(config, array_id, azimuth_deg)`, `_apply_zone_gain(mono, *, enabled, lin)`, `set_zone_gain_lin(lin)` used consistently. **Risk note:** #3b live behaviour (and #3c DOA capture) are unvalidatable headless — both default-OFF so zero regression; the user A/Bs at the kit.
