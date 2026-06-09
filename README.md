# Conferencing Audio Pipeline — Python

A Python port of the conferencing-audio configuration **control plane**, plus a
**PySide6 desktop app** with a 2D/3D layout editor.

It models a networked-conferencing audio system — mic coverage zones → matrix
mixer → AEC references + automixer → outputs — and validates correctness (above
all the **AEC self-reference rule**). It does **not** process, mix, cancel, or
stream real audio; AEC/automix/NLP are configuration + validation logic, and the
device models are generic (Dante is a transport *label* only). Coverage geometry
and steering angles are planning abstractions; the optional **placement simulator**
(see below) adds a heuristic acoustic *model* for recommending array/seat
placement, with an opt-in physics-validation backend — still no real-time DSP.

The engine is a faithful port of the TypeScript version and writes the **same
JSON schema** (`version` 1, camelCase keys), so configs interoperate between the
two.

## Layout

```
conf_pipeline/        the engine (pure dataclasses + functions, no Qt)
  model.py            types, geometry, point-in-zone, JSON (de)serialization
  matrix.py           crosspoint mixer (immutable ops)
  coverage.py         zones (dynamic/dedicated/exclusion) + mode-driven ports
  dsp.py              AEC reference resolution, automixer, mute
  angles.py           steering_angles (azimuth / down-tilt / off-nadir / distance)
  validation.py       validate() + code catalog
  devices.py          generic device factories
  persistence.py      serialize / deserialize (TS-compatible)
  api.py              public builder API, auto_configure, talkers, angles, coverage
  sim/                placement simulation (scoring, search, pluggable validation)
conf_pipeline_gui/    the PySide6 app
  state.py            AppState (undo/redo, selection, tool, camera)
  canvas.py           2D + 3D editor (QPainter, orbit camera, hit-testing)
  inspector.py        Build / AEC-DSP / Routing / Issues / Simulate / JSON tabs
  scenarios.py        sample configs (boardroom, huddle, meeting, conference, training, lecture, U-shape)
  app.py              main window + toolbar
tests/                pytest suite (109 tests)
run_gui.py            launcher
```

## Setup

```bash
python -m venv .venv
.venv\Scripts\python -m pip install -e ".[dev]"   # installs PySide6 + pytest
```

## Run the desktop app

```bash
.venv\Scripts\python run_gui.py
```

Toolbar: tools **Select / Connect / Room / Zone / Talker**, a **2D / 3D** toggle,
undo/redo, auto-configure, a rectangular-room shortcut, sample scenarios, and
JSON export/import. The **Load sample…** picker has seven rooms — boardroom,
huddle, meeting, conference (3 arrays), training/classroom, lecture hall, and a
U-shape boardroom (polygon table). Load **Boardroom** and select the **Presenter**
talker to see steering-angle rays from each ceiling array (azimuth / down-tilt /
off-nadir / distance) and the per-talker capture status (recorded / excluded /
not covered).

### Editor

- **Select** — drag devices, talkers, room vertices, and zones; resize zones by
  the corner handle.
- **Connect** — click device → device to wire a route (auto-picks compatible
  free ports by transport).
- **Room** — click to draw the outline (double-click to close), or *Rect room*.
- **Zone** — drag a Records (dynamic) / No-pickup (exclusion) zone, or click a
  dedicated one; pick the kind in the Build tab.
- **Talker** — click to drop a person.
- **3D** — drag to orbit, wheel to zoom, click to select, drag on the floor plane
  to move; devices/talkers sit at their real heights with drop-poles.

Keyboard: `V/C/R/Z/T` tools, `2`/`3` view, `Ctrl+Z`/`Ctrl+Y` undo/redo, `Del` delete.

## Use the engine directly

```python
import conf_pipeline as cp

c = cp.create_config("Boardroom", "2026-06-08T00:00:00Z")
c = cp.add_device(c, cp.create_processor("P", "DSP"))
c = cp.add_device(c, cp.create_wireless_mic("PM", "Presenter", "dante"))
c = cp.add_device(c, cp.create_loudspeaker("L", "Speaker", "analog"))
c = cp.route(c, "PM-out-dante-1", "P-in-dante-1")
c = cp.route(c, "P-out-analog-1", "L-in-analog-1")
c = cp.matrix_for(c, "P").route("P-in-dante-1", "P-out-analog-1")   # reinforce
c = cp.set_aec(c, "PM", cp.AecConfig(True, "P-out-analog-1"))       # the trap

res = cp.validate(c)        # -> AEC_REINFORCED_SHARED_REFERENCE error
print([e.code for e in res.errors])
print(cp.serialize(c, pretty=True))
```

## Test

```bash
.venv\Scripts\python -m pytest -q
```

The suite covers: the AEC self-reference rule (plain + reinforced-shared + the
dedicated far-end-only fix), coverage limits + exclusion zones, mode-switch port
regeneration + orphaned-route detection, matrix ops, automixer ranges, the worked
boardroom integration scenario, steering-angle math, talker coverage, lossless
JSON round-trips, and the placement-simulation engine (scoring, joint search,
fairness, optional physics validation).

## Device profiles & DSP blocks (1.7.0)

Each device has a vendor-neutral **capability profile** (`profile_id`) from
`DEVICE_PROFILES`; capabilities (AEC, automix, mute, supported DSP blocks,
coverage limits) are derived via `device_capabilities`. Devices also carry a
**DSP block chain** (`dsp_blocks`) — `gain`, `mute`, `peq4`, `agc`, `compressor`,
`delay`, `noiseReduction`, `deverb` — with range-checked params (settings only,
no audio). API: `assign_device_profile`, `add_dsp_block`, `update_dsp_block`,
`remove_dsp_block`, `set_dsp_block_enabled`, `create_dsp_block`. Validation adds
profile/block error codes and commissioning warnings. The PySide6 inspector gains
a profile selector and a **Processing blocks** editor.

**Schema version 2** (interoperable with the TS version); v1 JSON migrates
automatically. The AEC self-reference rule is unchanged.

## Placement simulation & recommendation (1.9.0)

A Python-only extension that turns the planning geometry into an **optimiser**:
given a room, an array, and talkers, it recommends the best array position +
steer and the best **seat** for a talker. Pure-stdlib engine in `conf_pipeline/sim/`:

```python
import conf_pipeline as cp
from conf_pipeline.model import Point2D

c = cp.create_config("Boardroom", "2026-06-09T00:00:00Z")
c = cp.set_room(c, cp.rectangular_room(8, 6, 3))
c = cp.add_device(c, cp.create_microphone_array("A", "Ceiling Array"))
c = cp.add_talker(c, cp.create_talker("T1", "Presenter", Point2D(5, 3)))

rec = cp.recommend_placement(c, "A", talker_id="T1")   # joint array + seat
print(rec.array_pos, rec.array_elev, rec.steer_off_nadir_deg, rec.talker_pos)
print(rec.score.total, rec.score.snr, rec.score.drr, rec.score.coverage, rec.score.fairness)

hm = cp.score_heatmap(c, "A")          # grid of "where to mount the array" scores
print(cp.estimated_rt60(c))            # Sabine RT60 from room volume
```

It blends four objectives — **direct-path SNR**, **direct-to-reverberant ratio**,
**coverage/on-axis**, and **multi-talker fairness** — weighted via `SimParams`. The
search derives the optimal steer analytically and runs coarse-to-fine, so it is
interactive (no numpy required).

When the room has **multiple arrays**, each talker is scored by whichever array
covers it best (`SimParams.consider_all_arrays`). When a **table** is defined — a
table is modelled as a pickup/coverage zone — recommended **seats are placed at the
table** rather than on open floor (`SimParams.seat_in_pickup_zones`). Both are
toggle-able in the Simulate tab.

**Optional physics validation** of the single top pick (`validate_recommendation`)
adds a pluggable backend: install `[sim]` for a numpy far-field delay-and-sum SNR,
or `[sim-rir]` for a pyroomacoustics image-source RIR (physical DRR + beam SNR):

```bash
.venv\Scripts\python -m pip install -e ".[sim]"      # or ".[sim-rir]"
```

In the desktop app, the **Simulate** tab drives all of this: choose the target
talker, tune the weights, toggle the **score heatmap** overlay, press **Recommend**
(★ markers + steer ray appear on the 2D/3D canvas), **Apply to layout** (one undo
step), and **Validate top pick** (runs off the GUI thread, backends auto-detected).

## Designer-inspired workflow (1.8.0)

Vendor-neutral, config/validation-only features (no audio/Dante/discovery/
firmware/network I/O): **Projects** (multi-room `create_project` / `add_room` /
`serialize_project` …), **Deployment** (`mark_deployed`, `deployment_diff`),
**Naming** (`apply_naming_scheme` + duplicate/empty-label warnings), **Routing
views** (`routing_summary`, `dante_subscriptions`, `signal_flow_report`), and
**Device templates** (`device_template` / `instantiate_template`). The PySide6 app
gains a room selector, **+/− Room**, **Auto-name**, **Deploy**, and a **Routing**
tab. JSON stays interoperable with the TypeScript version.

## Scope

This is a **configuration and signal-routing control plane**, not a DSP engine.
See the TypeScript project's README for the full AEC-rule explanation and the
validation-code catalog — the codes and rules are identical here.

## License

MIT.
