# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A Python port of a conferencing-audio **configuration control plane** — it models *what connects to
what* (mic coverage zones → matrix mixer → AEC references + automixer → outputs) and validates
correctness. The core engine does **not** process, mix, or stream real audio. The optional
`conf_pipeline_control` package is the exception: it *is* real-time DSP (host-side beamforming for a
physical sensiBel POLARIS / 8-capsule USB array), kept strictly behind an optional extra.

The JSON config schema is shared byte-for-byte with a **TypeScript sibling** at
`c:\Work\conferencing-audio-pipeline`. Keeping the two in sync is a hard constraint (see below).

## Commands

The venv interpreter is `./.venv/Scripts/python.exe` (Windows). Note: this is `.venv`, **not**
`.venv311` (that belongs to a different repo on this machine).

```bash
# Install (editable, with dev tools)
./.venv/Scripts/python.exe -m pip install -e ".[dev]"

# Full test suite (~830 tests, ~5 minutes — run ONCE; prefer backgrounding it)
QT_QPA_PLATFORM=offscreen ./.venv/Scripts/python.exe -m pytest -q

# Single test file / single test (the fast inner loop)
./.venv/Scripts/python.exe -m pytest -q tests/test_aec.py
./.venv/Scripts/python.exe -m pytest -q tests/test_aec.py::test_reinforced_shared_reference

# Type check (scope + strictness are in [tool.mypy]: conf_pipeline + conf_pipeline_control only)
./.venv/Scripts/python.exe -m mypy

# Run the desktop app
./.venv/Scripts/python.exe run_gui.py
```

- The GUI/offscreen smoke tests need `QT_QPA_PLATFORM=offscreen` prefixed (the Qt GUI itself is not
  type-checked — it's verified via these headless smoke tests instead). **On this Windows box, building
  the full `MainWindow` headless HANGS** — the GUI tests that use the `win`/MainWindow fixture run in CI
  (offscreen Linux), not here. A bare `QApplication([])` + constructing a single panel (e.g.
  `LivePanel(state)`) DOES work, so live-panel logic is verified locally with a small construct-and-poke
  probe + `pytest --collect-only` + mypy, and full GUI behaviour in CI. A suite stall around the Qt tests
  is this, not a regression.
- The Bash tool resets cwd between calls — always prefix `cd /c/Work/conferencing-audio-pipeline-py &&`.
- CI (`.github/workflows/ci.yml`) runs pytest across Python 3.10–3.13 and mypy on 3.12. Pinned dev
  deps live in `requirements-dev.txt`; loose ranges in `pyproject.toml`.

### Optional extras (heavy deps, gated)

`pip install -e ".[control]"` (numpy + sounddevice + scipy) enables live beamforming; `".[sim]"` /
`".[sim-rir]"` enable physics-validation backends for placement simulation; `".[octovox]"` enables
the HTTP bridge to the OCTOVOX cleaning server. The suite runs fully without any of them.

## Architecture

Three packages, split by dependency and responsibility:

- **`conf_pipeline/`** — the engine. Pure dataclasses + functions, **no Qt, no numpy**. Public API is
  re-exported flat from `conf_pipeline/__init__.py` (`import conf_pipeline as cp`). Entry points:
  `model.py` (types, geometry, JSON (de)serialization), `validation.py` (`validate()` + code
  catalog), `api.py` (the builder API: `create_config`, `route`, `set_aec`, `auto_route`, …),
  `persistence.py` (`serialize`/`deserialize`). The `sim/` subpackage is the placement optimizer.
- **`conf_pipeline_gui/`** — the PySide6 app ("Aniston Room Designer"), a "Stagebar" workflow shell:
  a ModeBar walks **DESIGN → SIMULATE → ROUTE → DEPLOY → LIVE**, each mode with its own canvas tools,
  overlays, and right panel (`panels/`). `state.py` holds `AppState` (undo/redo); `canvas.py` is the
  2D+3D QPainter editor.
- **`conf_pipeline_control/`** — host-side array control, only loaded behind the `[control]` extra.
  Itself split: a **design layer** (pure stdlib — `beamformer.py`, `steering.py`, `geometry.py`:
  zone→direction, delay-and-sum/LCMV, beam-pattern verification) and a **live layer** (numpy +
  sounddevice — `polaris_beamformer.py`, `virtual_mic_grid.py`, `beam_engine.py`, `live.py`, `doa.py`,
  `autosteer.py`).

The README is the authoritative feature catalog (per-version sections, worked code examples). Read it
before adding a feature so you match the established surface and prose.

### The live DSP chain (the big picture for `conf_pipeline_control`)

The live layer runs a fixed per-block stage chain on the beamformed mono. **Two parallel
implementations exist and a new stage must be added to BOTH:**
- `PolarisBeamformer.process_block` (`polaris_beamformer.py`) — driven by the A/B engine
  (`beam_engine.py`) and the multi-array controllers (`multibeam.py` / `multikit.py` / `multiroom.py`).
- `LiveBeamController.process` (`live.py`) — the zone ("Whole table") + auto-steer (`autosteer.py`) paths.

**Stage order is load-bearing** (left = early):
`preamp → beam → speech-HP → AEC → transient-suppress → dereverb → post-NR → PEQ → AGC → band-limit → voice-gate`.
e.g. the speech high-pass + transient duck sit BEFORE the AGC so rumble/taps don't make it pump (and a
transient's `duck_active` FREEZES the AGC, `agc.py`); the voice gate is LAST so the AGC can't chase it;
PEQ is after cleaning so the AGC levels the tone.

**Every optional stage follows one opt-in recipe** — copy it (`post_nr` / `dereverb` / `aec` /
`transient` / `voice_gate` / `peq` / `speech_band` all do): a cfg key **default-OFF** with a real
off/None/0 escape hatch and a **bit-exact pass-through when off** (return the *same* array object — this
is what keeps the suite byte-identical); built lazily in `_setup_runtime` / `_build_post_nr`; applied in
the chain; dropped in `reset_transient`; fanned out through `BeamEngine._clean_cfg` (the steered cfg) +
`AutoSteerController` + a GUI checkbox on the live panel. The reusable streaming-stage classes
(`peq.py` `StreamingPeq`, `transient.py`, `voice_gate.py`, `streaming_cleaner.py`,
`deepfilter_cleaner.py`, `streaming_aec.py`) share a `process(block[, noise_gate]) -> block` /
`reset()` contract; shared state they mutate is rebound atomically, never reset in place (see below).

## Hard invariants (don't break these)

**The AEC self-reference rule is the central correctness invariant.** A reinforced mic must never get
its own reinforced output as an AEC reference (`AEC_REINFORCED_SHARED_REFERENCE`). This is what
`validate()` exists to catch; `tests/test_aec.py` is its guard. When touching routing/AEC/automix
logic, keep this rule intact and tested.

**Schema = camelCase, lossless round-trip, TS parity.** `CONFIG_VERSION` is **5** (`model.py`); v1–v4
files migrate losslessly via a chained migration in `persistence.py`. When you change a serialized
field:
1. Map it in `model.py` (snake_case dataclass ⇄ camelCase JSON; add to `_NULLABLE_KEYS` if a nullable
   must persist as `null`).
2. Bump `CONFIG_VERSION` only if the on-disk shape changed.
3. Add an **additive** migration step in `persistence.py` that sets **its own explicit target
   version** — never `obj["version"] = CONFIG_VERSION` (that makes an old file skip intermediate
   steps; this was a real past bug).
4. Cover the field + migration in `tests/test_serialization.py` (an old file without the field must
   still round-trip; `deserialize(serialize(c))` must re-serialize byte-identically).
5. Add the matching camelCase key to the TS sibling at `c:\Work\conferencing-audio-pipeline`.

**`conf_pipeline` stays numpy-free.** numpy/sounddevice/scipy/pyroomacoustics live behind extras and
are imported **lazily inside functions/methods**, never at module top level. `conf_pipeline_control`
may use numpy. Importing `conf_pipeline` or `conf_pipeline_control` (the package root) must pull no
heavy deps.

**Real-time audio safety** (anything reached from the PortAudio callback — `_cb`, `_cb_input`,
`process_block`): never hold a lock across heavy DSP (FFT, covariance, delay-and-sum loops), join
threads, or do pathological allocation in the callback. Shared state mutated by both host and audio
threads must be serialized under the back-end `_state_lock` (with a None-guard in the hold path); a
tracker the DOA thread mutates lock-free must be **rebound atomically**
(`self._tracker = _TalkerTracker(...)`), not `.reset()`-ed in place.

**DSP conventions** (in `conf_pipeline_control`): azimuth **0° = +Y, clockwise** (`atan2(x, y)`);
off-nadir 90° = horizontal (a planar array can't resolve elevation). Steering delay sign: delay the
**early-arriving** mic so channels align on the farthest one — the opposite sign steers to the mirror
azimuth (a classic regression). Spatial-aliasing ceiling ≈ **5.6 kHz**; DOA/scoring stay band-limited
to **300–3800 Hz** and the beam output is low-passed at the cutoff. Widening these bands adds grating
lobes, not resolution.

**New default behaviour that changes output** (a filter/gate on by default) must sit behind a working
toggle with a real off/None/0 escape hatch, and must not break existing callers. New DSP needs
hardware-free tests (synthetic plane-wave / matched blocks, stubbed streams).

`virtual_mic_grid.py` is deliberately self-contained/removable: it may import shared-core modules but
must **not** import the steered beamformer sibling. Don't add that coupling.

## Project-specific agents

`.claude/agents/` defines four subagents tuned to this repo — prefer them for their tasks:
- **green-gate** — runs the suite + mypy + offscreen GUI tests, reports only failing context.
- **schema-parity-guard** — checks the camelCase/round-trip/version/migration/TS-parity checklist
  after edits to `model.py` / `persistence.py` / `api.py` or anything touching `CONFIG_VERSION`.
- **dsp-realtime-reviewer** — adversarial review of `conf_pipeline_control/` and the GUI live panel
  for the realtime-safety and DSP-correctness constraints above.
- **docs-maintainer** — drafts CHANGELOG/README updates and fixes stale facts (version, test count,
  schema version) before a release.
