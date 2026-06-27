"""Estimate a per-capsule calibration profile (gain / polarity / integer-delay) for the POLARIS array.

Records a few seconds of broadband room sound (or reads an existing multichannel WAV), estimates
per-capsule corrections relative to a reference capsule, prints a confidence summary, and writes a
``CalibrationProfile`` JSON you load at runtime:

    LiveBeamController(geom, calibration_path="polaris_cal.json")
    PolarisBeamformer(..., calibration_path="polaris_cal.json")

Install + run:

    pip install -e ".[control]"

    # live capture — play a steady broadband source (pink noise, or move speech around the array):
    python scripts/calibrate_capsules.py --device 7 --seconds 5 --out polaris_cal.json

    # ...or from an existing 8-channel WAV:
    python scripts/calibrate_capsules.py --wav capture8.wav --out polaris_cal.json

NOTES
  * GAIN alignment is robust on any broadband capture.
  * POLARITY / DELAY are only meaningful for a controlled, co-located stimulus; on a diffuse room
    capture their confidence is low and those corrections are withheld (the estimator never fakes
    certainty — low-confidence channels are left uncorrected).
  * Per the repo's hardware notes, standalone capture of all 8 POLARIS channels can fail on
    WDM-KS/DirectSound; if ``--device`` capture errors, record through the running engine / app and
    feed the resulting WAV with ``--wav`` instead.
"""
from __future__ import annotations

import argparse
import sys
import wave
from dataclasses import replace
from datetime import datetime

import conf_pipeline_control as cc
from conf_pipeline_control.calibration import estimate_calibration

POLARIS_RATE_HZ = 44100.0


def _read_wav(path: str):
    """Read a 16-bit PCM multichannel WAV → (np.float32 (N, channels), sample_rate)."""
    import numpy as np

    with wave.open(path, "rb") as w:
        ch = w.getnchannels()
        n = w.getnframes()
        sr = w.getframerate()
        raw = w.readframes(n)
    x = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
    return x.reshape(-1, ch), float(sr)


def main() -> int:
    ap = argparse.ArgumentParser(description="Estimate a per-capsule calibration profile")
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--device", type=int, help="input device index (see scripts/device_check.py)")
    src.add_argument("--wav", type=str, help="read an existing multichannel WAV instead of recording")
    ap.add_argument("--rate", type=float, default=POLARIS_RATE_HZ, help="sample rate (POLARIS = 44100)")
    ap.add_argument("--seconds", type=float, default=5.0, help="recording length")
    ap.add_argument("--channels", type=int, default=8, help="capsule count")
    ap.add_argument("--ref", type=int, default=None, help="reference channel (default: auto / median RMS)")
    ap.add_argument("--dead", type=int, default=None, help="dead capsule index to skip (kept identity)")
    ap.add_argument("--no-delay", action="store_true", help="skip integer-sample delay estimation")
    ap.add_argument("--no-polarity", action="store_true", help="skip polarity estimation")
    ap.add_argument("--out", type=str, default="capsule_calibration.json", help="output profile path")
    args = ap.parse_args()

    if not cc.controls_available():
        sys.exit('Calibration needs numpy + sounddevice. Install:\n    pip install -e ".[control]"')

    if args.wav:
        y8, rate = _read_wav(args.wav)
        print(f"Read {y8.shape[0]} frames x {y8.shape[1]} ch from {args.wav} @ {rate:.0f} Hz")
    else:
        rate = float(args.rate)
        print(f"Recording {args.seconds:.0f}s — play a steady broadband source now "
              f"(pink noise, or move speech around the array)…")
        y8 = cc.record_clip(args.device, int(rate), args.seconds, channels=args.channels)

    active = None
    if args.dead is not None:
        active = [i != args.dead for i in range(y8.shape[1])]

    est = estimate_calibration(
        y8, sample_rate=rate, reference_channel=args.ref, active_mask=active,
        estimate_delay=not args.no_delay, estimate_polarity=not args.no_polarity,
    )
    p = est.profile

    print(f"\nReference capsule: {est.reference_channel}")
    print(f"{'ch':>3} {'gainDb':>8} {'pol':>4} {'delay':>6} {'pConf':>6} {'dConf':>6}")
    for c in range(p.channels):
        flag = "  LOW-CONF" if c in est.low_confidence_channels else ""
        print(f"{c:>3} {p.gain_db[c]:>8.2f} {p.polarity[c]:>4} {p.delay_samples[c]:>6} "
              f"{est.polarity_confidence[c]:>6.2f} {est.delay_confidence[c]:>6.2f}{flag}")
    if est.low_confidence_channels:
        print(f"\nLow-confidence channels {list(est.low_confidence_channels)} left UNCORRECTED "
              f"(no fake certainty).")

    p = replace(p, created_at=datetime.now().isoformat(timespec="seconds"))
    p.save(args.out)
    print(f"\nSaved -> {args.out}")
    print(f"Use it:  PolarisBeamformer(..., calibration_path={args.out!r})  /  "
          f"LiveBeamController(geom, calibration_path={args.out!r})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
