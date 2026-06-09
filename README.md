# Conferencing Audio Pipeline — Python

A Python port of the conferencing-audio configuration **control plane**, plus a
**PySide6 desktop app** with a 2D/3D layout editor.

It models a networked-conferencing audio system — mic coverage zones → matrix
mixer → AEC references + automixer → outputs — and validates correctness (above
all the **AEC self-reference rule**). It does **not** process, mix, cancel, or
stream real audio; AEC/automix/NLP are configuration + validation logic, and the
device models are generic (Dante is a transport *label* only). Coverage geometry
and steering angles are planning abstractions, not acoustic simulations.

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
conf_pipeline_gui/    the PySide6 app
  state.py            AppState (undo/redo, selection, tool, camera)
  canvas.py           2D + 3D editor (QPainter, orbit camera, hit-testing)
  inspector.py        Build / AEC-DSP / Issues / JSON tabs
  scenarios.py        sample configs (boardroom, huddle)
  app.py              main window + toolbar
tests/                pytest suite (53 tests)
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
JSON export/import. Load **Boardroom** and select the **Presenter** talker to see
steering-angle rays from each ceiling array (azimuth / down-tilt / off-nadir /
distance) and the per-talker capture status (recorded / excluded / not covered).

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

53 tests cover: the AEC self-reference rule (plain + reinforced-shared + the
dedicated far-end-only fix), coverage limits + exclusion zones, mode-switch port
regeneration + orphaned-route detection, matrix ops, automixer ranges, the worked
boardroom integration scenario, steering-angle math, talker coverage, and
lossless JSON round-trips.

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

## Scope

This is a **configuration and signal-routing control plane**, not a DSP engine.
See the TypeScript project's README for the full AEC-rule explanation and the
validation-code catalog — the codes and rules are identical here.

## License

MIT.
