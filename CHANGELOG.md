# Changelog

Python port of the Conferencing Audio Pipeline. Format based on
[Keep a Changelog](https://keepachangelog.com/); versions originally tracked
the TypeScript project they were ported from. The JSON **config schema** is
camelCase, currently `CONFIG_VERSION` = 5 (v1–v4 files migrate losslessly);
the TS sibling is at matching v5 parity. The desktop app is presented as
**Aniston Room Designer**.

## [Unreleased]

### Fixed
- **Real-time DeepFilterNet3 (`post_nr_engine="dfn3"`) no longer distorts the voice.** Offline DFN3 (the
  New_OCTOVOX reference) sounds perfect; the live path was distorted by four separate, measured defects,
  now all fixed:
  - **The 44.1↔48 kHz streaming resampler was broken** (DFN3 is a 48 kHz model; the array runs at 44.1).
    The naive overlap-save reset the polyphase phase every block, drifted ~+90 samples/s from an
    integer-floor trim, and emitted the FIR's unsettled edge — dragging a round-trip to ≈−10 dB THD+N (vs
    −80 dB correct) and smearing the voice with broadband grit across the speech-formant band. Rewrote
    `_StreamingResampler` as a phase-coherent, settled-interior streamer with exact cumulative accounting:
    measured round-trip is now −67…−80 dB (matches a single-shot resample), drift-free.
  - **The loudness-restore makeup hard-clipped.** Every denoiser drops the talker, so `_LevelPreservingCleaner`
    boosts it back — but the RMS match ignored crest factor and the cleaner's ~49 ms latency, ratcheting a
    +6 dB overshoot that pushed peaks to **1.6–3.7× full scale**. Added an instant-attack/slow-release peak
    limiter (−1 dBFS) after the makeup and tightened the makeup cap 15→8 dB (the true level loss is <1 dB).
  - **The AGC re-clipped after the makeup.** `TargetLoudnessAgc` applied its RMS gain (up to +18 dB) with no
    output clamp, so boosting peaky cleaned voice pushed peaks to 3–5× full scale. Added the same peak
    limiter to the AGC output — it now guards the converter in every mode (single-array and 2-kit).
  - **DFN3 ran uncapped (musical noise).** `DEFAULT_ATTEN_LIM_DB` was 100 (≈unlimited), over-suppressing the
    noise floor ~20 dB harder than the offline reference and treating a quiet/overlapping second speaker as
    noise. Lowered to **32 dB** to match New_OCTOVOX's natural-voice default.
- **AGC now works in the single-array live modes.** The target-loudness AGC was only ever wired into the
  2-kit path; `LiveBeamController` (Follow / Lock-to-seat / Whole-table) had **no AGC stage at all**, and
  the A/B engine / combined-room paths never set a target — so "Normalize loudness" did nothing outside
  2-kit. Added a real `TargetLoudnessAgc` stage to `LiveBeamController` (after PEQ, AGC-frozen during a
  tap duck) + a global **"Normalize loudness (AGC)"** Hardware-card checkbox (ON by default, −20 dBFS) that
  feeds the zone / auto-steer / A-B / combined-room connects.
- **"Echo" in the room is dereverb, not AEC.** The "Cancel echo" toggle only removes *far-end loudspeaker*
  echo and needs the room speakers playing the far end + a loopback/Stereo-Mix reference (silent no-op
  otherwise, ERLE stays 0). Room reverberation of the local talker is handled by **dereverb**, which had
  no control in the "Whole table" zone mode — added a global **"Reduce room echo (dereverb)"** Hardware-card
  checkbox that applies to every live mode (OR'd with the per-mode dereverb toggles), and clarified both
  tooltips.

### Removed
- **LIVE panel: removed the "Capture everyone (all talkers)" listening mode + card and the
  "Clean via OCTOVOX (near-live)" card** from the desktop app (GUI controls only — the backend
  modules `multibeam` / `octovox_bridge` / `octovox_monitor` are untouched). The per-array **"Use"
  checkboxes still combine 2+ ticked arrays into one room capture** (the combine now uses built-in
  defaults instead of the removed card's beam-count / snap / record controls).

### Added
- **Table fence for the two-kit combined-room mode** (`conf_pipeline_control.fence`; opt-in, off by
  default) — when two POLARIS kits are running in "Two kits (combined room)" mode, the fence fuses
  the two independent bearings into an approximate 2D source position (`FenceDecider`: ray-cross /
  least-squares, `conf_pipeline_control/fence.py`) and keeps sources whose estimated position falls
  inside an operator-drawn table polygon while **vetoing** those that appear to originate outside it.
  The veto acts as an **output gate + selection veto** in `MultiKitController` — mode-agnostic (each
  kit can stay in its own delay-sum or other beam mode; **no null-steering** added in v1). The fence
  polygon is drawn live with a new freehand polygon tool and is **transient / live-only — never
  persisted and no schema change**.  Connection is refused with a clear error if fewer than exactly
  2 posed kits (array `bearingDeg` + position) are connected; an unposed array fails fast at Connect
  rather than silently.

  **Honest framing (important):**
  - **Loose-coupling fusion, not clock-synced triangulation.** The two arrays run on independent USB
    clocks with no inter-device timing; the position estimate is from asynchronous bearings, not a
    phase-coherent triangulation. Sharper sync triangulation is a deferred future upgrade.
  - **Soft fence, not a hard wall.** The ~40 mm aperture gives coarse bearings (~40–50° resolution);
    the fence uses a margin band + hysteresis to avoid flicker at the boundary — it keeps the
    conference table vs rejects a far-room source, not a surgical edge.
  - **Range disambiguation, not front/back resolution.** The POLARIS is a circular array (no
    front/back ambiguity). The fence's value is disambiguating a near table-talker from a far
    room-source on the same bearing, not resolving front vs back.
  - **Spacing and angle caveat (deployment-critical).** The two kits must be spaced apart **and
    not directly facing each other along the talker line** — if both arrays face the same table
    from exactly opposite sides, their bearing-rays become anti-parallel (degenerate triangulation)
    even when well-spaced; position estimates are unreliable in this geometry. Corner-place or
    angle the kits so the two rays cross at a non-degenerate angle. Near-parallel rays fall back to a
    level cross-check. Degenerate geometry degrades gracefully rather than silently misbehaving.
- **Voice-focus DSP suite — parametric EQ, tap suppressor, "voice only" gate, and a door / out-of-area cut.**
  Four new opt-in real-time stages on the live beam (all OFF by default, lazy-numpy, realtime-safe; the live
  chain is now `preamp → beam → AEC → transient → dereverb → noise-reduce → PEQ → AGC → band-limit → voice-gate`):
  - **Parametric EQ (`conf_pipeline_control.peq.StreamingPeq`)** — a live 4-band RBJ-biquad cascade
    (bell / low|high shelf / high|low pass) on the cleaned output, reusing the shared PEQ model. A LIVE
    "Tone — parametric EQ" card (tweakable while connected) + a one-click **"Hum notch (50 Hz)"** preset that
    notches mains hum + its in-band harmonics (50/100/150/200 Hz) — more transparent on tonal hum than the
    broadband cleaner. CLI: `area_autosteer.py --peq "freqHz:gainDb:q:type,…"` / `--hum-notch`.
  - **Table-tap / transient suppressor (`conf_pipeline_control.transient.StreamingTransientSuppressor`)** —
    a temporal de-thump that ducks impulsive knocks/taps (structure-borne, so spatial nulls can't catch them)
    while **preserving speech plosives** (a short-lookahead "burst-followed-by-voicing ⇒ keep / decays ⇒
    duck" classifier). Freezes the output AGC while ducking so it doesn't chase the dip. GUI "Suppress taps /
    knocks" on the A/B-engine + auto-steer cards.
  - **"Voice only" gate (`conf_pipeline_control.voice_gate.VoiceOnlyGate`)** — duck the output when the sound
    isn't speech (gaps between phrases, paper rustle, steady hum), reusing the syllabic-modulation
    speech-presence scorer. Onset-safe: fast attack + a shallow floor (a duck, never a hard mute) so the first
    syllable is never clipped. GUI "Mute non-speech (gate gaps & noise)". *It does NOT remove a competing
    human voice inside the pickup zone (that's speech) — only the spatial zone-nulling does.*
  - **Door / out-of-area cut in auto-steer** — the auto-follow mode can now actively null your **No-pickup
    (door) zones** and drop any talker whose direction is **outside your pickup areas / who left their seat**
    (`conf_pipeline.seat_mapper.exclusion_zone_azimuths` / `azimuth_in_pickup_zone`; a third **exclusion tier**
    in `compose_nulls`, ranked *detected interferer > user-drawn door > speculative empty seat*). GUI "Cut the
    door & anyone outside the pickup area". *Needs the array bearing + drawn zones; it can't cut someone who
    STANDS UP in place — that changes elevation, not direction, which a planar array can't see.*
- **Multi-array room capture — combine N POLARIS kits into one room-wide "capture everyone"**
  (`conf_pipeline_control.multiroom.MultiRoomController` + `RoomKitSpec`; a dynamic add/remove **kit list
  in the LIVE Hardware card**; `conf_pipeline.seat_mapper.seats_owned_by_array`) — add ≥2 kits (each its
  own input device) and several arrays cover a whole room at once: one combined feed + a per-person track
  for every talker in the room. Each kit runs the single-array `MultiBeamController`, restricted to the
  **seats it owns** (each seat → its nearest array, `seats_owned_by_array`) so the *same* voice is captured
  by one kit (best SNR) and never summed twice; the kits' feeds are combined **volume-domain** with
  number-of-open-mics attenuation (`nom_automix`) — because N POLARIS = N independent USB clocks, no
  cross-kit sample alignment is possible, so this sums (like the 2-kit cross-fade) rather than jointly
  beamforming. Mirrors the dual-kit realtime invariants (copy-on-write tap, watchdog, ONE combined AGC +
  master mute/gain, one output stream, distinct-device guard) and collects a per-person WAV per kit
  (namespaced by array) + the room feed. The single-array "Capture everyone" is unchanged for 0–1 kits.
  **Honest limit:** clean ownership needs snap-to-seats ON + every array posed + room seats; otherwise the
  combine is best-effort (an overlapping talker may be double-captured) and says so. (+12 tests.)
- **Capture everyone — simultaneous multi-talker automix + per-person tracks**
  (`conf_pipeline_control.multibeam`: `MultiBeamController` + `MultiTrackRecorder`; `scripts/capture_everyone.py`)
  — instead of committing to one dominant talker, this forms **several beams at once** (one per active
  talker) and automixes them into a single combined feed, **and** records a separate WAV track per person.
  Talkers are detected by DOA and **snapped to defined room seats** for a stable, jitter-free aim (hybrid —
  free DOA where no seat is near; reuses `conf_pipeline.seat_mapper`). Each beam steers to its talker while
  **nulling the others** (multi-look LCMV over a shared FFT — N beams cost ~one), is gated by the fan-proof
  `SpeechPresenceScorer`, and is mixed with **NOM** (number-of-open-mics) attenuation so simultaneous mics
  don't stack their noise floors; the mixed feed gets the target-loudness AGC. Persistent **beam slots**
  (matched by seat then bearing, with hold) keep each track on the same person across brief pauses. The
  `MultiTrackRecorder` writes one mono WAV per beam (named by its seat) plus the mixed feed. Validated live
  on the kit (engine streams cleanly; idle room → graceful silence). **Honest limit:** the ~40 mm 8-mic
  array separates **2-3 well-spaced talkers** (>~40-50° apart) — closer people merge into one beam. Pure
  planner + mixer + recorder are fully hardware-free tested (+29 tests). GUI "Capture everyone" mode to follow.
- **Mic-input preamp — manual level trim** (`conf_pipeline_control.preamp.InputPreamp` + the `PreampHost`
  mixin; `preamp_gain_db` on every live back-end + `set_preamp_gain_db` on each live control surface; a
  "Mic input" card in the LIVE panel; CLI `--preamp-gain-db` on the live scripts) — a uniform software gain
  applied to **all capsules at the front of the chain, before the beamformer**. **Off by default (0 dB)**, so
  the pipeline is byte-identical when unused. **Spatially neutral**: a uniform input scalar scales the array
  covariance by `g²` and leaves DOA and the trace-relative-loaded MVDR/LCMV beam unchanged (guarded by a
  ×0.01–100 sweep test). Deliberately **honest about what it is** — a level trim for input metering / a healthy
  operating level into level-sensitive DSP, **not an SNR improvement**: a post-ADC software gain scales signal
  and noise together and the output AGC re-levels it. A hardware-gain probe on the real POLARIS confirmed its
  capture endpoint exposes only a **cut-only digital volume** (−96…0 dB, no boost above unity), so there is no
  analog input gain to drive — hence software-trim-only, and no auto/hardware stage. (+24 tests.)
- **Real-time DeepFilterNet3 voice cleaning** (`post_nr_engine="dfn3"`;
  `conf_pipeline_control.deepfilter_cleaner.StreamingDeepFilter`) — the strongest cleaner now runs **live on the
  audio thread**, not just offline. DeepFilterNet3's official package can't build on this host (Python 3.14 / no
  Rust toolchain), so it runs a one-time **TorchDF → self-contained streaming ONNX** export via **ONNX Runtime**
  (the new `[dfn]` extra; the 16 MB model is bundled, git-LFS-tracked). Raw 10 ms frame in → cleaned frame out +
  carried model state, wrapped in a streaming 44.1↔48 kHz resampler and fixed-latency priming (~60 ms total),
  **warmed at Connect** so the first block never stalls the stream, and **realtime-safe** — it passes the raw
  voice through on prime / underrun / any error (never silence, never a throw) and falls back to the light gate
  if the model is missing. Selectable in all three cleaner pickers (A/B engine, auto-steer, 2-kit). (+7 tests.)
- **Level-preserving voice cleaning + a "Cleaning amount" dial** (`_LevelPreservingCleaner`;
  `post_nr_preserve_level` on by default, `post_nr_amount`) — every denoiser strips noise energy and drops the
  **talker** ~5-7 dB with it, so the cleaned voice came out **weak / muffled**. A shared, engine-agnostic
  **makeup** now restores the speech-region level the cleaner removed — slewed, speech-gated (uses the VAD's
  `noise_gate`), **held through pauses** so it never re-pumps the noise floor, and boost-only. Measured on a real
  conference recording: **SNR-neutral** on a clean room and **SNR-improving** in noise, with the talker's level
  fully restored (the gate / OM-LSA / DeepFilterNet3 all). The **Strength** combo now doubles as a
  **cleaning-amount** dial (Light / Medium / Full) that blends the original voice back in for a gentler,
  less-muffled result; default **Medium**. Applies on the A/B engine, auto-steer and 2-kit paths; CLI
  `--post-nr-amount` / `--no-post-nr-preserve-level`. (+9 tests.)
- **Guided first-run setup** (a "Getting started" checklist in the LIVE panel; `conf_pipeline_gui.panels.first_run`)
  — the onboarding parity piece. A compact, dismissible banner walks a first-time user through getting audio out
  of the array — **pick how to listen → connect → check capsules → calibrate the front → hear it** — ticking each
  step off as they do it with the **real controls** (it never re-runs DSP). Shows once (a stable QSettings flag),
  is re-openable from the menu ("Show LIVE getting-started"), and is honest about ordering and hardware: optional
  steps never block completion, irrelevant steps auto-skip per mode (no front-calibration for "Whole table"), and
  a no-hardware / simulation run can still finish (manual "Got it" fallback for the meter). All gate logic lives
  in a **pure, Qt-free step model** (`GuideSnapshot` + `step_done`/`active_step`/`required_done`/`progress`) so
  it's fully unit-testable headless — the two classic traps are designed out (the mode step keys off an explicit
  *touched* flag, not the "table" default; calibration keys off a success flag, never `front_offset != 0`).
  (+14 tests.)
- **Commissioning / as-built report** (`conf_pipeline.commissioning_report` + `CommissioningInfo`; an
  "Export commissioning report…" menu action) — the integrator-deliverable moat. Layers the measured **live
  state** onto the existing as-built design report: the honest **estimated latency** (`~N ms`, framed against
  the ≤ 150 ms target), the active **AI cleaning** stages, the **AEC reference + ERLE**, and the **A/B
  noise-proof** headline ("N dB quieter"), plus **capsule health** (silent-capsule list) and **front
  calibration**. Ends with a **derived pass/fail sign-off checklist** (room defined, coverage, AEC refs, no
  config errors, latency-in-target, noise-reduction-verified, capsules-live — each computed, never assumed) and
  a hand-signed acceptance form. Markdown or HTML, escaped; pure over `(config, info)` so it's fully testable —
  the GUI snapshots the running engine into `CommissioningInfo`, the library never reads a clock or a device.
  (+11 tests.)
- **A/B proof & measurement tool + live latency read-out** (`conf_pipeline_control.ab_capture.ABCapture`;
  a "Capture A/B proof (raw vs cleaned)" button in the LIVE transport; `estimated_latency_ms`) — the
  transparency moat: capture the beamformed mono **raw vs cleaned simultaneously** (a tap in `process_block`
  feeds the pre-cleaner and post-cleaner mono from the *same* audio), measure how much **quieter the
  background got** (noise-bed dB, + broadband RMS, + ERLE when AEC is on), and **one-click export** both clips
  + the numbers (`ab_raw.wav` / `ab_clean.wav` / `ab_proof.txt`). Turns "trust our AI cleaning" into proof an
  integrator can run in the customer's own room. Works on any live beam (A/B engine / auto-steer / zone);
  reuses the `ab_test` WAV/`rms_db` pattern. Plus an honest **estimated end-to-end DSP latency** read-out
  (`~N ms`, summed from the active stage frames) next to the live ERLE. (+13 tests.)
- **Live acoustic echo cancellation (AEC)** (`conf_pipeline_control.streaming_aec.StreamingAec` +
  `reference_capture.ReferenceCapture`; `aec` knob on `PolarisBeamformer` / `LiveBeamController` /
  `AutoSteerController`; GUI **"Echo cancel"** toggle in the A/B-engine and Auto-steer cards + a live ERLE-dB
  readout) — closes the headline gap from the Shure analysis (AEC was previously planning-level only). A
  frequency-domain partitioned-block NLMS (ported from OCTOVOX `aec_partitioned`) cancels the room's
  loudspeaker echo on the beamformed mono using a **far-end reference** captured automatically (WASAPI
  loopback → Stereo Mix → manual device), mono-downmixed + resampled to the engine rate. Runs at the
  post-beam seam **before** dereverb (AEC → dereverb → denoise → AGC); adapts on a far-end-activity gate and
  stays bounded via a leaky, magnitude-clipped update; clean pass-through (never fabricates cancellation) when
  no reference is delivered. **Off by default** (it only helps when the room plays far-end audio through
  speakers). Pure numpy on the audio thread. **Limits (documented):** no bulk-delay / clock-drift
  compensation or true double-talk detector yet, and WASAPI loopback needs a sounddevice/PortAudio build that
  supports it (else Stereo Mix must be enabled). Hardened by a 16-agent adversarial review. (+21 tests.)
- **Real-time dereverberation on the live output** (`conf_pipeline_control.streaming_cleaner.StreamingDereverb`;
  `dereverb` knob on `PolarisBeamformer` / `LiveBeamController` / `AutoSteerController`; GUI **Dereverb** toggle
  in the A/B-engine and Auto-steer cards) — a causal port of OCTOVOX's fast spectral late-reverb suppressor
  (Lebart 2001 / Habets), bringing dereverb from the offline OCTOVOX path into the live chain. It estimates the
  late-reverb power as a delayed, T60-decayed, one-pole-smoothed copy of the observed power and applies a
  spectral-subtraction gain (`G = max(1 − β·R/P, Gmin)`), running at the post-beam seam **before** the noise
  reducer (dereverb → denoise → AGC). A drop-in for `_PostNoiseSuppressor` (reuses its overlap-add STFT,
  warmup-passthrough and process/reset lock); pure numpy, gain floored so it only removes reverb energy.
  OFF by default. (+9 tests.)
- **AI-story naming** — the live voice cleaner is now surfaced as **"AI voice cleaning (OM-LSA)"** in the GUI
  (A/B-engine "Cleaner" and Auto-steer "Clean voice" pickers); tooltips keep the technical OM-LSA / OCTOVOX
  detail. No behaviour change.
- **Measure the steered beam's null depth on a 2nd source** (`conf_pipeline_control.measure_null_depth`,
  `NullDepthReport`) — beamforms a raw 8-ch clip BOTH ways (look-only vs look+LCMV-null) and reports the dB
  the null drops energy from the interferer direction, plus whether the talker at the look is preserved.
  The honest *spatial* figure (a few dB on the ≈40 mm POLARIS — the single-channel cleaner does the deep
  cut) for the talker+interferer A/B. Pure offline math (reuses `apply_design_offline`); hardware-free and
  deterministic. (+2 tests.) Note: live capture from POLARIS still needs the engine's own input stream —
  `record_clip`/`sd.rec` can't open these WDM-KS/DirectSound endpoints at 8 ch.

## [1.18.0] - 2026-06-17

Theme: **manual aiming, a visual-polish pass, and real-time noise suppression** (incl. the OCTOVOX
OM-LSA cleaner) on the live POLARIS path — all on top of the v1.17.0 room-aware steering. No schema
change (stays `CONFIG_VERSION` 5; TS sibling already at parity).

### Added
- **OCTOVOX voice cleaner on the real-time output** (`conf_pipeline_control/streaming_cleaner.py`
  `StreamingCleaner`; LIVE A/B card **"Cleaner"** picker; `post_nr_engine` knob; `--post-nr-engine` CLI) —
  brings OCTOVOX's *cleaning* to the live path. The conferencing engine already beamforms the 8 capsules to
  one mono voice in real time, so only the **single-channel noise reduction** is ported: a new post-beam
  engine that runs OCTOVOX's decision-directed **OM-LSA** denoiser (Ephraim–Malah / Cohen 2003) frame-by-frame
  at the existing `post_nr` seam. It's a **drop-in** for the light spectral gate — same
  `process(block, noise_gate)`/`reset()` contract, same overlap-add + minimum-statistics floor — but swaps the
  single-pole Wiener for the OM-LSA log-spectral-amplitude gain with a per-bin speech-presence floor, which is
  more natural and stronger on non-stationary noise. Runs **on the audio thread** at 44.1 kHz (~12 ms added,
  comfortably inside a ~100–150 ms conferencing budget); pure numpy, with a vendored `_exp1` exponential-integral
  approximation standing in for `scipy.special.exp1`. Select it in the GUI "Cleaner" combo (**OCTOVOX cleaner
  (OM-LSA)**, the default when the noise reducer is on; **Light gate (fast)** keeps the old behaviour), or via
  `post_nr_engine="omlsa"|"wiener"|"gate"`. DeepFilterNet3 is deliberately **not** in the live path (it needs
  48 kHz + torch and has no frame-streaming API), so it stays an offline / out-of-process path.
  **The cleaner applies to the auto-steer path too**, not just the A/B engine: `LiveBeamController` (which
  `AutoSteerController` wraps) gained the same `post_nr`/`post_nr_engine` knobs and runs the reducer on its
  beam output, and the **Auto-steer** section now has its own **Clean voice** (Off / OCTOVOX cleaner / Light
  gate) + **Strength** controls. (+25 tests.)
- **Suppress steady fans / AC from the real-time output** (`PolarisBeamformer` post-NR + LIVE A/B card) —
  the post-beam noise suppressor now learns the steady background by **minimum statistics** (the per-bin
  running minimum of the smoothed power over a ~0.7 s window) instead of a VAD-gated EMA, so it removes
  always-on fan/AC/HVAC hum **without needing silence**. The old gate trained only on VAD-flagged silences,
  so it never learned a steady *directional* source the DOA mistakes for a talker — which is why a fan
  wouldn't go away. Speech is preserved inherently (it sits above the learned floor) and the bounded Wiener
  gate never hard-mutes. Surfaced in the LIVE **A/B engine** card: **"Suppress steady noise (fans/AC)"**
  (`post_nr`) with a **Gentle / Medium / Aggressive** depth, plus **"Adaptive null (learn room noise)"**
  (data-adaptive `mode="mvdr"` + `auto_null`) to spatially null a *directional* fan/duct. `cc.MODE_MVDR` /
  `MODE_FRACDELAY` are now exported; `post_nr_minstat=False` (`--no-post-nr-minstat`) keeps the legacy gate.
  (+5 tests.)
- **Visual polish pass (all modes)** — a styling/consistency pass with **no functional or layout change**:
  consolidated the design-token layer (spacing/type/canvas-colour tokens in `theme.py`, de-duped issue
  colours, an AA contrast bump on the dimmest text); a **danger** button variant so destructive actions
  (Delete / Remove / Disconnect) read as destructive; and additive LIVE-state cues — a **prominent output
  meter** (peak-hold + clip + green/amber/red zones, replacing the flat bar), a distinct **solid
  steered/locked-direction arrow** on the room map (vs the dashed talker DOA), a **hardware-limit (i)** chip
  (azimuth-only / ~5.6 kHz / ~40–50° merge / planar), and **disabled-with-reason** hints on Mute/Gain.
  (+12 tests.)
- **Set a microphone array's room bearing in Design** (`conf_pipeline_gui/panels/design.py`) — the array's
  mounting heading (`bearing_deg`, 0° = +Y) is now editable from the **Design** properties panel (a **Bearing
  (°)** spin, like cameras/loudspeakers but without Tilt — the array is planar), routed to
  `set_array_bearing`. This was the missing prerequisite that made the room-aware features inert from the
  app: snap-steer ("Lock to seat"), seat-nulling, the live seat readout, and **click-to-aim** all need the
  array bearing, but it was only settable via the API. Now it's a one-field setup in Design. (+1 test.)
- **Lock the listening direction to a manual place** (`conf_pipeline.azimuth_for_array_point`, LIVE panel) —
  besides "Lock to seat", the steered POLARIS beam can now be pinned to **any direction**: a **"Manual angle"**
  entry in the lock picker reveals a degrees dial (0–360°, a compass that wraps), and **clicking a spot on the
  2D room map** aims the beam there. Both drive the same pin — a map click computes the point's array-relative
  azimuth (new `azimuth_for_array_point`, the same room-rotation as `seat_azimuth_for_array` but for an
  arbitrary point) and fills the dial. Click-to-aim is opt-in (armed only while a steered A/B engine is
  connected, via a new canvas `click_cb`) and a click it can't resolve falls through to normal selection.
  Works **without any seats** (a raw angle) and without an array bearing for the dial; the map click needs the
  array's position + room bearing. With "Null other seats" on, the nulls keep the seat **nearest your aim** so
  you never null your own look; the readout shows `locked → …°`. Click-to-aim is inert outside Live mode
  (the canvas is shared, so a backgrounded A/B session can't hijack Design/Simulate clicks). (+7 tests.)
- **Snap-steer / "Lock to seat"** (`conf_pipeline.seat_azimuth_for_array`, `BeamEngine.set_steering`,
  LIVE panel) — pin the steered POLARIS beam to a **chosen room seat** instead of following the loudest
  talker. New pure helper `seat_azimuth_for_array(config, array_id, seat_id)` returns a specific seat's
  **array-relative** azimuth (the inverse room rotation, shared with `seat_null_azimuths`); new
  `BeamEngine.set_steering(azimuth_deg)` forwards it to the steered back-end (`None` resumes DOA-follow).
  In the LIVE A/B card a **"Lock to seat"** picker lists the room's seats (+ "Follow talker") and pins /
  unpins the look live; when locked, **seat-nulling keeps the locked seat** (nulls the others) and the
  readout shows `locked → seat …`. The lock lives on the steered back-end, so it persists across
  steered↔grid switches. The room-aware coverage story completes: listen to one seat, null the rest.
  (+5 tests.)
- **Live monitoring for the A/B beam engine** (`BeamEngine`, `conf_pipeline_control/beam_engine.py`) —
  the POLARIS steered↔grid A/B engine can now **play its output on headphones** so you can *hear* the
  beamformed / NR'd result, not just watch the meter. Opt-in `monitor=True` + `output_device` open a
  second (output) stream — two independent streams joined by a drop-oldest queue (no duplex assumption),
  mirroring the back-ends — and fan the mono to all output channels. New `set_mute`/`set_gain_db` +
  `muted`/`gain_db` trim the **monitor playback** (and the meter, which is now post-gain/mute) while the
  host `output_queue` stays raw. In the LIVE panel the existing **Monitor**/output-device controls now
  feed the A/B engine, and **Mute/Gain are enabled** during an A/B session when monitoring is on (they
  route to the engine via `_active_ctl`). Use headphones — monitoring through room speakers feeds back
  into the array. (+3 tests.)
- **Post-beam noise suppression** (`PolarisBeamformer`, `conf_pipeline_control/polaris_beamformer.py`) —
  opt-in `post_nr=True` runs a light single-channel **spectral-gate** on the beamformed mono output, a
  **local fallback** for when the OCTOVOX cloud cleaning path (`/api/clean`) isn't running. A pure-numpy
  windowed-OLA STFT (`_PostNoiseSuppressor`) learns a per-bin noise floor **only on noise-only frames**
  (the SRP VAD's `noise_gate`, mirroring the MVDR cov gate) and applies a **gentle single-pole Wiener**
  gain `G = g_floor + (1−g_floor)·P/(P + oversub·N²)` — smooth, bounded in `[g_floor, 1]` so it never
  hard-mutes (no musical noise), with a 3-tap frequency smooth and a per-bin temporal one-pole. It's
  **byte-identical during warmup** (the gate is bypassed until `post_nr_warmup_frames` gated frames are
  seen) and off by default; tuning via `post_nr_floor_db` (-15), `post_nr_oversub` (1.5),
  `post_nr_gain_alpha` (0.5), `post_nr_frame` (512). Threads through `BeamEngine`
  (`steered_cfg={"post_nr": True}`) and the `polaris-beam-demo --post-nr` flag; reset on
  `reset_transient`. Adds STFT latency once engaged (~12 ms at frame 512; stacks on the freq-domain
  beam's ~35 ms) — acceptable for a cleaning fallback. (+10 hardware-free tests.)

## [1.17.0] - 2026-06-16

**Room-aware steering, steerable nulls + output AGC** — the steered POLARIS beam now
follows the talker at the matched seat and actively shapes the rest of the field. The
frequency-domain beam places **exact LCMV nulls** on known bearings; **detection-driven
auto-null** rejects the other live talkers; **room-aware seat-nulling** nulls the empty
seats (the system knows the layout) — the two null sources arbitrated within the M−1
budget by a single deterministic **null-budget composer** (measured interferers win,
speculative seat nulls fill the remainder). A live **room-aware seat readout** maps the
tracked talker to the nearest room seat, and an opt-in **target-loudness AGC** normalizes
the mono output. All built on the v5 microphone-array `bearingDeg` and the four beam modes
(`delaysum` / `fracdelay` / `superdirective` / `mvdr`). Python-only live DSP + GUI — no
schema change (stays v5, TS sibling already at parity).

### Added
- **Target-loudness AGC on the beam output** (`PolarisBeamformer`, `conf_pipeline_control/
  polaris_beamformer.py`) — opt-in `agc_target_db` normalizes the mono output level so a near vs a far
  talker lands at a consistent loudness. One scalar gain per block pushes the beam-output RMS toward the
  target, **EMA-slewed** (`tracking.ExponentialTracker`, `agc_slew_alpha`) so it ramps without pumping
  and **clamped** to ±`agc_max_gain_db` (default 18 dB) so it never amplifies the noise floor; below
  `agc_silence_db` (default −55 dBFS) it **holds** the gain instead of chasing silence. Control-pure
  (driven by output level only, no room/distance coupling) and sits below the user's `set_gain_db`.
  Off by default (`agc_target_db=None` is byte-identical to before); threads through `BeamEngine`
  (`steered_cfg={"agc_target_db": …}`) and the `polaris-beam-demo --agc-target-db` flag. The slew
  tracker is atomically rebound on `reset_transient` (mirroring the talker tracker, since the audio
  thread mutates it lock-free). (+3 hardware-free tests.)
- **Room-aware seat-nulling** — while the steered beam follows the talker at the matched seat, null the
  **other (empty) seats**. New pure `conf_pipeline.seat_null_azimuths(config, array_id, *,
  exclude_seat_id=None)` returns the non-target seats' **array-relative** azimuths (the inverse of the
  seat-mapper's room rotation: `azimuth = bearing_to_deg(array, seat) − array.bearingDeg`); new
  `BeamEngine.set_nulls(bearings)` forwards them to the steered back-end. In the LIVE panel's A/B-engine
  card a "Null the other (empty) seats" checkbox builds a **superdirective** steered back-end at Connect
  (so the nulls have an effect — the time-domain modes ignore them) and, each tick, pushes the empty
  seats (excluding the matched one) through the null-budget composer; the readout shows "nulling N
  seat(s)". The off-the-shelf-can't-do-this differentiator: the system knows the seat layout. (+5 tests.)
- **Null-budget arbitration** (`compose_nulls`, `conf_pipeline_control/polaris_beamformer.py`) — a
  single deterministic composer that merges the two competing null sources on the steered beam within
  the M−1 LCMV budget: **detected interferers (auto-null) take priority; speculative empty-seat nulls
  fill only what remains** ("adaptive evidence beats static geometry"). It drops nulls near the look
  from both lists before budgeting, de-dupes across sources, orders seats nearest-to-look, and accepts
  a `seat_null_max_count` self-cap to reserve headroom for live talkers. `PolarisBeamformer._doa_tick`
  /`set_steering` now route both sources through it (replacing the old order-based merge), with
  `null_min_sep_deg`/`null_merge_sep_deg`/`seat_null_max_count` ctor params. The talker-exclusion
  margin is tied to the tracker's `switch_margin_deg`, so a tracked talker that drifts up to the switch
  margin from the committed look is never self-nulled. (+8 hardware-free tests.)
- **Auto-null on the steered beam** (`PolarisBeamformer(auto_null=True)`) — wires the LCMV nulls into
  the live DOA loop: the steered superdirective / mvdr beam now follows the dominant talker **and
  nulls the other detected sources** (interferers). `_doa_tick` raises the SRP-PHAT peak budget
  (`auto_null_max`), takes the dominant as the look and the non-look detections as nulls, and re-solves
  off the audio lock each tick so a null appears / moves / clears as interferers do. `set_nulls(bearings)`
  adds explicit caller-supplied nulls (e.g. non-target seat bearings from the room-aware layer); both
  are reported via `active_nulls`. Threads through `BeamEngine(steered_cfg={"auto_null": True})` with no
  engine change, and `polaris-beam-demo --auto-null`. The time-domain modes (no null DOF) ignore it.
  (`conf_pipeline_control/polaris_beamformer.py`; +4 hardware-free tests — validated live on the array.)
- **Explicit LCMV nulls on the steered frequency-domain beam** (`_FreqDomainBeam`, `mode` =
  `superdirective` / `mvdr`) — the per-bin solve generalises from plain MVDR
  (`w = R⁻¹a / aᴴR⁻¹a`) to LCMV (`w = R⁻¹C (CᴴR⁻¹C)⁻¹ g`, `C = [a(look), a(φ₁)…]`, `g = [1,0,…]`),
  so the beam can place **exact spatial nulls on known interferer bearings** while staying
  distortionless at the look. Exposed via an optional `nulls=()` on `BeamStrategy.plan_look`/`set_look`
  (the time-domain `delaysum`/`fracdelay` tiers have no null degrees of freedom and ignore it); nulls
  are filtered (drop within 5° of the look or each other) and capped to the `M−1` budget, and a tiny
  trace-relative ridge regularises the DC/low-frequency bins where the manifolds collapse. The nulls
  **compose with** the measured-noise MVDR overlay (null a supplied bearing *and* the measured
  interferer field at once). The heavy solve stays in `plan_look` (off the audio lock); the callback
  is unchanged pure MAC. This is the DSP core for detection-driven auto-null (the wiring follows).
  (`conf_pipeline_control/polaris_beamformer.py`; +5 hardware-free tests — verified on the live array.)
- **Live room-aware seat readout** (LIVE panel + room-map canvas) — while a DOA session runs
  (auto-steer or the POLARIS A/B engine), the dominant detected talker is mapped through
  `nearest_seat_for_array` to the **nearest room seat** and surfaced two ways: a `· seat <id>
  (<n>° off)` suffix on the panel readout, and a highlighted ring + label on the matched seat on
  the room map. Pure GUI-side wiring — the control layer (`conf_pipeline_control`) is untouched, the
  seat is resolved from the array's room pose (`position` + v5 `bearingDeg`), and the highlight is
  drawn at the seat's **true world position** (independent of the auto-steer front-offset ray frame).
  Prefers the loudest in-sector talker (the one actually followed), falling back to the loudest
  overall. (`conf_pipeline_gui/panels/live.py`, `canvas.py`; +6 headless tests.)
- **Room-aware seat mapping** (`conf_pipeline/seat_mapper.py`) — a pure-stdlib geometry layer
  that turns a detected **array-relative azimuth** into the **nearest room seat**, building on the
  v5 array `bearingDeg`. `nearest_seat(...)` rotates the azimuth into room coordinates by the
  array's mounting bearing and picks the seat whose room-bearing-from-the-array is angularly
  closest (returns `None` past a configurable `max_separation_deg` "between seats" gate);
  `nearest_seat_for_array(config, array_id, azimuth_deg)` resolves the array pose + the room's seats
  from a `SystemConfig`. It **composes over the DOA** — no new `BeamStrategy`, so it works with all
  four beam modes — and reuses the engine's existing bearing helpers (`bearing_to_deg`,
  `angular_separation_deg`) and the `coverage_sim` synthetic seat-ID convention
  (`{furniture_id}-seat{index}`). The live GUI/API seat readout is a follow-up. (+9 tests.)
- **Microphone-array mounting bearing** (schema **v4 → v5**) — `MicrophoneArray` gains an
  optional `bearingDeg` (compass heading of the array's 0° reference, 0° = +Y), so a
  detected array-relative azimuth can be mapped into room coordinates. It's the prerequisite
  for room-aware steering. Additive and omit-when-absent (mirrors the loudspeaker's
  `bearingDeg`), so existing v1–v4 configs migrate byte-identically; `set_array_bearing`
  api + parity-matched in the TypeScript sibling (`bearingDeg` + `setArrayBearing`, same v5
  migration). (`conf_pipeline/model.py`, `persistence.py`, `api.py`; +3 round-trip/migration tests.)
- **Data-adaptive MVDR beamforming** (`mode="mvdr"`) — the flagship beam tier, reusing the
  superdirective STFT / `plan_look`-`commit_look` plumbing unchanged but feeding the per-bin solve a
  **measured** noise covariance instead of the fixed analytic Γ. A gated EMA (`_noise_cov`,
  `noise_only`-gated so it captures the noise/interference field, not the talker) is overlaid on the
  DOA-band bins with trace-relative loading; bins outside the band — and the whole cold-start period,
  before the warmup gate — fall back to the analytic superdirective design, so the beam degrades
  gracefully. The result **nulls the actual dominant interferer** rather than just isotropic
  background (verified: against a measured interferer the MVDR null is ≥2× deeper than the fixed
  superdirective, while staying exactly distortionless — unit gain — at the look). The DOA worker
  re-solves the weights every tick so the null tracks the evolving noise field even when the talker
  is stationary; the heavy solve stays off the audio lock (`plan_look`), and the gated EMA reuses the
  existing `_cov_lock` so the audio callback gains no new lock. `--mode mvdr` on the demo. Next:
  room-aware steering. (`conf_pipeline_control/polaris_beamformer.py`; +4 hardware-free tests:
  measured-interferer nulling, cold-start == superdirective, noise-gating, warmup/reset.)
- **Superdirective beamforming** (`mode="superdirective"`) — a third `PolarisBeamformer`
  tier and the first frequency-domain one: a windowed overlap-add STFT (1024/512 Hann, ~0.1%
  COLA ripple) with a per-FFT-bin **diffuse-noise MVDR** weight vector `w = R⁻¹a / (aᴴR⁻¹a)`,
  `R = Γ(f) + loading·I`, `Γ_ij = sinc(k·d_ij)` (`_FreqDomainBeam`, mirroring
  `beamformer.superdirective_weights` vectorised over bins). It rejects isotropic
  room/background noise far better than delay-and-sum, which the studio-grade capsules make
  genuinely usable at low diagonal loading. **Realtime-safe by construction:** `BeamStrategy`
  gained a `plan_look`/`commit_look` split — the per-bin matrix solves (several ms over 513
  bins) run in `plan_look` **off the audio lock**, and only the cheap atomic weight-array
  publish (`commit_look`) is taken under `_beam_lock`, so the audio callback is pure
  multiply-accumulate and never blocks behind a solve. An input/output FIFO adapts the caller's
  block size to the internal 512-hop framing (round-trip latency ≈ frame+hop, ~35 ms).
  `superdirective_loading` param (floored so `0` = max directivity stays solvable) + `--mode
  superdirective --loading` on the demo. This lands the STFT/plan-commit plumbing that the next
  tier — **data-adaptive MVDR** (measured covariance gated on `noise_only`) — reuses unchanged.
  (`conf_pipeline_control/polaris_beamformer.py`; +7 hardware-free tests: exact MVDR unit-gain,
  steering selectivity, block-size-agnostic FIFO, off-lock plan/commit, zero-loading construction.)
- **Fractional-delay beamforming** (`mode="fracdelay"`) — a sub-sample steering tier
  for `PolarisBeamformer`, alongside the default integer `delaysum`. Each capsule's
  steer delay is split into an integer part (the existing history-ring read) plus the
  sub-sample remainder, applied by a short Hann-windowed-sinc fractional-delay FIR per
  capsule (`_FracDelaySumBeam` / `_frac_delay_kernel`). This removes the up-to-±0.5-sample
  (≈3.9 mm) pointing error of integer rounding — one sample is ≈7.78 mm of travel at
  44.1 kHz vs the 80 mm aperture — tightening off-axis alignment for a flat, common
  ~0.16 ms (`(taps-1)/2`-sample) latency. Stays pure numpy and realtime-safe (no FFT, no
  added lock). Selectable on the standalone runtime (`--mode`), and on the A/B engine via
  `BeamEngine(steered_cfg={"mode": "fracdelay"})`. The `BeamStrategy` seam gained a
  `reset()` method so re-steer / re-activate drops streaming history cleanly. MVDR is the
  next documented seam. (`conf_pipeline_control/polaris_beamformer.py`; +5 hardware-free
  tests incl. a non-circular analytic-plane-wave alignment check.)
- **Live-panel A/B beamforming** — the desktop app's **LIVE** mode can now drive
  `BeamEngine` directly: a "POLARIS A/B beamformer" card with a **steered ↔ grid**
  strategy picker that switches **live** (glitch-free crossfade) on one shared input
  stream, the level meter, and the tracked direction drawn on the room map (the
  steered DOA, or the grid's selected bearing). Previously the steered / grid /
  BeamEngine modules were reachable only through the `polaris-*-demo` CLIs. The
  three session modes (A/B engine / auto-steer / OCTOVOX) are mutually exclusive;
  the engine has no playback path yet, so Mute / Gain are disabled during a
  BeamEngine session. (`conf_pipeline_gui/panels/live.py`; +1 headless GUI test.)

### Fixed
- **Calibrate-front now applies rear/left bearings instead of clamping.** The LIVE panel's
  "Calibrate front" measured the talker's bearing but fed the raw 0–360° DOA into the −180…180°
  Front-offset spin box, so any talker the array localized above 180° (common on the front/back-
  ambiguous POLARIS ring) was silently clamped to 180° — the value never matched the heard
  bearing and the pickup sector never centred on the talker. The measured azimuth is now wrapped
  into (−180, 180] before it's applied (the sector gate is wrap-aware, so steering is identical),
  and the status line shows both the heard bearing and the applied offset. (+8 headless tests.)

## [1.16.0] - 2026-06-15

**Room v4 + real-time array beamforming** — the room model gains cameras,
loudspeaker aim, and furniture, with a geometric coverage simulator that renders in
2D and 3D; and the optional `[control]` layer gains a real-time beamforming suite for
the physical sensiBel POLARIS 8-mic array (SRP-PHAT steering, a Nureva-style
virtual-mic grid, and an A/B engine over both). Schema **v3 → v4** (cameras /
loudspeaker aim / furniture — lossless from v1/v2/v3). The desktop app is rebranded
**Aniston Room Designer**.

### Added — room model & coverage simulation (schema v4)
- **Conferencing cameras** (`model.py`, `api.py`): a `ConferencingCamera` device with
  pose (`bearing_deg` / `tilt_deg`) and a `CameraSpec` profile (FOV / range);
  `create_camera` / `add_camera`; generic-ptz / wide / soundbar-camera profiles.
- **Loudspeaker aim** (`SpeakerSpec`) and **furniture geometry** — `RoomObject`
  enriched with size / rotation / `SeatAnchor`s, resolved against a
  `conf_pipeline/furniture.py` catalog (table / desk / chair / sofa / screen / …).
- **Geometric coverage simulation** (`conf_pipeline/coverage_sim.py`):
  `simulate_room_coverage` → `RoomCoverage` — mic pickup sectors + camera FOV with
  **height-aware furniture occlusion** + speaker dispersion, per-target hits and
  coverage % / gaps. A view-independent `CoverageWedge` so 2D and 3D share one
  contract; a `mic_coverage_fn` injection seam reserved for a beamformer-driven tier.
- **Validation** (5 codes): `CAMERA_UNPLACED` / `CAMERA_NO_SUBJECT`,
  `FURNITURE_OUTSIDE_ROOM` / `FURNITURE_GEOMETRY_INVALID`, `DEVICE_INSIDE_FURNITURE`.
- **GUI**: a floating **SimBar** (`simbar.py`) toggling Pickup / FOV / Dispersion /
  Occlusion with a coverage readout; a **Furniture tool** (catalog flyout,
  place / move / resize / rotate, one undo per gesture); coverage overlays + furniture
  rendered in **both 2D and 3D**; camera in the Design device picker with bearing /
  tilt aim.

### Added — real-time POLARIS array beamforming (`[control]` extra)
- **`PolarisBeamformer`** (`polaris_beamformer.py`): real-time **SRP-PHAT DOA +
  time-domain delay-and-sum** for the 8-mic array — estimates the dominant talker's
  azimuth and emits one steered mono beam (active-speaker isolation). Talker-hold
  smoothing; `start` / `stop` manage the stream + a ~10 Hz DOA worker; opt-in
  **wait-for-device + auto-reconnect** with structural fail-fast (`DeviceConfigError`).
  Defaults to all 8 capsules active (dead capsule opt-in). `polaris-beam-demo`.
- **`VirtualMicGrid`** (`virtual_mic_grid.py`): a Nureva-"Microphone-Mist"-style
  **selection** beamformer — a dense grid of fixed near-field virtual mics, all run per
  block, the loudest selected (no steering / DOA). Self-contained / removable.
  `polaris-vmic-demo`.
- **`BeamEngine`** (`beam_engine.py`): unifies both back-ends behind **one shared input
  stream** with runtime `set_mode("steered"|"grid")`, a normalized
  `Location{mode, angle_deg, xy, confidence}` report, and an equal-power crossfade on
  switch — a glitch-free A/B of the two strategies on one board.
  `polaris-beam-engine-demo`.
- **Beam-output band-limiting** — a pure-numpy Hann-windowed-sinc low-pass
  (`beam_bandlimit_hz`), **on by default** at the array's ~5.6 kHz spatial-aliasing
  cutoff (`None` / `0` disables); a unified `BeamEngine` toggle drives both back-ends.
- **Swappable tracking** (`conf_pipeline_control/tracking.py`): a `Tracker` /
  `ValueSmoother` interface with an `ExponentialTracker` (the grid's selection smoother)
  and an `AlphaBetaTracker` (constant-velocity / steady-state-Kalman hook); the steered
  talker-hold machine is unified under the same lifecycle.
- **Grid voice-activity gating** — the grid now **holds the last seat through silence**
  (`vad_floor_db` peak/median gate) instead of chasing noise, and exposes
  `speech_active` / `noise_only` for a future adaptive (MVDR) stage.

### Changed
- The desktop app's window title / display name / icon are **Aniston Room Designer**
  (wren mark; a white icon for the dark theme). The package name is unchanged.

### Tests
- **469 tests** (was 357): camera / furniture / coverage-sim / coverage-design API and
  GUI overlays; hardware-free beamformer suites (DOA recovery, near-field grid
  selection, BeamEngine seam round-trip + crossfade, band-limit FIR, tracking filters,
  grid VAD hold). `conf_pipeline` + `conf_pipeline_control` stay mypy-clean.

### Fixed
- A reset/realtime race in the grid back-end's selection-hold + smoother state (a mode
  switch could null the hold-state mid-block): selection state is now guarded by the
  back-end lock with a None-guard fallback, and `PolarisBeamformer.reset_transient`
  rebinds its tracker atomically rather than mutating it while the DOA thread reads it.
- The v2 → v3 migration hard-coded `CONFIG_VERSION`; the v3 → v4 step restored an
  additive, version-correct migration so v3 files round-trip losslessly into v4.

## [1.15.0] - 2026-06-12

**Commissioning, scenes & broadband honesty** — the phased evolution plan
landed in full: repository hygiene (CI, type checking, pinned dev deps),
wideband beam design with measured DI/beamwidth-vs-frequency curves, the
simulated commissioning workflow (device transport, per-device online state,
push + reconcile, project file manager), and the control story (scenes,
a local HTTP control API, scene scheduling). Schema v2 → v3 (scenes), with
schedules additive on v3. 357 tests (was 259); `conf_pipeline/` and
`conf_pipeline_control/` are mypy-clean.

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

**Scene scheduling (C3)** — recall a scene at a time, on the room's weekly
rhythm.

### Added
- **`SceneSchedule`** (`ControlConfig.schedules` — additive optional, schema
  **stays v3**, matching the 1.12 precedent for optional fields): recall
  ``sceneId`` at local ``"HH:MM"`` on the given weekday keys, every week;
  per-entry ``enabled``. Builders `create_scene_schedule` /
  `add_scene_schedule` / `remove_scene_schedule` /
  `set_scene_schedule_enabled`; `parse_hhmm` joins the model helpers.
- **`SceneScheduler`** (`conf_pipeline/scheduler.py`, stdlib): executes the
  config's schedule entries through the same `get_config`/`apply` pair as the
  control API, so GUI, HTTP, and scheduler mutate one consistent config.
  Deterministic by construction — injectable clock, `run_pending()` manual
  tick (fires at most once per scheduled minute, skips vanished scenes),
  `next_fire()`, and an optional daemon polling thread for headless use.
- **Validation**: `SCHEDULE_INVALID` — duplicate ids, missing scene, bad
  `"HH:MM"`, empty/unknown days.
- `GET /api/status` now reports the schedule list.
- Tests (`tests/test_scheduler.py`, 13): additive round-trip (v3 unchanged,
  pre-schedule v3 files load), builder guards, the validation matrix, and
  clock-driven firing — due/day/disabled/wrong-minute filters, once-per-minute
  dedup + re-arm a week later, multi-entry minutes, dangling-scene skip,
  `next_fire` same-day vs week-wrap, thread lifecycle, and the status
  endpoint.

**External control API (C2)** — a local HTTP surface for room-control
integrations: scene recall / mute / status.

### Added
- **`conf_pipeline/control_api.py`** (pure stdlib — `http.server`, **no new
  dependency**, not even an optional extra): `ControlApiServer` bound to
  localhost (ephemeral port by default) exposing JSON routes in the OCTOVOX
  `/api/…` style — `GET /api/status` (name, schema version, deployment state,
  mute groups, scenes), `GET /api/scenes`, `POST /api/scenes/<id>/recall`, and
  `POST /api/mute-groups/<id>` (`{"muted": bool}`), with JSON 400/404 errors.
  The recall response carries the scene's config-inert live-layer hints
  (`steer`, `activeZones`) so the external controller can aim the beamformer.
- The server owns no config: the host supplies `get_config` / `apply`
  callables. `ConfigHolder` is the thread-safe headless/test owner; the GUI
  can supply a main-thread-marshalled pair later.
- Tests (`tests/test_control_api.py`, 9): live request/response against an
  ephemeral-port server via urllib — status/scene payloads, recall side
  effects + hints, unknown-scene/group 404s, body validation 400s, unknown
  routes, sequential-consistency, and the start/stop lifecycle.

**Scenes (C1)** — named, recallable snapshots of the control surface.
**Schema v2 → v3** (lossless migration; the TS sibling needs a matching update
before it can read v3 exports — v1/v2 files keep loading here).

### Added
- **`Scene`** (`ControlConfig.scenes`, schema v3): typed sections —
  `muteStates` (mute-group id → muted), `zoneStates`
  (`{arrayId, zoneId, gainDb?, active?}`), and `steer`
  (`{arrayId, azimuthDeg, offNadirDeg}`). `gainDb` applies to the config on
  recall; **`active` and `steer` are config-inert live-layer hints** (which
  pickup areas to beamform and where to aim) — deliberately, because the
  config-side `always_on` flag is a zone-*type* invariant (dedicated ⇔ True),
  not an operational toggle, so recall must not touch it. `None` fields mean
  "leave as-is", so scenes can be partial.
- **API**: `create_scene` / `add_scene` / `remove_scene` / `get_scene`,
  `capture_scene` (snapshot every mute group's state + every pickup area's
  gain trim), `recall_scene` (pure config→config; entries referencing vanished
  things are skipped — validation owns flagging them). Mute-group builders now
  preserve scenes when rebuilding `ControlConfig`.
- **Migration**: `CONFIG_VERSION` 2 → 3 with a chained, lossless
  `_migrate_v2_to_v3` (adds an empty `control.scenes`; v1 files run the
  existing v1→v2 step first). Unsupported versions are still rejected.
- **Validation**: `SCENE_INVALID` — empty scene, duplicate scene ids, or a
  scene referencing a missing mute group / array / coverage area (incl. steer
  targets).
- **GUI**: a **Scenes** editor in the Route panel — capture the current
  surface as a named scene, recall, remove — beside the mute-group editor.
- Tests (`tests/test_scenes.py`, 12 + 1 smoke): camelCase round-trip incl.
  field omission, v2→v3 lossless upgrade (byte-compared modulo the additive
  fields), the v1→v2→v3 chain, capture→drift→recall restoring the surface,
  dangling-ref recall safety, purity/idempotence, scene↔mute-group
  coexistence, and the validation matrix. Existing hardcoded `version == 2`
  asserts now track `CONFIG_VERSION`.

**Push + reconcile (A3)** — "Deploy to online devices": push the design through
the transport, read back, and reconcile device-reported vs designed.

### Added
- **`push_to_online(config, transport)`** (`transport.py`) → `PushReport`:
  pushes every *online* designed device (connecting as needed), reads each
  back, and attaches a `ReconcileEntry` per device (matches / which components
  differ — label, profile, ports, processing blocks — the same granularity as
  `deployment_diff`'s fingerprint, so the two views of "changed" always
  agree). Offline devices are skipped and reported; per-device transport
  errors are collected, never raised. `report.complete` / `.clean` /
  `.summary()`. **`reconcile_online(config, transport)`** is the read-only
  check (no push).
- **`AppState.push_online()`**: runs the push and updates the last-deployed
  snapshot **only on a clean, complete push** — a partial push (offline
  devices, failures, mismatched read-back) leaves the snapshot alone so the
  DEPLOY dot and the changed-since-deploy badges keep pointing at exactly the
  devices that didn't make it.
- **Deploy panel**: a **⇪ Deploy to online devices** button in the Online-room
  group (enabled while online) rendering the push report — pushed/skipped/
  failed plus the per-device ✓/✗ reconcile lines — inline in the panel.
- Tests: 6 engine tests (drift detection + component granularity, offline
  skipping, drifted-room push round-trip, skipped/uninstalled devices,
  per-device failure collection via a flaky simulated push) and 3 GUI smoke
  tests (clean push → snapshot refreshed → dot DONE; partial push with the
  drifted device unplugged → snapshot untouched → still nagging; the panel
  button lifecycle).

**Online-room state (A2)** — per-device connected / offline / changed-since-
deploy, surfaced through the existing workflow status-dot infrastructure.

### Added
- **`online_room_status(config, last_deployed, transport)`** (`transport.py`)
  → one `OnlineDeviceState` per designed device: `online` / `connected` from
  the transport, `changed_since_deploy` / `new_since_deploy` from
  `deployment_diff` (a never-deployed design marks every device new), plus an
  `in_sync` aggregate. `SimulatedTransport.has_device` joins the simulation
  controls.
- **`AppState` online session** (per room): `go_online()` seeds the simulated
  transport on first use — **from the last deployed snapshot when there is
  one, else from the current design** — and connects everything discoverable;
  `go_offline()`, `device_status()`, and `simulate_device_offline()` (the
  demo/test "unplug" control). `deploy()` now also *installs* newly designed
  devices into an already-seeded simulated room, so shipping the design makes
  them discoverable.
- **Deploy panel**: an **Online room (simulated)** group — Go online/offline
  button, a summary line (`n/m online · k changed/new since deploy`), and one
  status row per device (●/○, connected/changed/new tags, and a per-device
  *offline* checkbox driving the simulated unplug).
- **Workflow dots**: the DEPLOY dot now regresses to *in progress* while the
  room is online and any device is changed/new since the last deploy; the
  hint chip reports offline devices and pending changes.
- Tests: 4 engine tests (status matrix: never-deployed, changed+new vs
  deploy, connection/offline reflection, sorting) and 3 GUI smoke tests (the
  full online lifecycle driving the deploy dot through DONE→PARTIAL,
  deploy-installs-new-devices, and the panel group rendering).

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
