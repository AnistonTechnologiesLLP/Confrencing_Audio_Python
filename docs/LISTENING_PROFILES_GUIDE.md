# Listening Processing Profiles Guide

Phase 10 of the audio front-end hardening. A **Listening Processing Profile** is a *descriptive* recipe
for what the live pipeline does in each "Listening mode". The model itself is deliberately **inert**: it
puts the dropdown's behaviour into plain words so an operator can *see* the processing flow — it changes
no DSP and applies nothing.

Phase 10 also flips the LIVE panel's cleanup **checkbox defaults** to the recommended chain, so a fresh
session ships with sensible cleaning pre-ticked (a GUI-default change only — engine/CLI/library defaults
stay OFF). The built-ins below describe those recommended defaults. See §3 for the model-vs-GUI split.

It is a different thing from an **Audio Room Profile** (Phase 9). Keep both in mind:

| | Audio Room Profile (Phase 9) | Listening Processing Profile (Phase 10) |
|---|---|---|
| What it is | A saved, room-specific **setup record** (which calibration, which HVAC notches, egress prefs) | A **processing recipe** for a live listening mode |
| Scope | One physical room | One way of listening (a dropdown mode) |
| Storage | A JSON document you save/load per room | Built-in, one per mode (Manual is derived from your live toggles) |
| Both are | **Inert** — neither one touches the running engine | **Inert** — describes only; the real chain is still fixed at Connect |

---

## 1. What a Listening Processing Profile is
A description of the live processing chain for one LIVE **Listening mode** — its spatial choice (fixed
zones / auto-steer / lock-seat / two-kit automix), whether cleanup stages are on, and an honest
**processing-flow summary** plus any operator notes. In code it is
[`conf_pipeline_control.listening_profile.ListeningProfile`](../conf_pipeline_control/listening_profile.py);
the built-ins are `BUILTIN_LISTENING_PROFILES` and you fetch one with `listening_profile_for_mode(...)`.

## 2. The six built-in profiles (one per dropdown mode)
The dropdown is unchanged — same six modes, same order. Each maps to one built-in profile:

The built-ins describe the LIVE panel's **recommended** defaults (the cleanup the GUI now ships pre-ticked):

| Dropdown mode | Profile `id` | Spatial | Denoise | Dereverb | AGC | Taps |
|---|---|---|---|---|---|---|
| Follow the room (auto-steer) | `follow_the_room` | auto-steer | **OM-LSA** | **ON** (recommended) | **ON** | **ON** |
| Lock to a seat | `lock_to_seat` | lock seat | **OM-LSA** | OFF | **ON** | **ON** |
| Whole table | `whole_table` | fixed beam | **OFF** (no denoiser on this path) | OFF | **ON** | OFF |
| Clean audio (hands-off) | `clean_audio` | auto-steer | **OM-LSA** | **ON** (recommended) | **ON** | **ON** |
| Manual (advanced) | `manual` | your toggles | your toggles | your toggles | your toggles | your toggles |
| Two kits (combined room) | `two_kits` | automix | **OM-LSA** (per kit) | OFF | **ON** (combined) | OFF |

e.g. Clean audio's flow is `… → auto-steer ON → transient ON → dereverb ON → … → denoise OM-LSA → AGC ON → … → output`.

Three honesty rules the built-ins encode, matching the real shipped defaults:
- **The recommended cleanup is ON by default** — AGC on every mode, plus the OM-LSA denoiser and
  tap-suppression on the steering paths (Follow / Lock-to-seat / Clean / Two-kits). **Dereverb is the
  exception: it is recommended ON only for Follow / Clean audio** (where a naturalness warning shows) and
  stays OFF everywhere else — it is never a global default, because it can colour a dry room. The base
  "Whole table" path has no denoiser, so its denoise stays off.
- **"Clean audio" uses OM-LSA, never DeepFilterNet3** — the recommended cleaner is the natural-sounding
  OM-LSA; DFN3 stays a manual choice.
- **AEC and the voice gate stay OFF** (opt-in): AEC needs a far-end reference; the gate can clip soft
  speech. Recommended-on is *not* everything-on.

## 3. Descriptive model vs. the GUI defaults
Two separate things ship together here, and it's worth keeping them straight:
- **The ListeningProfile model is descriptive** — it changes no DSP and applies nothing; the real chain
  is still fixed when you **Connect**. The summary is words.
- **The LIVE panel's checkbox defaults changed** — the recommended cleanup is now **pre-ticked** (see §4),
  so a fresh session Connects with it on. This is a **GUI-default** change only.

What neither one does:
- The **engine / CLI / library defaults are unchanged** — constructing a `PolarisBeamformer` /
  `BeamEngine` / `LiveBeamController` in code (or via the CLI) still defaults every cleaner **OFF**. Only
  the GUI checkboxes are pre-ticked. The byte-identical engine tests are untouched.
- It does **not** add, remove, rename, or reorder any listening mode or any control — every recommended
  toggle can still be unticked before Connect.
- It does **not** turn on DFN3, AEC, the voice gate, or pre-NR. "Clean audio" uses OM-LSA.
- It does **not** promote room-specific tones (HVAC notches, a room's calibration) to global defaults —
  those belong to the placement check or an Audio Room Profile, and the summary says so.
- It does **not** touch, replace, or rename the **Audio Room Profile Manager** (Phase 9), which stays
  exactly as it was.

## 4. Where you see it (GUI)
On the **LIVE** panel, directly under the "Listening mode" dropdown, a read-only **flow summary** label
shows the selected mode's profile name, its one-line processing flow, and any notes — ending with
*"Descriptive only — applied when you Connect (nothing changes now)."* Picking a different mode updates
the text. It is selectable for copy/paste and never alters a control.

The recommended cleanup is **pre-ticked** on a fresh LIVE panel: global "Normalize loudness (AGC)" on the
Hardware card; "Suppress steady noise" (OM-LSA) + "Suppress taps / knocks" on the A/B-engine and
auto-steer cards; and per-kit OM-LSA + combined AGC on the Two-kits card. **Dereverb is NOT pre-ticked
globally** — the global "Reduce room echo (dereverb)" checkbox stays OFF, and dereverb is auto-enabled
(on the auto-steer path's own checkbox) only when you pick **Follow the room** or **Clean audio**. Every
one of these is a normal checkbox — untick any of them before Connect to turn that stage off.

## 5. Using it in code
```python
from conf_pipeline_control.listening_profile import listening_profile_for_mode

prof = listening_profile_for_mode("clean")          # the "Clean audio (hands-off)" built-in
print(prof.name)            # 'Clean audio (hands-off)'
print(prof.flow_summary())  # capture → preamp → … → denoise OM-LSA → AGC ON → … → output
print(prof.warnings())      # honest notes (AI cleaning latency, room-specific notches, …)

# Manual mode reflects your live toggles, it does not override them:
flags = {"post_nr": True, "post_nr_engine": "omlsa", "agc": True, "auto_steer": True}
manual = listening_profile_for_mode("manual", manual_flags=flags)
```
Profiles round-trip to camelCase JSON via `to_dict()` / `from_dict()` / `to_json()` / `from_json()` if
you want to persist or inspect one. They carry no live state and import no heavy deps (pure stdlib).

## 6. Relationship with Audio Room Profiles
An Audio Room Profile may optionally remember a **preferred** listening profile for that room via the
backwards-compatible `preferredListeningProfileId` field (e.g. `"clean_audio"`). It is a *preference
reference only* — it is never auto-applied, never changes a default, and old room-profile JSON without
the field still loads (the field defaults to `""`). See
[AUDIO_ROOM_PROFILE_GUIDE.md](AUDIO_ROOM_PROFILE_GUIDE.md).

## 7. Safety summary
- The ListeningProfile **model** is descriptive: no DSP change, no apply, no network call, no driver.
- The LIVE panel's **recommended cleanup is pre-ticked** (a GUI-default change): AGC + OM-LSA denoise +
  tap-suppression on the paths that have them. **Dereverb is NOT global** — its global checkbox stays OFF
  and it is auto-enabled only on the Follow / Clean auto-steer path. AEC and the voice gate stay off (opt-in).
- **Engine / CLI / library defaults are unchanged** — every cleaner still defaults OFF in code; the
  byte-identical engine tests are untouched. Only the GUI checkboxes changed.
- Every recommended toggle can be unticked before Connect; the dropdown still applies at Connect.
- The Audio Room Profile Manager is untouched.
