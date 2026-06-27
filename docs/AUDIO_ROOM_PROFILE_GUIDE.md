# Audio Room Profile Guide

Phase 9 of the audio front-end hardening. An **Audio Room Profile** is a saveable, room-specific record
of how you set up a POLARIS room — and it is deliberately **inert**: saving/loading a profile never
changes the running audio pipeline.

---

## 1. What an Audio Room Profile is
A JSON document that captures the right setup **for one room** — which capsule calibration to use, the
measured HVAC tones to notch, whether you record / transcribe, and operator notes. You manage them from
the app: **menu (☰) → "Audio room profiles…"**, or in code via
[`conf_pipeline_control.room_profile.AudioRoomProfile`](../conf_pipeline_control/room_profile.py).

## 2. What it stores
| section | fields |
|---|---|
| top | `name`, `device`, `sampleRate`, `channels`, `createdAt`, `updatedAt`, `notes` |
| `calibration` | `enabled`, `profilePath` (a saved capsule-calibration JSON), `summary` |
| `placement` | `resultPath`, `lastStatus`, `lastScore`, `detectedTonesHz`, `notchSuggestionsHz`, `hpfSuggestionHz`, `autoApplySuggestions` |
| `preNrCleanup` | `enabled`, `hpfHz`, `notchesHz` |
| `egress` | `cleanMono48k`, `asr16k`, `wavRecording`, `externalSink` |
| `transcription` | `enabled`, `provider`, `sampleRate`, `vadEnabled` |
| `safety` | `dfn3ForcedOn`, `dereverbForcedOn`, `placementSuggestionsAutoApplied`, `realAsrNetworkCall`, `virtualMicDriverBundled` (all default **false**) |

## 3. What it does **not** do
- It does **not** apply anything to the running audio engine (loading is preview + validate only).
- It does **not** enable calibration, pre-NR, DFN3, dereverb, or transcription.
- It does **not** auto-apply placement suggestions.
- It makes **no** network call and bundles **no** virtual-mic driver.
- The `safety.*` flags are descriptive: a `true` value means "this profile would change a safe default",
  and `validate()` **warns** about it. The model never sets one true itself.

## 4. How to create a profile
In the app: **menu → "Audio room profiles…" → New**, type a **Profile name**, then **Save…**. In code:
```python
from conf_pipeline_control.room_profile import AudioRoomProfile
p = AudioRoomProfile(name="Conference Room A - AC On", sample_rate=44100.0, notes="2026-06")
p.save("rooms/conf_a.json")
```

## 5. How to attach calibration
Reference a saved capsule-calibration profile (from `scripts/calibrate_capsules.py`). Attaching is **not**
enabling:
```python
p.attach_calibration("polaris_cal.json", summary="8ch @44100, ref 0")
# p.calibration.enabled stays False — turning it on is a separate, explicit step later.
```

## 6. How to attach a placement result
Reference a saved placement JSON (from `scripts/check_placement.py`) and copy its findings into the
profile **draft**. In the app: **"Copy placement suggestions…"** and pick the placement JSON. In code:
```python
from conf_pipeline_control.placement import PlacementResult
p.copy_placement_suggestions(PlacementResult.load("placement_a.json"), result_path="placement_a.json")
```
This fills `placement.*` and the `preNrCleanup` notches/HPF **draft** — but leaves `preNrCleanup.enabled`
False and `placement.autoApplySuggestions` False.

## 7. How to store HPF/notch suggestions
Set them directly or via `copy_placement_suggestions` (§6):
```python
p.pre_nr_cleanup.hpf_hz = 120.0
p.pre_nr_cleanup.notches_hz = [102.0, 140.0, 177.0]
# stored for THIS room; enabled stays False — nothing is forced on.
```
When you later (separately, explicitly) decide to use them, feed them to the engine via Phase 2:
`cc.build_pre_nr_bands(hpf_hz=p.pre_nr_cleanup.hpf_hz, notches=p.pre_nr_cleanup.notches_hz)`.

## 8. How to import / export
- **Export** writes the current profile JSON to a location you choose (app: **Export…**; code: `p.save(path)`).
- **Import** loads + previews + validates a profile from any location (app: **Import…**; code:
  `AudioRoomProfile.load(path)`); then **Save…** to keep it in your profile store. Both round-trip the
  camelCase JSON losslessly. **Validate** shows warnings (missing referenced files, version/device/rate/
  channel mismatch, any unsafe flag).

## 9. Why settings are room-specific
Placement tones, calibration, and the useful cleanup differ per room — a different AC/fan has different
lines, and a different position has a different beam. **Measured notch frequencies must be re-measured
per room**; they are never global defaults. Keep one profile per room and re-run the placement check
when the room or layout changes.

## 10. Why loading does not auto-apply to the live engine
Auto-applying a stored profile could silently change the audio (force a cleaner on, push last room's
notches into this room) — the opposite of the measurement-first, opt-in principle. So a profile is an
inert document: it records your intent and is applied to the engine only by a **separate, explicit**
action (a deliberately later step). The window states this:

> *Profiles are room-specific. Loading a profile does not apply it to the running audio engine.
> Placement suggestions are not auto-applied. Measured notch frequencies must be re-measured per room.*
