# Audio Operator Workflow Guide

Phase 6 of the audio front-end hardening. This surfaces everything built in Phases 1–5 to a non-DSP
operator — **without changing any default behaviour**. It is a read-out + diagnostics surface, not a
new control surface: OFF stages show OFF, a failed calibration and a BAD placement are shown as
warnings (never hidden), and placement suggestions are **never auto-applied**.

The operator surface has three forms, all over one headless model
([`OperatorStatus`](../conf_pipeline_control/operator.py)):

- **CLI** — `scripts/operator_diagnostics.py` (print + export).
- **GUI panel** — `OperatorStatusPanel` (a read-only widget that renders the model).
- **Diagnostics export** — `reports/audio/operator_diagnostics_<stamp>.{json,md}`.

---

## 1. The seven sections

| # | Section | Surfaces (from) |
|---|---|---|
| 1 | **Device** | name, sample rate, channels, block size, latency estimate, input/output status, channel-count warning |
| 2 | **Calibration** (Phase 1) | enabled/disabled, profile (device/rate/channels/ref/createdAt), gain/polarity/delay summary, added latency, **load-failure warning** |
| 3 | **Placement Check** (Phase 3) | GOOD/ACCEPTABLE/BAD, score, reasons, recommendations, detected tones, HPF/notch suggestions, imbalance, clipping, hotspot |
| 4 | **Spatial / Cleanup Pipeline** (Phases 1–2) | the real stage order with **[on]/[off]** per stage + active-cleaning summary |
| 5 | **Output / Egress** (Phase 4) | 48 kHz + 16 kHz routes, WAV/external sink, frames pushed, pending buffer, latency |
| 6 | **Transcription** (Phase 5) | provider (mock?), session status, chunks, VAD config |
| 7 | **Diagnostics** | all of the above + warnings, exported to JSON + Markdown |

---

## 2. CLI

```bash
pip install -e ".[control]"

# quick snapshot (defaults: clean beam, all cleaners off):
python scripts/operator_diagnostics.py

# include a placement result + show pre-NR + AGC engaged, export to reports/audio:
python scripts/operator_diagnostics.py --placement reports/audio/placement_center.json --pre-nr --agc-target-db -20
```

It prints the 7-section status and writes `reports/audio/operator_diagnostics_<stamp>.json` + `.md`.
Flags mirror the engine: `--calibration <profile.json>`, `--pre-nr`, `--post-nr`, `--aec`, `--dereverb`,
`--agc-target-db`, `--rate`, `--out`, `--json`.

Example pipeline read-out:

```
## Pipeline
- active cleaning: HPF/notch
  - [off] preamp — uniform mic-input gain
  - [off] calibration — per-capsule gain / polarity / delay
  - [on]  beam — delaysum beamform + DOA + null steering
  - [off] dereverb — late-reverb suppress
  - [on]  pre-NR HPF/notch — linear cleanup BEFORE the denoiser
  - [off] post-NR — DFN3 / OM-LSA / Wiener / gate
  - [on]  AGC/limiter — target-loudness + limiter
  - [on]  band-limit — anti-alias low-pass
- Cleaners are default-OFF and never forced on; this is a read-out, not a control.
```

---

## 3. Building the model in code

```python
import conf_pipeline_control as cc
from conf_pipeline_control.operator import OperatorStatus

status = OperatorStatus.build(
    engine=bf,                       # a PolarisBeamformer (its flags are read, not changed)
    calibration_path="cal.json",     # optional — surfaces load failures
    placement=placement_result,      # optional PlacementResult (Phase 3)
    egress=egress_router,            # optional EgressRouter (Phase 4)
    transcription=transcription_stream,   # optional TranscriptionStream (Phase 5)
    generated_at="2026-06-26T18:00",
)
print(status.to_markdown())
status.save("reports/audio")         # writes operator_diagnostics_<stamp>.{json,md}
```

`status.device_section()` / `calibration_section()` / `placement_section()` / `pipeline_section()` /
`egress_section()` / `transcription_section()` / `warnings()` return plain dicts.

---

## 4. GUI panel

**In the running app (Phase 8):** launch `python run_gui.py`, open the **app menu (☰) → "Audio operator
diagnostics…"**. A separate read-only **Audio Operator Diagnostics** window opens, built from the running
engine, with **Refresh** and **Export JSON + Markdown** buttons. It has no DSP controls and applies
nothing; when no beam is connected it shows all-off defaults (connect a beam, then Refresh for live
status). Export writes `reports/audio/operator_diagnostics_<stamp>.{json,md}` (same as the CLI).

**Audio Room Profiles (Phase 9):** the app menu also has **"Audio room profiles…"** — save/load/validate
room-specific setup profiles (calibration ref, placement result + suggestions, pre-NR notches/HPF,
egress/transcription prefs). It is **management only** and never applies anything to the running engine.
See [AUDIO_ROOM_PROFILE_GUIDE.md](AUDIO_ROOM_PROFILE_GUIDE.md).

**Listening Processing Profiles (Phase 10):** the LIVE panel shows a read-only **flow summary** under the
"Listening mode" dropdown — a plain-language description of what each mode does to the audio (spatial,
denoise, dereverb, AGC). The summary model is **descriptive only** (the real chain is fixed at Connect).
Phase 10 also pre-ticks the **recommended** cleanup in the LIVE panel — AGC + OM-LSA denoise +
tap-suppression (AEC/voice-gate stay opt-in) — a GUI-default change; engine/CLI defaults stay OFF.
**Dereverb is not global**: its global checkbox stays OFF and it is auto-enabled only on the Follow /
Clean auto-steer path. Every toggle is still unticked-able before Connect. See
[LISTENING_PROFILES_GUIDE.md](LISTENING_PROFILES_GUIDE.md).

**Apply a calibration profile:** the LIVE panel's Hardware card has **"Load calibration profile…"** —
pick a saved per-capsule CalibrationProfile JSON to apply it to the live engine (validated on load;
applied at Connect, rebuilds if already live). Calibration is **OFF by default** (raw capsules); after you
apply one, this window's **Calibration** section shows *ON* with the profile details. See
[CALIBRATION_GUIDE.md](CALIBRATION_GUIDE.md).

**Lobe Control (Phase 11):** the LIVE panel's **"Lobe control"** card aims/shapes the beamformer pickup
*after* calibration — main direction (manual angle / seat), pickup focus (Wide/Medium/Narrow), and a
suppress-direction null (≤2). It uses honest labels (a null **reduces** pickup, it does **not** mute — a
reduced-pickup zone, not a hard-mute zone) and warns when calibration is OFF (less accurate) or placement
is BAD (underperforms). It changes no DSP default until used. See [LOBE_CONTROL_GUIDE.md](LOBE_CONTROL_GUIDE.md).

`conf_pipeline_gui/panels/operator.py` `OperatorStatusPanel` is the small read-only `QWidget` that renders
`OperatorStatus.to_dict()` (wrapped by `OperatorDiagnosticsWindow` for the menu action above):

```python
panel = OperatorStatusPanel()
panel.set_status(OperatorStatus.build(engine=bf, placement=result).to_dict())
panel.summary()                      # the multi-line read-out string
panel.section("placement")           # the section dict
panel.warnings()                     # the warnings list
```

> **Why a model + a thin panel.** The full app `MainWindow` hangs when built headless on this box (see
> `CLAUDE.md`), so — exactly as `StageStrip` does — the panel is a single widget probed offscreen, and
> the *logic* lives in the headless `OperatorStatus` model (fully tested). Full MainWindow GUI behaviour
> is exercised in CI.

---

## 5. Honesty rules (built in)

- **OFF is OFF.** A stage shows `[off]` unless it is really configured on; nothing is forced on.
- **Failed calibration is a warning**, not a silent fallback — `"Calibration profile failed to load"`.
- **A BAD/ACCEPTABLE placement is a warning** with its reasons.
- **Suggestions are not auto-applied** — `placement.suggestedPreNrBands` is shown with
  `autoApplied=false`, and the operator opts in (and re-measures per room — the tones are this room's).
- **No raw 8ch as clean output** — the egress section states it, and the egress router enforces it.
- **Virtual mic is an external adapter seam**, not a bundled driver.
- **Transcription is mock/dev** — no real ASR vendor, no network call by default.

---

## 6. What this phase did NOT change

No DSP was rebuilt and no default changed. The only engine edit is cosmetic: `active_cleaning_stages()`
now also lists `HPF/notch` when pre-NR is on (Phase 2 had noted it wasn't surfaced). Everything else is
a new, read-only model + CLI + panel + export.
