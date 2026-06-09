"""DSP block factories + parameter range checking (mirrors the TS version)."""
from __future__ import annotations

from .model import DSP_RANGES, PEQ_MAX_BANDS, DspBlock, DspBlockKind


def _in_range(v, rng) -> bool:
    try:
        return rng[0] <= float(v) <= rng[1]
    except (TypeError, ValueError):
        return False


def default_peq_band() -> dict:
    return {"freqHz": 1000, "gainDb": 0, "q": 1, "type": "bell"}


def create_dsp_block(kind: DspBlockKind, block_id: str) -> DspBlock:
    params = {
        "gain": {"gainDb": 0},
        "mute": {"muted": False},
        "peq4": {"bands": [default_peq_band()]},
        "agc": {"targetDb": -18, "maxGainDb": 12},
        "compressor": {"thresholdDb": -18, "ratio": 3, "attackMs": 10, "releaseMs": 200, "makeupDb": 0},
        "delay": {"delayMs": 0},
        "noiseReduction": {"amountDb": 12},
        "deverb": {"amount": 0.3},
    }[kind]
    return DspBlock(id=block_id, kind=kind, enabled=True, params=dict(params))


def dsp_block_param_issues(block: DspBlock) -> list[str]:
    issues: list[str] = []
    r = DSP_RANGES
    p = block.params

    def check(ok: bool, msg: str) -> None:
        if not ok:
            issues.append(msg)

    k = block.kind
    if k == "gain":
        check(_in_range(p.get("gainDb"), r["gainDb"]), "gainDb out of range -60..12")
    elif k == "mute":
        check(isinstance(p.get("muted"), bool), "muted must be boolean")
    elif k == "peq4":
        bands = p.get("bands", [])
        check(len(bands) <= PEQ_MAX_BANDS, f"peq4 allows at most {PEQ_MAX_BANDS} bands")
        for i, b in enumerate(bands):
            check(_in_range(b.get("freqHz"), r["peqFreqHz"]), f"band {i + 1} freqHz out of range")
            check(_in_range(b.get("gainDb"), r["peqGainDb"]), f"band {i + 1} gainDb out of range")
            check(_in_range(b.get("q"), r["peqQ"]), f"band {i + 1} q out of range")
    elif k == "agc":
        check(_in_range(p.get("targetDb"), r["agcTargetDb"]), "agc targetDb out of range")
        check(_in_range(p.get("maxGainDb"), r["agcMaxGainDb"]), "agc maxGainDb out of range")
    elif k == "compressor":
        check(_in_range(p.get("thresholdDb"), r["compThresholdDb"]), "compressor thresholdDb out of range")
        check(_in_range(p.get("ratio"), r["compRatio"]), "compressor ratio out of range")
        check(_in_range(p.get("attackMs"), r["compAttackMs"]), "compressor attackMs out of range")
        check(_in_range(p.get("releaseMs"), r["compReleaseMs"]), "compressor releaseMs out of range")
        check(_in_range(p.get("makeupDb"), r["compMakeupDb"]), "compressor makeupDb out of range")
    elif k == "delay":
        check(_in_range(p.get("delayMs"), r["delayMs"]), "delayMs out of range")
    elif k == "noiseReduction":
        check(_in_range(p.get("amountDb"), r["nrAmountDb"]), "noiseReduction amountDb out of range")
    elif k == "deverb":
        check(_in_range(p.get("amount"), r["deverbAmount"]), "deverb amount out of range 0..1")
    return issues
