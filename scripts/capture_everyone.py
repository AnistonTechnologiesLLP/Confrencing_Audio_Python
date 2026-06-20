"""Live **capture everyone**: form a beam per talker, snap each to a room seat, NOM-automix them into
one feed, and (optionally) record a separate per-person WAV track for each beam.

    python scripts/capture_everyone.py --device 8 --radius 0.04 --beams 3
    python scripts/capture_everyone.py --device 8 --radius 0.04 --config room.json --record out/

Needs the [control] extra + a connected POLARIS. Seats (for the hybrid snap) come from a saved config
(--config); without one it runs free-DOA (snap is inert). Honest limit: the ~40 mm array separates
2-3 well-spaced talkers — people closer than ~40-50° merge into one beam.
"""
import argparse
import json
import sys
import time

import conf_pipeline as cp
import conf_pipeline_control as cc
from conf_pipeline.model import MicrophoneArray, Point2D


def _load_config(path, bearing):
    if path:
        from conf_pipeline import persistence

        with open(path, encoding="utf-8") as f:
            config = persistence.deserialize(json.load(f))
        array_id = next((d.id for d in config.devices if isinstance(d, MicrophoneArray)), None)
        return config, array_id
    config = cp.create_config("capture-everyone", "2026-01-01T00:00:00Z")
    config = cp.add_device(config, cp.create_microphone_array("A", "POLARIS", position=Point2D(0.0, 0.0)))
    config = cp.set_array_bearing(config, "A", float(bearing))
    return config, "A"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Live multi-talker capture (one feed + per-person tracks)")
    ap.add_argument("--device", type=int, required=True, help="input device index (see device_check.py)")
    ap.add_argument("--radius", type=float, required=True, help="REAL capsule-circle radius in metres")
    ap.add_argument("--rate", type=float, default=44100.0, help="sample rate (POLARIS = 44100)")
    ap.add_argument("--beams", type=int, default=3, help="max simultaneous beams (2-3 is the array's ceiling)")
    ap.add_argument("--dead", type=int, default=None, help="dead capsule index to mask off (e.g. 4)")
    ap.add_argument("--config", default=None, help="saved room config (.json) for seats + array bearing")
    ap.add_argument("--bearing", type=float, default=0.0, help="array mounting bearing if no --config, deg")
    ap.add_argument("--no-snap", action="store_true", help="disable seat snap (pure free-DOA aiming)")
    ap.add_argument("--agc-target-db", type=float, default=None, help="target-loudness AGC on the mixed feed")
    ap.add_argument("--record", default=None, help="directory to write per-person WAV tracks on exit")
    args = ap.parse_args(argv)

    if not cc.controls_available():
        sys.exit('Live audio needs the [control] extra:\n    pip install -e ".[control]"')

    config, array_id = _load_config(args.config, args.bearing)
    if array_id is None:
        sys.exit("no microphone array found in the config")

    ctl = cc.MultiBeamController(
        config, array_id, device=args.device, radius_m=args.radius, sample_rate=args.rate,
        n_beams=args.beams, dead_capsule=args.dead, snap=not args.no_snap, agc_target_db=args.agc_target_db,
    )
    recorder = None
    if args.record:
        recorder = cc.MultiTrackRecorder(args.beams, args.rate)
        recorder.start()
        ctl.set_recorder(recorder)

    print(f"Capture everyone: up to {args.beams} beams @ {args.rate:.0f} Hz, "
          f"snap={'off' if args.no_snap else 'on'}{' · recording' if recorder else ''}. Ctrl+C to stop.")
    ctl.start()
    try:
        while True:
            time.sleep(0.4)
            if ctl.error:
                print(f"\nengine error: {ctl.error}")
                break
            live = [b for b in ctl.status() if b.active]
            cells = " ".join(f"[{b.seat_id or f'az{round(b.azimuth_deg)}'}: {b.level:4.2f}]" for b in live)
            print(f"\r  {len(live)} talking  {cells:<70}", end="", flush=True)
    except KeyboardInterrupt:
        print("\nStopping ...")
    finally:
        ctl.stop()
        if recorder is not None:
            recorder.stop()
            paths = recorder.write(args.record, prefix="capture")
            print(f"Wrote {len(paths)} track(s):")
            for p in paths:
                print(f"  {p}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
