# Phase 0 — Discovery Report

**Project:** `conferencing-audio-pipeline-py` (POLARIS 8-MEMS host-side beamforming front-end)
**Scope:** Map the real audio pipeline from source; confirm existing vs missing production pieces;
record a green safety baseline. **No code changed in Phase 0.**
**Method:** Parallel source exploration + direct reads of both DSP chains + repo-wide grep, then a
baseline test run. Every claim below is cited to a file (and line where verified directly).

---

## 1. Current real pipeline order

There are **two parallel per-block DSP chains** and they run the **same stage order**. A new stage
must be added to BOTH (repo invariant).

- `PolarisBeamformer.process_block` — `conf_pipeline_control/polaris_beamformer.py:1812–1880`
  (driven by `BeamEngine`, `multibeam`/`multikit`/`multiroom`)
- `LiveBeamController._process_block` — `conf_pipeline_control/live.py:553–647`
  (the "Whole table" zone path + auto-steer)

**Verified order (left = earliest):**

```
preamp                      preamp.py InputPreamp            (uniform 8ch gain, default off/0 dB)
  → beam                    polaris_beamformer.py / live.py  (delaysum|fracdelay|superdirective|mvdr|rtf_mvdr)
  → AEC                     streaming_aec.py StreamingAec    (default OFF; far-end ref from reference_capture.py)
  → transient suppress      transient.py                     (default OFF)
  → dereverb                streaming_cleaner.py             (default OFF; Lebart/Habets)
  → post-NR                 gate | omlsa | wiener | DFN3     (default OFF; DFN3 = deepfilter_cleaner.py)
  → PEQ                     peq.py StreamingPeq              (default OFF; RBJ biquads, has highpass/bell)
  → AGC                     agc.py TargetLoudnessAgc         (default OFF unless agc_target_db set)
  → zone-gain               agc.py _apply_zone_gain          (default OFF)
  → band-limit (LP ~5.6kHz) polaris_beamformer.py _band_limit(default ON; process_block path only)
  → voice-gate              voice_gate.py VoiceOnlyGate      (default OFF; LAST, so AGC can't chase it)
```

**Critical findings for later phases:**
- **No HPF stage exists.** `CLAUDE.md` documents a `speech-HP` stage, but `speech_band`/`speech-HP`
  is found **only in CLAUDE.md** (repo-wide grep) — never implemented. (→ Phase 2)
- **PEQ (the only notch/HP-capable linear filter) runs AFTER the neural/spectral post-NR (DFN3),**
  and is off by default. The brief's required order is the reverse. (→ Phase 2)
- The band-limit LP exists only on `process_block`; `live.py` has no band-limit. Order parity gap.
- `preamp` is uniform across capsules — **no per-capsule correction before the beam.** (→ Phase 1)

Realtime safety (verified): heavy solves (covariance EMA, LCMV weight recompute) run off the audio
lock on the DOA/control thread and are published atomically; the callback does only bounded numpy work.

---

## 2. Existing runtime flags (constructor / live params)

All optional stages follow one opt-in recipe: **default-OFF, real off/None/0 escape, bit-exact
pass-through when off** (returns the *same* array object — this is what keeps the suite byte-identical).

| Flag / param | Stage | Default | Notes |
|---|---|---|---|
| `preamp_gain_db` | preamp | 0 (off) | uniform, all 8 ch |
| `mode` | beam | `superdirective` | + delaysum/fracdelay/mvdr/rtf_mvdr |
| `auto_null`, `set_nulls`, exclusion nulls | null steering | off | LCMV; budget M−1 = 7 |
| `aec` (+ reference capture) | AEC | OFF | ERLE reported |
| `transient_suppress` | transient | OFF | lookahead = added latency |
| `dereverb` | dereverb | OFF | T60/beta/early-ms |
| `post_nr` + `post_nr_engine` (`gate`/`omlsa`/`wiener`/`dfn3`) | post-NR | OFF (`gate`) | `post_nr_amount`, `post_nr_minstat`, depth |
| `post_nr_preserve_level` | makeup | — | level-preserving cleaner |
| `peq` + `peq_bands` | PEQ | OFF | RBJ biquads |
| `agc_target_db` (+ `agc_max_gain_db`) | AGC | OFF (None) | target-loudness + limiter |
| `live_zone_gain` | zone trim | OFF | post-AGC |
| `beam_bandlimit_hz` | band-limit | ON (~5.6 kHz) | `None` disables |
| `voice_gate` | VAD gate | OFF | shallow duck, last stage |
| `track_covariance` | DOA cov | on when steering | feeds DOA/nulls |

GUI exposes these via the live panel (see §4). Post-NR engine/depth/amount are **live-only** (not
persisted to config).

---

## 3. Existing config fields (schema)

`conf_pipeline/model.py` — **CONFIG_VERSION = 5**, camelCase on the wire, snake_case dataclasses,
TS-parity with `c:\Work\conferencing-audio-pipeline` (hard constraint).

- **`DspBlock`** (per-device post-zone chain): `gainDb`, `agcTargetDb`, `agcMaxGainDb`, `nrAmountDb`,
  `deverbAmount`, `compThresholdDb`, `delayMs`, `peqFreqHz`/`peqGainDb`/`peqQ` (4-band).
- **`MicrophoneArray`**: routing/zones/`aec`, optional `position`/`elevation`/`profile_id`, and
  **`bearing_deg`** (v5; array mounting orientation, used by room-aware steering).
- **`AecConfig`**: `enabled`, `reference_bus_id`.

**No calibration field. No placement field. No persisted post-NR/engine field.** (Calibration profiles
and placement results would be new — see Phases 1/3. Any new persisted field must be mirrored in the TS
sibling + migrated.)

---

## 4. Existing GUI controls

`conf_pipeline_gui/panels/live.py` — rich operator live panel (PySide6, "Aniston Room Designer"):
- **Hardware:** per-array use/device select.
- **Beam/Coverage:** listening-mode selector (Follow the room / Lock to seat / Whole table / Clean
  audio / Manual / Two kits), mute, output gain, monitor (headphones).
- **Auto-Steer:** enable, front offset, gate-when-empty, cleaner combo, AEC, **Calibrate front** button.
- **POLARIS A/B Engine:** strategy (Steered/Grid/RTF-MVDR), null empty seats, suppress steady noise
  (post-NR), noise depth (Gentle/Medium/Aggressive), cleaner (None/OM-LSA/DFN3/Light gate), dereverb,
  transient, voice-gate, AEC, adaptive null (MVDR), lock-to-seat, manual angle.
- **Two Kits:** dual-device + array + combined output.
- **Recording:** A/B test, **A/B proof (raw vs cleaned)**, monitor-RAW bypass; multibeam per-person record.

**Missing UI (gaps):** per-capsule **calibration** flow (only *front bearing* calibration exists),
**placement check**, explicit **pipeline-order** display, **16 kHz/transcription** route, latency readout.

---

## 5. Existing tests + safety baseline

**87 test files** in `tests/` (DSP, beamformer, DOA, nulls, AEC, dereverb, cleaner/DFN3, AGC, PEQ,
transient, voice-gate, preamp, multibeam/multikit/multiroom, A/B capture, report, serialization,
plus 13 `test_gui_*.py` GUI smoke tests).

**Baseline run (Phase 0, this session):**
```
QT_QPA_PLATFORM=offscreen PYTHONIOENCODING=utf-8 ./.venv/Scripts/python.exe -m pytest -q \
    --ignore-glob='tests/test_gui_*.py' -p no:cacheprovider
→ 900 passed in 44.00s   (exit 0)
```
GUI/MainWindow tests excluded on purpose: per repo `CLAUDE.md` they **hang on this Windows box** and
are verified in CI (offscreen Linux) instead. **Baseline = clean green.** This is the regression
reference for all later phases.

---

## 6. Existing output paths

All verified to emit **processed clean mono** (post-beam → cleaners → AGC), never raw 8ch:
- Clean mono **WAV recorder** — `live.py` `record_path` (16-bit mono).
- Live clean mono **PCM monitor** — `live.py` `sounddevice.OutputStream`.
- **A/B proof WAVs** (raw beam vs cleaned) + metrics — `ab_capture.py` `write_ab_proof`.
- **Per-person tracks** + mixed — `multibeam.py` `MultiTrackRecorder`.
- **Combined-room mono** — `multikit.py` / `multiroom.py` output streams.
- Far-end **reference capture** (AEC input, not egress) — `reference_capture.py`.

No 16 kHz / ASR / network egress. Resampling only inside `reference_capture._resample_to`.

---

## 7. Confirmed missing pieces (from source, not assumed)

1. **Per-capsule calibration** (gain/polarity/delay/phase before DOA) — MISSING. → Phase 1
2. **HPF stage + linear notch before DFN3** — MISSING (no HPF at all; PEQ is post-DFN3, off). → Phase 2
3. **Auto live placement check** (scored GOOD/ACCEPTABLE/BAD, tonal/rumble/hiss/imbalance, survey) — MISSING. → Phase 3
4. **16 kHz ASR-ready egress + egress router + optional virtual mic** — MISSING (clean mono already exists). → Phase 4
5. **Transcription-ready stream + provider interface** — MISSING (no ASR infra at all). → Phase 5
6. **UI for calibration + placement + pipeline-order + latency** — MISSING. → Phase 6
7. **Tests/report sections for the above new features** — to be added per phase. → Phases 1–7

---

## 8. Risk areas

- **Dual-chain divergence:** `process_block` (polaris) vs `_process_block` (live). Every stage edit
  must land in both, in the same position. Highest-likelihood regression source.
- **Bit-exact-off invariant:** new stages must return the same array object when disabled, or the
  byte-identical suite breaks.
- **Realtime callback safety:** no locks across FFT/cov/solve; atomic rebind of DOA-thread trackers.
- **Schema/TS parity:** new persisted fields (calibration profile, placement result if persisted)
  must be mirrored in the TS sibling and migrated with an additive step (own target version).
- **Sample-rate assumptions:** engine rate is configurable; POLARIS native 44100 Hz; DFN3 internal
  48 kHz with resamplers. Don't hard-code; resample for the 16 kHz ASR path explicitly.
- **Hardware reality:** array can silently deliver a subset of 8 capsules (beam goes hollow);
  standalone `sd.rec`/`InputStream(channels=8)` fail on POLARIS — capture only through the engine.
  This directly motivates Phase 1 (calibration) + Phase 3 (placement/capsule-health) safety checks.
- **GUI MainWindow hangs locally** — GUI work (Phase 6) verified via single-panel probes + CI.
- **Do-not-list compliance:** do not force DFN3/dereverb on; keep defaults off; keep measurement-first
  spine (linear cleanup before neural).

---

## 9. Recommended phase order

Keep the brief's order — it already matches dependency reality, with the discovery refinement that
several phases are "verify + fill a small gap" rather than "build from scratch":

1. **Phase 1 — Per-capsule calibration** (genuine gap; unblocks correct DOA/beam/null). Profile
   format + save/load + runtime enable + safe fallback + A/B; apply *before* DOA in both chains.
2. **Phase 2 — HPF/notch before DFN3** (genuine gap; cheap, high-value, measurement-first). Add a
   default speech HPF + linear notch seam *before* post-NR in both chains; keep PEQ; preserve byte-off.
3. **Phase 3 — Auto live placement check** (genuine gap; pre-meeting operator value; feeds Phase 2
   notch suggestions). Independent run, scored result, survey mode.
4. **Phase 4 — Clean mono egress** (small extension on top of existing recorders/streams): add 16 kHz
   ASR-ready PCM + named egress router (+ optional virtual mic if feasible).
5. **Phase 5 — Transcription-ready stream** (genuine gap): provider interface + mock + chunking on the
   Phase 4 clean mono.
6. **Phase 6 — GUI**: surface calibration, placement, pipeline order, latency, clean-output route.
7. **Phase 7 — Final verification + docs**: end-to-end via existing harness (`ab_test`,
   `commissioning_report`, scripts) + the new-feature tests.

**Acceptance for Phase 0:** pipeline mapped from source ✓, no assumptions ✓, missing pieces confirmed
from source ✓, baseline recorded (900 passed) ✓, phase tracker created ✓.

**Stop here. Do not start Phase 1 code until this is signed off.**
