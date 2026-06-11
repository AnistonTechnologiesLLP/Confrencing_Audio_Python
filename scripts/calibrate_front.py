"""Find the auto-steer Front offset: measure the bearing of a 'front' talker.

The array's azimuth-0 is arbitrary relative to your desk. Run this while someone
talks from the direction you want to call "front" (the sector centre); it reports
the measured bearing — pass that as ``--front-offset`` to area_autosteer.py (or
type it into the Live tab's Front offset).

    pip install -e ".[control]"

    python scripts/calibrate_front.py --device 7 --radius 0.035
    # → "Front offset: 37°"  then:
    python scripts/area_autosteer.py --device 7 --radius 0.035 --front-offset 37 --monitor
"""
from __future__ import annotations

import argparse
import sys

import conf_pipeline_control as cc

DEAD_CAPSULE = 5
POLARIS_RATE_HZ = 44100.0


def main() -> int:
    ap = argparse.ArgumentParser(description="Measure the 'front' bearing for auto-steer")
    ap.add_argument("--device", type=int, required=True, help="input device index (see device_check.py)")
    ap.add_argument("--radius", type=float, required=True, help="REAL capsule-circle radius, m")
    ap.add_argument("--rate", type=float, default=POLARIS_RATE_HZ, help="sample rate (POLARIS = 44100)")
    ap.add_argument("--seconds", type=float, default=4.0, help="recording length")
    ap.add_argument("--off-nadir", type=float, default=90.0, help="look elevation (90 = horizontal desk)")
    ap.add_argument("--dead", type=int, default=DEAD_CAPSULE, help="dead capsule index to mask off")
    args = ap.parse_args()

    if not cc.controls_available():
        sys.exit('Calibration needs numpy + sounddevice. Install:\n    pip install -e ".[control]"')

    geom = cc.with_active_channels(cc.sensibel_8(radius_m=args.radius), [i != args.dead for i in range(8)])
    print(f"Recording {args.seconds:.0f}s — have someone talk from the FRONT now…")
    y8 = cc.record_clip(args.device, int(args.rate), args.seconds, channels=geom.n_channels)
    res = cc.detect_offline(y8, int(args.rate), geom, off_nadir_deg=args.off_nadir, max_talkers=1)
    if not res.detections:
        print("No clear talker detected — try again, louder / closer.")
        return 1
    d = res.detections[0]
    print(f"\nFront offset: {d.azimuth_deg:.0f}°   (salience {d.salience_db:.0f} dB)")
    print(f"Use it:  python scripts/area_autosteer.py --device {args.device} "
          f"--radius {args.radius} --front-offset {d.azimuth_deg:.0f} --monitor")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
