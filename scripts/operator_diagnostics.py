"""Operator diagnostics — print + export the audio front-end status (Phase 6).

Builds the headless :class:`OperatorStatus` (Device / Calibration / Placement / Pipeline / Output /
Transcription + warnings) from a representative engine config (+ an optional saved placement / calibration
profile), prints it, and writes ``reports/audio/operator_diagnostics_<stamp>.{json,md}``. Read-only —
it changes no default, applies no suggestion, makes no network call.

    pip install -e ".[control]"

    # a quick snapshot (defaults: clean beam, all cleaners off):
    python scripts/operator_diagnostics.py

    # include a placement result + show pre-NR + AGC engaged:
    python scripts/operator_diagnostics.py --placement reports/audio/placement_center.json --pre-nr --agc-target-db -20
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime

import conf_pipeline_control as cc
from conf_pipeline_control.operator import OperatorStatus


def main() -> int:
    ap = argparse.ArgumentParser(description="Operator diagnostics for the POLARIS audio front-end")
    ap.add_argument("--rate", type=float, default=44100.0, help="engine sample rate (POLARIS = 44100)")
    ap.add_argument("--calibration", type=str, help="a calibration profile JSON to load")
    ap.add_argument("--placement", type=str, help="a placement_*.json to include")
    ap.add_argument("--pre-nr", action="store_true", help="show pre-NR engaged (HPF 120 Hz example)")
    ap.add_argument("--post-nr", action="store_true", help="show post-NR engaged")
    ap.add_argument("--aec", action="store_true", help="show AEC engaged")
    ap.add_argument("--dereverb", action="store_true", help="show dereverb engaged")
    ap.add_argument("--agc-target-db", type=float, default=None, help="show AGC engaged at this target")
    ap.add_argument("--out", type=str, default="reports/audio", help="export directory")
    ap.add_argument("--json", action="store_true", help="print JSON instead of Markdown")
    args = ap.parse_args()

    if not cc.controls_available():
        sys.exit('Operator diagnostics needs numpy + sounddevice. Install:\n    pip install -e ".[control]"')

    kw = {}
    if args.pre_nr:
        kw.update(pre_nr=True, pre_nr_bands=cc.build_pre_nr_bands(hpf_hz=120.0))
    if args.post_nr:
        kw.update(post_nr=True)
    if args.aec:
        kw.update(aec=True)
    if args.dereverb:
        kw.update(dereverb=True)
    if args.agc_target_db is not None:
        kw.update(agc_target_db=args.agc_target_db)
    if args.calibration:
        try:
            kw.update(calibration=cc.CalibrationProfile.load(args.calibration))
        except cc.CalibrationError as exc:
            print(f"warning: calibration {args.calibration!r} not loaded: {exc}", file=sys.stderr)

    from conf_pipeline_control.polaris_beamformer import PolarisBeamformer
    eng = PolarisBeamformer(device=None, sample_rate=args.rate, **kw)
    try:
        eng._setup_runtime()                       # populate latency + active_cleaning_stages
    except Exception:
        pass

    placement = None
    if args.placement:
        try:
            placement = cc.PlacementResult.load(args.placement)
        except cc.PlacementError as exc:
            print(f"warning: placement {args.placement!r} not loaded: {exc}", file=sys.stderr)

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    st = OperatorStatus.build(engine=eng, calibration_path=args.calibration,
                              placement=placement, generated_at=stamp)
    print(st.to_dict() if args.json else st.to_markdown())
    jp, mp = st.save(args.out, stamp=stamp)
    print(f"\nSaved -> {jp}\n         {mp}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
