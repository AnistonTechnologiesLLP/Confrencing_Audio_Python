# Auto-generate Coverage Zones from Furniture Seating — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A one-click DESIGN action that turns a room's furniture seating into microphone coverage zones, clustered by what the array can physically resolve (reusing the #1 calibrated beamwidth), replacing the selected array's zones.

**Architecture:** A new pure-stdlib module `conf_pipeline/seat_zones.py` derives seats from furniture, computes each seat's array-relative look (azimuth + steered half-beamwidth, *identical* to `coverage_sim.coverage_caveats`), clusters un-resolvable neighbours, builds `dynamic` `RectShape` pickup zones, and replaces the array's zones. A DESIGN-panel button applies it via `AppState.set_config` (one undo). No schema change.

**Tech Stack:** Python 3.10+ (pure stdlib in `conf_pipeline`), PySide6 (GUI), pytest, mypy. venv `./.venv/Scripts/python.exe`.

## Global Constraints

- `conf_pipeline` stays **numpy-free** — `seat_zones.py` is pure stdlib (`math`, `dataclasses`, `copy`).
- **No schema / persistence change.** `CoverageZone` + `SeatAnchor` are already serialized at `CONFIG_VERSION = 5`. No version bump, no migration, no TS-sibling edit. Generated zones are ordinary config data.
- **Coherence with #1:** the separability decision MUST use the same computation as `conf_pipeline/coverage_sim.py` `coverage_caveats` — `steering_angles(Point3D(array.x, array.y, _device_elev(config, array)), Point3D(seat.x, seat.y, SEATED_HEAD_M))` → `.azimuth_deg` / `.downtilt_deg`, then `steered_beamwidth_deg(cap.aperture_m, SIM_SPEECH_FREQ_HZ, downtilt)` (or `DEFAULT_PICKUP_BEAM_HALF_DEG` when `aperture_m is None`), and `separable(angular_separation_deg(a, b), max(half_a, half_b))`. A test asserts a seat pair `coverage_caveats` flags as un-separable ends up merged into one zone.
- Respect `MAX_ZONES_PER_ARRAY = 8` and the invariant `always_on == (type == "dedicated")` (generated zones are `dynamic` → `always_on=False`).
- Operates on the **selected** array; each array owns the seats nearest to it (multi-array rooms don't double-cover). **Replace-all** that array's zones (clear then regenerate). If the array owns no seats, leave the config unchanged + a warning (don't wipe zones for nothing).
- GUI verified **headless** (single-panel construct + `pytest --collect-only` + mypy); full `MainWindow` is CI-only (hangs on this box). The Qt GUI is not mypy-checked, but mypy must stay clean for the typed packages.
- Branch `feat/auto-seat-zones` (stacked on `feat/aperture-aware-coverage`). Commit per task, **no push**; trailer `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`. Bash resets cwd → prefix `cd /c/Work/conferencing-audio-pipeline-py &&`. There is an unrelated `M CLAUDE.md` in the tree — never stage it.

## File Structure

- **Create** `conf_pipeline/seat_zones.py` — the whole feature backend (Tasks 1-3): `derived_room_seats`, `cluster_seats`, `SeatLook`, `generate_seat_zones`, `SeatZoneResult`.
- **Modify** `conf_pipeline/__init__.py` — export the public API (Task 3).
- **Modify** `conf_pipeline_gui/panels/design.py` — the button + handler (Task 4).
- **Modify** `README.md`, `CHANGELOG.md` (Task 5).
- **Tests:** `tests/test_seat_zones.py` (Tasks 1-3), `tests/test_gui_auto_seat_zones.py` (Task 4).

---

### Task 1: Seat derivation (`derived_room_seats`)

**Files:**
- Create: `conf_pipeline/seat_zones.py`
- Test: `tests/test_seat_zones.py`

**Interfaces:**
- Consumes: `conf_pipeline.model` (`SystemConfig`, `RoomObject`, `SeatAnchor`, `Point2D`), `conf_pipeline.furniture` (`furniture_type`, `resolved_dimensions`).
- Produces: `derived_room_seats(config: SystemConfig) -> list[tuple[str, SeatAnchor]]` — `(seat_id, anchor)` pairs; ids `"{obj.id}-seat{i}"` (1-based), matching `seat_mapper.room_seats` / `coverage_sim.room_targets`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_seat_zones.py
from conf_pipeline.model import Point2D, RoomObject, RoomLayout, SeatAnchor, SystemConfig
from conf_pipeline.seat_zones import derived_room_seats


def _room(objects):
    return SystemConfig(
        devices=[],
        room=RoomLayout(
            vertices=[Point2D(-3, -3), Point2D(3, -3), Point2D(3, 3), Point2D(-3, 3)],
            height=3.0,
            objects=objects,
        ),
    )


def test_chair_yields_one_seat_at_its_position():
    cfg = _room([RoomObject(id="c1", kind="chair", position=Point2D(1.0, 2.0))])
    seats = derived_room_seats(cfg)
    assert len(seats) == 1
    sid, anchor = seats[0]
    assert sid == "c1-seat1"
    assert abs(anchor.position.x - 1.0) < 1e-9 and abs(anchor.position.y - 2.0) < 1e-9


def test_sofa_yields_capacity_seats_spread_across_width():
    # sofa catalog: width 2.0 m, capacity 3, no rotation → 3 seats along +X centred on position
    cfg = _room([RoomObject(id="s1", kind="sofa", position=Point2D(0.0, 0.0))])
    seats = derived_room_seats(cfg)
    assert [sid for sid, _ in seats] == ["s1-seat1", "s1-seat2", "s1-seat3"]
    xs = sorted(a.position.x for _, a in seats)
    # evenly spaced across width 2.0 → at -2/3, 0, +2/3 (fractions 1/6, 3/6, 5/6)
    assert abs(xs[0] - (-2.0 / 3.0)) < 1e-6
    assert abs(xs[1] - 0.0) < 1e-6
    assert abs(xs[2] - (2.0 / 3.0)) < 1e-6
    assert all(abs(a.position.y) < 1e-9 for _, a in seats)


def test_explicit_seats_used_verbatim_not_derived():
    obj = RoomObject(id="t1", kind="table", position=Point2D(0.0, 0.0),
                     seats=[SeatAnchor(position=Point2D(0.0, -0.6)), SeatAnchor(position=Point2D(0.0, 0.6))])
    seats = derived_room_seats(_room([obj]))
    assert [sid for sid, _ in seats] == ["t1-seat1", "t1-seat2"]
    assert abs(seats[0][1].position.y - (-0.6)) < 1e-9


def test_bare_table_yields_no_seats():
    cfg = _room([RoomObject(id="t1", kind="table", position=Point2D(0.0, 0.0))])
    assert derived_room_seats(cfg) == []


def test_rotated_sofa_spreads_along_rotated_width():
    # 90° clockwise: local +X maps to -Y (x'=lx*cos+ly*sin, y'=-lx*sin+ly*cos; cos90=0, sin90=1)
    cfg = _room([RoomObject(id="s1", kind="sofa", position=Point2D(0.0, 0.0), rotation_deg=90.0)])
    seats = derived_room_seats(cfg)
    ys = sorted(a.position.y for _, a in seats)
    assert all(abs(a.position.x) < 1e-6 for _, a in seats)  # spread now along Y
    assert abs(ys[0] - (-2.0 / 3.0)) < 1e-6 and abs(ys[2] - (2.0 / 3.0)) < 1e-6
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd /c/Work/conferencing-audio-pipeline-py && ./.venv/Scripts/python.exe -m pytest -q tests/test_seat_zones.py`
Expected: FAIL — `ModuleNotFoundError: No module named 'conf_pipeline.seat_zones'`.

- [ ] **Step 3: Write minimal implementation**

```python
# conf_pipeline/seat_zones.py
"""Generate microphone coverage zones from furniture seating (pure stdlib).

Sub-feature #2 of the POLARIS table-array coverage workflow. Derives seats from
furniture, clusters them by what the array can physically resolve (reusing the
aperture-aware beamwidth from directivity.py — sub-feature #1), and builds one
pickup zone per cluster. No numpy, no schema change.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from .furniture import furniture_type, resolved_dimensions
from .model import Point2D, SeatAnchor, SystemConfig


def derived_room_seats(config: SystemConfig) -> list[tuple[str, SeatAnchor]]:
    """All ``(seat_id, anchor)`` for the room, deriving seats from seating furniture.

    Explicit ``RoomObject.seats`` win verbatim. Otherwise furniture whose catalog
    ``seat_capacity >= 1`` (chair/seat = 1, sofa = N) yields that many seats: one at
    the object's position for capacity 1, else N spread evenly across its width
    (local +X, rotated by ``rotation_deg``). Other furniture (tables, screens, …)
    yields nothing. Ids are ``"{obj.id}-seat{i}"`` (1-based), matching
    :func:`conf_pipeline.seat_mapper.room_seats`.
    """
    room = config.room
    if room is None:
        return []
    out: list[tuple[str, SeatAnchor]] = []
    for obj in room.objects:
        if obj.seats:
            for i, seat in enumerate(obj.seats, start=1):
                out.append((f"{obj.id}-seat{i}", seat))
            continue
        ft = furniture_type(obj.kind)
        capacity = ft.seat_capacity if ft else 0
        if capacity < 1:
            continue
        width, _depth, _h = resolved_dimensions(obj)
        rad = math.radians(obj.rotation_deg or 0.0)
        cos_r, sin_r = math.cos(rad), math.sin(rad)
        for i in range(capacity):
            frac = (i + 0.5) / capacity
            lx = -width / 2.0 + width * frac           # local +X offset; ly = 0 (centre depth)
            px = obj.position.x + lx * cos_r            # obb_corners rotation: x' = lx*cos + ly*sin
            py = obj.position.y - lx * sin_r            #                       y' = -lx*sin + ly*cos
            out.append((f"{obj.id}-seat{i + 1}", SeatAnchor(position=Point2D(px, py), facing_deg=obj.rotation_deg)))
    return out
```

- [ ] **Step 4: Run to verify it passes**

Run: `cd /c/Work/conferencing-audio-pipeline-py && ./.venv/Scripts/python.exe -m pytest -q tests/test_seat_zones.py && ./.venv/Scripts/python.exe -m mypy`
Expected: PASS; mypy clean.

- [ ] **Step 5: Commit**

```bash
cd /c/Work/conferencing-audio-pipeline-py && git add conf_pipeline/seat_zones.py tests/test_seat_zones.py && git commit -m "$(printf 'feat(coverage): derive room seats from furniture seating\n\nCo-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>')"
```

---

### Task 2: Separability clustering (`cluster_seats`)

**Files:**
- Modify: `conf_pipeline/seat_zones.py`
- Test: `tests/test_seat_zones.py`

**Interfaces:**
- Consumes: `conf_pipeline.model.angular_separation_deg`, `conf_pipeline.directivity.separable`, `conf_pipeline.model.MAX_ZONES_PER_ARRAY`.
- Produces:
  - `SeatLook` dataclass: `seat_id: str`, `azimuth_deg: float`, `half_deg: float` (a seat's array-relative look).
  - `cluster_seats(looks: list[SeatLook], *, max_zones: int = MAX_ZONES_PER_ARRAY, factor: float = 1.5) -> tuple[list[list[str]], bool]` — groups of seat-ids (azimuth-sorted) the array can't resolve apart, plus `forced_merge` (True if it had to merge resolvable groups to fit `max_zones`).

- [ ] **Step 1: Write the failing test**

```python
# add to tests/test_seat_zones.py
from conf_pipeline.seat_zones import SeatLook, cluster_seats


def test_well_separated_seats_stay_individual():
    looks = [SeatLook("a", 0.0, 10.0), SeatLook("b", 60.0, 10.0), SeatLook("c", 120.0, 10.0)]
    groups, forced = cluster_seats(looks)
    assert groups == [["a"], ["b"], ["c"]]
    assert forced is False


def test_close_seats_below_resolution_merge():
    # 10° apart, half-width 30° → separable needs ≥ 1.5*30 = 45° → merge all three
    looks = [SeatLook("a", 0.0, 30.0), SeatLook("b", 10.0, 30.0), SeatLook("c", 20.0, 30.0)]
    groups, forced = cluster_seats(looks)
    assert groups == [["a", "b", "c"]]
    assert forced is False


def test_more_groups_than_cap_force_merge_and_flag():
    looks = [SeatLook(str(i), float(i * 20), 5.0) for i in range(10)]  # 10 resolvable seats, cap 8
    groups, forced = cluster_seats(looks, max_zones=8)
    assert len(groups) == 8
    assert forced is True
    # every seat still assigned exactly once
    assert sorted(s for g in groups for s in g) == sorted(str(i) for i in range(10))


def test_output_is_azimuth_sorted():
    looks = [SeatLook("hi", 170.0, 10.0), SeatLook("lo", 5.0, 10.0)]
    groups, _ = cluster_seats(looks)
    assert groups == [["lo"], ["hi"]]
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd /c/Work/conferencing-audio-pipeline-py && ./.venv/Scripts/python.exe -m pytest -q tests/test_seat_zones.py::test_close_seats_below_resolution_merge`
Expected: FAIL — `ImportError: cannot import name 'SeatLook'`.

- [ ] **Step 3: Write minimal implementation**

Add to `conf_pipeline/seat_zones.py` (imports + code):

```python
# extend the imports at the top of conf_pipeline/seat_zones.py:
from .directivity import separable
from .model import MAX_ZONES_PER_ARRAY, angular_separation_deg


@dataclass
class SeatLook:
    seat_id: str
    azimuth_deg: float   # room-frame bearing of the seat from the array (0° = +Y, CW)
    half_deg: float      # array's steered 3 dB half-beamwidth toward this seat


def cluster_seats(
    looks: list[SeatLook], *, max_zones: int = MAX_ZONES_PER_ARRAY, factor: float = 1.5
) -> tuple[list[list[str]], bool]:
    """Group seats the array cannot resolve apart.

    Sorts by azimuth and merges each seat into the running group when it is **not**
    ``separable`` from the previous seat (``angular_separation_deg`` vs the larger of
    the two half-beamwidths). If more than ``max_zones`` groups remain, repeatedly
    merges the closest-in-azimuth adjacent pair until ``max_zones`` (``forced_merge``
    True). Returns ``(groups_of_seat_ids, forced_merge)`` in azimuth order.
    """
    if not looks:
        return [], False
    ordered = sorted(looks, key=lambda L: L.azimuth_deg)
    groups: list[list[SeatLook]] = [[ordered[0]]]
    for cur in ordered[1:]:
        prev = groups[-1][-1]
        sep = angular_separation_deg(prev.azimuth_deg, cur.azimuth_deg)
        if separable(sep, max(prev.half_deg, cur.half_deg), factor):
            groups.append([cur])
        else:
            groups[-1].append(cur)

    forced = False
    while len(groups) > max_zones:
        forced = True
        # merge the adjacent pair with the smallest azimuth gap (group reps = last/first members)
        best_i, best_gap = 0, float("inf")
        for i in range(len(groups) - 1):
            gap = angular_separation_deg(groups[i][-1].azimuth_deg, groups[i + 1][0].azimuth_deg)
            if gap < best_gap:
                best_gap, best_i = gap, i
        groups[best_i] = groups[best_i] + groups[best_i + 1]
        del groups[best_i + 1]

    return [[L.seat_id for L in g] for g in groups], forced
```

- [ ] **Step 4: Run to verify it passes**

Run: `cd /c/Work/conferencing-audio-pipeline-py && ./.venv/Scripts/python.exe -m pytest -q tests/test_seat_zones.py && ./.venv/Scripts/python.exe -m mypy`
Expected: PASS; mypy clean.

- [ ] **Step 5: Commit**

```bash
cd /c/Work/conferencing-audio-pipeline-py && git add conf_pipeline/seat_zones.py tests/test_seat_zones.py && git commit -m "$(printf 'feat(coverage): cluster seats by array-resolvable separability\n\nCo-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>')"
```

---

### Task 3: Zone generation (`generate_seat_zones`) + export

**Files:**
- Modify: `conf_pipeline/seat_zones.py`
- Modify: `conf_pipeline/__init__.py`
- Test: `tests/test_seat_zones.py`

**Interfaces:**
- Consumes: `derived_room_seats`, `cluster_seats`, `SeatLook` (Tasks 1-2); `conf_pipeline.angles` (`Point3D`, `steering_angles`); `conf_pipeline.directivity` (`SIM_SPEECH_FREQ_HZ`, `steered_beamwidth_deg`); `conf_pipeline.coverage_sim` (`SEATED_HEAD_M`, `DEFAULT_PICKUP_BEAM_HALF_DEG`, `_device_elev`); `conf_pipeline.profiles.device_capabilities`; `conf_pipeline.coverage` (`dynamic_zone`, `add_coverage_zone`, `remove_coverage_zone`); `conf_pipeline.model` (`MicrophoneArray`, `RectShape`, `Point2D`, `CoverageZone`).
- Produces:
  - `SeatZoneResult` dataclass: `config: SystemConfig`, `created: list[str]`, `merged: list[str]`, `warnings: list[str]`.
  - `generate_seat_zones(config: SystemConfig, array_id: str) -> SeatZoneResult` — replaces `array_id`'s zones with seat-derived clusters; raises `ValueError` if `array_id` is unknown / not a microphone array.
  - Exported as `cp.generate_seat_zones`, `cp.SeatZoneResult`, `cp.derived_room_seats`.

- [ ] **Step 1: Write the failing test**

```python
# add to tests/test_seat_zones.py
import conf_pipeline as cp
from conf_pipeline.coverage_sim import coverage_caveats
from conf_pipeline.model import MicrophoneArray, Point2D, RoomObject, RoomLayout, SystemConfig
from conf_pipeline.seat_zones import generate_seat_zones


def _polaris_room(objects, *, bearing=0.0, pos=Point2D(0.0, 0.0)):
    arr = cp.create_microphone_array("a1", "Array", position=pos)
    arr.profile_id = "polaris-8"
    arr.bearing_deg = bearing
    arr.elevation = 0.75
    return SystemConfig(
        devices=[arr],
        room=RoomLayout(vertices=[Point2D(-4, -4), Point2D(4, -4), Point2D(4, 4), Point2D(-4, 4)],
                        height=3.0, objects=objects),
    )


def test_well_spaced_seats_get_individual_zones():
    # two chairs ~3.5 m apart, ~2 m from the array → wide azimuth gap → 2 zones
    cfg = _polaris_room([
        RoomObject(id="c1", kind="chair", position=Point2D(-1.8, 2.0)),
        RoomObject(id="c2", kind="chair", position=Point2D(1.8, 2.0)),
    ])
    res = generate_seat_zones(cfg, "a1")
    arr = next(d for d in res.config.devices if d.id == "a1")
    assert len(arr.zones) == 2
    assert all(z.type == "dynamic" for z in arr.zones)
    assert len(res.created) == 2


def test_close_seats_merge_into_one_zone_coherent_with_caveats():
    # two chairs ~0.4 m apart at ~1.2 m. Coherence with sub-feature #1: if coverage_caveats
    # flags the two seat positions (as separate zones) un-separable, generate_seat_zones must
    # merge those same two seats into ONE zone.
    cfg = _polaris_room([
        RoomObject(id="c1", kind="chair", position=Point2D(-0.2, 1.2)),
        RoomObject(id="c2", kind="chair", position=Point2D(0.2, 1.2)),
    ])
    # 1) seed the two seats as separate manual zones and confirm #1 calls them unseparable
    seeded = cp.add_coverage_zone(cfg, "a1",
        cp.dynamic_zone("z-c1", "c1", cp.RectShape(Point2D(-0.5, 0.9), 0.6, 0.6)))
    seeded = cp.add_coverage_zone(seeded, "a1",
        cp.dynamic_zone("z-c2", "c2", cp.RectShape(Point2D(-0.1, 0.9), 0.6, 0.6)))
    assert any("separat" in c.lower() for c in coverage_caveats(seeded))
    # 2) the generator merges the same two seats into one zone
    res = generate_seat_zones(cfg, "a1")
    arr = next(d for d in res.config.devices if d.id == "a1")
    assert len(arr.zones) == 1
    assert res.merged  # a human-readable merge note


def test_replace_all_clears_existing_zones():
    cfg = _polaris_room([RoomObject(id="c1", kind="chair", position=Point2D(0.0, 2.0))])
    # pre-seed a manual zone
    cfg2 = cp.add_coverage_zone(cfg, "a1", cp.dynamic_zone("manual", "Manual", cp.RectShape(Point2D(-3, -3), 0.5, 0.5)))
    res = generate_seat_zones(cfg2, "a1")
    arr = next(d for d in res.config.devices if d.id == "a1")
    assert all(z.id != "manual" for z in arr.zones)  # manual zone gone (replace-all)


def test_no_seats_leaves_config_unchanged_with_warning():
    cfg = _polaris_room([RoomObject(id="t1", kind="table", position=Point2D(0.0, 1.5))])
    res = generate_seat_zones(cfg, "a1")
    assert res.created == []
    assert res.warnings
    arr = next(d for d in res.config.devices if d.id == "a1")
    assert arr.zones == []  # unchanged (was empty)


def test_unknown_array_raises():
    cfg = _polaris_room([RoomObject(id="c1", kind="chair", position=Point2D(0.0, 2.0))])
    import pytest
    with pytest.raises(ValueError):
        generate_seat_zones(cfg, "nope")


def test_result_zones_validate():
    cfg = _polaris_room([
        RoomObject(id="c1", kind="chair", position=Point2D(-1.8, 2.0)),
        RoomObject(id="c2", kind="chair", position=Point2D(1.8, 2.0)),
    ])
    res = generate_seat_zones(cfg, "a1")
    assert cp.validate(res.config).ok


def test_no_position_falls_back_to_per_seat_with_warning():
    cfg = _polaris_room([
        RoomObject(id="c1", kind="chair", position=Point2D(-0.2, 1.2)),
        RoomObject(id="c2", kind="chair", position=Point2D(0.2, 1.2)),
    ])
    arr = next(d for d in cfg.devices if d.id == "a1")
    arr.position = None  # un-pose: can't compute looks
    res = generate_seat_zones(cfg, "a1")
    out = next(d for d in res.config.devices if d.id == "a1")
    assert len(out.zones) == 2          # per-seat, no merge
    assert any("position" in w.lower() for w in res.warnings)
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd /c/Work/conferencing-audio-pipeline-py && ./.venv/Scripts/python.exe -m pytest -q tests/test_seat_zones.py::test_close_seats_merge_into_one_zone_coherent_with_caveats`
Expected: FAIL — `ImportError: cannot import name 'generate_seat_zones'`.

- [ ] **Step 3: Write minimal implementation**

Add to `conf_pipeline/seat_zones.py`:

```python
# extend imports:
import copy
from .angles import Point3D, steering_angles
from .coverage import add_coverage_zone, dynamic_zone, remove_coverage_zone
from .coverage_sim import DEFAULT_PICKUP_BEAM_HALF_DEG, SEATED_HEAD_M, _device_elev
from .directivity import SIM_SPEECH_FREQ_HZ, steered_beamwidth_deg
from .model import CoverageZone, MicrophoneArray, RectShape
from .profiles import device_capabilities

_ZONE_MARGIN_M = 0.35   # capture radius padding around the seat point(s)
_ZONE_MIN_SIDE_M = 0.6  # smallest zone side


@dataclass
class SeatZoneResult:
    config: SystemConfig
    created: list[str]        # generated zone labels
    merged: list[str]         # human-readable "merged because …" notes
    warnings: list[str]


def _owned_seats(config: SystemConfig, array_id: str,
                 seats: list[tuple[str, SeatAnchor]]) -> list[tuple[str, SeatAnchor]]:
    """The subset of ``seats`` whose nearest microphone array (Euclidean) is
    ``array_id`` — mirrors :func:`conf_pipeline.seat_mapper.seats_owned_by_array`
    but over derived seats. Ties go to the lowest array id. Arrays without a
    position are ignored as owners."""
    arrays = [d for d in config.devices
              if isinstance(d, MicrophoneArray) and d.position is not None]
    if not arrays:
        return []
    out: list[tuple[str, SeatAnchor]] = []
    for sid, anchor in seats:
        best_id, best_d = None, float("inf")
        for a in sorted(arrays, key=lambda a: a.id):
            d = math.hypot(a.position.x - anchor.position.x, a.position.y - anchor.position.y)
            if d < best_d - 1e-9:
                best_d, best_id = d, a.id
        if best_id == array_id:
            out.append((sid, anchor))
    return out


def _zone_for_group(array_id: str, n: int, points: list[Point2D],
                    seat_ids: list[str]) -> CoverageZone:
    xs = [p.x for p in points]
    ys = [p.y for p in points]
    x0, x1 = min(xs) - _ZONE_MARGIN_M, max(xs) + _ZONE_MARGIN_M
    y0, y1 = min(ys) - _ZONE_MARGIN_M, max(ys) + _ZONE_MARGIN_M
    w = max(x1 - x0, _ZONE_MIN_SIDE_M)
    h = max(y1 - y0, _ZONE_MIN_SIDE_M)
    cx, cy = (x0 + x1) / 2.0, (y0 + y1) / 2.0
    shape = RectShape(origin=Point2D(cx - w / 2.0, cy - h / 2.0), width=w, height=h)
    label = f"Seat {seat_ids[0].split('-seat')[-1]}" if len(seat_ids) == 1 else f"Seats ({len(seat_ids)})"
    return dynamic_zone(id=f"{array_id}-z{n}", label=label, shape=shape)


def generate_seat_zones(config: SystemConfig, array_id: str) -> SeatZoneResult:
    """Replace ``array_id``'s coverage zones with zones derived from room seating,
    clustered by what the array can physically resolve (sub-feature #1 beamwidth)."""
    array = next((d for d in config.devices if d.id == array_id), None)
    if not isinstance(array, MicrophoneArray):
        raise ValueError(f"No microphone array {array_id!r} in config")

    owned = _owned_seats(config, array_id, derived_room_seats(config))
    if not owned:
        return SeatZoneResult(config, [], [], ["No seats found for this array — place chairs/sofas or set seat anchors."])

    anchors = {sid: anchor for sid, anchor in owned}
    warnings: list[str] = []

    if array.position is None:
        # no pose → can't compute looks → one zone per seat (no merge), cap at 8
        seat_ids = [sid for sid, _ in owned]
        groups = [[sid] for sid in seat_ids][:8]
        if len(seat_ids) > 8:
            warnings.append("More than 8 seats and no array position — only the first 8 got zones.")
        warnings.append("Set the array position for separability-aware grouping.")
        forced = False
    else:
        cap = device_capabilities(array)
        src = Point3D(array.position.x, array.position.y, _device_elev(config, array))
        looks: list[SeatLook] = []
        for sid, anchor in owned:
            sa = steering_angles(src, Point3D(anchor.position.x, anchor.position.y, SEATED_HEAD_M))
            half = (steered_beamwidth_deg(cap.aperture_m, SIM_SPEECH_FREQ_HZ, sa.downtilt_deg)
                    if cap.aperture_m is not None else DEFAULT_PICKUP_BEAM_HALF_DEG)
            looks.append(SeatLook(sid, sa.azimuth_deg, half))
        groups, forced = cluster_seats(looks)

    # build zones; replace-all on the array
    new_array = array
    for zid in [z.id for z in array.zones]:
        new_array = remove_coverage_zone(new_array, zid)
    created: list[str] = []
    merged: list[str] = []
    for n, group in enumerate(groups, start=1):
        pts = [anchors[sid].position for sid in group]
        zone = _zone_for_group(array_id, n, pts, group)
        new_array = add_coverage_zone(new_array, zone)
        created.append(zone.label)
        if len(group) > 1:
            merged.append(f"{zone.label}: merged {len(group)} seats this array cannot resolve apart.")
    if forced:
        warnings.append("More seats than this array can address — merged the closest into shared zones.")

    new_devices = [new_array if d.id == array_id else d for d in config.devices]
    new_config = copy.copy(config)
    new_config.devices = new_devices
    return SeatZoneResult(new_config, created, merged, warnings)
```

Then export in `conf_pipeline/__init__.py` — add alongside the other `from .<module> import …` lines and to `__all__` if the file maintains one:

```python
from .seat_zones import SeatZoneResult, derived_room_seats, generate_seat_zones
```

(Read `conf_pipeline/__init__.py` first and match its exact export idiom — whether names are added to an `__all__` list, and the import grouping/order.)

- [ ] **Step 4: Run to verify it passes**

Run: `cd /c/Work/conferencing-audio-pipeline-py && ./.venv/Scripts/python.exe -m pytest -q tests/test_seat_zones.py && ./.venv/Scripts/python.exe -m mypy`
Expected: PASS (all seat-zone tests); mypy clean.

- [ ] **Step 5: Commit**

```bash
cd /c/Work/conferencing-audio-pipeline-py && git add conf_pipeline/seat_zones.py conf_pipeline/__init__.py tests/test_seat_zones.py && git commit -m "$(printf 'feat(coverage): generate seat-derived coverage zones (cp.generate_seat_zones)\n\nCo-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>')"
```

---

### Task 4: DESIGN-panel button + handler (GUI; CI-verified)

**Files:**
- Modify: `conf_pipeline_gui/panels/design.py`
- Test: `tests/test_gui_auto_seat_zones.py`

**Interfaces:**
- Consumes: `cp.generate_seat_zones` (Task 3); `AppState.set_config` (one undo step), `DesignPanel._selected_array_id()`, `DesignPanel._toast(...)` (existing). Adds `QPushButton`/`QMessageBox` (import `QMessageBox` from `PySide6.QtWidgets` if not already imported in design.py).
- Produces: `DesignPanel._auto_zones_from_seating()` handler + a "Auto-generate zones from seating" button in the panel's Coverage group.

- [ ] **Step 1: Write the failing probe test**

```python
# tests/test_gui_auto_seat_zones.py
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import pytest

pytest.importorskip("PySide6")
from PySide6.QtWidgets import QApplication, QMessageBox

import conf_pipeline as cp
from conf_pipeline.model import Point2D, RoomObject, RoomLayout, SystemConfig
from conf_pipeline_gui.state import AppState
from conf_pipeline_gui.panels.design import DesignPanel


@pytest.fixture(scope="module")
def qapp():
    yield QApplication.instance() or QApplication([])


def _state_with_polaris_and_chairs():
    arr = cp.create_microphone_array("a1", "Array", position=Point2D(0.0, 0.0))
    arr.profile_id = "polaris-8"
    arr.bearing_deg = 0.0
    arr.elevation = 0.75
    cfg = SystemConfig(
        devices=[arr],
        room=RoomLayout(vertices=[Point2D(-4, -4), Point2D(4, -4), Point2D(4, 4), Point2D(-4, 4)],
                        height=3.0,
                        objects=[RoomObject(id="c1", kind="chair", position=Point2D(-1.8, 2.0)),
                                 RoomObject(id="c2", kind="chair", position=Point2D(1.8, 2.0))]),
    )
    st = AppState()
    st.set_config(cfg)
    st.select({"kind": "device", "id": "a1"})
    return st


def test_button_generates_zones_one_undo(qapp, monkeypatch):
    monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: None)  # no modal in headless
    st = _state_with_polaris_and_chairs()
    panel = DesignPanel(st)
    panel.refresh()
    base = st._idx
    panel._auto_zones_from_seating()
    arr = next(d for d in st.config.devices if d.id == "a1")
    assert len(arr.zones) >= 1          # zones were generated
    assert st._idx == base + 1          # exactly one undo step
    panel.deleteLater()


def test_button_no_seats_toasts_no_change(qapp, monkeypatch):
    monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: None)
    arr = cp.create_microphone_array("a1", "Array", position=Point2D(0.0, 0.0))
    arr.profile_id = "polaris-8"
    cfg = SystemConfig(devices=[arr],
                       room=RoomLayout(vertices=[Point2D(-3, -3), Point2D(3, -3), Point2D(3, 3), Point2D(-3, 3)],
                                       height=3.0, objects=[RoomObject(id="t1", kind="table", position=Point2D(0, 1.5))]))
    st = AppState(); st.set_config(cfg); st.select({"kind": "device", "id": "a1"})
    panel = DesignPanel(st); panel.refresh()
    base = st._idx
    panel._auto_zones_from_seating()
    assert st._idx == base               # no undo step (no change)
    panel.deleteLater()
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd /c/Work/conferencing-audio-pipeline-py && QT_QPA_PLATFORM=offscreen ./.venv/Scripts/python.exe -m pytest -q tests/test_gui_auto_seat_zones.py`
Expected: FAIL — `AttributeError: 'DesignPanel' object has no attribute '_auto_zones_from_seating'`.

- [ ] **Step 3: Implement the button + handler**

Read `conf_pipeline_gui/panels/design.py` first: find where the Coverage group is built (the "+ Zone" / mode buttons block, ~line 100-112) and add a button there matching the existing idiom; confirm `QMessageBox` is imported (add `QMessageBox` to the `from PySide6.QtWidgets import (...)` block if absent). Add the handler.

Button (place beside the existing "+ Zone" button in the Coverage group; match the file's `addWidget` idiom):

```python
auto_btn = QPushButton("Auto-generate zones from seating")
auto_btn.clicked.connect(self._auto_zones_from_seating)
# <coverage-group layout>.addWidget(auto_btn)   # use the same layout var the "+ Zone" button uses
```

Handler:

```python
def _auto_zones_from_seating(self):
    aid = self._selected_array_id()
    if not aid:
        return self._toast("Add a microphone array first.")
    try:
        res = cp.generate_seat_zones(self.state.config, aid)
    except ValueError as exc:
        return self._toast(str(exc))
    if not res.created:
        return self._toast(res.warnings[0] if res.warnings else "No seats to cover.")
    self.state.set_config(res.config)  # one undo step + repaint
    lines = [f"Created {len(res.created)} zone(s): " + ", ".join(res.created)]
    lines += res.merged + res.warnings
    QMessageBox.information(self, "Auto-generate zones from seating", "\n".join(lines))
    self._toast("Generated zones from seating")
```

(If `self._toast` is not on `DesignPanel`/`PanelBase`, use the same toast mechanism the panel's other handlers use — read the file and match it.)

- [ ] **Step 4: Run probe + collect + mypy**

Run: `cd /c/Work/conferencing-audio-pipeline-py && QT_QPA_PLATFORM=offscreen ./.venv/Scripts/python.exe -m pytest -q tests/test_gui_auto_seat_zones.py && QT_QPA_PLATFORM=offscreen ./.venv/Scripts/python.exe -m pytest --collect-only -q 2>&1 | tail -2 && ./.venv/Scripts/python.exe -m mypy`
Expected: probe PASS; collection clean; mypy clean.

- [ ] **Step 5: Commit**

```bash
cd /c/Work/conferencing-audio-pipeline-py && git add conf_pipeline_gui/panels/design.py tests/test_gui_auto_seat_zones.py && git commit -m "$(printf 'feat(gui): one-click "Auto-generate zones from seating" in DESIGN\n\nCo-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>')"
```

---

### Task 5: Docs + green-gate

**Files:**
- Modify: `README.md`, `CHANGELOG.md`

- [ ] **Step 1: CHANGELOG** — under the existing `## [Unreleased]` → `### Added` (do NOT add a version section; pyproject stays 1.18.0), add a bullet matching the existing voice:

```markdown
- **Auto-generate coverage zones from furniture seating** (`cp.generate_seat_zones`, sub-feature #2 of
  the POLARIS table-array coverage workflow). One DESIGN click derives seats from chairs/sofas (or
  explicit seat anchors), clusters seats the array cannot physically resolve into shared zones (reusing
  the calibrated aperture beamwidth), and replaces the selected array's zones. Honest by construction —
  never more than 8 zones, merges what a small array can't separate. No schema change.
```

- [ ] **Step 2: README** — add a short note in the coverage/Designer-workflow section (read the surrounding prose and match its depth/voice): the one-click auto-zone generation, seat derivation from furniture, separability-aware merging tied to the honest beamwidth, replace-all, no schema change. Do not claim Shure parity or anything beyond the facts.

- [ ] **Step 3: green-gate** — run the suite (excluding the 5 `MainWindow` files that hang headless on this box; they run in CI) + mypy:

```bash
cd /c/Work/conferencing-audio-pipeline-py && QT_QPA_PLATFORM=offscreen ./.venv/Scripts/python.exe -m pytest -q -p no:cacheprovider \
  --ignore=tests/test_gui_calibrate_front.py --ignore=tests/test_gui_coverage.py \
  --ignore=tests/test_gui_live_seat.py --ignore=tests/test_gui_smoke.py --ignore=tests/test_gui_twokit.py \
  && ./.venv/Scripts/python.exe -m mypy
```
Expected: all green; mypy clean. (A confirming `schema-parity-guard` pass is fine since `__init__.py` gained an export, but no schema changed.)

- [ ] **Step 4: Commit**

```bash
cd /c/Work/conferencing-audio-pipeline-py && git add README.md CHANGELOG.md && git commit -m "$(printf 'docs(coverage): auto-generated seat zones (sub-feature #2)\n\nCo-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>')"
```

---

## Self-Review (writing-plans)

**Spec coverage:** ✓ seat derivation from seating furniture, explicit-anchors-win, no bare-table guessing (Task 1) · ✓ separability clustering using #1's calibrated beamwidth + 8-cap force-merge (Task 2) · ✓ owned-by-nearest-array, replace-all, dynamic RectShape zones, no-pose fallback, empty-seats guard, result with created/merged/warnings (Task 3) · ✓ coherence-with-`coverage_caveats` test (Task 3) · ✓ one-click DESIGN button, one undo, summary dialog (Task 4) · ✓ no schema change, numpy-free, no-channel/gain (all) · ✓ docs + green-gate (Task 5).

**Deviation from spec (intentional, more precise):** the spec said "no pose (position/bearing)" gates clustering; in fact only `array.position` is needed — the seat azimuth from `steering_angles` is room-frame and the *separation* between seats is bearing-independent (exactly as `coverage_caveats` does it). The fallback therefore triggers on missing `position` only. Documented in Task 3.

**Placeholder scan:** code is concrete in every code step. Task 3/4 "read the file and match the idiom" applies only to the `__init__.py` export grouping and the Qt layout-var/toast mechanism, which must be confirmed against the live files — a real instruction, not a deferral.

**Type consistency:** `derived_room_seats(config) -> list[tuple[str, SeatAnchor]]`, `SeatLook(seat_id, azimuth_deg, half_deg)`, `cluster_seats(looks, *, max_zones, factor) -> tuple[list[list[str]], bool]`, `generate_seat_zones(config, array_id) -> SeatZoneResult(config, created, merged, warnings)` are used consistently across Tasks 1→4. Reused signatures (`steered_beamwidth_deg`, `separable`, `steering_angles`, `device_capabilities`, `dynamic_zone`, `add_coverage_zone`, `remove_coverage_zone`, `_device_elev`, `SEATED_HEAD_M`, `DEFAULT_PICKUP_BEAM_HALF_DEG`) match the live code read during planning.
