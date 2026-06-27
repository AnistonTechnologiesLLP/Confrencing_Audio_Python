# Per-Capsule Calibration Guide

Phase 1 of the audio front-end hardening. This guide covers **per-capsule calibration** — gain,
polarity and integer-sample-delay alignment of the 8 raw MEMS capsules, applied at the **front** of
the live path (right after the uniform preamp, **before** the beamformer, DOA and null steering).

> **Why.** DOA, beamforming and null steering all assume the capsules are gain-aligned,
> phase-consistent, delay-aligned and not polarity-inverted. Before Phase 1 the array was fed raw —
> only a *uniform* preamp gain and a dead-capsule on/off mask sat in front. Real MEMS parts mismatch;
> uncorrected mismatch hollows the beam and biases DOA. Calibration is the cheap, measurement-first
> fix that makes everything downstream work better. It is **OFF by default** and a no-op until you
> supply a profile.

---

## 1. The calibration profile

A small JSON record (camelCase keys, one source of truth on disk and in the GUI). Module:
[conf_pipeline_control/calibration.py](../conf_pipeline_control/calibration.py) →
`CalibrationProfile`.

```json
{
  "version": 1,
  "device": "POLARIS_8MEMS",
  "sampleRate": 48000.0,
  "channels": 8,
  "createdAt": "2026-06-26T17:03:32",
  "gainDb":        [0, 0, 0, 0, 0, 0, 0, 0],
  "delaySamples":  [0, 0, 0, 0, 0, 0, 0, 0],
  "polarity":      [1, 1, 1, 1, 1, 1, 1, 1],
  "referenceChannel": 0,
  "notes": ""
}
```

| field | meaning |
|---|---|
| `gainDb[c]` | per-capsule gain correction (dB). Applied as `10**(gainDb/20)`. |
| `polarity[c]` | `+1` or `-1`; `-1` flips an inverted capsule. |
| `delaySamples[c]` | integer samples to **delay** capsule `c` (≥ 0, causal). |
| `referenceChannel` | the capsule everything else is aligned to. |
| `sampleRate` | the rate the **delays** were measured at (delays don't transfer across rates). |

A profile where every `gainDb` is 0, every `polarity` is +1 and every `delaySamples` is 0 is
**neutral** — calibration stays a bit-exact no-op.

API: `CalibrationProfile.save(path)` / `CalibrationProfile.load(path)` /
`.from_dict` / `.to_dict` / `.from_json` / `.to_json`. A missing or malformed profile raises a single
controlled `CalibrationError`.

---

## 2. Measuring a profile

Use [scripts/calibrate_capsules.py](../scripts/calibrate_capsules.py). Play a steady **broadband**
source (pink noise, or steady speech moved around the array) so every capsule sees comparable energy.

```bash
pip install -e ".[control]"

# live capture:
python scripts/calibrate_capsules.py --device 7 --seconds 5 --out polaris_cal.json

# ...or from an existing multichannel WAV (preferred on POLARIS — see the hardware note below):
python scripts/calibrate_capsules.py --wav capture8.wav --out polaris_cal.json
```

It prints a per-capsule table with confidence and writes the profile:

```
ch   gainDb  pol  delay  pConf  dConf
 0     0.00    1      0   1.00   1.00
 1    -6.02    1      0   1.00   0.00
 3     0.00   -1      0   1.00   0.00
```

What the estimator does ([`estimate_calibration`](../conf_pipeline_control/calibration.py)):

- **gain** — per-capsule RMS vs the reference ⇒ a dB correction (clamped to ±12 dB so a near-dead
  capsule can't demand a huge boost). **Robust on any broadband capture.**
- **polarity** — sign of the normalised cross-correlation peak vs the reference.
- **delay** — integer lag of the cross-correlation peak; corrections delay every capsule to align
  with the latest-arriving one (causal).
- **honest confidence** — a near-silent or decorrelated capsule (`|xcorr| < 0.2`) is **left
  uncorrected** and flagged `LOW-CONF`. The estimator never fakes a correction.

> Gain alignment is the reliable, always-useful output. **Polarity and delay are only meaningful for
> a controlled, co-located stimulus** — on a diffuse room capture the propagation delay between
> capsules dominates the hardware delay, so confidence is low and those corrections are withheld.

---

## 3. Using a profile at runtime

Pass a `CalibrationProfile` object **or** a path. Default is off (byte-identical pipeline). Works on
**both** DSP chains identically:

```python
import conf_pipeline_control as cc
from conf_pipeline_control.calibration import CalibrationProfile

# steered / A-B engine + multi-array controllers (process_block path):
bf = cc.PolarisBeamformer(device=13, calibration_path="polaris_cal.json")

# zone "Whole table" + auto-steer path (_process_block):
prof = CalibrationProfile.load("polaris_cal.json")
lc = cc.LiveBeamController(geom, calibration=prof)
```

The corrected 8-channel stream feeds the beamformer **and** the DOA covariance, so steering, nulls
and the beam all see aligned capsules.

**A/B (calibrated vs uncalibrated):** construct two engines — one with `calibration=…`, one without —
and compare. (A GUI toggle lands in Phase 6.)

---

## 4. Safety & fallback behaviour

Calibration **never crashes the runtime**. It degrades to *off* (and logs to stderr) when:

| situation | behaviour |
|---|---|
| no profile supplied | off — bit-exact pass-through (default) |
| profile file missing / unreadable | off |
| malformed JSON / bad field shape | off (controlled `CalibrationError`, caught) |
| `channels` ≠ engine capsule count | off (unsafe to apply) |
| `sampleRate` ≠ engine rate **with** non-zero delays | delays dropped, **gain/polarity kept** |
| neutral profile | off (no-op) |

**Dead-capsule interaction:** calibration honors the array's active mask. A masked / dead capsule is
forced to identity — it is **never** gained up, delayed or revived, and the estimator skips it. A
profile that only names a correction for a dead capsule reduces to neutral.

**Latency:** the only added latency is the largest `delaySamples` correction
(`CapsuleCalibrator.latency_samples`); zero when no delay corrections are present.

---

## 5. Where it sits in the pipeline

```
raw 8ch capture
  → uniform preamp
  → PER-CAPSULE CALIBRATION   ← gain / polarity / delay align (this guide)
  → DOA / beamforming / null steering
  → (existing cleanup chain: AEC / dereverb / post-NR / PEQ / AGC / band-limit / voice-gate)
  → clean mono output
```

Calibration is mathematically commutative with the uniform preamp (a per-channel gain after a global
scalar), so its exact position relative to the preamp does not change the result; it is placed
immediately after the preamp in both `PolarisBeamformer.process_block` and
`LiveBeamController._process_block`.
