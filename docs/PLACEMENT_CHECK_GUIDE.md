# Placement Check Guide

Phase 3 of the audio front-end hardening. Before a meeting, record a few seconds of **room noise**
(no speech) and let the placement check tell you whether the POLARIS array is in a good spot —
**GOOD / ACCEPTABLE / BAD** — with reasons, recommendations, and notch/HPF suggestions.

> **Diagnostics only.** The check is a pure analyzer + CLI. It never changes the live audio pipeline.
> Its suggestions are exportable and fed to Phase 2's pre-NR stage **only if you opt in**.

---

## 1. Run a check

```bash
pip install -e ".[control]"

# live capture (ask the room to stay quiet for the recording):
python scripts/check_placement.py --device 7 --seconds 10 --label "Table center"

# ...or analyze a recorded 8-channel room-noise WAV:
python scripts/check_placement.py --wav room_noise_8ch.wav --out reports/audio/placement_center.json
```

Example output:

```
Placement: BAD
Score: 31/100   (Table center)
Noise: -21.9 dBFS   rumble -21.9   speech -63.9   hiss -60.4
Detected tones (Hz): 60, 141
Reasons:
  - Strong low-frequency rumble (54 dB over the speech band)
  - Tonal peak(s) at 60, 141 Hz (likely HVAC/fan)
Recommendations:
  - Move the array off the desk / away from AC ducts and structural vibration
  - Move the array 0.5–1 m away from airflow/vent/fan and re-check
  - Consider notch filters at 60, 141 Hz (pre-NR stage)
Suggested pre-NR cleanup (opt-in; re-measure per room):
  HPF: 120 Hz
  Notches: 60 Hz, 141 Hz
```

Flags: `--label`, `--out`, `--seconds`/`--duration`, `--rate`/`--sample-rate`, `--json`, `--markdown`.

---

## 2. Survey mode — compare positions

Save a result per position, then compare and pick the best:

```bash
python scripts/check_placement.py --wav pos_a.wav --label "Position A" --out reports/audio/placement_a.json
python scripts/check_placement.py --wav pos_b.wav --label "Position B" --out reports/audio/placement_b.json
python scripts/check_placement.py --wav pos_c.wav --label "Position C" --out reports/audio/placement_c.json

python scripts/check_placement.py --compare reports/audio/placement_*.json
# label            status      score
# Position C       GOOD          91
# Position B       ACCEPTABLE    76
# Position A       BAD           42
# Recommended position: Position C  (GOOD, 91/100)
```

---

## 3. What it measures

| metric | band / rule | what it catches |
|---|---|---|
| total noise RMS | full-band | overall loudness of the room |
| speech-band noise | 300–3400 Hz | noise where the voice lives |
| low-frequency rumble | 20–200 Hz | desk thump, AC duct / structural vibration |
| broadband hiss | 1000–8000 Hz (capped at Nyquist) | airy fan hiss, noisy cabling |
| tonal peaks | 50–1000 Hz, prominence ≥ 9 dB | fan/HVAC tones (→ notch suggestions) |
| clipping risk | \|sample\| ≥ 0.99 | input gain too high / a loud nearby source |
| channel imbalance | per-capsule RMS vs median | a blocked/covered capsule or very local airflow |
| local hotspot | heuristic | one loud capsule + tones/hiss → something close to one side |

**Why it's robust to record gain:** scoring keys off *bandwidth-normalised density ratios* (rumble-vs-
speech, hiss-vs-speech) and *peak prominence* — both gain- and bandwidth-independent. A flat, quiet
room scores GOOD whether you recorded at −60 or −40 dBFS. The dBFS fields are reported for reference.

---

## 4. Scoring & statuses

Deterministic, no ML. Start at 100, subtract documented penalties:

| issue | penalty |
|---|---|
| rumble: moderate / strong | −12 / −30 |
| each tonal peak (capped) | −12 (max −40) |
| hiss: elevated / high | −8 / −20 |
| imbalance: moderate / strong | −8 / −20 |
| near clipping | −35 |
| very loud room (≥ −35 dBFS) | −15 |

`score ≥ 85 → GOOD`, `60–84 → ACCEPTABLE`, `< 60 → BAD`. Every penalty adds a human-readable reason.

---

## 5. Feeding the suggestions into pre-NR (opt-in)

The detected tones become notch suggestions and (if rumble is flagged) an HPF suggestion. Convert them
straight to Phase 2 pre-NR bands:

```python
import conf_pipeline_control as cc
from conf_pipeline_control.placement import PlacementResult

result = PlacementResult.load("reports/audio/placement_center.json")
bands = result.to_pre_nr_bands()                 # HPF 120 Hz + a notch per detected tone
bf = cc.PolarisBeamformer(device=7, pre_nr=True, pre_nr_bands=bands)
```

> ⚠️ **These tones are THIS room's.** Nothing is applied automatically and nothing is a global default.
> Re-run the check (and regenerate the bands) for every room — a different AC/fan has different lines.

---

## 6. Honest limits

- It diagnoses **noise/placement**, not acoustics in full — it won't measure RT60 or echo paths.
- Tone frequency is reported to within the analysis bin resolution (a few Hz) — fine for a notch.
- Polarity/level imbalance can have benign causes; the hotspot result is a **heuristic**, not a verdict.
- Capture **room noise without speech** — speech energy invalidates the noise-band metrics.
- It does not claim perfect room diagnosis; when a finding is uncertain it is not asserted as fact.
