# Changelog

Python port of the Conferencing Audio Pipeline. Format based on
[Keep a Changelog](https://keepachangelog.com/); versions track the TypeScript
project they were ported from. The JSON **config schema** (`CONFIG_VERSION` = 1,
camelCase keys) is identical to the TS version, so configs interoperate.

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
