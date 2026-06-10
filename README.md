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
  api.py              public builder API, auto_configure, auto_route, talkers, angles, coverage, floor-plan
  coverage_check.py   array coverage circles + covered/uncovered/overlap report
  report.py           shareable design report (Markdown / HTML)
  sim/                placement simulation (scoring, search, pluggable validation)
conf_pipeline_gui/    the PySide6 app
  state.py            AppState (undo/redo, selection, tool, camera)
  canvas.py           2D + 3D editor (QPainter, orbit camera, floor-plan image, coverage circles)
  inspector.py        Build / AEC-DSP / Routing / Issues / Simulate / Live / JSON tabs
  scenarios.py        sample configs (boardroom, huddle, meeting, conference, training, lecture, U-shape)
  app.py              main window + toolbar (Auto-Route, Show coverage, Floor plan, Export report)
conf_pipeline_control/ host-side array-microphone control (optional [control] extra)
  geometry.py         physical capsule layout (ArrayGeometry, sensibel_8)
  steering.py         coverage zones → beamformer look/null directions
  beamformer.py       delay-and-sum + LCMV null-steering + beam pattern (pure stdlib)
  control.py          MicController interface + SimulatedMicController
  audio.py / live.py  real-time capture + beamforming (numpy + sounddevice)
  octovox_bridge.py   zones → azimuths + HTTP client to the OCTOVOX clean server
  octovox_monitor.py  near-live cleaned monitor (rolling chunk → clean → playback)
  ab_test.py          A/B harness: record → beamform N ways → WAVs + dB report
tests/                pytest suite (195 tests)
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

## Designer-style features (1.10.0)

Four capabilities modelled on **Shure Designer 6**, all offline and vendor-neutral:

- **Coverage areas** — each array's floor coverage circle (mount height × profile
  cone angle) drawn on the 2D canvas via **Show coverage**; `cp.coverage_report(c)`
  returns covered / uncovered / overlapping arrays (also shown in the Issues tab).
- **Auto-Route** — one-click optimize (`cp.auto_route`): AEC references + automixer
  + near-end send (from `auto_configure`), then far-end → loudspeaker feeds and a
  synced mic mute-link, with a change summary. Idempotent and never breaks the AEC
  self-reference rule. Toolbar **Auto-Route** button shows what changed.
- **Floor-plan import** — load a floor-plan image under the room (**Floor plan…**),
  then **Calibrate…** by dragging a line over a known distance and entering its
  length (`cp.set_room_background`, `cp.calibrated_scale`). Stored by path.
- **Design report** — `cp.design_report(config, "markdown"|"html")` produces a
  shareable doc (room + RT60, devices, routing, AEC, coverage, validation);
  **Export report** writes `.md`/`.html`.

## Live array-microphone control (1.11.0)

Drive an **actual array microphone** with **coverage-area selection**, the way a
Shure MXA920 lets you pick which areas to capture and which to mute — but for an
array that exposes only raw multi-channel audio (e.g. a **sensiBel 8-capsule**
array over USB). Such arrays have no on-device zone protocol; the steering is done
**on the host**, in the new optional `conf_pipeline_control` package. The
pickup/exclusion zones you already draw in the app become the beamformer:

```python
import conf_pipeline as cp
import conf_pipeline_control as cc

c = ...                                  # a config with an array + pickup/exclusion zones
geom = cc.sensibel_8(radius_m=0.05)      # set radius to your array's actual value
design = cc.design_zone_beams(c, "A", geom, freq_hz=1000)
print(design.summary())                  # pickup gain, white-noise gain, excluded-area leak per zone
```

`design_zone_beams` steers a beam toward each **Records / dedicated** zone and
places spatial **nulls** toward each **No-pickup (exclusion)** zone (pure stdlib —
`cmath`, no numpy). It returns verification numbers and you can evaluate the
directivity directly (`cc.beam_pattern_azimuth`, `cc.response_db`,
`cc.directivity_index_db`) to *prove* a pickup area is on-axis and an excluded
area is attenuated.

**Superdirective by default** — a small array is barely directional with plain
delay-and-sum at speech frequencies, so it picks up diffuse background almost as
loudly as the talker. The default mode is **superdirective** (diffuse-noise MVDR):
it minimises pickup of isotropic background while holding unity gain on the talker,
buying **~5 dB more voice-vs-background** in the low/mid speech band on an 8-capsule
array. A **diagonal-loading** knob trades directivity for robustness to self-noise
and capsule mismatch (raise it if the beam hisses). `mode="delaysum"` selects the
robust delay-and-sum instead. In the GUI: the **Beamformer** group (Mode +
**Focus ↔ robust** slider).

**Lobes + leakage — where off-target voices get in.** A beam isn't a clean cone:
it has one **main lobe** (toward the talker) plus **side lobes** (smaller pickup
peaks elsewhere) and, on a sparse array at high frequency, **grating lobes**
(near-full pickup in another direction). `cc.analyze_lobes(...)` counts and locates
them (main width, side-lobe levels, grating-lobe warnings); `cc.talker_leakage_db`
reports how loudly each *placed talker* is currently captured (target ≈ 0 dB,
everyone else below). On the 8-capsule / 0.05 m array this is 2–3 lobes with
−12…−17 dB side-lobes in the 0.5–2 kHz speech band, growing to ~8 lobes at 4–8 kHz.

**Subtract out-of-area voices.** `design_zone_beams(..., suppress_outside_talkers=
True)` adds every placed talker that is **not** inside a pickup zone as a beam null
(on top of the exclusion-zone nulls), up to the array's null budget (`n_active−1`).
A talker outside the zone drops from a side-lobe level (e.g. −23 dB) to a deep null
(−120 dB). GUI: **Null talkers outside the pickup zone** in the Beamformer group;
the design readout lists each talker's pickup level and `[pickup]`/`[OUTSIDE]` tag.

**Hear and measure it — A/B harness.** `cc.ab_compare(config, array, geom, y8, sr)`
runs a recorded clip through **omni / delay-sum / superdirective / aggressive /
nulled** and returns mono signals + a dB report (DI, WNG, per-talker leakage);
`cc.save_ab_report` writes the WAVs + `report.txt`. GUI: **A/B test — record &
compare** records from the array and saves the set so you can *listen* to the
difference. An **Aggressive preset** pushes superdirectivity to the limit — safe
because the SBM100B capsules are **80 dBA SNR** (studio-grade), so the extra
self-noise from low diagonal loading stays inaudible where ordinary MEMS would hiss.

**Two layers, split by dependency:**

- **Design (always available, pure stdlib):** geometry, zone→direction, weights,
  and beam-pattern verification. Importing `conf_pipeline_control` pulls no
  numpy/sounddevice; the full 195-test suite runs without them.
- **Live (optional `[control]` extra):** capture the array's channels and apply
  the weights in real time — a frequency-domain (per-FFT-bin), Hann-windowed
  overlap-add beamformer — with a live output level, mute, gain, and optional WAV
  recording of the steered output:

  ```bash
  .venv\Scripts\python -m pip install -e ".[control]"   # numpy + sounddevice
  ```

In the desktop app, the **Live** inspector tab does all of this: choose the
target array and capsule radius, **Design beam from zones** (shows per-zone gains
and an azimuth-response sparkline), pick the input device, **Connect**, then watch
the level meter and toggle **Mute** / **Gain**. Without the extra it runs against
a built-in **simulated** controller so the workflow is fully usable offline.

**Hearing the output** — tick **Monitor output** and pick an output device to play
the beamformed signal live (full-duplex). Use **headphones**: monitoring a ceiling
array through room speakers feeds back into the array and howls. You can also save
the steered output to a WAV by passing `record_path=` to `LiveBeamController`.

**Active capsules** — if a capsule is dead or a channel in the stream carries no
audio, switch it off (per-capsule checkboxes, or **Detect silent capsules** to
auto-probe the live stream and uncheck the silent ones). The beamformer then
designs over the active capsules only and gives the rest zero weight, so one bad
channel doesn't corrupt the beam (`cc.with_active_channels(geom, mask)`).

> **Honest fidelity note.** An *N*-capsule array forms at most *N*−1 nulls; null
> depth and beamwidth are bounded by the array's aperture and degrade away from
> the design band. Excluded areas are **strongly attenuated**, not perfectly
> silenced, and a planar array separates areas mainly by **azimuth / horizontal
> offset** (two areas on the same bearing are hard to tell apart). This is the
> physics of the hardware, surfaced rather than hidden.

## OCTOVOX integration — clean the steered voice (1.11.0)

This app does the **spatial** front-end (room + drawn zones → which direction to
listen); [OCTOVOX](../New_OCTOVOX) does the **cleaning** (calibration, dereverb,
DeepFilterNet3, residual suppression, VAD automix, EQ/AGC). The bridge hands
OCTOVOX the **raw 8 channels + the zone-derived azimuths**, so OCTOVOX runs its
own direction-aware beamform-then-clean chain steered at the talker you picked and
nulling the areas you excluded — no redundant beamforming.

```python
import conf_pipeline_control as cc
za = cc.zone_azimuths(config, "A1")            # pickup zone → target_az, exclusions → interferer_az
client = cc.OctovoxClient("http://127.0.0.1:5050")
res = client.clean_8ch(y8, 44100, target_az=za.target_az, interferer_az=za.interferer_az)
# res.mono is the cleaned voice @ 48 kHz (input auto-resampled 44100 → 48000)
```

- **Azimuth mapping** — this app's compass bearing (0°=+Y, CW) → OCTOVOX's math
  azimuth (0°=+X, CCW): `cc.to_octovox_azimuth`. An **azimuth offset** calibrates
  the array's physical mounting rotation.
- **Transport** — pure HTTP (`/api/upload` → `/api/clean` → fetch the cleaned WAV);
  the two projects stay independent. Needs the `[octovox]` extra
  (`requests` + `scipy`) and a running OCTOVOX server (`python run.py`, port 5050).
- **Near-live cleaned monitor** (GUI: **Clean via OCTOVOX** in the Live tab) —
  captures rolling chunks of the raw array, cleans each through OCTOVOX, and plays
  the result back. **Delayed by ~chunk + processing (~4–5 s), not real-time
  talkback** (OCTOVOX's neural stages are whole-file/offline), with audible seams
  at chunk boundaries. Use headphones. `cc.CleanMonitor` drives it.

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
