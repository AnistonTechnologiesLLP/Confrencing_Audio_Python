# POLARIS Audio Front-End — Deployment / Operator Guide

The production deployment guide for the hardened POLARIS 8-MEMS audio front-end (Phases 1–6). It tells
an operator how to bring up a room **in the right order**, and states the honest limits up front.

> **Read this first — the principles that drive the order below:**
> - **Placement is a primary quality lever.** Physical position + linear cleanup often beat heavy
>   neural cleanup. Fix the room before reaching for DFN3.
> - **Room-specific tones must be measured per room.** The HPF/notch suggestions are *this room's* —
>   they are **not** global defaults and are never auto-applied.
> - **Audio "fencing" is attenuation/rejection, not a perfect wall.** Beamforming + nulls reduce
>   off-axis sound; they don't delete it.
> - **DFN3 is optional and downstream of linear cleanup.** It is OFF by default.
> - **Virtual mic support is an external adapter seam, not a bundled driver.**
> - **Real ASR providers are not bundled.** Transcription ships a mock/dev provider; no network by default.

---

## Operator bring-up sequence

```
1. Connect POLARIS → verify 8-channel input
2. Run the placement check with ROOM NOISE (no speech)
3. Move the array if BAD (or ACCEPTABLE with strong warnings) and re-check
4. Run or load per-capsule calibration
5. Enable calibration only after a valid profile loads
6. Optionally apply the MEASURED HPF/notch suggestions to the pre-NR stage
7. Keep DFN3 / dereverb optional — not forced on
8. Verify the clean mono egress (48 kHz)
9. Verify the 16 kHz ASR-ready stream if transcription is needed
10. Export diagnostics before deployment
```

### 1–3. Verify input + check placement
```bash
pip install -e ".[control]"
python scripts/device_check.py                       # confirm the POLARIS 8-ch input + per-capsule levels
python scripts/check_placement.py --device 7 --seconds 10 --label "Table center"
```
If the result is **BAD** (or **ACCEPTABLE** with strong rumble/tone/hiss warnings), move the array
0.5–1 m away from airflow/vents/fans and off resonant surfaces, then re-check. Survey several spots and
pick the best: `python scripts/check_placement.py --compare reports/audio/placement_*.json`.
(See [PLACEMENT_CHECK_GUIDE.md](PLACEMENT_CHECK_GUIDE.md).)

### 4–5. Calibrate the capsules
```bash
python scripts/calibrate_capsules.py --device 7 --seconds 5 --out polaris_cal.json
# then load it at runtime (default is OFF until you pass a profile):
#   PolarisBeamformer(device=7, calibration_path="polaris_cal.json")
```
A missing/malformed profile degrades to **calibration OFF** (surfaced as a warning, never hidden). Gain
alignment is the reliable output; polarity/delay need a controlled stimulus.
(See [CALIBRATION_GUIDE.md](CALIBRATION_GUIDE.md).)

### 6. Apply measured HPF/notch (opt-in)
Feed the placement check's measured tones into the pre-NR stage — **for this room only**:
```python
from conf_pipeline_control.placement import PlacementResult
bands = PlacementResult.load("reports/audio/placement_center.json").to_pre_nr_bands()
bf = cc.PolarisBeamformer(device=7, calibration_path="polaris_cal.json", pre_nr=True, pre_nr_bands=bands)
```
The pre-NR HPF/notch runs **before** the denoiser (the measurement-first order) and adds **zero**
latency. (See [PRE_NR_CLEANUP_GUIDE.md](PRE_NR_CLEANUP_GUIDE.md).)

### 7. Keep neural cleanup optional
DFN3 / OM-LSA / Wiener / gate and dereverb stay **OFF** unless you opt in (`post_nr=…`, `dereverb=True`).
Linear cleanup (HPF/notch) first; reach for DFN3 only if a measured need remains.

### 8–9. Verify egress + ASR stream
```python
router = cc.EgressRouter(sample_rate=44100.0, wav_path="meeting.wav", asr_rate=16000)
bf = cc.PolarisBeamformer(device=7, agc_target_db=-20.0, output_callback=router.push)
# 48 kHz clean mono = router.latest_pcm16(); 16 kHz ASR-ready = router.drain_asr_pcm16()
```
The router refuses raw multichannel — only processed clean mono is routed.
(See [AUDIO_EGRESS_GUIDE.md](AUDIO_EGRESS_GUIDE.md) + [TRANSCRIPTION_STREAM_GUIDE.md](TRANSCRIPTION_STREAM_GUIDE.md).)

### 10. Export diagnostics
```bash
python scripts/operator_diagnostics.py --placement reports/audio/placement_center.json \
    --calibration polaris_cal.json --pre-nr --agc-target-db -20
# → prints the 7-section status + writes reports/audio/operator_diagnostics_<stamp>.{json,md}
```
(See [AUDIO_OPERATOR_WORKFLOW_GUIDE.md](AUDIO_OPERATOR_WORKFLOW_GUIDE.md).)

---

## Default behaviour (nothing is forced on)

| Feature | Default | Why |
|---|---|---|
| Per-capsule calibration | **OFF** | requires a valid measured profile |
| Pre-NR HPF/notch | **OFF** | room-specific, opt-in |
| Office-AC preset | **OFF** | an example measured-room preset only |
| AEC / transient / dereverb | **OFF** | opt-in per room |
| post-NR (DFN3/OM-LSA/Wiener/gate) | **OFF** | neural/spectral cleanup is downstream + optional |
| PEQ / AGC / voice-gate | **OFF** | opt-in |
| Band-limit (anti-alias LP ~5.6 kHz) | **ON** | fixed array-physics filter (the one default-on stage) |
| Placement check | **Manual** | a diagnostic, not live DSP |
| Egress router | **Optional** | the consumer attaches it |
| Transcription stream | **Optional** | mock/provider seam only |
| Real ASR vendor | **Not bundled** | no network by default |
| Virtual mic | **Not bundled** | external adapter seam only |

With every opt-in feature off, the pipeline is **byte-identical** to the pre-Phase-1 behaviour.

---

## Pipeline order (the real, verified order)

```
capture (8ch)
 → uniform preamp
 → per-capsule calibration            (Phase 1; OFF by default)
 → DOA / beamforming / null steering
 → AEC → transient                    (OFF by default)
 → dereverb                           (OFF by default)
 → pre-NR HPF/notch                   (Phase 2; OFF by default; BEFORE the denoiser)
 → post-NR / DFN3 / OM-LSA / Wiener / gate   (OFF by default)
 → PEQ                                (OFF by default; tone, AFTER cleaning)
 → AGC / limiter → [zone-gain]        (OFF by default; zone-gain is a post-AGC per-zone trim)
 → band-limit                         (ON; anti-alias)
 → voice-gate                         (OFF by default; last)
 → processed clean MONO
 → EgressRouter (Phase 4)  → 48 kHz PCM / 16 kHz ASR int16 / WAV / external sink
 → TranscriptionStream (Phase 5) → VAD/chunk → provider (mock)
```
Note vs the brief: a `zone-gain` trim sits between AGC and band-limit (post-AGC per-zone gain, default
off) — documented here rather than omitted.

---

## Honest limits (state these to stakeholders)

- Beamforming/nulls **attenuate** off-axis sound; they are not a perfect acoustic wall.
- Placement diagnoses noise/placement, not full room acoustics (no RT60/echo-path measurement).
- Calibration's gain alignment is robust; polarity/delay need a controlled stimulus.
- The ASR VAD is a lightweight energy gate; transcription uses a mock provider (no bundled vendor, no
  network by default).
- The full desktop GUI is verified in CI; locally only single-panel probes run (MainWindow hangs
  headless on Windows, per `CLAUDE.md`).
