"""Live coverage-area voice isolation on a desk/table POLARIS 8-array.

End-to-end Stage 2-3 path: build the array with the REAL radius and the dead
capsule masked off, design a beam toward a chosen bearing (and null others),
then capture → beamform → monitor live. This is the "listen only to the people
in this area" feature for a desk array.

    pip install -e ".[control]"        # numpy + sounddevice

    # Find the device index first (must show 8 channels):
    python scripts/device_check.py

    # Listen toward the front of the desk (azimuth 0°), monitor on headphones:
    python scripts/desk_isolation.py --device 7 --radius 0.035 --azimuth 0 --monitor

    # Reject two interferers behind/beside you, and record the isolated voice:
    python scripts/desk_isolation.py --device 7 --radius 0.035 --azimuth 0 \
        --null 150 --null 210 --monitor --record out.wav

Azimuth is a compass bearing (0° = array's +Y / "front", clockwise). Off-nadir
defaults to 90° (horizontal) — right for a desk array whose capsules lie flat
and whose talkers are across the table. IMPORTANT: monitor on HEADPHONES; routing
the beam to a speaker in the same room feeds back.
"""
from __future__ import annotations

import argparse
import sys
import time

import conf_pipeline_control as cc

# The array's dead channel (sensiBel POLARIS capsule index 5 is silent).
DEAD_CAPSULE = 5
POLARIS_RATE_HZ = 44100.0


def build_geometry(radius_m: float, dead: int, n: int = 8) -> "cc.ArrayGeometry":
    """Real circular geometry with one capsule masked inactive."""
    geom = cc.sensibel_8(radius_m=radius_m)
    mask = [i != dead for i in range(n)]
    return cc.with_active_channels(geom, mask)


def main() -> int:
    ap = argparse.ArgumentParser(description="Live desk-array voice isolation")
    ap.add_argument("--device", type=int, required=True, help="input device index (see device_check.py)")
    ap.add_argument("--radius", type=float, required=True,
                    help="REAL capsule-circle radius in metres (NOT the 0.05 placeholder)")
    ap.add_argument("--rate", type=float, default=POLARIS_RATE_HZ, help="sample rate (POLARIS = 44100)")
    ap.add_argument("--azimuth", type=float, default=0.0, help="look bearing, deg (0 = front, CW)")
    ap.add_argument("--off-nadir", type=float, default=90.0, help="look elevation, deg (90 = horizontal)")
    ap.add_argument("--null", type=float, action="append", default=[],
                    help="bearing(s) to reject, deg; repeatable")
    ap.add_argument("--freq", type=float, default=cc.DEFAULT_DESIGN_FREQ_HZ, help="design frequency, Hz")
    ap.add_argument("--mode", choices=[cc.MODE_SUPERDIRECTIVE, cc.MODE_DELAYSUM],
                    default=cc.MODE_SUPERDIRECTIVE)
    ap.add_argument("--loading", type=float, default=0.05, help="diagonal loading (raise if it hisses)")
    ap.add_argument("--dead", type=int, default=DEAD_CAPSULE, help="dead capsule index to mask off")
    ap.add_argument("--monitor", action="store_true", help="play the isolated beam (use HEADPHONES)")
    ap.add_argument("--output-device", type=int, default=None, help="monitor output device index")
    ap.add_argument("--record", default=None, help="path to record the isolated mono WAV")
    ap.add_argument("--preamp-gain-db", type=float, default=0.0,
                    help="manual mic-input level trim (dB) before the beamformer — software gain; "
                         "does NOT improve SNR (the output AGC cancels it when on). 0 = no change")
    args = ap.parse_args()

    if not cc.controls_available():
        sys.exit('Live audio needs numpy + sounddevice. Install:\n    pip install -e ".[control]"')

    geom = build_geometry(args.radius, args.dead)
    look = cc.bearing_direction(args.azimuth, args.off_nadir, label="coverage-area")
    nulls = [cc.bearing_direction(az, args.off_nadir) for az in args.null]

    design = cc.design_from_bearings(
        geom, look, nulls,
        freq_hz=args.freq, mode=args.mode, loading=args.loading, array_id="POLARIS",
    )
    print(design.summary())
    print(f"\nAperture {geom.aperture_m()*100:.1f} cm, {geom.n_active}/{geom.n_channels} capsules active.")
    if look.off_nadir_deg < 30:
        print("Note: off-nadir < 30° is a downward (ceiling) look, not a desk look.")

    # Lazy import: only needs the [control] extra here.
    from conf_pipeline_control.live import LiveBeamController

    ctrl = LiveBeamController(
        geom,
        device=args.device,
        samplerate=args.rate,           # NEVER rely on the 48000 default for POLARIS
        record_path=args.record,
        monitor=args.monitor,
        output_device=args.output_device,
        preamp_gain_db=args.preamp_gain_db,
    )
    ctrl.apply_design(design)
    print(f"\nOpening device {args.device} @ {args.rate:.0f} Hz ... Ctrl+C to stop.")
    if args.monitor:
        print("Monitoring live — wear HEADPHONES to avoid feedback.")
    ctrl.connect()
    try:
        while True:
            time.sleep(0.5)
            print(f"\r  output level: {ctrl.read_level():5.3f}   ", end="", flush=True)
    except KeyboardInterrupt:
        print("\nStopping ...")
    finally:
        ctrl.disconnect()
    if args.record:
        print(f"Recorded isolated voice → {args.record}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
