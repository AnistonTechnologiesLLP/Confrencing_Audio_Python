"""Stage 1 diagnostic: prove the POLARIS array delivers 8 live channels @ 44100 Hz.

This isolates *device/driver* problems from *beamformer* problems. On Windows a
multichannel USB array often only exposes 2 channels under MME/WASAPI — you
usually need a host API (or device entry) that surfaces all 8. Run this FIRST;
only move on to beamforming once it shows 8 non-silent channels and the dead
capsule reading near-zero.

    pip install -e ".[control]"        # numpy + sounddevice

    python scripts/device_check.py                 # list host APIs + input devices
    python scripts/device_check.py --device 7      # 3 s raw capture, per-channel RMS
    python scripts/device_check.py --device 7 --rate 44100 --channels 8 --seconds 3

Reads nothing from the rest of the package except the import-guarded enumeration
helper, so it works the moment the [control] extra is installed.
"""
from __future__ import annotations

import argparse
import math
import sys


def _require_deps():
    try:
        import numpy  # noqa: F401
        import sounddevice  # noqa: F401
    except Exception:
        sys.exit(
            'Live audio needs numpy + sounddevice. Install the extra:\n'
            '    pip install -e ".[control]"'
        )


def list_devices() -> None:
    import sounddevice as sd

    hostapis = sd.query_hostapis()
    print("Host APIs (Windows: ASIO / WASAPI usually expose the most channels):")
    for i, ha in enumerate(hostapis):
        default_in = ha.get("default_input_device", -1)
        print(f"  [{i}] {ha['name']:<24} default input device = {default_in}")
    print()

    print("Input devices (★ = can deliver >= 8 channels):")
    print(f"  {'idx':>3}  {'in':>3}  {'rate':>7}  {'hostapi':<22}  name")
    for i, d in enumerate(sd.query_devices()):
        nin = int(d.get("max_input_channels", 0))
        if nin <= 0:
            continue
        star = "★" if nin >= 8 else " "
        ha = hostapis[d["hostapi"]]["name"] if d.get("hostapi") is not None else "?"
        rate = float(d.get("default_samplerate", 0.0))
        print(f" {star}{i:>3}  {nin:>3}  {rate:>7.0f}  {ha:<22}  {d.get('name','')}")
    print(
        "\nPick the device index whose name is your POLARIS AND shows >= 8 input "
        "channels.\nIf none shows 8, the host API is the problem (try the ASIO / "
        "WASAPI entry), not the code."
    )


def capture(device: int, rate: float, channels: int, seconds: float) -> int:
    import numpy as np
    import sounddevice as sd

    print(f"Capturing {seconds:.1f}s from device {device} @ {rate:.0f} Hz, {channels} ch ...")
    try:
        rec = sd.rec(
            int(seconds * rate),
            samplerate=rate,
            channels=channels,
            device=device,
            dtype="float32",
        )
        sd.wait()
    except Exception as exc:  # noqa: BLE001
        print(f"\n!! Capture FAILED: {exc}")
        print(
            "   Common causes: wrong sample rate for this device (POLARIS is "
            "44100 Hz),\n   the host API can't open this channel count, or the "
            "device index is wrong.\n   Re-run without --device to review the list."
        )
        return 1

    rms = np.sqrt(np.mean(rec.astype(float) ** 2, axis=0))
    peak = np.max(np.abs(rec.astype(float)), axis=0)
    print("\n  ch   RMS(dBFS)   peak(dBFS)   status")
    silent = []
    for ch in range(channels):
        r = rms[ch]
        p = peak[ch]
        r_db = 20.0 * math.log10(r) if r > 1e-12 else -120.0
        p_db = 20.0 * math.log10(p) if p > 1e-12 else -120.0
        status = "live"
        if r_db < -80.0:
            status = "SILENT (dead capsule? expected for index 5)"
            silent.append(ch)
        print(f"  {ch:>2}   {r_db:>8.1f}   {p_db:>8.1f}   {status}")

    print()
    live = channels - len(silent)
    print(f"Summary: {live}/{channels} channels live; silent = {silent or 'none'}")
    if channels >= 8 and live >= 7 and silent in ([], [5]):
        print("OK — geometry should mark the silent channel inactive (capsule 5).")
    elif live < 7:
        print(
            "WARNING — fewer than 7 live channels. Beamforming needs the real "
            "capsules; fix the device/host API before going further."
        )
    return 0


def main() -> int:
    _require_deps()
    ap = argparse.ArgumentParser(description="POLARIS array device/capture diagnostic")
    ap.add_argument("--device", type=int, default=None, help="input device index to capture from")
    ap.add_argument("--rate", type=float, default=44100.0, help="sample rate (POLARIS = 44100)")
    ap.add_argument("--channels", type=int, default=8, help="channel count to open")
    ap.add_argument("--seconds", type=float, default=3.0, help="capture duration")
    args = ap.parse_args()

    if args.device is None:
        list_devices()
        return 0
    return capture(args.device, args.rate, args.channels, args.seconds)


if __name__ == "__main__":
    raise SystemExit(main())
