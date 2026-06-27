"""Auto live placement check for the POLARIS array — is it in a bad acoustic/noise position?

Record (or load) a few seconds of ROOM NOISE (no speech), score the position GOOD/ACCEPTABLE/BAD with
reasons + recommendations, and get notch/HPF suggestions for the Phase 2 pre-NR stage. Diagnostics
only — this never changes the live audio pipeline.

    pip install -e ".[control]"

    # check one position (live capture):
    python scripts/check_placement.py --device 7 --seconds 10 --label "Table center"

    # ...or from a recorded 8-channel room-noise WAV:
    python scripts/check_placement.py --wav room_noise_8ch.wav --out reports/audio/placement_center.json

    # survey: compare several positions you saved, and pick the best:
    python scripts/check_placement.py --compare reports/audio/placement_*.json

Notes:
  * Capture ROOM NOISE (ask everyone to stay quiet) — speech invalidates the noise metrics.
  * Detected tones are THIS room's HVAC/fan lines — re-measure per room; nothing is auto-applied.
"""
from __future__ import annotations

import argparse
import glob
import sys
import wave

import conf_pipeline_control as cc
from conf_pipeline_control.placement import PlacementError, PlacementResult, analyze_placement, compare_placements

POLARIS_RATE_HZ = 44100.0


def _read_wav(path: str):
    import numpy as np

    with wave.open(path, "rb") as w:
        ch, n, sr = w.getnchannels(), w.getnframes(), w.getframerate()
        raw = w.readframes(n)
    x = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
    return x.reshape(-1, ch), float(sr)


def _print_summary(r: PlacementResult) -> None:
    print(f"\nPlacement: {r.status}")
    print(f"Score: {r.score}/100" + (f"   ({r.label})" if r.label else ""))
    print(f"Noise: {r.noise_rms_dbfs:.1f} dBFS   "
          f"rumble {r.low_frequency_rumble_dbfs:.1f}   speech {r.speech_band_noise_dbfs:.1f}   "
          f"hiss {r.broadband_hiss_dbfs:.1f}")
    if r.detected_tones_hz:
        print("Detected tones (Hz): " + ", ".join(f"{t:.0f}" for t in r.detected_tones_hz))
    if r.reasons:
        print("Reasons:")
        for why in r.reasons:
            print(f"  - {why}")
    if r.recommendations:
        print("Recommendations:")
        for rec in r.recommendations:
            print(f"  - {rec}")
    bands = r.to_pre_nr_bands()
    if bands:
        hpf = [b for b in bands if b["type"] == "highpass"]
        notch = [b for b in bands if b["type"] == "bell"]
        print("Suggested pre-NR cleanup (opt-in; re-measure per room):")
        if hpf:
            print(f"  HPF: {hpf[0]['freqHz']:.0f} Hz")
        if notch:
            print("  Notches: " + ", ".join(f"{b['freqHz']:.0f} Hz" for b in notch))


def _markdown(r: PlacementResult) -> str:
    lines = [f"### Placement: {r.status} ({r.score}/100){' — ' + r.label if r.label else ''}", ""]
    lines.append(f"- noise {r.noise_rms_dbfs:.1f} dBFS, rumble {r.low_frequency_rumble_dbfs:.1f}, "
                 f"speech {r.speech_band_noise_dbfs:.1f}, hiss {r.broadband_hiss_dbfs:.1f}")
    if r.detected_tones_hz:
        lines.append("- detected tones (Hz): " + ", ".join(f"{t:.0f}" for t in r.detected_tones_hz))
    for why in r.reasons:
        lines.append(f"- reason: {why}")
    for rec in r.recommendations:
        lines.append(f"- recommend: {rec}")
    return "\n".join(lines)


def _run_compare(paths) -> int:
    files: list[str] = []
    for p in paths:
        files.extend(sorted(glob.glob(p)) or [p])
    results = []
    for f in files:
        try:
            results.append(PlacementResult.load(f))
        except PlacementError as exc:
            print(f"skip {f}: {exc}", file=sys.stderr)
    if not results:
        print("No placement results to compare.", file=sys.stderr)
        return 1
    print(f"{'label':<24} {'status':<11} {'score':>5}")
    for r in sorted(results, key=lambda x: x.score, reverse=True):
        print(f"{(r.label or '(unlabelled)'):<24} {r.status:<11} {r.score:>5}")
    best = compare_placements(results)
    print(f"\nRecommended position: {best.label or '(unlabelled)'}  ({best.status}, {best.score}/100)")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Auto live placement check (room-noise diagnostics)")
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--device", type=int, help="input device index (see scripts/device_check.py)")
    src.add_argument("--wav", type=str, help="analyze an existing multichannel room-noise WAV")
    src.add_argument("--compare", nargs="+", help="compare saved placement JSONs and pick the best")
    ap.add_argument("--label", type=str, default="", help="a name for this position")
    ap.add_argument("--rate", "--sample-rate", dest="rate", type=float, default=POLARIS_RATE_HZ)
    ap.add_argument("--seconds", "--duration", dest="seconds", type=float, default=10.0)
    ap.add_argument("--channels", type=int, default=8)
    ap.add_argument("--out", type=str, default=None, help="write the result JSON here")
    ap.add_argument("--json", action="store_true", help="print the result as JSON")
    ap.add_argument("--markdown", action="store_true", help="print the result as markdown")
    args = ap.parse_args()

    if args.compare:
        return _run_compare(args.compare)

    if not cc.controls_available():
        sys.exit('Placement check needs numpy + sounddevice. Install:\n    pip install -e ".[control]"')

    if args.wav:
        cap, rate = _read_wav(args.wav)
        print(f"Read {cap.shape[0]} frames x {cap.shape[1]} ch from {args.wav} @ {rate:.0f} Hz")
    else:
        rate = float(args.rate)
        print(f"Recording {args.seconds:.0f}s of ROOM NOISE — keep quiet now…")
        cap = cc.record_clip(args.device, int(rate), args.seconds, channels=args.channels)

    if args.channels and args.wav is None and cap.shape[1] != 8:
        print(f"warning: captured {cap.shape[1]} channels (expected 8)", file=sys.stderr)

    result = analyze_placement(cap, sample_rate=rate, label=args.label)

    if args.json:
        print(result.to_json())
    elif args.markdown:
        print(_markdown(result))
    else:
        _print_summary(result)

    out = args.out
    if out is None and args.label:
        safe = "".join(c if c.isalnum() else "_" for c in args.label).strip("_").lower()
        out = f"reports/audio/placement_{safe or 'check'}.json"
    if out:
        result.save(out)
        print(f"\nSaved -> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
