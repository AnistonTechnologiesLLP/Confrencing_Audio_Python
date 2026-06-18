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
    ap.add_argument("--aec", action="store_true", help="enable live echo cancellation (opens a far-end reference)")
    ap.add_argument("--probe-ref", action="store_true",
                    help="just probe the far-end reference: play a test tone and confirm loopback captures it")
    args = ap.parse_args(argv)

    import numpy as np

    import conf_pipeline_control as cc

    if not cc.controls_available():
        print("Live audio needs the [control] extra (numpy + sounddevice).")
        return 2

    if args.probe_ref:
        return _probe_ref(args.rate)

    blocks: list = []

    def collect(mono):
        blocks.append(np.asarray(mono, dtype=float).copy())

    cfg = {"post_nr": True, "post_nr_engine": args.engine, "dereverb": not args.no_dereverb, "aec": args.aec}
    if args.device is not None:
        candidates = [args.device]
    else:                                          # device indices shift across enumerations → find POLARIS by name
        import sounddevice as sd

        devs = sd.query_devices()
        candidates = [i for i, d in enumerate(devs)
                      if int(d.get("max_input_channels", 0)) >= 8
                      and ("pol" in str(d.get("name", "")).lower()
                           or "digital audio interface" in str(d.get("name", "")).lower())]
        candidates += [i for i, d in enumerate(devs)
                       if int(d.get("max_input_channels", 0)) >= 8 and i not in candidates]
        if not candidates:
            print("No 8-channel input device found — is the POLARIS array connected?")
            return 1
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
    if args.aec:
        print(f"AEC reference: {eng.aec_ref_source or '(none — pass-through)'}  ·  ERLE {eng.aec_erle_db:+.1f} dB")
    ok = finite and y.size > 0
    print("LIVE CHAIN OK" if ok else "LIVE CHAIN FAILED")
    return 0 if ok else 1


def _probe_ref(rate: float) -> int:
    """Confirm the far-end reference path works on this machine: open ReferenceCapture, play a 1 s test
    tone through the default output, and verify the loopback ring received it (independent of room
    acoustics — this validates Phase-3a capture, not echo cancellation)."""
    import time

    import numpy as np

    import conf_pipeline_control as cc

    rc = cc.ReferenceCapture(rate)
    rc.start()
    print(f"reference source: {rc.source or '(none)'}  ·  available={rc.available}  err={rc.error or '-'}")
    if not rc.available:
        print("REFERENCE CAPTURE: no WASAPI-loopback / Stereo-Mix source opened on this machine.")
        rc.stop()
        return 1
    try:
        import sounddevice as sd

        t = np.arange(int(rate)) / rate
        tone = (0.2 * np.sin(2 * np.pi * 440.0 * t)).astype(np.float32)
        sd.play(tone, samplerate=int(rate))
        time.sleep(1.2)
        sd.stop()
    except Exception as exc:
        print(f"(could not play a test tone: {exc}; relying on whatever audio is currently playing)")
        time.sleep(1.0)
    captured = rc.recent(int(rate))
    rc.stop()
    crms = float(np.sqrt(np.mean(captured * captured)))
    print(f"captured RMS over the last 1 s = {20.0 * np.log10(crms + 1e-12):.1f} dBFS")
    ok = crms > 1e-4
    print("REFERENCE CAPTURE OK — loopback is receiving system playback." if ok
          else "REFERENCE CAPTURE: ring is silent (nothing playing, or loopback not delivering).")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
