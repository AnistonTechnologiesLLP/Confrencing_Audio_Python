# Changelog

Python port of the Conferencing Audio Pipeline. Format based on
[Keep a Changelog](https://keepachangelog.com/); versions track the TypeScript
project they were ported from. The JSON **config schema** (`CONFIG_VERSION` = 1,
camelCase keys) is identical to the TS version, so configs interoperate.

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
