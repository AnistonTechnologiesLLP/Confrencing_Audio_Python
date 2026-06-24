# RTF-MVDR beamformer (data-estimated steering) — design

- **Date:** 2026-06-24
- **Status:** Approved (brainstorm) — pending implementation plan
- **Component:** `conf_pipeline_control` (live DSP layer), v1 in `PolarisBeamformer` / the A/B engine path
- **Author:** brainstormed with the user via `superpowers:brainstorming`

## Summary

Add an opt-in **RTF-MVDR** beam mode that estimates the target talker's **relative
transfer function (RTF)** from the live data and uses it as the MVDR steering vector,
instead of the current model-based plane-wave steering vector `a0(az)` aimed at the
SRP-PHAT direction. The goal is real-room robustness: an RTF captures the *actual*
source→mic path (reverberation, near-field, and per-capsule gain/phase mismatch), which
the idealized free-field `a0` does not.

This is **complementary** to SRP-PHAT, not a replacement: SRP-PHAT keeps doing DOA and
keeps driving the UI/sectors/seat-map/auto-null, and additionally **gates** which frames
train the RTF and **validates** the estimated RTF's direction.

## Background — what already exists

The live beam already supports a data-adaptive MVDR:

- `MODE_SUPERDIRECTIVE` — diffuse-noise MVDR with a fixed analytic coherence model `Γ`.
- `MODE_MVDR` (`polaris_beamformer.py`) — **data-adaptive** MVDR against a *measured*
  noise covariance, gated on `noise_only` frames (warmup `_NOISE_WARMUP_FRAMES = 16`),
  solved per FFT bin in `_FreqDomainBeam` (`w = R⁻¹a / (aᴴR⁻¹a)`).

The one part that is still **model-based** is the **steering vector**: today it is the
plane-wave `a0(az)` at the SRP-PHAT look. RTF-MVDR replaces that `a0` with a
data-estimated RTF `h`, reusing the same per-bin solve and the same measured-covariance
machinery.

Measured DOA accuracy of the existing SRP-PHAT (for reference): ~0.5° mean / ≤1.0° max
angular error (2° grid-quantization-limited), robust down to 0 dB SNR; two-talker
resolution ~45°. RTF-MVDR does not change DOA; it changes the *extraction filter*.

## Decisions (locked in brainstorming)

1. **Scope: complementary.** Keep SRP-PHAT for control/UI/sectors; add RTF-MVDR as the
   extraction filter behind it. The azimuth-based features (sector gating, seat map,
   direction arrow, auto-null, lock-to-seat) keep working unchanged.
2. **Gating: SRP-PHAT in-sector gate.** A confident SRP-PHAT talker detection marks
   *target-present* frames (train the RTF); `noise_only` marks *noise* frames (train the
   noise covariance). Reuses existing logic; lowest self-cancellation risk.
3. **v1 path: `PolarisBeamformer` / the A/B engine.** It already has the measured-noise
   MVDR + per-bin solve + noise-only gating, and the A/B-proof tool sits on it for a
   direct head-to-head measurement vs the current plane-wave MVDR. Single steered target.
   Porting to `LiveBeamController` (auto-steer / multi-sector) is a documented follow-on.
4. **Estimator: GEVD / max-SNR.** RTF via the principal generalized eigenvector of
   `(R_target, R_noise)`: `h = R_noise · v`, normalized. Principled max-output-SNR,
   robust to imperfect gating and colored/directional noise, and not dependent on any
   single reference capsule. Cost (an 8×8 GEVD per band per update) runs on the control
   thread, off the audio callback — like today's weight computation.

## Architecture & components

Opt-in, default-OFF, **bit-exact pass-through when off** (the established stage recipe).
Five units:

1. **`conf_pipeline_control/rtf_mvdr.py` (new, pure / no I/O — fully headless-testable):**
   - `estimate_rtf_gevd(R_target, R_noise, *, loading) -> h` — per-band RTF as the
     principal generalized eigenvector (`h = R_noise · v`), **normalized to unit norm**
     (robust to a dead/hot capsule — no dependence on a fixed reference capsule), with
     diagonal loading for conditioning.
   - `rtf_mvdr_weights(R_target, R_noise, *, loading) -> W` — feeds `h` into the standard
     `w = R⁻¹h / (hᴴR⁻¹h)` per band.
2. **Two gated covariance EMAs** in `PolarisBeamformer`: a new `R_target` (confident-talker
   frames) alongside the existing `R_noise` (`noise_only`-gated). Both updated on the audio
   thread as cheap rank-1 accumulations under the existing covariance lock; the
   target/noise/hold gate flag is set by the SRP-PHAT control tick.
3. **`_FreqDomainBeam` extension** — when `mode="rtf_mvdr"`, the control-thread weight
   update calls `rtf_mvdr_weights(snapshot(R_target), snapshot(R_noise))` instead of
   building the plane-wave `a0`, then **atomically rebinds** the weight array (existing
   pattern). The audio thread only applies the latest weights — no new per-block cost.
4. **Fallback ladder** — until both covariances are warmed / GEVD is ill-conditioned / no
   confident target → fall back to the existing plane-wave MVDR (or superdirective) at the
   SRP-PHAT look. RTF-MVDR is therefore **never worse** than today's beam when it cannot
   estimate.
5. **SRP-PHAT unchanged** — still does DOA, sectors, UI; additionally supplies the gate and
   a sanity cross-check (below). Nothing is removed.

## Data flow & robustness

Per control tick (~8 Hz, off the audio thread — mirrors today's DOA re-steer):

1. SRP-PHAT runs as-is → a confident detection (azimuth + salience) or `noise_only`.
2. **Three-way gate** sets a flag the audio thread reads:
   - confident talker → accumulate the instantaneous covariance into **`R_target`**,
   - `noise_only` → accumulate into **`R_noise`** (today's behaviour),
   - ambiguous / transitional (low salience) → **accumulate into neither** (hold), so
     neither covariance is contaminated.
3. Once both EMAs pass warmup → control thread computes GEVD → RTF → MVDR weights →
   atomic rebind. The audio thread keeps applying the latest weights.

**Robustness ladder (prevents MVDR self-cancellation of the talker):**

- **Warmup:** require ≥ `N_target` and ≥ `N_noise` frames before trusting the RTF; until
  then → plane-wave-MVDR fallback.
- **Min target dwell:** a freshly-estimated RTF is adopted only after a minimum continuous
  target-active stretch, so a brief false detection cannot poison it.
- **SRP-PHAT cross-check:** the RTF's implied direction must sit near the detected
  azimuth; if it is wildly off (the gate caught an interferer) → reject, hold last-good,
  fall back. *This is the complementary payoff — SRP-PHAT validates the RTF.*
- **Conditioning:** diagonal loading on `R_noise`; if the principal generalized-eigenvalue
  ratio is below threshold (no clear target above noise) → do not adopt → fallback.
- **Distortionless normalization to unit-norm (no fixed reference capsule)** — so a
  dead/hot capsule cannot break the RTF.

**Realtime safety:** the two EMA updates are cheap rank-1 accumulations on the audio
thread under the existing covariance lock; GEVD/solve run off-thread; weights are
atomically rebound; no new lock across heavy DSP and no per-block allocation.

## Testing & validation

Headless (numpy, synthetic covariances / stubbed DOA — no hardware):

- **`tests/test_rtf_mvdr.py` (pure RTF math)** on synthetic covariances built from a known
  target steering + a directional interferer + diffuse noise:
  - *distortionless*: target gain within **0.5 dB** of unity;
  - *interference rejection*: interferer null **≥ 10 dB**;
  - *the key win*: SINR **≥ 3 dB better than plane-wave-`a0` MVDR** on a case with
    per-capsule gain/phase mismatch and a reverberant/multipath target — proving RTF wins
    where the plane-wave model breaks;
  - degenerate cases: `R_target ≈ R_noise` (no target) → fallback flagged, not garbage;
    ill-conditioned → loading engages; dead/hot reference capsule → unit-norm
    normalization still holds.
- **Gating state-machine tests:** the three-way gate + warmup + min-dwell + cross-check
  rejection, driven by a stubbed DOA sequence; assert which covariance trains and when the
  RTF is adopted / held / rejected.
- **Realtime-safety + bit-exact-off:** `mode != "rtf_mvdr"` → byte-identical to today;
  cheap EMA updates; atomic rebind (mirror the existing covariance/weight tests).
- **Live validation:** the A/B-proof tool head-to-head — RTF-MVDR vs `MODE_MVDR` — reporting
  measured SINR / noise-suppression dB in the room; plus a `dsp-realtime-reviewer` pass over
  the diff.

(The `≥ 3 dB` / `≥ 10 dB` / `0.5 dB` thresholds are initial acceptance bars for the
synthetic tests, to be tightened once measured.)

## Error handling & honest limits (scope, not apology)

- Ill-conditioned / no-target → the fallback ladder (never worse than today's beam).
- Self-cancellation is bounded by warmup + min-dwell + the SRP-PHAT cross-check, but it is
  RTF-MVDR's inherent risk — documented, not eliminated.
- **Needs a working, roughly-calibrated array** — the RTF is *more* sensitive to dead/
  imbalanced capsules than SRP-PHAT. Pairs naturally with the separate capsule-health
  check (out of scope here, tracked separately).
- v1 = single steered target (the A/B-engine path); **multi-sector per-target RTF is the
  documented follow-on** port to `LiveBeamController`.
- Improves reverb robustness vs the plane-wave model but is **not** dereverb; very low SNR
  still degrades the gate.

## GUI

One new entry in the existing **Beam → Mode** combo: *"RTF-MVDR (learns the talker's
signature)"* — opt-in, default unchanged (superdirective / current). Optional small status
read-out (*learning / locked / fallback*), matching the per-stage-meter philosophy. No
other UI change for v1 (sectors / seat / UI untouched).

## Files touched

- **New:** `conf_pipeline_control/rtf_mvdr.py`, `tests/test_rtf_mvdr.py`.
- **Modified:** `conf_pipeline_control/polaris_beamformer.py` (new `MODE_RTF_MVDR`, second
  gated `R_target` EMA, `_FreqDomainBeam` RTF path, fallback ladder), the beam-mode
  plumbing/config key, the GUI Beam → Mode combo entry, `README.md` + `CHANGELOG.md`.

## Out of scope / follow-on

- Porting RTF-MVDR to `LiveBeamController` (auto-steer) with **per-sector** RTFs for the
  multi-sector use case.
- The capsule-health / per-capsule calibration check (separate feature; RTF-MVDR depends
  on a healthy array but does not implement the check).
- Any change to SRP-PHAT itself (e.g. sub-degree peak interpolation) — independent.
