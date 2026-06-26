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

## Capabilities at a glance — the live array (IntelliMix-class voice processing)

The optional `[control]` extra and the desktop app's **LIVE** mode turn a sensiBel POLARIS
8-capsule USB array into a host-side voice-processing front end — the same *family* of capabilities a
ceiling-array DSP (e.g. Shure IntelliMix on an MXA920) gives a room: automatic mixing, acoustic echo
cancellation, noise reduction, automatic gain, and a virtual acoustic boundary — but on one 8-mic ring,
with an **inspectable, measured** chain you can A/B in the customer's own room. Everything below is
shipped; the parenthetical is the LIVE-panel control.

| Capability (in the customer's terms) | What it does | LIVE control | Honest limit |
|---|---|---|---|
| **Automatic speaker selection** (the automixer) | picks the active talker and equal-power cross-fades to them across zones / two kits, so only the person speaking is open | Listening mode → *Follow the room* / *Two kits (combined room)* | switched selection, **not** 8 simultaneous lobes — one beam per array |
| **Echo cancellation (AEC)** | cancels the far-end loudspeaker echo (the Zoom/Teams downlink) from the beam, with a live ERLE read-out | *Echo cancel* | one post-beam AEC (per-kit *before* the selector on two kits — the faithful translation of IntelliMix's "per-channel" AEC) |
| **Noise reduction / voice cleaning** | removes steady fans/AC + broadband hiss; gate / **OM-LSA (AI)** / **DeepFilterNet3** engines, Gentle→Aggressive depth, level-preserving | *Cleaner* + *Suppress steady noise (fans/AC)* | the **A/B proof tool measures it in the room** (≈20 dB on steady noise in our testing — run it for the real number) |
| **Automatic gain / loudness** | normalizes the talker toward a target loudness (slewed, silence-held, clamped ±18 dB) | *Normalize output loudness (AGC)* | driven by output loudness, not per-talker distance |
| **Virtual Acoustic Boundary** (cut the door / outside) | nulls *directional* sound from outside the drawn pickup area + exclusion zones; the active nulls are drawn on the room map | *Cut the door & anyone outside the pickup area* | cuts directional outsiders; a same-bearing or in-place stander isn't separable (physics) |
| **Auto-follow** (Automatic Coverage) | SRP-PHAT detects the live talker and re-steers the beam to them each tick | Listening mode → *Follow the room (auto-steer)* | follows/selects the active talker (one beam), not 8 auto-placed lobes |
| **Live talker → seat read-out** | maps the live direction to the nearest room seat on the map | shown automatically in *Follow* / *Lock to a seat* | azimuth + seat, **not** XYZ camera-tracking coordinates |

Worked detail: [Live array-microphone control](#live-array-microphone-control-1110) ·
[Auto-steer](#auto-steer--follow-talkers-by-direction-1130) ·
[Real-time array beamforming](#real-time-array-beamforming-1170).

**IntelliMix ↔ this pipeline** — the "they have IntelliMix; what do *you* have?" answer. The trap is
to copy their **per-lobe** architecture; the faithful translation onto one 8-mic ring is **per-kit /
per-zone**:

| IntelliMix / MXA920 | Here | Status |
|---|---|---|
| Automatic mixing (8-lobe automix) | active-speaker selection + equal-power cross-fade (per zone / per kit) | ✓ — switched, not 8 lobes |
| Acoustic echo cancellation | `StreamingAec` (post-beam; per-kit *before* the selector on two kits) | ✓ |
| Noise reduction / Enhanced Noise Filtering | gate + min-statistics + **OM-LSA** + **DeepFilterNet3** | ✓ — and **measured / A/B-provable** |
| Automatic gain control | `TargetLoudnessAgc` | ✓ |
| Virtual Acoustic Boundary | exclusion-zone / out-of-pickup nulling (LCMV) + live map markers | ✓ |
| Automatic Coverage (auto-detect, no aiming) | auto-steer (SRP-PHAT → re-steer) | ✓ — one beam, not 8 |
| Steerable Coverage (manual lobes) | design pickup zones | ✓ — zones |
| Talker localization (XYZ for camera tracking) | azimuth + nearest room seat | partial — **no XYZ** (triangulation = roadmap) |
| Autofocus (re-aim as people move) | continuous DOA re-steer | ✓ |
| Data-estimated steering (real-room covariance) | `rtf_mvdr` — GEVD max-SNR over measured target/noise covariance; SRP-PHAT still gates frames + cross-checks; falls back to plane-wave MVDR; opt-in, A-B-engine path | ✓ — `PolarisBeamformer` / A-B engine (v1) |

### Why ours is different: the chain is inspectable, not a black box

A ceiling-array DSP is a sealed box — you take its NR/AEC numbers on faith. Ours **proves** them, live, in
the room — this is the differentiator, not the parity:

- **A/B proof tool** — records ~8 s of the beam *both ways at once* (raw vs cleaned), reports how much
  quieter the background got (dB) plus the AEC ERLE, and exports both clips. A number you can hand a
  customer, not a claim. (*Capture A/B proof* in LIVE.)
- **Per-stage activity meters** — Echo / Dereverb / Denoise / Auto gain each show what *they* did this
  moment, and stay honest: a stage that's on with nothing to do reads near-zero (not faked busy), Echo
  shows *idle* with no far-end, and Auto gain is bipolar (boost vs cut).
- **Master RAW bypass** — one click A/Bs the *whole* chain live at **matched loudness**, so the difference
  you hear is the cleaning, not a level jump. (*Monitor RAW (bypass cleaning)*.)

### What it can't do (and why) — scope, not apology

These are consequences of 8 mics on a ~40 mm ring, not software TODOs. The product wins on cost, the
inspectable/measured chain above, and the Design→Deploy→LIVE workflow — stated in the same breath:

- **No simultaneous multi-zone capture.** It follows/selects the active speaker (switched); a ceiling
  array runs many lobes at once. Two kits = coverage *by switching*, not two simultaneous streams.
- **No XYZ talker coordinates.** It reports azimuth + the nearest seat, not a 3-D position — not a
  drop-in camera-tracking XYZ source. (Triangulating two kits is the roadmap to part of this.)
- **No in-place-stander cut, no close-talker split.** Standing in place changes elevation, which a planar
  ring can't see; two people ~0.6–1 m apart fall inside one beam (~1.8–2.6 m wide at 2 m). Azimuth only;
  ~5.6 kHz spatial-aliasing ceiling; talkers within ~40–50° merge into one detection.

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
tests/                pytest suite (1074 tests; incl. headless GUI smoke)
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
- **"Cut (no pickup)" zone toggle** — `cp.set_zone_type(config, array_id, zone_id, zone_type)` flips a
  coverage zone between active (`dynamic`) and cut (`exclusion`) with one API call. In the GUI: selecting
  a zone in DESIGN shows a **"Cut (no pickup)"** checkbox that toggles it in-place (one undo step). Use
  it to silence a problem area — a hallway, an HVAC corner, an auto-generated zone over a doorway —
  without deleting and redrawing the zone.

  Two "cut" concepts to keep straight:
  - **Design-time zone type** (this feature): marks a zone `exclusion` at design time so it is never a
    steer target and never gets an output channel. The checkbox is the design intent.
  - **Runtime "Cut the door & anyone outside the pickup area"** toggle (existing auto-steer behaviour):
    decides whether the live engine actively **nulls** all `exclusion` zones during auto-steer sessions.
    A cut zone is excluded from steer targets regardless; it is actively nulled only when this runtime
    toggle is on.

  No schema change (the `type` field was already serialized in config v5).

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
  opt-in wait-for-device / auto-reconnect. Five selectable strategies — `delaysum`,
  sub-sample `fracdelay`, frequency-domain `superdirective`, data-adaptive `mvdr`,
  and **`rtf_mvdr`** (GEVD / max-SNR over a measured target-vs-noise covariance —
  captures real reverb and per-capsule mismatch; falls back to plane-wave MVDR during
  warmup or when the SRP-PHAT cross-check fails; opt-in, `PolarisBeamformer` / A-B-engine
  path only)
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

**Mic-input preamp** (LIVE **"Mic input"** card; `preamp_gain_db` / CLI `--preamp-gain-db`): a uniform
software gain applied to all capsules at the **front** of the chain, before the beam. Off by default
(0 dB), so the pipeline is byte-identical when unused. It is **spatially neutral** — a uniform input
scalar scales the array covariance by `g²` and leaves DOA and the trace-relative-loaded MVDR/LCMV beam
unchanged — and deliberately **honest**: a level trim (input metering / a healthy operating level into
level-sensitive DSP), **not** an SNR gain, because a post-ADC software multiply scales signal and noise
together and the output AGC re-levels it. (A hardware probe confirmed this POLARIS exposes no boostable
hardware gain — its Windows capture endpoint only attenuates, −96…0 dB — so there is no analog preamp
to drive; the trim stays software-only.)

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

**Capture everyone — simultaneous multi-talker** (`conf_pipeline_control.multibeam`; CLI
`scripts/capture_everyone.py`): instead of committing to one dominant talker, form a beam per active
person — DOA-detected and **snapped to defined room seats** for a stable, jitter-free aim (free DOA
where no seat is near) — gate each with the fan-proof speech scorer, and **NOM-automix** them into one
combined feed. It also records a **separate WAV track per person** (for recording / transcription /
diarization). Each beam nulls the others (multi-look LCMV over a shared FFT, so N beams cost ~one);
persistent **beam slots** keep each track on the same person across brief pauses. Honest limit: the
~40 mm array separates **2-3 well-spaced talkers** (>~40-50° apart) — closer people merge into one beam.

**Multi-array room capture** (`conf_pipeline_control.multiroom.MultiRoomController`; the LIVE Hardware
card's **"Kits — multiple arrays"** list): add ≥2 POLARIS (each its own input device) to cover a whole
room — several arrays at once, one combined feed + a per-person track for every talker. Each seat is
handled by its **nearest** array (`seats_owned_by_array`), so a voice is captured by one kit (best SNR)
and never summed twice; the kits combine **volume-domain** (`nom_automix`) because N independent USB
clocks can't be sample-aligned (no joint beamforming across kits — same wall as the 2-kit automix). Clean
ownership needs snap-to-seats on + arrays posed + seats defined; otherwise it's best-effort and flags it.

**Table fence (two-kit)** — an opt-in spatial gate for the *Two kits (combined room)* listening mode.
Each kit reports its own DOA bearing; the fence module (`conf_pipeline_control.fence`, `FenceDecider`)
fuses the two bearings into an approximate 2D source position (ray-cross / least-squares) and passes
only sources whose estimated position falls inside an operator-drawn table polygon, vetoing the rest via
an **output gate + selection veto** in `MultiKitController`. The polygon is drawn live with a freehand
polygon tool; it is **transient / live-only and is never persisted** (no schema change, combined-output
is byte-identical when the fence is off).

Honest limits to set expectations:
- **Loose-coupling fusion.** The two arrays run on independent USB clocks; the position estimate is from
  asynchronous bearings, not a phase-coherent triangulation. Sharper sync triangulation is a future
  upgrade.
- **Soft fence.** The ~40 mm aperture gives coarse bearings (~40–50° resolution); a margin band +
  hysteresis prevents boundary flicker — this keeps the conference table vs rejects a far-room source,
  not a surgical edge.
- **Range disambiguation.** The POLARIS is a circular array (no front/back ambiguity). The fence's
  value is telling apart a near table-talker from a far room-source on the same bearing, not resolving
  front vs back.
- **No null-steering** is added by the fence in v1 — it gates selection; the kits can stay in their own
  delay-sum or other beam mode.
- **Deployment caveat.** Kits must be spaced apart **and not directly facing each other along the talker
  line**. Directly-opposite-facing kits make the two bearing-rays anti-parallel (degenerate
  triangulation) even when well-spaced; corner-place or angle the kits so the rays cross at a useful
  angle. Near-parallel rays fall back to a level cross-check. Connection is refused with a visible
  reason if fewer than exactly 2 posed kits (each with `bearingDeg` + position set) are connected.

**Learn the array's bearing from a DOA measurement** — if you can't read the array's mounting heading
from the ceiling or a drawing, the DESIGN panel's **"Learn bearing from a reference…"** button infers it
from one DOA capture: stand a reference talker at a known room point, record a short clip (the button
reuses the existing calibrate-front DOA worker), and `cp.learn_bearing(array_pos, ref_point,
measured_az_deg)` back-computes the heading and calls `cp.set_array_bearing` for you. The CLI
`scripts/learn_bearing.py` does the same with explicit `--ref-x / --ref-y` coordinates. The GUI
button's default reference is a point 2 m straight ahead (+Y) of the array; a room-seat picker for
arbitrary reference points is a follow-up. The pure geometric solve is tested; live DOA capture is the
hardware part (validate at the kit). No schema change — sets the existing v5 `bearingDeg` field.

**Per-zone live gain on the mono beam** (opt-in, **off by default**) — pickup zones carry a per-zone
`gain_db` trim whose primary use is separate Dante output channels (one output per zone). The
**"Apply per-zone gain (live)"** checkbox surfaces it for the single mono feed: `cp.active_zone_gain_db`
looks up which pickup zone the active azimuth falls into and applies its `gain_db` as a **post-AGC
static offset** — the AGC normalises loudness first, then the zone trim adjusts it, so they don't fight.
Auto-steer-scoped in v1 (A/B engine and zone-beam paths are unaffected). No schema change. Live A/B
pending at the kit.

**Caveat:** the ~cm aperture means coarse-zone selection, not MXA920/Nureva-scale pinpoint — these
isolate a zone, A/B two strategies, or (with **Capture everyone**) separate 2-3 well-spaced talkers,
but two people seated close together at one table still merge into a single beam.

**Honest aperture-aware coverage simulation** (`conf_pipeline/directivity.py`; sub-feature #1 of the
POLARIS table-array coverage workflow): the geometric coverage simulator now scores each array with its
**real aperture-limited beamwidth** instead of a fixed 35° half-angle placeholder. For profiles that
declare `aperture_m` (currently only `polaris-8`), `steered_beamwidth_deg(aperture_m, freq_hz, steer_deg)`
computes the 3 dB main-lobe half-angle (broadside ≈ 0.47·λ/aperture, widening toward endfire, clamping
near-omni when λ ≫ aperture). The constant `_BW_K = 0.47` is calibrated to the measured sensiBel POLARIS
delay-sum beam — it matches the real beam to within ~1° at 1500/3000 Hz (a coherent circular ring is
roughly 2× narrower than the naive linear-aperture formula). The **`polaris-8` profile** (aperture 0.08 m,
element spacing 0.0306 m) uses this path; all other profiles keep the legacy 35° (no regression). The
coverage report (`cp.simulate_room_coverage`) also gains **honesty caveats** (`coverage_caveats` →
`RoomCoverage.caveats`): a per-pickup-zone-pair **separability warning** for zone pairs the beam cannot
resolve (zones closer than the 3 dB beamwidth), and a **spatial-aliasing / grating-lobe note** for the
~5.6 kHz alias ceiling (`alias_ceiling_hz(element_spacing_m)`) above which directivity degrades. Both
warnings are surfaced in the **Simulate** panel's read-only "Coverage warnings" list. This is pure-stdlib
(`conf_pipeline` stays numpy-free); the calibration test is numpy-only and guarded by a `[control]`
skip. No schema change — `aperture_m` and `element_spacing_m` are profile-catalog constants (code-only,
never serialized); configs persist only `profileId` and round-trip byte-identically at CONFIG_VERSION 5.

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
