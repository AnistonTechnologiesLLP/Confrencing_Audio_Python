"""Live multi-azimuth detection + in-area voice extraction on the POLARIS 8-array.

Scans azimuth in real time (SRP-PHAT), shows where people are talking, and steers
beams at the ones inside your coverage **sector** (center ± half-width) while
nulling the ones outside. This is the "listen only to the people in this area"
feature for a desk/table array.

    pip install -e ".[control]"        # numpy + sounddevice

    # Find the device index first (must show 8 channels @ 44100):
    python scripts/device_check.py

    # Front of the desk, ±60° sector, monitor on headphones:
    python scripts/area_autosteer.py --device 7 --radius 0.035 \
        --sector-center 0 --sector-width 120 --front-offset 0 --monitor

Sector is given as a full width (``--sector-width 120`` = ±60°). ``--front-offset``
rotates azimuth-0 to your desk's "front." Off-nadir defaults to 90° (horizontal),
right for a desk array. IMPORTANT: monitor on HEADPHONES to avoid feedback.

The console shows a live readout: each detected talker's bearing, its salience,
and whether it's IN or OUT of the sector. In-sector talkers are captured; when
nobody is in the sector the output is muted (``--no-gate`` to keep the last beam).
"""
from __future__ import annotations

import argparse
import sys
import time

import conf_pipeline_control as cc

DEAD_CAPSULE = 5
POLARIS_RATE_HZ = 44100.0


def build_geometry(radius_m: float, dead: int, n: int = 8):
    geom = cc.sensibel_8(radius_m=radius_m)
    return cc.with_active_channels(geom, [i != dead for i in range(n)])


def _format_detections(dets, sector) -> str:
    if not dets:
        return "  (silence — nobody detected)"
    parts = []
    for d in dets:
        tag = "IN " if d.in_sector else "out"
        parts.append(f"[{tag} {d.azimuth_deg:5.0f}° {d.salience_db:4.1f}dB]")
    n_in = sum(1 for d in dets if d.in_sector)
    return f"  {n_in} in-area · " + " ".join(parts)


def main() -> int:
    ap = argparse.ArgumentParser(description="Live multi-azimuth detect + in-area extraction")
    ap.add_argument("--device", type=int, required=True, help="input device index (see device_check.py)")
    ap.add_argument("--radius", type=float, required=True,
                    help="REAL capsule-circle radius in metres (NOT the 0.05 placeholder)")
    ap.add_argument("--rate", type=float, default=POLARIS_RATE_HZ, help="sample rate (POLARIS = 44100)")
    ap.add_argument("--sector-center", type=float, default=0.0, help="coverage arc centre bearing, deg")
    ap.add_argument("--sector-width", type=float, default=120.0, help="coverage arc FULL width, deg (±half)")
    ap.add_argument("--front-offset", type=float, default=0.0, help="rotate azimuth-0 to the desk front, deg")
    ap.add_argument("--off-nadir", type=float, default=90.0, help="look elevation, deg (90 = horizontal)")
    ap.add_argument("--max-talkers", type=int, default=3, help="max simultaneous azimuths to track")
    ap.add_argument("--min-separation", type=float, default=40.0, help="min angular separation, deg (resolution)")
    ap.add_argument("--freq", type=float, default=cc.DEFAULT_DESIGN_FREQ_HZ, help="beam design frequency, Hz")
    ap.add_argument("--loading", type=float, default=0.05, help="diagonal loading (raise if it hisses)")
    ap.add_argument("--update-hz", type=float, default=8.0, help="re-steer rate")
    ap.add_argument("--dead", type=int, default=DEAD_CAPSULE, help="dead capsule index to mask off")
    ap.add_argument("--no-gate", action="store_true", help="don't mute when nobody is in the sector")
    ap.add_argument("--monitor", action="store_true", help="play the extracted audio (use HEADPHONES)")
    ap.add_argument("--output-device", type=int, default=None, help="monitor output device index")
    ap.add_argument("--record", default=None, help="path to record the extracted mono WAV")
    ap.add_argument("--preamp-gain-db", type=float, default=0.0,
                    help="manual mic-input level trim (dB) before the beamformer — software gain; "
                         "does NOT improve SNR (the output AGC cancels it when on). 0 = no change")
    args = ap.parse_args()

    if not cc.controls_available():
        sys.exit('Live audio needs numpy + sounddevice. Install:\n    pip install -e ".[control]"')

    geom = build_geometry(args.radius, args.dead)
    sector = cc.SectorConfig(
        center_deg=args.sector_center,
        half_width_deg=args.sector_width / 2.0,
        front_offset_deg=args.front_offset,
    )
    ctrl = cc.AutoSteerController(
        geom, sector,
        device=args.device,
        samplerate=args.rate,
        off_nadir_deg=args.off_nadir,
        max_talkers=args.max_talkers,
        min_separation_deg=args.min_separation,
        freq_hz=args.freq,
        loading=args.loading,
        update_hz=args.update_hz,
        gate_when_empty=not args.no_gate,
        monitor=args.monitor,
        output_device=args.output_device,
        record_path=args.record,
        preamp_gain_db=args.preamp_gain_db,
    )

    print(
        f"Array: {geom.n_active}/{geom.n_channels} capsules, aperture "
        f"{geom.aperture_m()*100:.1f} cm. Sector: {args.sector_center:.0f}° "
        f"±{args.sector_width/2:.0f}° (front-offset {args.front_offset:.0f}°)."
    )
    print(f"Opening device {args.device} @ {args.rate:.0f} Hz ... Ctrl+C to stop.")
    if args.monitor:
        print("Monitoring live — wear HEADPHONES to avoid feedback.")

    ctrl.start()
    try:
        while True:
            time.sleep(1.0 / max(1.0, args.update_hz))
            dets = ctrl.detections()
            line = _format_detections(dets, sector)
            lvl = ctrl.read_level()
            err = f"  !{ctrl.error}" if ctrl.error else ""
            print(f"\r{line}  | lvl {lvl:4.2f}{err}      ", end="", flush=True)
    except KeyboardInterrupt:
        print("\nStopping ...")
    finally:
        ctrl.stop()
    if args.record:
        print(f"Recorded extracted audio → {args.record}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
