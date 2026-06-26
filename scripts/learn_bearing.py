"""Infer the array ``bearing_deg`` from a DOA measurement of a reference at a KNOWN point.

Records a reference talker standing at a known position for a few seconds, runs offline DOA
to find the dominant azimuth, then solves for the array bearing that makes the geometry
consistent. Print the resulting bearing so you can type it into DESIGN's "Bearing (°)" spin
or pass it to area_autosteer.py.

Usage example (reference talker 2 m directly ahead on the +Y axis, zero horizontal shift):

    python scripts/learn_bearing.py --device 7 --radius 0.035 \\
        --array-x 0 --array-y 0 --ref-x 0 --ref-y 2
    # → Array bearing: 330°   (if the dominant DOA came in at 30°)

Then in DESIGN select the array → "Learn bearing…" which does the same automatically.

    pip install -e ".[control]"
"""
from __future__ import annotations

import argparse
import sys

import conf_pipeline as cp
from conf_pipeline.model import Point2D

DEAD_CAPSULE = 5
POLARIS_RATE_HZ = 44100.0


def main() -> int:
    ap = argparse.ArgumentParser(
        description=(
            "Records a reference talker at a KNOWN point, infers the array bearing.\n"
            "The reference person stands at (--ref-x, --ref-y) and talks for the recording "
            "duration while the script measures the DOA; the bearing is then solved.\n"
            "Requires the [control] extra:  pip install -e \".[control]\""
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--device", type=int, required=True,
                    help="Input device index (run device_check.py to list)")
    ap.add_argument("--radius", type=float, default=0.035,
                    help="POLARIS capsule-circle radius in metres (default: 0.035)")
    ap.add_argument("--rate", type=float, default=POLARIS_RATE_HZ,
                    help="Sample rate in Hz (POLARIS = 44100, default)")
    ap.add_argument("--seconds", type=float, default=4.0,
                    help="Recording length in seconds (default: 4)")
    ap.add_argument("--off-nadir", type=float, default=90.0,
                    help="Look elevation angle in degrees — 90 = horizontal (default: 90)")
    ap.add_argument("--dead", type=int, default=DEAD_CAPSULE,
                    help=f"Dead capsule index to mask off (default: {DEAD_CAPSULE})")
    ap.add_argument("--array-x", type=float, required=True,
                    help="Array position X in metres (room frame)")
    ap.add_argument("--array-y", type=float, required=True,
                    help="Array position Y in metres (room frame)")
    ap.add_argument("--ref-x", type=float, required=True,
                    help="Reference talker position X in metres (room frame)")
    ap.add_argument("--ref-y", type=float, required=True,
                    help="Reference talker position Y in metres (room frame)")
    args = ap.parse_args()

    try:
        import conf_pipeline_control as cc
    except ImportError:
        sys.exit(
            'Learn bearing needs numpy + sounddevice.  Install with:\n'
            '    pip install -e ".[control]"'
        )

    if not cc.controls_available():
        sys.exit(
            'Learn bearing needs numpy + sounddevice.  Install with:\n'
            '    pip install -e ".[control]"'
        )

    array_pos = Point2D(args.array_x, args.array_y)
    ref_point = Point2D(args.ref_x, args.ref_y)

    geom = cc.with_active_channels(
        cc.sensibel_8(radius_m=args.radius),
        [i != args.dead for i in range(8)],
    )

    print(
        f"Recording {args.seconds:.0f} s — have the reference talker stand at "
        f"({ref_point.x}, {ref_point.y}) and talk now…"
    )
    y8 = cc.record_clip(args.device, int(args.rate), args.seconds, channels=geom.n_channels)
    res = cc.detect_offline(y8, int(args.rate), geom, off_nadir_deg=args.off_nadir, max_talkers=1)

    if not res.detections:
        print("No clear talker detected — try again, louder / closer.")
        return 1

    d = res.detections[0]
    bearing = cp.learn_bearing(array_pos, ref_point, d.azimuth_deg)
    print(
        f"\nDOA measured:   {d.azimuth_deg:.1f}°  (salience {d.salience_db:.0f} dB)"
    )
    print(f"Array bearing:  {bearing:.1f}°")
    print(
        "\nSet it in DESIGN → select the array → Bearing (°) spin, "
        "or use the 'Learn bearing…' button to do this automatically."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
