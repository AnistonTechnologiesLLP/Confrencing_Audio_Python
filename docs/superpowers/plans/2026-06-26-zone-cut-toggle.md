# Cut-a-Zone Toggle Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A one-click "Cut (no pickup)" toggle that flips a coverage zone between active (`dynamic`) and cut (`exclusion`), honored live by the existing exclusion handling with no live-DSP change.

**Architecture:** Add a pure `set_zone_type(array, zone_id, zone_type)` to `conf_pipeline/coverage.py` (mirroring `set_zone_gain_db`), a thin `cp.set_zone_type(config, …)` wrapper in `api.py`, and a "Cut (no pickup)" checkbox in the DESIGN per-zone editor. The live layer is untouched — an `exclusion` zone is already excluded from steer targets and nulled by the existing runtime "Cut the door…" toggle.

**Tech Stack:** Python 3.10+ (pure stdlib in `conf_pipeline`), PySide6 (GUI), pytest, mypy. venv `./.venv/Scripts/python.exe`.

## Global Constraints

- `conf_pipeline` stays **numpy-free**; `coverage.py`/`api.py` are pure stdlib.
- **No schema / persistence change.** `CoverageZone.type` and `always_on` are already serialized at `CONFIG_VERSION = 5`. No version bump, no migration, no TS-sibling edit.
- **Invariant:** `always_on == (type == "dedicated")` — `set_zone_type` must enforce it. An `exclusion` zone may **not** carry an `output_channel` (validation forbids it) — flipping to exclusion clears it.
- **Manual-mode cap:** flipping that raises the pickup-zone count above `MAX_MANUAL_LOBES` (only un-cut / →pickup can) raises `CoverageError("MANUAL_LOBE_LIMIT", …)`.
- **No live-DSP change.** The live engine already honors `exclusion` zones (`is_pickup_zone` filtering + `seat_mapper.exclusion_zone_azimuths` nulling via the existing runtime toggle).
- GUI verified **headless** (single-panel construct + `pytest --collect-only` + mypy); full `MainWindow` is CI-only (hangs on this box). The Qt GUI is not mypy-checked, but mypy must stay clean for the typed packages.
- Branch `feat/zone-cut-toggle` (off `master`, independent of #1/#2). Commit per task, **no push**; trailer `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`. Bash resets cwd → prefix `cd /c/Work/conferencing-audio-pipeline-py &&`. There is an unrelated `M CLAUDE.md` in the tree — never stage it.

## File Structure

- **Modify** `conf_pipeline/coverage.py` — `set_zone_type` (Task 1).
- **Modify** `conf_pipeline/api.py` — config wrapper (Task 1).
- **Modify** `conf_pipeline/__init__.py` — export `set_zone_type` (Task 1).
- **Modify** `conf_pipeline_gui/panels/design.py` — the "Cut (no pickup)" checkbox + handler (Task 2).
- **Modify** `README.md`, `CHANGELOG.md` (Task 3).
- **Tests:** `tests/test_zone_type.py` (Task 1), `tests/test_gui_zone_cut.py` (Task 2).

---

### Task 1: `set_zone_type` backend + config wrapper + export

**Files:**
- Modify: `conf_pipeline/coverage.py` (add `set_zone_type` near the other zone setters, ~line 161-198)
- Modify: `conf_pipeline/api.py` (add wrapper near `set_zone_gain_db`, ~line 263)
- Modify: `conf_pipeline/__init__.py` (export `set_zone_type` where `set_zone_gain_db` is exported)
- Test: `tests/test_zone_type.py`

**Interfaces:**
- Consumes: `conf_pipeline.model` (`CoverageZone`, `CoverageZoneType`, `MicrophoneArray`, `MAX_MANUAL_LOBES`, `is_pickup_zone`); `coverage` internals (`CoverageError`, `pickup_zone_count`, `generate_array_output_ports`).
- Produces:
  - `coverage.set_zone_type(array: MicrophoneArray, zone_id: str, zone_type: CoverageZoneType) -> MicrophoneArray`
  - `api.set_zone_type(config: SystemConfig, array_id: str, zone_id: str, zone_type: CoverageZoneType) -> SystemConfig`, exported as `cp.set_zone_type`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_zone_type.py
import pytest

import conf_pipeline as cp
from conf_pipeline.coverage import CoverageError, set_zone_type
from conf_pipeline.model import Point2D, RectShape, is_pickup_zone


def _array_with_zone(zone, mode="automatic"):
    arr = cp.create_microphone_array("a1", "Array", mode=mode, zones=[zone], position=Point2D(0.0, 0.0))
    return arr


def _dyn(zid="z1"):
    return cp.dynamic_zone(zid, "Zone", RectShape(Point2D(0.0, 0.0), 1.0, 1.0))


def test_dynamic_to_exclusion_sets_flags_and_clears_channel():
    from conf_pipeline.coverage import set_zone_output_channel
    arr = _array_with_zone(_dyn())
    arr = set_zone_output_channel(arr, "z1", 3)   # assign a channel, then flip to exclusion
    out = set_zone_type(arr, "z1", "exclusion")
    z = out.zones[0]
    assert z.type == "exclusion"
    assert z.always_on is False
    assert z.output_channel is None          # cleared (exclusion can't carry one)
    assert not is_pickup_zone(z)


def test_exclusion_to_dynamic_restores_pickup():
    arr = _array_with_zone(cp.exclusion_zone("z1", "Zone", RectShape(Point2D(0.0, 0.0), 1.0, 1.0)))
    out = set_zone_type(arr, "z1", "dynamic")
    z = out.zones[0]
    assert z.type == "dynamic"
    assert z.always_on is False
    assert is_pickup_zone(z)


def test_to_dedicated_sets_always_on():
    arr = _array_with_zone(_dyn())
    out = set_zone_type(arr, "z1", "dedicated")
    assert out.zones[0].type == "dedicated"
    assert out.zones[0].always_on is True


def test_unknown_zone_raises():
    arr = _array_with_zone(_dyn())
    with pytest.raises(CoverageError):
        set_zone_type(arr, "nope", "exclusion")


def test_manual_mode_uncut_past_cap_raises():
    # manual array already at MAX_MANUAL_LOBES pickup zones + 1 cut zone; un-cutting it would overflow
    zones = [cp.dynamic_zone(f"p{i}", f"P{i}", RectShape(Point2D(float(i), 0.0), 0.5, 0.5)) for i in range(8)]
    zones.append(cp.exclusion_zone("cut", "Cut", RectShape(Point2D(9.0, 0.0), 0.5, 0.5)))
    arr = cp.create_microphone_array("a1", "Array", mode="manual", zones=zones, position=Point2D(0.0, 0.0))
    with pytest.raises(CoverageError):
        set_zone_type(arr, "cut", "dynamic")   # would be 9 pickup lobes > 8


def test_flip_to_exclusion_in_manual_mode_is_allowed():
    # flipping a pickup zone TO exclusion lowers the count → always safe even in manual mode
    zones = [cp.dynamic_zone(f"p{i}", f"P{i}", RectShape(Point2D(float(i), 0.0), 0.5, 0.5)) for i in range(8)]
    arr = cp.create_microphone_array("a1", "Array", mode="manual", zones=zones, position=Point2D(0.0, 0.0))
    out = set_zone_type(arr, "p0", "exclusion")
    assert out.zones[0].type == "exclusion"


def test_config_wrapper_and_validate_and_roundtrip():
    cfg = cp.create_config()
    cfg = cp.add_device(cfg, cp.create_microphone_array("a1", "Array", position=Point2D(0.0, 0.0)))
    cfg = cp.add_coverage_zone(cfg, "a1", _dyn())
    cfg = cp.set_zone_type(cfg, "a1", "z1", "exclusion")
    arr = next(d for d in cfg.devices if d.id == "a1")
    assert arr.zones[0].type == "exclusion"
    assert cp.validate(cfg).ok
    # byte-identical round-trip
    assert cp.serialize(cp.deserialize(cp.serialize(cfg))) == cp.serialize(cfg)


def test_cut_zone_is_honored_by_exclusion_azimuths():
    # a cut zone shows up in the live null path's azimuth list (proves no live-DSP edit needed)
    from conf_pipeline.seat_mapper import exclusion_zone_azimuths
    cfg = cp.create_config()
    arr = cp.create_microphone_array("a1", "Array", position=Point2D(0.0, 0.0))
    arr.bearing_deg = 0.0
    cfg = cp.add_device(cfg, arr)
    cfg = cp.add_coverage_zone(cfg, "a1", cp.dynamic_zone("z1", "Zone", RectShape(Point2D(1.0, 1.0), 0.5, 0.5)))
    assert exclusion_zone_azimuths(cfg, "a1") == []   # not cut yet → no nulls
    cfg = cp.set_zone_type(cfg, "a1", "z1", "exclusion")
    assert len(exclusion_zone_azimuths(cfg, "a1")) == 1   # now cut → nulled live
```

NOTE to implementer: verify `cp.create_microphone_array`, `cp.add_device`, `cp.create_config`, `cp.dynamic_zone`, `cp.exclusion_zone`, `cp.add_coverage_zone`, `cp.validate`, `cp.serialize`/`cp.deserialize` exist with these names (they do — confirm in `conf_pipeline/__init__.py`), and that the array-level `set_zone_output_channel` is importable from `conf_pipeline.coverage`. Adjust the test to the real API if any differ.

- [ ] **Step 2: Run to verify it fails**

Run: `cd /c/Work/conferencing-audio-pipeline-py && ./.venv/Scripts/python.exe -m pytest -q tests/test_zone_type.py`
Expected: FAIL — `ImportError: cannot import name 'set_zone_type'`.

- [ ] **Step 3: Write minimal implementation**

In `conf_pipeline/coverage.py` — extend the model import to include `CoverageZoneType` (it currently imports `CoverageMode, CoverageZone, MicrophoneArray, …` from `.model`), then add after `set_zone_gain_db`/`_with_gain` (~line 176):

```python
def set_zone_type(array: MicrophoneArray, zone_id: str, zone_type: CoverageZoneType) -> MicrophoneArray:
    """Change a coverage zone's type (``dynamic``/``dedicated``/``exclusion``).

    Enforces the ``always_on == (type == "dedicated")`` invariant. Flipping TO
    ``exclusion`` ("cut" — no pickup) clears any ``output_channel`` (an exclusion
    zone may not carry one). Regenerates the array's output ports. Raises
    ``CoverageError`` if the zone is unknown, or if the change would push the
    pickup-zone count above ``MAX_MANUAL_LOBES`` while the array is in manual mode
    (only un-cutting / flipping to a pickup type can trip this)."""
    zone = next((z for z in array.zones if z.id == zone_id), None)
    if zone is None:
        raise CoverageError("COVERAGE_ZONE_INVALID", f'Array "{array.id}" has no zone "{zone_id}".')

    becomes_pickup = zone_type != "exclusion"
    others_pickup = sum(1 for z in array.zones if z.id != zone_id and is_pickup_zone(z))
    new_pickup = others_pickup + (1 if becomes_pickup else 0)
    if array.coverage_mode == "manual" and new_pickup > MAX_MANUAL_LOBES:
        raise CoverageError(
            "MANUAL_LOBE_LIMIT",
            f'Array "{array.id}" in manual mode would have {new_pickup} pickup lobes; max is {MAX_MANUAL_LOBES}.',
        )

    def _flip(z: CoverageZone) -> CoverageZone:
        n = copy.copy(z)
        n.type = zone_type
        n.always_on = zone_type == "dedicated"
        if zone_type == "exclusion":
            n.output_channel = None
        return n

    new = copy.copy(array)
    new.zones = [copy.copy(z) if z.id != zone_id else _flip(z) for z in array.zones]
    new.ports = generate_array_output_ports(array.id, array.coverage_mode, pickup_zone_count(new.zones), new.zones)
    return new
```

In `conf_pipeline/api.py` — ensure `CoverageZoneType` is imported (it imports `CoverageMode, CoverageZone, …` from `.model`; add `CoverageZoneType`), then add after `set_zone_gain_db` (~line 265):

```python
def set_zone_type(config: SystemConfig, array_id: str, zone_id: str, zone_type: CoverageZoneType) -> SystemConfig:
    """Change a coverage zone's type. Flipping to ``"exclusion"`` marks it no-pickup
    ("cut") — it is then excluded from steer targets and nulled by the live cut toggle."""
    return _array_fn(config, array_id, lambda d: cov.set_zone_type(d, zone_id, zone_type))  # type: ignore[arg-type]
```

In `conf_pipeline/__init__.py` — add `set_zone_type` to the api re-export block alongside `set_zone_gain_db` (read the file and match the exact import grouping / any `__all__`).

- [ ] **Step 4: Run to verify it passes**

Run: `cd /c/Work/conferencing-audio-pipeline-py && ./.venv/Scripts/python.exe -m pytest -q tests/test_zone_type.py && ./.venv/Scripts/python.exe -m mypy`
Expected: PASS; mypy clean.

- [ ] **Step 5: Commit**

```bash
cd /c/Work/conferencing-audio-pipeline-py && git add conf_pipeline/coverage.py conf_pipeline/api.py conf_pipeline/__init__.py tests/test_zone_type.py && git commit -m "$(printf 'feat(coverage): set_zone_type — flip a zone between pickup and cut (exclusion)\n\nCo-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>')"
```

---

### Task 2: DESIGN-panel "Cut (no pickup)" checkbox

**Files:**
- Modify: `conf_pipeline_gui/panels/design.py` (per-zone selection editor — where `output_channel`/`gain_db` are edited)
- Test: `tests/test_gui_zone_cut.py`

**Interfaces:**
- Consumes: `cp.set_zone_type` (Task 1); the panel's existing selected-zone accessor + per-zone editor + `AppState.set_config` + the panel's toast.
- Produces: a "Cut (no pickup)" `QCheckBox` + a handler `_toggle_zone_cut(self, checked)` that flips the selected zone via `cp.set_zone_type` in one undo step.

- [ ] **Step 1: Write the failing probe test**

```python
# tests/test_gui_zone_cut.py
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import pytest

pytest.importorskip("PySide6")
from PySide6.QtWidgets import QApplication

import conf_pipeline as cp
from conf_pipeline.model import Point2D, RectShape
from conf_pipeline_gui.state import AppState
from conf_pipeline_gui.panels.design import DesignPanel


@pytest.fixture(scope="module")
def qapp():
    yield QApplication.instance() or QApplication([])


def _state_with_zone():
    cfg = cp.create_config()
    cfg = cp.add_device(cfg, cp.create_microphone_array("a1", "Array", position=Point2D(0.0, 0.0)))
    cfg = cp.add_coverage_zone(cfg, "a1", cp.dynamic_zone("z1", "Zone", RectShape(Point2D(0.0, 0.0), 1.0, 1.0)))
    st = AppState()
    st.set_config(cfg)
    return st


def _zone_type(st):
    return next(d for d in st.config.devices if d.id == "a1").zones[0].type


def test_cut_checkbox_flips_zone_one_undo(qapp):
    st = _state_with_zone()
    # select the zone the way the panel expects (read design.py for the exact selection dict shape)
    st.select({"kind": "zone", "id": "z1", "array_id": "a1"})
    panel = DesignPanel(st)
    panel.refresh()
    base = st._idx
    panel._toggle_zone_cut(True)      # cut
    assert _zone_type(st) == "exclusion"
    assert st._idx == base + 1        # one undo step
    panel._toggle_zone_cut(False)     # un-cut
    assert _zone_type(st) == "dynamic"
    panel.deleteLater()
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd /c/Work/conferencing-audio-pipeline-py && QT_QPA_PLATFORM=offscreen ./.venv/Scripts/python.exe -m pytest -q tests/test_gui_zone_cut.py`
Expected: FAIL — `AttributeError: 'DesignPanel' object has no attribute '_toggle_zone_cut'`.

- [ ] **Step 3: Implement the checkbox + handler**

Read `conf_pipeline_gui/panels/design.py` first: find the per-zone selection editor (the block that shows the selected zone's `output_channel`/`gain_db` controls), how the **selected zone + its array id** are determined (the selection dict shape — adjust the test's `st.select({...})` to match the real shape), and the panel's toast mechanism. Then:

- Add a `QCheckBox("Cut (no pickup)")` to the per-zone editor, matching the file's widget/layout idiom. In the refresh that populates the editor, set its checked state to `selected_zone.type == "exclusion"` (guard against re-entrant signals the way the panel's other editor widgets do — e.g. a `_refreshing` flag or `blockSignals`).
- Add the handler:

```python
def _toggle_zone_cut(self, checked):
    sel = self._selected_zone()          # use the panel's real accessor (zone + array id)
    if sel is None:
        return
    array_id, zone_id = sel               # adapt to the real return shape
    new_type = "exclusion" if checked else "dynamic"
    try:
        self.state.set_config(cp.set_zone_type(self.state.config, array_id, zone_id, new_type))
    except cp.CoverageError as exc:        # or the real exception path
        self._toast(str(exc))
```

(Use the panel's REAL selected-zone accessor, toast name, and `CoverageError` import path — read the file; the names above are placeholders to be matched to the actual code. If the panel identifies the selected zone differently, wire `_toggle_zone_cut` to that.)

- [ ] **Step 4: Run probe + collect + mypy**

Run: `cd /c/Work/conferencing-audio-pipeline-py && QT_QPA_PLATFORM=offscreen ./.venv/Scripts/python.exe -m pytest -q tests/test_gui_zone_cut.py && QT_QPA_PLATFORM=offscreen ./.venv/Scripts/python.exe -m pytest --collect-only -q 2>&1 | tail -2 && ./.venv/Scripts/python.exe -m mypy`
Expected: probe PASS; collection clean; mypy clean.

- [ ] **Step 5: Commit**

```bash
cd /c/Work/conferencing-audio-pipeline-py && git add conf_pipeline_gui/panels/design.py tests/test_gui_zone_cut.py && git commit -m "$(printf 'feat(gui): "Cut (no pickup)" toggle on a coverage zone in DESIGN\n\nCo-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>')"
```

---

### Task 3: Docs + green-gate

**Files:**
- Modify: `README.md`, `CHANGELOG.md`

- [ ] **Step 1: CHANGELOG** — under the existing `## [Unreleased]` → `### Added` (no version section; pyproject stays as-is), add a bullet matching the existing voice:

```markdown
- **"Cut (no pickup)" zone toggle** (`cp.set_zone_type`). One click in DESIGN flips a coverage zone
  between active (`dynamic`) and cut (`exclusion`), so you can silence a problem area (a hallway, an
  HVAC corner, an auto-generated zone over a doorway) without deleting and redrawing it. A cut zone is
  excluded from steer targets and nulled live by the existing "Cut the door…" auto-steer toggle. No
  schema change.
```

- [ ] **Step 2: README** — add a short note in the coverage/Designer-workflow section (read the surrounding prose, match its depth/voice): the cut toggle flips a zone to no-pickup; its relationship to the runtime "Cut the door & anyone outside the pickup area" auto-steer toggle (cut = mark the zone no-pickup at design time; the runtime toggle decides whether to *actively null* no-pickup zones during auto-steer; a cut zone is never *steered to* regardless). Also update the stale test count: run `QT_QPA_PLATFORM=offscreen ./.venv/Scripts/python.exe -m pytest --collect-only -q -p no:cacheprovider 2>&1 | tail -1` to get the actual collected count and update the `tests/  pytest suite (N tests; …)` line in the file-tree listing (this branch is off master, so the current number there is master's — use the live count).

- [ ] **Step 3: green-gate** — run the suite EXCLUDING the 5 `MainWindow`-fixture GUI files (they hang headless on this box; CI runs them) + mypy:

```bash
cd /c/Work/conferencing-audio-pipeline-py && QT_QPA_PLATFORM=offscreen ./.venv/Scripts/python.exe -m pytest -q -p no:cacheprovider \
  --ignore=tests/test_gui_calibrate_front.py --ignore=tests/test_gui_coverage.py \
  --ignore=tests/test_gui_live_seat.py --ignore=tests/test_gui_smoke.py --ignore=tests/test_gui_twokit.py \
  && ./.venv/Scripts/python.exe -m mypy
```
Expected: all green; mypy clean.

- [ ] **Step 4: Commit**

```bash
cd /c/Work/conferencing-audio-pipeline-py && git add README.md CHANGELOG.md && git commit -m "$(printf 'docs(coverage): cut-a-zone toggle (#3a)\n\nCo-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>')"
```

---

## Self-Review (writing-plans)

**Spec coverage:** ✓ `set_zone_type` flips type + enforces `always_on` + clears channel-on-exclusion + regenerates ports + manual-cap guard + unknown-zone raise (Task 1) · ✓ config wrapper + export (Task 1) · ✓ round-trip + validate + `exclusion_zone_azimuths` coherence proving no live edit (Task 1) · ✓ GUI "Cut (no pickup)" checkbox reflecting type + one-undo flip + CoverageError→toast (Task 2) · ✓ no live-DSP change (relies on existing exclusion handling; documented) · ✓ docs incl. relationship to the runtime toggle + test-count fix (Task 3) · ✓ no schema change, numpy-free (all).

**Placeholder scan:** code is concrete. Task 1's first test has an explicit illustrative line called out for deletion. Task 2's handler/selection names are flagged "read design.py and match the real accessor/toast/exception" — a real instruction (the Qt panel's selected-zone mechanism must be confirmed against the live file), not a deferral.

**Type consistency:** `set_zone_type(array, zone_id, zone_type)` (coverage) and `set_zone_type(config, array_id, zone_id, zone_type)` (api) are used consistently; `CoverageZoneType` values `"dynamic"|"dedicated"|"exclusion"` match `model.py`; `MAX_MANUAL_LOBES`, `is_pickup_zone`, `pickup_zone_count`, `generate_array_output_ports`, `CoverageError` match the live `coverage.py`.
