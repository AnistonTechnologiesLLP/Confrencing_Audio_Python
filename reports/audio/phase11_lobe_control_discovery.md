# Phase 11 — Lobe Control · Discovery

> Gate before any code. Inspects the existing beamforming + GUI surface so Lobe Control **surfaces and
> reuses** what's there (no rewrites). Lobe Control is *not* capsule calibration: calibration aligns the 8
> raw MEMS channels (Phase 1); Lobe Control shapes the beamformer pickup pattern *after* that.

## 1. Beamformer classes
- **Design layer (pure stdlib)** `conf_pipeline_control/beamformer.py`: `delay_and_sum_weights` (`:139`),
  `superdirective_weights(…, loading=0.05)` (`:148`), `lcmv_weights` (`:194`), `design_from_bearings`
  (`:646`), `design_zone_beams`, `beam_pattern_azimuth` (`:264`), `analyze_lobes` (`:305`, returns a
  `LobeReport` with **`beamwidth_3db_deg`** `:296` — a *measured* output, not a control). Modes here:
  `MODE_DELAYSUM` (`:435`), `MODE_SUPERDIRECTIVE` (`:436`).
- **Live layer (numpy)** `conf_pipeline_control/polaris_beamformer.py` `PolarisBeamformer`: the real-time
  steered back-end. Modes (`:151–155`): `MODE_DELAYSUM`, `MODE_FRACDELAY`, `MODE_SUPERDIRECTIVE`,
  `MODE_MVDR`, `MODE_RTF_MVDR`. `self.mode` is fixed at construction (`:1177`); `_freq_domain()` (`:1559`)
  = superdirective/mvdr/rtf.
- **`BeamEngine`** (`beam_engine.py`): A/B wrapper = steered + grid `PolarisBeamformer` on one stream;
  exposes `set_steering` (`:231`) + `set_nulls` (`:224`) that delegate to `self._steered`.
- **`LiveBeamController`** (`live.py`): the zone / "Whole table" + auto-steer inner controller.
- **`AutoSteerController`** (`autosteer.py`): wraps a `LiveBeamController`, runs the DOA control loop.

## 2. DOA / auto-steer
- `doa.detect(…) → DoaResult` of `Detection`s (`doa.py:134/92/101`), band-limited 300–3800 Hz.
- `AutoSteerController` (`autosteer.py:75`): coverage **sectors** (live-settable property `:221/226`),
  `hold_cycles`, `reselect_deg`, `max_talkers`. The control thread picks the dominant in-sector talker and
  steers the inner controller. This is the **"Follow speaker"** behaviour — reuse as-is.

## 3. Seat-lock (already implemented, A/B engine path)
- GUI: `live_beameng_lockseat` combo (`live.py:681`, "Follow talker (DOA)" + per-seat) →
  `_on_beameng_lockseat_changed` (`:1979`) resolves `cp.seat_azimuth_for_array(config, array_id, seat_id)`
  → `e.set_steering(az)`; `set_steering(None)` resumes DOA-follow.
- Manual angle / map-click lock: `_beameng_locked_manual_az` (`:152`), `_on_canvas_click_live` (`:1940`)
  → `cp.azimuth_for_array_point(...)` → `set_steering`. Manual angle is "array-relative, 0° = the array's
  reference, clockwise" (`:697`).
- **Lobe Control will reuse these** (`set_steering` + the seat/point→azimuth helpers), not replace them.

## 4. Null-steering
- `PolarisBeamformer.set_nulls(bearings)` (`:1522`); `compose_nulls(…)` budget arbiter (`:556`);
  `auto_null` / **`auto_null_max=2`** (`:1113/1114`); `active_nulls()` telemetry (`:1552`, under
  `_state_lock`); `seat_null_azimuths(...)` (`seat_mapper.py:154`). GUI null controls already exist:
  `live_beameng_nullseats` ("Null the other seats"), `live_autosteer_zonecut`. **Max nulls is already
  bounded to 2 in the engine** — Lobe Control honours the same cap.

## 5. LIVE listening-mode dropdown
- `live_listening_mode` (6 modes: `follow`/`seat`/`table`/`clean`/`manual`/`twokit`),
  `_on_listening_mode_changed` (drives auto-steer / A-B engine checkboxes + per-mode card visibility; a
  pre-Connect facade). Phase 10 added the descriptive flow-summary. **Lobe Control must not break these**;
  it reflects the chosen mode (fixed / follow / lock-seat / whole-table) rather than adding a 7th mode.

## 6. "Calibrate front" (distinct from everything else)
- `live_front_offset` spin −180..180° (`:451`, "Rotate the array's azimuth-0 to your room/desk front") +
  `live_calib_btn` "Calibrate front (talk from the front, then click)" (`:541`) → `_live_calibrate_front`.
  This establishes the **azimuth-0 reference** (where "front" is). It is **not** capsule calibration and
  **not** lobe direction — Lobe Control's main angle is measured *relative to this front reference*.

## 7. Seat map / room geometry
- `conf_pipeline/seat_mapper.py`: `seat_azimuth_for_array(config, array_id, seat_id) → Optional[float]`
  (`:181`), `azimuth_for_array_point(config, array_id, point)` (`:200`), `seat_null_azimuths(…)` (`:154`),
  core `_array_relative_azimuth(pos, bearing_deg, target)` (`:147`). Array bearing set via
  `cp.set_array_bearing(config, device_id, bearing_deg)` (`api.py:487`). All **array-relative degrees**.
  Seats come from the room `SystemConfig`; a seat with no resolvable bearing returns **`None`** (must be
  handled — "Lock-to-seat handles missing seat safely").

## 8. Polar / lobe visualisation
- `Canvas` (`canvas.py:77`) draws sector **wedges** (`_wedge_path` `:604`, `_draw_wedge_2d` `:615`), DOA
  rays, halos, and a **solid steered arrow** (`live.py:2553`) + seat dots, fed by the LIVE-ops view
  (`:2524`). **But the Canvas is injected by MainWindow** (`self._canvas`, `:153`) → it is `None` in a
  headless `LivePanel`, so it can't anchor offscreen tests. `cc.beam_pattern_azimuth` exists but there is
  **no embedded polar plot**. → Lobe Control adds a **minimal, self-contained `LobePreview` widget**
  (top-down array + main-lobe wedge + null line + seat dot, clearly labelled "preview"), and optionally
  feeds the Canvas overlay when present.

## 9. GUI test patterns
- Two kinds: **MainWindow/`win` fixture** (`test_gui_{calibrate_front,coverage,live_seat,smoke,twokit}.py`)
  — **hangs headless on this box → CI-only**; and **`LivePanel(AppState())`-direct** (offscreen, runs
  locally — `test_gui_listening_profiles.py`, `test_gui_calibration_apply.py`). Lobe Control's GUI test
  (`test_gui_lobe_control.py`) will be **LivePanel-direct** (`QT_QPA_PLATFORM=offscreen`, `importorskip
  PySide6`). Model tests are pure stdlib.

## 10. Units — degrees (array-relative azimuth)
- `set_steering(azimuth_deg)` takes **degrees**; convention azimuth **0° = +Y, clockwise** (`atan2(x, y)`),
  array-relative (0° = the front reference from §6). Seat/point→angle helpers return degrees or `None`.
  → Lobe `mainAngleDeg` is **degrees in [−180, +180]** (matches `live_front_offset`), normalised by the
  engine. Internally a direction becomes a steering vector inside the weight solver; the **operator-facing
  unit is degrees**, with seat-id as an alternative that resolves to degrees.

## 11. Beam width / focus — mode + loading, NOT continuous beamwidth
- There is **no continuous beamwidth knob**. Two real, existing levers:
  1. **mode**: `delaysum` (broad, robust) vs `superdirective`/`mvdr`/`rtf_mvdr` (directive).
  2. **loading** (diagonal loading) — the existing **`live_robust` slider** (`:359`, 0–100, default 60 ≈
     0.05 loading, live via `_on_live_loading_changed`): higher loading = more robust/**broader**, lower =
     more directive/**narrower** (but more self-noise).
- → Width presets map honestly to **(beam_mode + loading)**, reusing the existing capability:
  **Wide** = robust/broad (high loading), **Medium** = current default, **Narrow** = directive (low
  loading). Honest note in the UI: *"focus presets (mode + robustness), not a continuous physical
  beamwidth."* `analyze_lobes().beamwidth_3db_deg` can be shown as an *estimated* −3 dB width in the
  preview, clearly labelled estimate.

## 12. Safe rebuild / live update (no audio-thread block)
- **Live, lock-free:** the beam weight plan `_W` is published by a **single atomic array assignment**
  (`:671`) and snapshotted once per block (`:763`); `set_steering`/`set_nulls` mutate control-side state
  under `_state_lock` and bump a **`_steer_gen` epoch** (`:1321/1511`) to invalidate in-flight DOA ticks.
  → **Direction + nulls + loading update LIVE** (no reconnect, no callback lock). The UI must **debounce**
  the angle dial/slider (don't call `set_steering` every tick) and must not recompute geometry per tick.
- **Construction-time:** `self.mode` (beam_mode) is fixed at build → a **width change that crosses modes**
  (e.g. delaysum↔superdirective) **applies at Connect**; use the existing reconnect path
  (`_live_reconnect()` added in the calibration step: `_live_disconnect()` + `_live_toggle_connect()`).
- Calibration status for the warning = `self._calibration_path is not None` (added in the calibration
  step). **Placement status is NOT held by the live panel** (no placement state in `live.py`) → the lobe
  warning takes placement status as an **explicit input** (`set_lobe_placement_status(...)`, default
  unknown ⇒ no warning); `warnings()` is a pure function of `(calibration_on, placement_status)`.

---

## Plan (no surprises; default-OFF, reuse-not-rewrite)
1. **Model** `conf_pipeline_control/lobe_control.py` — `LobeControl` (+ `LobeNull`): version/enabled/mode/
   `main_angle_deg`/`beam_width`/`beam_mode`/`target_seat_id`/`auto_steer`/`nulls`/`safety`
   (`max_nulls=2`). `validate()` (clamp angle to [−180,180]; bound nulls to `max_nulls`; width ∈
   {wide,medium,narrow}), `summary()`, `warnings(calibration_on, placement_status)`, camelCase JSON,
   `default_lobe_for_mode(mode, *, target_seat_id=None)`.
2. **Listening profiles** — add `beam_width` to `LpSpatial` (default `"medium"`); built-ins set it per
   mode (table=wide, follow=medium, seat=medium, etc.); a mode→LobeControl mapping. *Manual = user's
   toggles (no override); Whole table never forces narrow; Lock-to-seat tolerates a missing seat.*
3. **GUI** — a compact "Lobe Control" Card in the LIVE panel: main direction (manual angle dial + seat
   combo), width preset (wide/medium/narrow, honest note), suppress-direction (off/angle/seat, ≤2,
   "reduces not mutes" warning), a mode read-out (fixed/follow/lock-seat/whole-table reflecting the
   listening dropdown), a **summary** label, a minimal **`LobePreview`** widget, and calibration-OFF /
   placement-BAD warnings. Direction/nulls/loading apply **live (debounced)** via the active steered
   engine's `set_steering`/`set_nulls`; width-mode at Connect. No new listening mode; existing controls
   untouched.
4. **Tests** — `test_lobe_control.py` (model 1–13), `test_gui_lobe_control.py` (GUI 14–17, LivePanel-
   direct), and confirm room-profile / operator / listening tests still pass (18–20).
5. **Docs** — `LOBE_CONTROL_GUIDE.md`, `phase11_lobe_control_report.md`; update listening/operator guides
   + tracker.

## Out of scope (honest)
No perfect audio fencing / "soundproof" claims; no continuous physical beamwidth; no new DSP math; no
engine/CLI/calibration default changes; no auto-applied placement notches; no removal of existing
controls; no 7th listening mode; no push/merge.
