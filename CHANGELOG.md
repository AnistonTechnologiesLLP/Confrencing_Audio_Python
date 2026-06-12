# Changelog

Python port of the Conferencing Audio Pipeline. Format based on
[Keep a Changelog](https://keepachangelog.com/); versions track the TypeScript
project they were ported from. The JSON **config schema** (`CONFIG_VERSION` = 1,
camelCase keys) is identical to the TS version, so configs interoperate.

## [Unreleased]

**Wideband (subband) beam design** — the published beam design is now verified
across the speech band (250 Hz–8 kHz) instead of asserted at a single 1 kHz
design frequency. Python-only, `conf_pipeline_control` only; the JSON config
schema is unchanged.

### Added
- **Per-band design verification** (`beamformer.py`): `design_zone_beams`,
  `design_from_bearings`, and `design_multi_bearings` re-derive the weights at
  each **octave-band center** (250 / 500 / 1k / 2k / 4k / 8k Hz —
  `SPEECH_OCTAVE_CENTERS_HZ`) by default and attach a `BandMetrics` per band to
  each `ZoneBeam` (`band_metrics`: weights, pickup gain, WNG, DI, excluded-area
  attenuation, per-band degradation note). `BeamDesign.band_freqs` records the
  grid; `summary()` gains a per-beam band line (DI/WNG ranges + the worst
  excluded leak and the band it occurs at). A custom grid is a parameter away
  (`bands=(…)`); `bands=()` opts out (used by the auto-steer control loop, since
  the live runtime re-derives weights per FFT bin anyway).
- `freq_hz` is now documented as the **reference frequency**: the legacy scalar
  fields (`pickup_gain_db`, `di_db`, `wng_db`, `exclusion_atten_db`, lobes) are
  reported at it, unchanged — the single-frequency design is the `bands=()`
  special case, so existing callers and serialized expectations are unaffected.
- Tests (`tests/test_wideband.py`, 14): pickup unity and deep exclusion nulls at
  **every** band center (zone + bearing + delay-and-sum paths), per-band WNG
  surfacing the low-frequency cost, single-frequency equivalence at each center,
  custom/empty/invalid grids, dead-capsule zero weights per band, summary
  content, and a numpy cross-check that the stdlib per-band weights equal the
  live runtime's per-FFT-bin weights (skipped without the `[control]` extra).

**Project file manager (A4)** — recent files, autosave, crash recovery, and a
user-visible migration notice on open.

### Added
- **`conf_pipeline/files.py`** (pure stdlib — deliberately the only engine
  module that touches the filesystem): `ProjectFileManager` with
  `open_config` / `save_config` (atomic writes), a most-recent-first
  **recent-files list** (deduped, capped at 10, pruned of deleted files,
  persisted), **autosave** of an opaque workspace payload, and **crash
  recovery** — the autosave file doubles as the crash marker: it is cleared on
  clean exit, so a leftover autosave at startup means the last session died.
  `OpenResult.migrated_from` + `migration_notice()` report when an old-schema
  file (e.g. v1) was upgraded on open. All state lives in one per-user
  directory (`%APPDATA%/conf-pipeline` / `~/.local/state/conf-pipeline`),
  overridable via `CONF_PIPELINE_STATE_DIR` or the constructor.
- **GUI**: an **Open recent** submenu in the ☰ menu (populated on open, with
  *Clear list*); import/export now go through the manager; opening an
  old-schema file shows an explicit **"File upgraded"** dialog; a 30 s
  **autosave timer** snapshots the whole multi-room workspace (as a project
  JSON) whenever there are unsaved edits; on startup `main()` offers **"Recover
  unsaved work?"** when a crash left an autosave behind, restoring every room;
  closing the window cleanly clears the marker. `AppState.load_rooms` replaces
  the workspace wholesale for recovery.
- Tests: `tests/test_files.py` (11 — round trip + recent semantics, v1
  migration notice, autosave/recovery lifecycle incl. missing-meta and
  project-payload round trip, env-var state dir) and 4 GUI smoke tests
  (autosave tick + clean-close lifecycle, multi-room crash recovery via a
  monkeypatched dialog, migration notice on `_open_path`, recent-menu
  populate/open). `tests/conftest.py` points the state dir at a temp directory
  so the suite never touches real user state.

**Device transport (A1)** — the device-facing seam of the commissioning
workflow, simulated behind a clean interface (no real protocols).

### Added
- **`conf_pipeline/transport.py`** (pure stdlib): abstract `DeviceTransport`
  (site-level — `discover` / `connect` / `disconnect` / `read_config` /
  `push_config` / `read_status`) mirroring the `MicController` /
  `SimulatedMicController` pattern: the base owns the connection registry
  (idempotent connect/disconnect, config I/O requires a connection, status
  polling deliberately doesn't, context manager disconnects all). Plus
  `DiscoveredDevice`, `DeviceStatus`, and `TransportError`.
- **`SimulatedTransport`**: a deterministic, hardware-free room of devices —
  seeded with (deep-copied) device configs, `set_offline` simulates unplugging
  (drops the connection, vanishes from discovery), `add_device` plugs one in,
  pushes update the device-side store. Seeding with drifted configs is the
  reconcile-diff story Phase A3 builds on.
- Tests (`tests/test_transport.py`, 13): discovery determinism + offline
  filtering, connection bookkeeping + error paths, copy isolation on
  `read_config`, push round-trip, the seeded-drift → push → reconciled
  scenario, status-without-connection, and simulation-control guards.

**Broadband verification curves** — directivity index and beamwidth as a
*function of frequency*, turning the README's honest-fidelity note from an
assertion into a measured result.

### Added
- **`frequency_curves(design)`** (`beamformer.py`) → one
  `BeamFrequencyCurve` per beam: DI, −3 dB beamwidth, WNG, and lobe/grating
  counts at each frequency of a grid (default: **third-octave centers**,
  250 Hz–8 kHz — `SPEECH_THIRD_OCTAVE_CENTERS_HZ`), re-deriving the weights per
  point with the same formula the live runtime applies per FFT bin. Pure stdlib.
  `BeamFrequencyCurve.table()` renders an aligned text table with grating-lobe
  warnings and per-point degradation notes.
- **GUI**: the Live panel's *Design beam from zones* readout now appends the
  DI/beamwidth-vs-frequency table for the first beam, under the azimuth
  sparkline — the canvas readout shows where the beam narrows, where
  superdirectivity pays off, and where grating lobes start.
- Tests (+7): curve shape/grid, known-geometry physics on the 10 cm aperture
  (DI rises ≥ 2 dB and beamwidth at 8 kHz < half its 250 Hz value for
  delay-and-sum; superdirective beats delay-and-sum by > 2 dB DI at 250–630 Hz),
  octave-grid consistency with `BandMetrics`, empty-design/bad-grid handling,
  table content, and a GUI smoke test asserting the readout carries both the
  per-band line and the curve table.

### Notes
- The live overlap-add path was **already broadband-correct** (it re-derives
  weights per FFT bin from the design's directions); what was narrowband was the
  *published verification*. This release closes that gap — the readout now
  proves what the runtime actually does, honest about where physics degrades
  (per-band WNG/DI make the low-band cost visible).

**Repository hygiene** — no engine, schema, or GUI behaviour changes.

### Added
- **GitHub Actions CI** (`.github/workflows/ci.yml`): `pytest` on push/PR across
  Python 3.10–3.13 (Ubuntu, Qt offscreen + PortAudio system libs), with a
  coverage report surfaced in the log (reported, not enforced), plus a separate
  **mypy** job.
- **mypy type-checking** over `conf_pipeline/` and `conf_pipeline_control/`
  (`[tool.mypy]` in `pyproject.toml`); the codebase now passes cleanly.
  `is_mic_device` / `is_processor` became `TypeGuard`s, lazily-bound
  numpy/sounddevice attributes are typed `Any`, stale `type: ignore` comments
  were removed, and a handful of annotations tightened (`PortKind`,
  `DeviceTemplate.transport`/`coverage_mode` Literals). No runtime behaviour
  change beyond a few `assert <processor> is not None` statements on
  invariants that already held.
- **Pinned dev/test dependencies** (`requirements-dev.txt`): exact versions for
  pytest / pytest-cov / coverage / mypy / PySide6 / numpy / scipy / sounddevice /
  requests (numpy & scipy pins split at Python 3.11). The `[dev]` extra now
  includes `pytest-cov` and `mypy`.
- `.gitignore` grew `.mypy_cache/` and coverage artifacts (`.coverage*`,
  `coverage.xml`, `htmlcov/`); the index was already free of build artifacts.

## [1.14.0] - 2026-06-12

**"Stagebar" UI redesign** — a complete UX + visual overhaul of the desktop app.
The invisible workflow becomes the navigation: five top-level modes
(**DESIGN → SIMULATE → ROUTE → DEPLOY → LIVE**, `Ctrl+1…5`) replace the 25-action
toolbar and the 7-tab inspector. Python-only and GUI-only: the engine, the
`[control]` live layer, and the JSON config schema are untouched.

### Added
- **ModeBar** (`modebar.py`): centered five-mode switcher with live status dots
  (● done · ◔ in progress · ○ untouched) driven by the new `workflow.py` stage
  predicates; the LIVE dot pulses red while an audio session is connected — from
  any mode.
- **Per-mode right panels** (`panels/`): Design (build + room actions + selection
  editor), Simulate (placement tools), Route (routing + AEC/automixer/DSP chains +
  mute groups, merged — one job), Deploy (pre-flight checklist, inline deploy
  diff, import/export/report, raw JSON in a collapsed card), and Live (the old
  wall of controls folded into four collapsible cards over a **pinned transport
  footer** — meter / Connect / Mute / gain never scroll out of reach). Panels
  refresh coalesced and only while visible, catching up on `showEvent`.
- **LIVE operations view** (`canvas.py`): while a session runs, the floor plan
  shows the steering-sector wedge, real-time DOA detection rays (green
  in-sector / red nulled, front-relative bearings matching `doa.sector_gate`),
  and a level halo breathing with the output meter — published as transient
  state by the Live panel's meter tick, never entering undo history.
- **Mode-aware canvas**: geometry editing gated to DESIGN (drags, handles,
  context menus); SIMULATE keeps talkers draggable for what-if; ROUTE draws
  routes bold with transport labels over dimmed zones; DEPLOY badges devices
  added (+) / changed (~) since the last deploy.
- **Global Issues drawer** (`issues.py`): the validation pill in the top bar
  opens a slide-in errors/warnings drawer in every mode; clicking an issue
  selects the offending device on the canvas.
- **Shell chrome**: left tool rail with per-mode tools and a zone-kind flyout
  (`toolrail.py`), floating 2D/3D + overlays view bar on the canvas
  (`viewbar.py`), ☰ app menu + room-switcher popover (with rename, previously
  unexposed), programmatic theme-tinted line icons (`icons.py`, no assets),
  next-step hint chips in every panel header, and mode-aware canvas
  empty-states. Tool keys `V/C/R/Z/T` hop to their home mode from anywhere.
- `MainWindow.closeEvent` now disconnects a running live session (previously
  nothing did).

### Changed
- `theme.py` owns the "Conduit" palettes/QSS (grown with chrome + canvas roles);
  the canvas backdrop, grid, and hint text follow the palette — the light theme
  no longer gets a dark canvas.
- Deploy diffs render inline in the Deploy panel instead of vanishing into a
  toast; the config JSON view serializes only while actually visible.

### Removed
- The toolbar, the 7-tab `inspector.py` (carved into `panels/`), and the
  getting-started strip (`guide.py`) — its predicates live on in `workflow.py`
  as the ModeBar dots, hint chips, and empty-states.

### Tests
- Smoke suite reworked for the shell: mode switching, workflow dots, validation
  pill + drawer, hidden-panel staleness, LIVE overlay painting without hardware,
  deploy badges, a simulated-backend live connect/disconnect round-trip (the
  session lifecycle is finally under test), context-menu construction (catching
  a long-standing right-click crash), 3D drag gating, and the processor hint.
  **259 tests total.**

### Fixed (post-redesign review)
- Right-clicking the canvas raised IndexError before the context menu could
  open (the handler passed the string ``"2d"`` as the view transform —
  long-standing; menu construction is now split from the modal exec and tested).
- The 3D view bypassed the new mode gating: devices/talkers were draggable in
  every mode; now it follows the same profile as 2D, and the hover cursor only
  advertises drags the mode allows.
- Hint chips dead-ended in processor-less designs (suggested the documented
  no-op Auto-Route); they now point to adding a processor first, like the old
  banner did.
- The Issues drawer rebuilt its list synchronously inside its own item-click
  (the NoWheel crash class) — refreshes are now coalesced onto the next tick.
- A live session's overlay repainted the full canvas ~17×/s even when idle
  (payloads now dedup), could attach to the wrong array (session array is
  pinned at connect; no fallback), and silently vanished in the 3D view (a
  hint label now says where it went). Pickers stay stable during a session.
- An armed floor-plan calibration leaked into other modes; the zone-kind
  flyout was undiscoverable (now a visible split-arrow button); manual sample
  rates were reset by unrelated refreshes; ☰-menu tooltips never showed; the
  view-bar separator rendered as a dot; mode switches refreshed panels twice;
  the deploy-badge cache could go stale after garbage collection; and
  ``Ctrl+Shift+J`` now jumps to the raw config JSON.

## [1.13.0] - 2026-06-11

**Multi-azimuth auto-steer** — host-side, real-time "listen only to the people in
this area" for a raw multi-channel array (e.g. sensiBel 8). A small array can't
*separate* sources well, but a circular array is strong at **azimuth**, so instead
of fighting that we detect *where* each talker is and steer at the ones inside a
coverage **sector**. Python-only and additive (the JSON config schema is
unchanged); needs the `[control]` extra (numpy + sounddevice).

### Added
- **DOA detection** (`conf_pipeline_control.doa`): SRP-PHAT azimuth scan from the
  array's spatial covariance (PHAT-whitened for reverb robustness, speech-band only
  to dodge spatial aliasing), with a multi-peak picker (`detect`) that returns up to
  `max_talkers` bearings, honouring a resolution-aware `min_separation_deg` and a
  peak-to-median VAD floor. Plus the **sector gate** (`in_sector` / `sector_gate`,
  wrap-aware with a `front_offset`) and `detect_offline` for tuning on a recording.
  The active-capsule mask is respected (a dead capsule is excluded from the scan).
- **Auto-steer controller** (`conf_pipeline_control.autosteer.AutoSteerController`,
  `SectorConfig`): a slow control thread snapshots the live covariance, detects
  talkers, gates them to the sector, and rebuilds a multi-look beam
  (`design_multi_bearings` — one beam per in-sector talker, nulling the out-of-sector
  ones) which it re-applies live. Hysteresis (hold + re-select deadband) stops beam
  flicker during turn-taking; optionally mutes the output when nobody is in the area.
- **Live runtime covariance tap** (`live.LiveBeamController(track_covariance=True)` +
  `snapshot_covariance()`): opt-in, thread-safe band-covariance estimate for DOA;
  **off by default with zero overhead** and no behaviour change.
- **`design_multi_bearings`** / **`bearing_direction`** (`beamformer`): steer one
  beam at each of several bearings while nulling others, without a room/zone config.
- **PySide6 GUI** — an **Auto-steer (follow talkers in a sector)** group in the Live
  tab: sector centre/width, front-offset, max talkers, a "mute when empty" gate, a
  live readout of detected bearings (IN/out of sector), and a **Calibrate front**
  button (records a 'front' talker and sets the offset). Sector controls update a
  running session **live** (no reconnect). Reuses the existing transport
  (Connect/gain/meter/monitor) and the device-native sample-rate match.
- **Scripts**: `scripts/area_autosteer.py` (live detect + extract with a radar
  readout), `scripts/calibrate_front.py` (measure the front bearing),
  `scripts/device_check.py` (8-channel/44100 device diagnostic),
  `scripts/desk_isolation.py` (fixed-bearing extraction).
- pytest coverage: 15 new hardware-free tests (synthetic-mixture DOA recovery,
  resolution/threshold limits, sector gate, multi-look design, stubbed auto-steer
  loop). **251 tests total.**

### Notes (honest limits)
- Azimuth is reliable; **range is not** on a planar array — the coverage boundary is
  an angular sector, not a metric radius. Angular resolution ≈ beamwidth, so two
  talkers closer than ~40–50° on a small array merge into one detection.

## [1.12.0] - 2026-06-11

**More Shure-Designer-6 parity** — four config-only, vendor-neutral, offline
capabilities closing the remaining gaps against Designer's coverage/commissioning
workflow. All additive: the JSON config schema stays version 2 and interoperable
with the TypeScript version (new fields are optional and omitted when unset).

### Added
- **Per-coverage-area output channels + gain** (`CoverageZone.output_channel`,
  `CoverageZone.gain_db`): a pickup area can carry its own numbered output channel
  (1..`MAX_ZONES_PER_ARRAY`) feeding a dedicated Dante out — the way an MXA920's
  *steerable coverage* gives each of its 8 areas an individual output — plus a
  per-area gain trim. The array regenerates an `…-out-ch-N` port per channelled
  area (sorted by channel). Builders (`conf_pipeline.coverage`):
  `set_zone_output_channel`, `set_zone_gain_db`, `auto_assign_zone_channels`
  (sequential, idempotent, skips exclusion zones); API wrappers
  `set_zone_output_channel`, `set_zone_gain_db`, `auto_assign_zone_channels`.
  New validation codes `COVERAGE_CHANNEL_INVALID` (out-of-range / on an exclusion
  zone), `COVERAGE_CHANNEL_DUPLICATE` (two areas share a channel on one array),
  `COVERAGE_GAIN_INVALID` (gain out of `[ZONE_GAIN_DB_MIN, ZONE_GAIN_DB_MAX]`).
- **Zone-vs-coverage report** (`conf_pipeline.coverage_check.zone_coverage_report`
  → `ZoneCoverageReport` / `ZoneCoverageStatus`): closer to Designer than the
  array-circle overlap check — for each *drawn coverage area* it reports whether
  the centroid (and every corner) sits inside the owning array's floor coverage
  circle, and which arrays cover the centroid (more than one ⇒ automix **lobe
  contention**). Convenience views: `.uncovered`, `.partial`, `.contended`.
- **`optimize_room`** (`conf_pipeline.api.optimize_room` → `OptimizeRoomResult`):
  one-click "do everything" that chains the existing pieces — recommend + apply
  each array's best placement/steer (when a room + talkers exist), assign every
  pickup area its own output channel, then `auto_route` — returning the new config
  plus a human-readable change list. Each stage is opt-out (`place_arrays`,
  `assign_channels`, `route`) and idempotent; a failing array never aborts the run.
- **Logic / mute control** (`ControlConfig`, `MuteGroup`, `ZoneChannelRef`,
  `MuteTrigger`): config-only commissioning parity with Designer's mute-control /
  logic blocks. A mute group is a named set of devices and/or coverage-area output
  channels that mute together, with a `software`/`logicIn`/`button` trigger. API:
  `create_mute_group`, `add_mute_group`, `remove_mute_group`, `set_mute_group_muted`.
  New validation code `CONTROL_MUTE_GROUP_INVALID` (empty group, or a missing
  device/array/zone reference); a non-mute-capable member raises the existing
  `MUTE_LINK_UNSUPPORTED` warning. `SystemConfig.control` is an additive optional
  field, omitted from JSON when unset.
- **Design report**: a **Coverage areas** table (array, area, type, output channel,
  gain) with the zone-vs-coverage summary, and a **Mute groups** section.
- **PySide6 GUI**: selecting a pickup zone now shows an **Output channel** picker
  (— / 1..8) and an **Area gain** trim in the selection panel; the Issues-tab
  coverage line reports coverage-area-in-pickup and contention counts; an
  **Optimize room** toolbar button runs `optimize_room` (one undo step + summary).
- pytest coverage for all four — 28 new tests (channel/gain builders + validation,
  TS-interop round-trip + field omission, the zone-coverage report incl. contention,
  `optimize_room` stages/idempotence/opt-out, mute groups + validation). **223 tests
  total.**

### Changed (UI/UX)
- **Toolbar restructured** into captioned, tooltipped sections — Tools / View /
  Edit / Design / Room / Project / File — instead of one flat row of ~25 buttons.
  Each action carries a unicode glyph + a descriptive tooltip; the one-click
  automation (**✨ Optimize room**, **⚡ Auto-Route**) is accent-styled as primary.
- **Getting-started guide** (`conf_pipeline_gui/guide.py`): a dismissible strip
  under the toolbar with a live checklist — room → mic array → coverage zone →
  talker → optimize — each step showing ✓ when satisfied and a one-click action
  button (the predicates read the live config, so ticks update no matter how the
  design is built). Reopen via a **？ Guide** toolbar button.
- **Canvas empty state**: a centered hint (draw a room / use the guide / load a
  sample) replaces the blank canvas when nothing is placed yet.
- **Inspector status banner**: a always-visible line above the tabs showing the
  validation state (✓ valid / ✗ N errors / N warnings) plus the single most useful
  next step, with links that jump to the relevant tab.
- **Canvas context menus**: right-click a device / zone / talker (or empty floor)
  for Edit / Delete / quick-add actions; the cursor now reflects what's grabbable
  (open-hand over movable items, resize over zone corners, crosshair while drawing).
- **Mute-group editor** in the Routing tab — create a group over the mute-capable
  mics, toggle its mute, and remove it (surfacing the `ControlConfig` / `MuteGroup`
  model that previously had API + validation but no UI).
- 8 headless GUI smoke tests (`tests/test_gui_smoke.py`, Qt offscreen, skipped when
  PySide6 is absent) covering the window build, guide progress, the mute-group
  add/toggle/remove cycle, the inspector banner, and canvas context/hover helpers.
  **231 tests total.**

### Fixed
- Canvas right-click on the *body* of a coverage zone now opens the Edit/Delete
  menu — the handler tested for a `"zone"` hit kind that `_hit_test` never returns
  (it returns `"zone-move"` / `"zone-resize"`), so body clicks previously fell
  through to the empty-floor menu.

### Notes
- The JSON schema stays v2: a config with no channels/gain/control round-trips
  byte-for-byte to the same JSON as before, so existing files (and the TS version)
  are unaffected.
- The UI/UX changes are presentation-only — no engine, schema, or API changes.

## [1.11.0] - 2026-06-10

**Live array-microphone control** — a Python-only addition (no TS counterpart)
that drives an **actual array microphone** with **coverage-area selection** (à la
Shure MXA920) for arrays exposing only raw multi-channel audio (e.g. a sensiBel
8-capsule array). The steering is host-side; the engine's pickup/exclusion zones
become beamformer weights. Additive — the JSON config schema is unchanged.

### Added
- **New package `conf_pipeline_control/`** (design layer is pure stdlib — no numpy):
  - `geometry.py` — `ArrayGeometry`, `circular_array`, `sensibel_8(radius_m)`;
    capsule positions in a local frame, `SOUND_SPEED_MPS`.
  - `steering.py` — coverage zones → `Direction` look/null vectors, reusing
    `conf_pipeline.steering_angles` so beam bearings match the canvas rays.
    `look_direction`, `zone_look_direction`, `pickup_directions`,
    `exclusion_directions`, `zone_centroid`.
  - `beamformer.py` — narrowband design in pure `cmath`: `steering_vector`,
    `delay_and_sum_weights`, `lcmv_weights` (unit gain at the look direction,
    exact nulls toward exclusion directions, via a stdlib complex solver),
    `response_db`, `white_noise_gain_db`, `beam_pattern_azimuth`, and the
    app-facing `design_zone_beams → BeamDesign` (one beam per pickup zone, nulling
    exclusions, with verification numbers and a `summary()`).
  - `control.py` — `MicController` interface (connect / mute / gain / level /
    `apply_design`) + `SimulatedMicController` (hardware-free, deterministic level).
  - `audio.py` / `live.py` — **optional `[control]` extra** (numpy + sounddevice):
    input/output device enumeration and `LiveBeamController`, a real-time
    frequency-domain (per-FFT-bin), Hann-windowed 50 %-overlap-add beamformer with
    a live meter, mute/gain, **live monitoring** (`monitor=True` opens a full-duplex
    stream that plays the beamformed mono out to `output_device`), and optional WAV
    recording of the steered output. Import-guarded: a clear "install the extra"
    message if the deps are absent.
  - **Active-capsule mask** (`ArrayGeometry.active`, `with_active_channels`): a
    dead or non-audio channel can be switched off; the beamformer designs over the
    active capsules only and scatters zero weight to the rest (so the full-length
    weight vector still aligns with the device's channels), and the null-count
    limit becomes `n_active − 1`.
  - **Superdirective beamforming** (`superdirective_weights`, `diffuse_coherence`,
    `directivity_index_db`, `design_zone_beams(mode=…, loading=…)`): diffuse-noise
    MVDR that rejects isotropic background far better than delay-and-sum on a small
    array (~+5 dB directivity index in the 300 Hz–1 kHz speech band on the 8-capsule
    geometry), with diagonal loading trading directivity for robustness. Now the
    **default** mode (`MODE_SUPERDIRECTIVE`); the live per-FFT-bin runtime applies
    it broadband. GUI: a **Beamformer** group (Mode + Focus↔robust slider).
  - **Lobe analysis + leakage + out-of-zone suppression** (`analyze_lobes` →
    `LobeReport`, `talker_leakage_db`, `design_zone_beams(suppress_outside_talkers=
    …)`): count/locate a beam's main + side + grating lobes (so you see where
    off-target voices leak in), report each placed talker's pickup level
    (`[pickup]`/`[OUTSIDE]`), and **null every talker outside the pickup zone** as an
    extra interferer (on top of exclusion zones, up to `n_active−1`) — an out-of-area
    voice drops from a side-lobe level (~−23 dB) to a deep null (−120 dB). The null
    set flows to the live runtime via `BeamDesign.null_dirs`. GUI: lobe count +
    grating warning + per-talker leakage in the design readout, and a **Null talkers
    outside the pickup zone** toggle.
- **PySide6 GUI**: a **Live** inspector tab — array + capsule-radius + design-freq
  selectors, per-capsule **active checkboxes** + a **Detect silent capsules** probe
  (captures briefly off the GUI thread and unchecks dead channels), **Design beam
  from zones** (per-zone pickup/WNG/leak readout + an azimuth-response sparkline),
  input-device picker (auto-matching the device's native sample rate), a
  **Monitor output** toggle + output-device picker (play the beam live on
  headphones), **Connect/Disconnect**, a **Mute** toggle, a **Gain** slider, and a
  dB-scaled level meter driven by a `QTimer`. Falls back to the simulated controller
  (with a banner) when the extra is absent, so the workflow is fully usable offline.
- **OCTOVOX bridge** (`conf_pipeline_control/octovox_bridge.py`,
  `octovox_monitor.py`): connect the spatial front-end to the **OCTOVOX** voice-
  cleaning pipeline over HTTP. `zone_azimuths` maps an array's pickup zone →
  OCTOVOX `target_az` and exclusion zones → `interferer_az` (with
  `to_octovox_azimuth` handling the compass→math azimuth convention and a
  mounting-offset calibration). `OctovoxClient.clean_8ch` resamples the raw 8-ch
  clip 44100→48000, uploads it, runs `/api/clean` steered at those azimuths, and
  fetches the cleaned mono — so OCTOVOX does the cleaning while this app supplies
  the direction. `CleanMonitor` adds a **near-live cleaned monitor** (rolling
  chunks → clean → delayed playback; ~4–5 s latency, not real-time) — chunks
  **overlap and are equal-power crossfaded with level-matching** so OCTOVOX's
  per-chunk peak-normalisation and neural-stage edge transients don't click/pump at
  the seams, and a **speech gate** (`speech_gate`, noise-floor tracker) plays
  silence for noise-only chunks instead of OCTOVOX's normalised-up noise floor —
  fixing the "only noise, no voice" pumping in a quiet room. Direction steering is
  **opt-in** (`CleanMonitor` passes `target_az` only when enabled): by default
  OCTOVOX auto-beamforms, which is reliable on a small / front-back-ambiguous array;
  forcing a wrong azimuth could otherwise null the voice. The dead capsule is
  repaired (`repair_dead_channels`) from its ring-neighbours before sending, since
  OCTOVOX has no active-capsule mask. GUI: a **Clean via OCTOVOX** group in the Live
  tab (server URL, **Steer to pickup zone** toggle, azimuth offset, chunk).
- **A/B measurement harness** (`conf_pipeline_control/ab_test.py`): record a raw
  8-ch clip and beamform it offline **omni / delay-sum / superdirective /
  aggressive / nulled** (`ab_compare`, `apply_design_offline`), returning mono
  signals + a dB report (DI, WNG, per-talker leakage); `save_ab_report` writes the
  WAVs + `report.txt` so the steering effect is audible and measurable. GUI: an
  **A/B test — record & compare** button and an **Aggressive preset** (max
  superdirectivity, safe given the SBM100B's 80 dBA SNR).
- **Optional extras** (`pyproject.toml`): `control` (numpy + sounddevice) and
  `octovox` (adds requests + scipy for the bridge). The base engine and GUI need
  none of them.
- pytest coverage for the design layer (numpy-free) — 195 tests total
  (+29: steering geometry, steering-vector/main-lobe/LCMV-null math, zone-driven
  design, the active-capsule mask, and the controller/simulated backend).

### Notes
- Importing `conf_pipeline_control` never imports numpy/sounddevice; the live path
  imports them lazily, behind availability gates.
- **Fidelity is stated, not hidden:** an *N*-capsule array forms at most *N*−1
  nulls; excluded areas are strongly attenuated (not perfectly muted), and a planar
  array discriminates mainly by azimuth/horizontal offset. The code reports
  white-noise gain and excluded-area leakage so the trade-offs are visible.

## [1.10.0] - 2026-06-09

**Shure-Designer-inspired features** (Python-only, offline, vendor-neutral). Four
capabilities that mirror Designer 6, all preserving the TS-compatible JSON schema.

### Added
- **Coverage areas + checks** (`conf_pipeline/coverage_check.py`): each array's
  floor coverage circle from mount height × profile cone angle
  (`array_coverage_radius`, `array_coverage_circle`), plus `coverage_report`
  (covered / uncovered / overlapping arrays). A `coverage_angle_deg` was added to
  `DeviceCapabilities` (ceiling 120°, table 130°). GUI: a **Show coverage** toggle
  draws the circles on the 2D canvas, and the Issues tab gains a coverage summary.
- **Auto-Route** (`cp.auto_route → AutoRouteResult`): one-click optimize layered on
  `auto_configure` — adds far-end → loudspeaker feeds and a synced mic mute-link,
  returns a human-readable change list, and is **idempotent** (re-running is a
  no-op). `auto_configure` is now idempotent too (reuses existing reference/automix
  buses). Never violates the AEC self-reference rule (locked by a zero-errors test).
  GUI: an **Auto-Route** toolbar button with a summary dialog (one undo step).
- **Floor-plan import + scale** (`RoomLayout.background` / `RoomBackground`):
  load a floor-plan image (stored by path) under the room in 2D, with builders
  `set_room_background` / `set_room_background_scale` / `clear_room_background` and
  a unit-tested `calibrated_scale`. GUI: **Floor plan…** import + a **Calibrate…**
  drag-a-known-distance gesture; missing image files degrade gracefully.
- **Design report export** (`conf_pipeline/report.py`): `design_report(config,
  fmt)` produces a shareable **Markdown or HTML** doc (room + RT60, device/channel
  table, routing, AEC references, coverage status, validation) with no new
  dependency (stdlib `html.escape`). GUI: an **Export report** toolbar action.
- pytest coverage for all four (engine-only) — 139 tests total.

## [1.9.0] - 2026-06-09

**Placement simulation & recommendation** — a Python-only addition (no TS
counterpart) that recommends where to mount/steer a microphone array and where a
talker should sit, by optimising a fast geometric acoustic model with an optional
physics-validation step. Additive: the JSON config schema is unchanged (still v2).

### Added
- **Simulation engine** (`conf_pipeline/sim/`, pure stdlib — no numpy):
  - `scoring.py` — four objectives blended into one score: direct-path level/SNR
    (inverse-distance spreading + main-lobe rolloff), direct-to-reverberant ratio
    (Sabine RT60 → critical distance), coverage/on-axis (gaussian lobe gated by
    pickup/exclusion zones), and multi-talker fairness (mean/worst/variance
    aggregate). `estimated_rt60`, `score_placement`.
  - `search.py` — `recommend_placement` (joint array-pose + seat, coarse-to-fine,
    steer derived not searched, min-separation from other talkers) and
    `score_heatmap` (where-to-mount-the-array grid). **Multi-array aware**: each
    talker is scored by the best-covering array (`consider_all_arrays`); when a
    pickup zone (a "table") is defined, seats are placed at the table
    (`seat_in_pickup_zones`). Both are toggle-able in the Simulate tab.
  - `validate.py` — pluggable physics backends: `farfield` (numpy plane-wave UCA
    delay-and-sum) and `pyroomacoustics` (image-source RIR: physical DRR + beam
    SNR). `available_backends`, `numpy_available`, `validate_recommendation`.
  - Public API on `conf_pipeline`: `recommend_placement`, `score_heatmap`,
    `score_placement`, `estimated_rt60`, `validate_recommendation`,
    `available_backends`, `numpy_available`, `SimParams`, `Recommendation`,
    `PlacementScore`, `Heatmap`, `Candidate` (and `SimValidationResult`).
- **PySide6 GUI**: a **Simulate** inspector tab (target talker, grid step, RT60
  auto/manual, four objective-weight sliders, heatmap toggle, **Recommend**,
  **Apply to layout** as one undo step, and a backend-aware **Validate top pick**
  that runs off the GUI thread). The canvas gains a score-heatmap overlay (2D) and
  recommended array/seat/steer markers (2D + 3D).
- **Room measurements**: per-wall length labels on the canvas plus an always-on
  `Room W × D × H m` readout in the status bar.
- **Five more sample rooms** in the **Load sample…** picker — meeting room,
  conference room (3 arrays), training room / classroom, lecture hall / auditorium,
  and a U-shape boardroom (polygon table) — each validates and round-trips, driven
  by a `SCENARIOS` registry.
- **Optional extras** (`pyproject.toml`): `sim` (numpy → far-field validation),
  `sim-rir` (pyroomacoustics → image-source RIR validation). The base engine and
  GUI need neither.
- pytest coverage for the engine (numpy-free; the validator path is exercised when
  the optional extras are installed) — 109 tests total (incl. per-scenario
  validation, round-trip, and simulation smoke).

### Notes
- The engine stays a planning model: numpy/pyroomacoustics are imported only inside
  the validation functions, behind availability gates, so importing `conf_pipeline`
  has no new dependencies.

## [1.8.0] - 2026-06-09

Mirrors TypeScript 1.8.0 — Designer-inspired workflow features, vendor-neutral
and configuration/validation only (no audio/Dante/discovery/firmware/network I/O).

### Added
- **Projects (multi-room)** (`conf_pipeline/project.py`): `Project` / `ProjectRoom`
  with `create_project`, `add_room`, `remove_room`, `rename_room`,
  `set_active_room`, `update_room`, `get_active_room`, `serialize_project`,
  `deserialize_project` (per-room v1→v2 migration on load).
- **Deployment** (`deployment.py`): `set_deployment_status`, `mark_deployed`,
  and pure `deployment_diff`.
- **Naming** (`naming.py`): `apply_naming_scheme`, `suggested_label`,
  `label_collisions`, plus `NAMING_DUPLICATE_LABEL` / `NAMING_EMPTY_LABEL`
  warnings.
- **Routing views** (`routing.py`): `subscriptions`, `dante_subscriptions`,
  `routing_summary`, `signal_flow_report`.
- **Device templates** (`templates.py`): `device_template`, `instantiate_template`.
- **PySide6 GUI**: a room selector + **+/− Room**, **Auto-name** and **Deploy**
  toolbar actions, and a **Routing** tab (summary + signal-flow). 72 tests.

### Notes
- `SystemConfig.deployment` is an additive optional field; JSON stays
  interoperable with the TypeScript version (schema still v2).

## [1.7.0] - 2026-06-09

Mirrors TypeScript 1.7.0 — vendor-neutral DSP and device-capability modeling.

### Added
- **Device capability profiles** (`conf_pipeline/profiles.py`): the same 9
  vendor-neutral profiles as the TS catalog, with `DEVICE_PROFILES`,
  `get_device_profile`, `device_capabilities`, `default_profile_id`,
  `assign_device_profile`. Factories assign a default `profile_id`.
- **DSP block chains** (`Device.dsp_blocks`): kinds `gain`, `mute`, `peq4`, `agc`,
  `compressor`, `delay`, `noiseReduction`, `deverb` with range-checked params
  (`params` uses the same camelCase keys as the TS JSON). Builders
  `create_dsp_block`, `dsp_block_param_issues`, `default_peq_band`; API
  `add_dsp_block`, `update_dsp_block`, `remove_dsp_block`, `set_dsp_block_enabled`.
- **Validation**: the same new error codes (`DEVICE_PROFILE_UNKNOWN`,
  `DEVICE_CAPABILITY_MISMATCH`, `DSP_BLOCK_UNSUPPORTED`, `DSP_BLOCK_INVALID`,
  `DSP_TARGET_UNRESOLVED`) and commissioning warnings (`AEC_NO_FAR_END`,
  `AUTOMIX_OUTPUT_UNSET`, `MUTE_LINK_UNSUPPORTED`, `DSP_CHAIN_NO_LEVEL`).
- **PySide6 GUI**: profile selector + capability hint in the device inspector and
  a **Processing blocks** editor (per-device chain with compact editors for every
  block kind incl. PEQ bands) in the AEC/DSP tab.
- pytest coverage for profiles, DSP blocks, validation, v1→v2 migration, and
  round-trip (65 tests total).

### Changed
- **`CONFIG_VERSION` 1 → 2** with v1 migration (fills default profiles + empty DSP
  chains); JSON remains interoperable with the TypeScript version.

## [1.6.1] - 2026-06-08

### Added
- **Engine port (Python).** A faithful, dependency-free port of the TypeScript
  control plane as dataclasses + pure functions:
  - `model` — types, geometry, point-in-zone helpers, and TS-compatible JSON
    (de)serialization (camelCase keys, nullable vs. optional fields preserved).
  - `matrix` — immutable crosspoint mixer.
  - `coverage` — dynamic/dedicated/**exclusion** zones, mode-driven port
    regeneration (exclusion zones produce no lobe).
  - `dsp` — AEC reference resolution, automixer, mute linking.
  - `angles` — `steering_angles` (azimuth / down-tilt / off-nadir / distance).
  - `validation` — `validate()` with the full code catalog incl. the AEC
    self-reference rule (`AEC_SELF_REFERENCE`, `AEC_REINFORCED_SHARED_REFERENCE`).
  - `api` — builder API, `auto_configure`, talkers, `array_to_talker_angles`,
    `talker_coverage`; `persistence` — `serialize` / `deserialize`.
- **PySide6 desktop app** (`conf_pipeline_gui`): a 2D **and** 3D layout editor
  rendered with QPainter (orbit camera, no extra deps), a tabbed inspector
  (Build / AEC-DSP / Issues / JSON), undo/redo, keyboard shortcuts, sample
  scenarios, and JSON export/import. Devices, routes (transport-colored), zones
  (incl. exclusion), talkers with capture badges, and steering-angle rays render
  in both views; selection, drag-move, draw, and connect interactions are wired
  to the engine.
- **pytest** suite (53 tests) mirroring the TS tests: AEC positive/negative,
  coverage + exclusion, mode-switch port regen + orphan detection, matrix ops,
  automixer ranges, the boardroom integration scenario, steering-angle math,
  talker coverage, and lossless JSON round-trips (incl. camelCase schema parity).

### Notes
- Parity target is feature-complete with TS **1.6.1**: device elevation, talkers,
  exclusion zones, and steering angles are all included.
- The browser/HTML UI is not ported; the desktop app replaces it on the Python
  side. Configs are interchangeable via the shared JSON schema.

[1.6.1]: #161---2026-06-08
