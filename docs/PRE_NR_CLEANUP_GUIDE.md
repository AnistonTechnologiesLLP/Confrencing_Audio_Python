# Pre-NR Linear Cleanup Guide (HPF + notches before the denoiser)

Phase 2 of the audio front-end hardening. The **pre-NR linear cleanup** stage runs cheap, predictable
linear filters — a speech high-pass and narrow notches — **before** the post-NR / DFN3 / OM-LSA / gate
denoiser.

> **Why (measurement-first).** Low-frequency rumble and tonal HVAC/fan lines are *predictable* noise a
> 2nd-order biquad removes for free. Spending a neural denoiser's capacity on them is wasteful and can
> muffle speech. So the correct order is: dereverb → **HPF/notch (linear)** → denoiser → tone PEQ.
> DFN3 is not the first fix.

The stage **is** a second [`StreamingPeq`](../conf_pipeline_control/peq.py) instance (it already has
`highpass` and `bell` filters, exact zero-latency IIR). It is **OFF by default** — a bit-exact no-op
until you pass bands.

---

## 1. Turning it on

Both DSP chains accept the same two opt-in params (mirrors `peq`/`peq_bands`):

```python
import conf_pipeline_control as cc

bands = cc.build_pre_nr_bands(hpf_hz=120.0, notches=[102.0, 140.0, 177.0])

# steered / A-B engine + multi-array controllers:
bf = cc.PolarisBeamformer(device=13, pre_nr=True, pre_nr_bands=bands)

# zone "Whole table" + auto-steer path:
lc = cc.LiveBeamController(geom, pre_nr=True, pre_nr_bands=bands)
```

Bands use the same dict shape as PEQ: `{"freqHz", "gainDb", "q", "type"}`. The builders make intent
clear:

| helper | makes |
|---|---|
| `hpf_band(freq_hz, q=0.707)` | a 2nd-order high-pass (rumble removal) |
| `notch_band(freq_hz, q=8.0, depth_db=12.0)` | a narrow dip (a tonal interferer); depth is always attenuation |
| `build_pre_nr_bands(hpf_hz=None, notches=None)` | HPF first, then a notch per entry |

`notches` entries may be a bare frequency, a `(freq, q, depth_db)` tuple, or a
`{"freqHz","q","depthDb"}` dict.

---

## 2. The Office-AC / HVAC preset — **a measured-room example, not a global default**

```python
bf = cc.PolarisBeamformer(device=13, pre_nr=True, pre_nr_bands=cc.office_ac_preset())
# office_ac_preset() == HPF 120 Hz + notches at 102 / 140 / 177 Hz
```

> ⚠️ **These tones are from ONE room's HVAC survey.** They are **not** universal constants and are
> **never** applied as a global default — you must opt in by passing them. Re-measure per room: a
> different AC/fan has different lines. **Phase 3's placement check reports the actual tonal peaks**,
> which you then feed straight in: `build_pre_nr_bands(notches=detected_tones)`.

Hardcoding the previous room's tones into a new room is a bug, not a feature.

---

## 3. Where it sits

```
… → dereverb (if enabled)
  → PRE-NR HPF / notch   ← this stage (linear, zero-latency)
  → post-NR / DFN3 / OM-LSA / Wiener / gate (if enabled)
  → PEQ (existing tone-shaping, after cleaning)
  → AGC → band-limit → voice-gate → output
```

The existing post-NR PEQ is unchanged — it stays after the denoiser for tone-shaping. This stage is a
separate, earlier instance.

---

## 4. Behaviour & safety

- **Default OFF** ⇒ bit-exact pass-through (the pipeline is byte-identical without it).
- **Zero added latency** — IIR biquads, no lookahead. The latency estimate is unchanged.
- **Invalid bands** (0 Hz / non-positive Q / unknown type) are dropped to a safe no-op.
- **HPF** keeps the speech band: a 120 Hz HPF cuts a 50 Hz rumble hard, leaves 1 kHz essentially
  untouched.
- **Notches** are narrow (Q≈8): they dip the named tone and leave the speech either side of it.
- Does **not** enable DFN3 or dereverb — those remain opt-in and off by default.

---

## 5. Picking values

- **HPF**: 80–120 Hz is a safe speech high-pass (male fundamentals start ~85 Hz; go no higher than
  ~120 Hz unless you've confirmed no low speech energy matters). The placement check's low-frequency
  rumble estimate (Phase 3) tells you if you need it.
- **Notches**: only for *steady tonal* lines (fans, transformer hum at 50/100 Hz, AC compressor
  harmonics). Use the placement check's detected-tone list. Keep depth modest (≤ ~15 dB) and Q narrow
  so speech harmonics nearby survive. Broadband hiss is **not** a notch target — leave that to the
  denoiser.
