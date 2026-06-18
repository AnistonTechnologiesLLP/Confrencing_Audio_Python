"""Hardware-in-the-loop smoke test for the live enhancement chain.

Runs the :class:`BeamEngine` on the real POLARIS array for a few seconds with the
real-time **dereverb** + **AI (OM-LSA) cleaner** enabled (beam → dereverb → cleaner
→ AGC), drains the mono output, and reports that it runs end-to-end on the actual
device without errors and produces finite audio at a sane level. Objective only
(no subjective listening) — it validates the *integration* on hardware; DSP
correctness is covered by the unit tests.

    python scripts/validate_live_enhance.py                 # auto-try MME/DirectSound/WDM-KS 8-ch entries
    python scripts/validate_live_enhance.py --device 1 --seconds 4
    python scripts/validate_live_enhance.py --no-dereverb   # cleaner only (A/B the dereverb stage)

Needs the [control] extra (numpy + sounddevice). Set PYTHONIOENCODING=utf-8 on a
cp1252 Windows console.
"""
from __future__ import annotations

import argparse
import time


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--device", type=int, default=None, help="input device index (8-ch POLARIS)")
    ap.add_argument("--rate", type=float, default=44100.0)
    ap.add_argument("--seconds", type=float, default=4.0)
    ap.add_argument("--no-dereverb", action="store_true", help="disable the dereverb stage (cleaner only)")
    ap.add_argument("--engine", default="omlsa", choices=("omlsa", "gate", "wiener"))
    args = ap.parse_args(argv)

    import numpy as np

    import conf_pipeline_control as cc

    if not cc.controls_available():
        print("Live audio needs the [control] extra (numpy + sounddevice).")
        return 2

    blocks: list = []

    def collect(mono):
        blocks.append(np.asarray(mono, dtype=float).copy())

    cfg = {"post_nr": True, "post_nr_engine": args.engine, "dereverb": not args.no_dereverb}
    candidates = [args.device] if args.device is not None else [1, 8, 19]
    eng = None
    used = None
    last_err = None
    for dev in candidates:
        try:
            e = cc.BeamEngine(device=dev, fs=args.rate, mode="steered",
                              steered_cfg=cfg, output_callback=collect)
            e.start()
            eng, used = e, dev
            break
        except Exception as exc:  # try the next host-API entry for the array
            last_err = exc
    if eng is None:
        print(f"Could not open any candidate input device {candidates}: {last_err}")
        return 1

    print(f"Opened device {used} @ {args.rate:.0f} Hz; chain = beam → "
          f"{'dereverb → ' if not args.no_dereverb else ''}{args.engine} cleaner → AGC.")
    print(f"Capturing {args.seconds:.1f}s of live audio ...")
    try:
        time.sleep(args.seconds)
    finally:
        eng.stop()

    if not blocks:
        print("WARNING: no output blocks received — the stream produced nothing.")
        return 1
    y = np.concatenate(blocks)
    rms = float(np.sqrt(np.mean(y * y))) if y.size else 0.0
    peak = float(np.max(np.abs(y))) if y.size else 0.0
    finite = bool(np.all(np.isfinite(y)))
    drv = getattr(eng._steered, "_dereverb", None)
    nr = getattr(eng._steered, "_post_nr", None)
    rms_db = 20.0 * np.log10(rms + 1e-12)
    print(f"blocks={len(blocks)}  samples={y.size} ({y.size / args.rate:.2f}s)  "
          f"rms={rms_db:.1f} dBFS  peak={peak:.3f}  finite={finite}")
    print(f"dereverb engaged={getattr(drv, '_engaged', None)}  "
          f"cleaner engaged={getattr(nr, '_engaged', None)}  cleaner mode={getattr(nr, 'mode', None)}")
    ok = finite and y.size > 0
    print("LIVE CHAIN OK" if ok else "LIVE CHAIN FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
