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
JSON schema** (camelCase keys; currently `version` 5 — older v1–v4 files load
and migrate losslessly), at matching v5 parity with the TS sibling. The PySide6
desktop app is presented as **Aniston Room Designer**.

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
  transport.py        DeviceTransport seam + SimulatedTransport, online status, push + reconcile
  files.py            project file manager: recent files, autosave, crash recovery, migration notice
  control_api.py      local HTTP control API (scene recall / mute / status; stdlib http.server)
  scheduler.py        scene scheduler (weekly "HH:MM" recalls; injectable clock)
  furniture.py        furniture catalog (kind → size / seat-capacity / absorption) + resolvers
  coverage_sim.py     geometric room coverage: mic sectors + camera FOV + occlusion → RoomCoverage
  seat_mapper.py      DOA azimuth → nearest room seat (room-aware; reuses array bearingDeg)
  sim/                placement simulation (scoring, search, pluggable validation)
conf_pipeline_gui/    the PySide6 app — "Stagebar" workflow-modes shell
  state.py            AppState (undo/redo, selection, tool, mode, camera, live overlay)
  workflow.py         stage predicates + status powering the ModeBar dots & hint chips
  canvas.py           2D + 3D editor (QPainter; mode-aware overlays incl. the LIVE operations view)
  theme.py            "Conduit" palettes → QSS (dark + light, canvas roles included)
  icons.py            programmatic line-icon factory (no assets, theme-tinted)
  modebar.py          DESIGN→SIMULATE→ROUTE→DEPLOY→LIVE switcher with status dots
  toolrail.py         per-mode canvas tools (zone-kind flyout on the Zone tool)
  viewbar.py          floating 2D/3D + overlays popover on the canvas
  simbar.py           floating coverage-sim toggles (pickup / FOV / dispersion / occlusion)
  issues.py           global validation drawer (slides over the panel in any mode)
  panels/             one purpose-built right panel per mode
                      (design · simulate · route · deploy · live + shared common)
  scenarios.py        sample configs (boardroom, huddle, meeting, conference, training, lecture, U-shape)
  app.py              main window: top bar (☰ menu, rooms, ModeBar, validation pill)
conf_pipeline_control/ host-side array-microphone control (optional [control] extra)
  geometry.py         physical capsule layout (ArrayGeometry, sensibel_8)
  steering.py         coverage zones → beamformer look/null directions
  beamformer.py       delay-and-sum + LCMV null-steering + beam pattern (pure stdlib)
  doa.py              SRP-PHAT multi-azimuth detection + sector gate (numpy)
  autosteer.py        detect talkers → gate to sector → steer/extract live
  control.py          MicController interface + SimulatedMicController
  audio.py / live.py  real-time capture + beamforming (numpy + sounddevice)
  octovox_bridge.py   zones → azimuths + HTTP client to the OCTOVOX clean server
  octovox_monitor.py  near-live cleaned monitor (rolling chunk → clean → playback)
  ab_test.py          A/B harness: record → beamform N ways → WAVs + dB report
  polaris_beamformer.py  real-time SRP-PHAT DOA + steered beam (4 modes, auto-null, AGC, post-NR) (POLARIS 8-mic)
  virtual_mic_grid.py    Nureva-style fixed near-field virtual-mic grid, loudest selected
  beam_engine.py      A/B engine: steered + grid back-ends on one shared input stream
  tracking.py         swappable smoothers (EMA + constant-velocity/Kalman-family hook)
tests/                pytest suite (605 tests; incl. headless GUI smoke)
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

The app is organised around the workflow itself: a centered **ModeBar** walks
left-to-right through **DESIGN → SIMULATE → ROUTE → DEPLOY → LIVE** (`Ctrl+1…5`),
each mode showing only its canvas tools (left rail), its overlays, and a
purpose-built right panel. Status dots on the mode buttons track progress
(● done · ◔ in progress · ○ untouched; the LIVE dot pulses red while a session
is connected), each panel header carries a next-step hint chip, and the
**validation pill** in the top bar opens the global **Issues drawer** from any
mode. The **☰ menu** holds samples, import/export/report, room actions, and the
theme toggle; the room switcher next to it manages multi-room projects. In LIVE
mode the canvas becomes an **operations view** — the steering-sector wedge,
real-time DOA rays (green in-sector / red nulled), and a breathing level halo
are drawn on the same floor plan you designed, while the Live panel's transport
footer (meter / Connect / Mute / gain) never scrolls out of reach. On the
canvas, **right-click** (in DESIGN) for context menus, and the cursor changes to
show what's grabbable. The **Load sample** menu has seven rooms — boardroom,
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
  dedicated one; pick the kind on the Zone button's flyout in the tool rail.
- **Talker** — click to drop a person.
- **3D** — drag to orbit, wheel to zoom, click to select, drag on the floor plane
  to move; devices/talkers sit at their real heights with drop-poles.

Geometry editing lives in DESIGN; SIMULATE still lets you drag talkers for
what-if exploration, ROUTE adds the Connect tool, and DEPLOY badges devices
added (+) or changed (~) since the last deploy.

Keyboard: `V/C/R/Z/T` tools (hop to their home mode from anywhere), `2`/`3`
view, `Ctrl+1…5` modes, `Ctrl+Z`/`Ctrl+Y` undo/redo, `Del` delete.

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

## More Designer-6 parity (1.12.0)

Four further offline, vendor-neutral capabilities that close the remaining gaps
against Designer's coverage/commissioning workflow. All additive — the JSON config
schema stays v2 and a config that uses none of them round-trips byte-for-byte to
the same JSON as before (so existing files and the TS version are unaffected).

- **Per-coverage-area output channels + gain** — a pickup area can carry its own
  numbered output channel (1..8) feeding a dedicated Dante out, the way an MXA920's
  **steerable coverage** gives each of its 8 areas an individual output, plus a
  per-area gain trim:

  ```python
  c = cp.set_zone_output_channel(c, "A", "z1", 1)   # area z1 → array out channel 1
  c = cp.set_zone_gain_db(c, "A", "z1", -3.0)        # per-area trim
  c = cp.auto_assign_zone_channels(c, "A")           # number every pickup area (idempotent)
  ```

  The array grows an `A-out-ch-1` port per channelled area. Validation flags an
  out-of-range channel / one on an exclusion zone (`COVERAGE_CHANNEL_INVALID`),
  two areas sharing a channel (`COVERAGE_CHANNEL_DUPLICATE`), and a bad gain
  (`COVERAGE_GAIN_INVALID`). In the GUI: selecting a pickup zone shows an **Output
  channel** picker and an **Area gain** trim.
- **Zone-vs-coverage report** — `cp.zone_coverage_report(c)` answers, for each drawn
  coverage *area*, "is it inside its array's pickup circle (centroid + every corner),
  and is any area covered by 2+ arrays (automix **lobe contention**)?" — closer to
  Designer than the array-circle overlap check. Views: `.uncovered`, `.partial`,
  `.contended`. The Issues tab shows the area-in-pickup and contention counts.
- **Optimize room** — `cp.optimize_room(c) → OptimizeRoomResult` is the one-click
  "do everything": recommend + apply each array's best placement/steer, give every
  pickup area its own output channel, then `auto_route` — with a change summary.
  Each stage is opt-out (`place_arrays` / `assign_channels` / `route`) and
  idempotent. Toolbar **Optimize room** button (one undo step + summary).
- **Logic / mute control** — `ControlConfig` + `MuteGroup` model Designer's
  mute-control / logic blocks: a named set of devices and/or coverage-area output
  channels that mute together, with a `software` / `logicIn` / `button` trigger.

  ```python
  g = cp.create_mute_group("mg1", "Room mute", device_ids=["A"],
                           zone_refs=[cp.ZoneChannelRef("A", "z1")], trigger="button")
  c = cp.add_mute_group(c, g)
  c = cp.set_mute_group_muted(c, "mg1", True)
  ```

  Validation flags an empty group or a dangling device/array/zone reference
  (`CONTROL_MUTE_GROUP_INVALID`). The design report gains **Coverage areas** and
  **Mute groups** sections, and the GUI's **Routing** tab has a **Mute groups**
  editor (create over the mute-capable mics, toggle mute, remove).

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
design = cc.design_zone_beams(c, "A", geom)
print(design.summary())                  # pickup gain, white-noise gain, excluded-area leak per zone
for m in design.beams[0].band_metrics:   # …and the same numbers per octave band
    print(f"{m.freq_hz:6.0f} Hz: pickup {m.pickup_gain_db:+.1f} dB, "
          f"DI {m.di_db:+.1f} dB, excluded leak {max(m.exclusion_atten_db):+.0f} dB")
```

`design_zone_beams` steers a beam toward each **Records / dedicated** zone and
places spatial **nulls** toward each **No-pickup (exclusion)** zone (pure stdlib —
`cmath`, no numpy). It returns verification numbers and you can evaluate the
directivity directly (`cc.beam_pattern_azimuth`, `cc.response_db`,
`cc.directivity_index_db`) to *prove* a pickup area is on-axis and an excluded
area is attenuated.

**Wideband by default** — speech spans ~250 Hz–8 kHz, so the design is re-derived
and verified at each **octave-band center** (250…8000 Hz), not just at one
frequency: each beam carries per-band pickup / DI / WNG / excluded-leak numbers
(`ZoneBeam.band_metrics`), and `freq_hz` is just the *reference* band for the
headline scalars. This matches what the live runtime actually does (it applies
the design **per FFT bin**); pass `bands=()` to skip the per-band verification
in a hot loop, or your own list of centers for a finer grid.

**Measured, not asserted** — `cc.frequency_curves(design)` returns DI, −3 dB
beamwidth, WNG, and lobe/grating counts **as a function of frequency**
(third-octave grid by default) for each beam, with a `table()` text rendering;
the Live panel's design readout shows it under the azimuth sparkline. On the
8-capsule / 10 cm aperture this is the fidelity note as numbers: the beam
narrows from ~130° at 250 Hz to ~20–30° at 8 kHz while grating lobes start
appearing in the top octave.

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

## Auto-steer — follow talkers by direction (1.13.0)

Beam *design from zones* needs you to know where people sit. **Auto-steer** instead
finds them: it scans azimuth in real time (SRP-PHAT direction-of-arrival), keeps the
talkers inside a coverage **sector** (a centre bearing ± half-width — the "area"
expressed as an angle, since a planar array measures bearing, not range), and steers
a beam at each one while nulling the talkers outside. It adapts as people talk in
turn or move — good for a desk/table array.

```python
import conf_pipeline_control as cc
geom = cc.with_active_channels(cc.sensibel_8(radius_m=0.035), [i != 5 for i in range(8)])
sector = cc.SectorConfig(center_deg=0, half_width_deg=60, front_offset_deg=37)
ctrl = cc.AutoSteerController(geom, sector, device=7, samplerate=44100, monitor=True)
ctrl.start()                       # detect → gate → steer, live
print(ctrl.detections())           # [(azimuth, in_sector, salience), …]
ctrl.stop()
```

In the desktop app, the Live tab's **Auto-steer (follow talkers in a sector)** group
exposes the sector centre/width, a **Front offset** (rotates azimuth-0 to your desk's
"front"), max talkers, and a "mute when empty" gate, with a live readout of the
detected bearings. The sector controls update a running session **without
reconnecting**. **Calibrate front** records a 'front' talker and sets the offset for
you. The pure-DOA / control pieces are `conf_pipeline_control.doa` (`detect`,
`sector_gate`, `detect_offline`) and `conf_pipeline_control.autosteer`; the live
covariance the scan needs comes from `LiveBeamController(track_covariance=True)`
(opt-in, zero overhead when off).

CLI helpers: `scripts/device_check.py` (confirm 8 channels @ 44100),
`scripts/calibrate_front.py` (measure the front bearing), and
`scripts/area_autosteer.py` (live detect + extract with a radar readout).

> **Same physics caveat.** Azimuth is reliable; **range is not**, so the area is an
> angular arc, not a metric radius. Resolution ≈ beamwidth — two talkers closer than
> ~40–50° on a small array merge into one detection.

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

## Scenes & external control (1.15.0)

**Scenes** (schema v3) are named, recallable snapshots of the control surface —
mute-group states and per-area gain trims, plus config-inert live-layer hints
(**active** areas and per-array **steer** bearings) that tell the beamformer
what to do on recall. Capture/recall/remove from the Route panel or the API
(`cp.capture_scene`, `cp.recall_scene`). Older v1/v2 files migrate losslessly.

**External control API** — a pure-stdlib local HTTP server for room-control
integrations (no extra deps):

```python
holder = cp.ConfigHolder(config)
with cp.ControlApiServer(holder.get, holder.apply, port=8765) as srv:
    ...  # GET /api/status · GET /api/scenes
         # POST /api/scenes/<id>/recall · POST /api/mute-groups/<id> {"muted": true}
```

Recall responses include the scene's `steer` / `activeZones` hints so the
caller can aim the live beamformer.

**Scheduling** — schedules live in the config (additive on v3): recall a scene
at a local time on chosen weekdays, every week. `cp.SceneScheduler` executes
them through the same `get/apply` pair as the API (injectable clock,
`run_pending()` manual tick, or a daemon polling thread):

```python
c = cp.add_scene_schedule(c, cp.create_scene_schedule("morning", "meeting", "08:30", ["mon", "tue", "wed", "thu", "fri"]))
holder = cp.ConfigHolder(c)
with cp.SceneScheduler(holder.get, holder.apply) as sched:
    ...  # fires at 08:30 on weekdays; sched.next_fire() tells you when
```

## Cameras, furniture & coverage simulation (1.16.0)

Schema **v4** adds three things the room model was missing: **conferencing cameras**
(a placed device with `bearingDeg` / `tiltDeg` aim and an FOV/range spec),
**loudspeaker aim**, and **furniture** (`RoomObject` with size / rotation / seat
anchors, resolved against a small catalog). On top of them, a **geometric coverage
simulator** answers "who is actually covered?":

```python
import conf_pipeline as cp

cov = cp.simulate_room_coverage(config)              # RoomCoverage (mics / cameras / speakers)
print(cov.summary["mic_coverage_pct"], cov.summary["mic_gaps"])   # who's picked up, and the gaps
                                                     # height-aware furniture occlusion applied
```

Coverage is computed as view-independent wedges (mic pickup, camera FOV, speaker
dispersion) so the desktop app renders the same contract in **both 2D and 3D**. The
app gains a floating **SimBar** to toggle the overlays and a **Furniture tool** to
place / move / resize / rotate items. Lossless from v1/v2/v3; a config that uses none
of the v4 fields round-trips byte-for-byte.

**Schema v5** additionally gives `MicrophoneArray` an optional `bearingDeg` (its mounting
heading, 0° = +Y) — the prerequisite for mapping a detected array-relative azimuth into room
coordinates (room-aware steering). Additive and omit-when-absent, so v1–v4 configs still
migrate byte-identically; set it with `cp.set_array_bearing(config, array_id, deg)`.

## Real-time array beamforming (1.17.0)

The `[control]` extra gains a real-time beamforming suite for the physical **sensiBel
POLARIS 8-mic** array (numpy + sounddevice), separate from the offline design layer
above:

- **Steered** — `cc.PolarisBeamformer` runs **SRP-PHAT** direction-finding and steers a
  beam at the dominant talker (active-speaker isolation), with talker-hold smoothing and
  opt-in wait-for-device / auto-reconnect. Four selectable strategies — `delaysum`,
  sub-sample `fracdelay`, frequency-domain `superdirective`, and data-adaptive `mvdr`
  (each behind a `plan_look`/`commit_look` contract so the heavy per-bin solve stays off
  the audio callback). The frequency-domain modes place **exact LCMV nulls**:
  `auto_null=True` follows the talker **and nulls the other detected interferers**, and
  `set_nulls(...)` adds caller-supplied bearings — both within the `M−1` null budget.
- **Selection** — `cc.VirtualMicGrid` is a Nureva-"Microphone-Mist"-style grid of
  fixed near-field virtual mics, all run per block, the loudest selected (no steering).
  It **holds the last seat through silence** (a peak/median VAD) instead of chasing
  noise.
- **A/B** — `cc.BeamEngine` runs both behind **one shared input stream** with runtime
  `set_mode("steered"|"grid")`, a normalized `Location` report, and an equal-power
  crossfade — so you can compare strategies live on one board.

```python
import conf_pipeline_control as cc

eng = cc.BeamEngine(device=7)         # one POLARIS USB device, 8 ch @ 44100 Hz
eng.start()
eng.set_mode("grid")                  # glitch-free crossfade to grid selection
print(eng.current_location)           # Location(mode, angle_deg, xy, confidence)
```

The beam output is band-limited at the array's ~5.6 kHz spatial-aliasing cutoff by
default (`beam_bandlimit_hz`), selection/steering smoothing is swappable behind a
`Tracker` interface (`tracking.py`, with a constant-velocity Kalman-family hook), and
`polaris-beam-demo` / `polaris-vmic-demo` / `polaris-beam-engine-demo` are console
entry points. The desktop app's **LIVE** mode also drives the engine directly — a
steered ↔ grid picker that switches live, a **headphone monitor** of the output, and the
tracked direction drawn on the room map. You can **lock the look** to a room seat, a
**manual angle dial**, or by **clicking the spot on the map** (a solid arrow shows where
the beam is aimed, distinct from the dashed talker DOA).

**Room-aware** (built on the v5 array `bearingDeg`): `conf_pipeline.seat_mapper` turns a
detected azimuth into the **nearest room seat**, surfaced live as a `· seat <id>` readout
and a room-map highlight; the LIVE A/B card's **"Null the other (empty) seats"** feeds the
non-target seat bearings to the steered beam, arbitrated against auto-null by a single
**null-budget composer** (measured interferers win the budget; speculative seat nulls fill
the remainder). An opt-in **target-loudness AGC** (`agc_target_db`) normalizes the mono
output level — EMA-slewed, clamped to ±18 dB, and held through silence.

**Noise suppression for fans / AC** (LIVE A/B card): **"Suppress steady noise (fans/AC)"**
(`post_nr`) runs a gentle single-channel Wiener gate on the beam output that learns the
steady background by **minimum statistics** (the per-bin running minimum — no silence or
VAD needed, so it removes always-on fan/AC/HVAC hum the old gate couldn't), with a
**Gentle / Medium / Aggressive** depth; speech is preserved (it sits above the floor) and
the gate never hard-mutes. A **"Cleaner"** picker chooses the engine: **OCTOVOX cleaner
(OM-LSA)** — OCTOVOX's decision-directed Ephraim–Malah/Cohen denoiser ported to run live on
the mono output (stronger and more natural on non-stationary noise; pure numpy, ~12 ms) —
or the **light gate** (the single-pole spectral gate), or **DeepFilterNet3** — the neural
denoiser, now running **live** on the audio thread via a self-contained streaming ONNX
(no torch at runtime; the `[dfn]` extra). The same cleaners are available on the
**auto-steer** path (its **Clean voice** + **Strength** controls), not just the A/B engine.
**"Adaptive null (learn room noise)"** switches the steered beam to data-adaptive `mvdr` +
`auto_null` to spatially **null a directional fan/duct**. Every cleaner is **level-preserving**
(a shared speech-gated makeup restores the ~5-7 dB any denoiser strips, so the cleaned voice
never sounds weak), and the **Strength** combo doubles as a **cleaning-amount** dial that blends
the original voice back in (less muffled) — Light / Medium / Full. All opt-in and fixed at
Connect; A/B them by ear on the monitor.

**Caveat:** the ~cm aperture means coarse-zone selection, not
MXA920/Nureva-scale pinpoint — these isolate a zone or A/B two strategies, they don't
separate two people at one table.

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
