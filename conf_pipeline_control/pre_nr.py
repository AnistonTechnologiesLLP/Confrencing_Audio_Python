"""Pre-NR linear cleanup — speech HPF + notch band builders for the pre-denoise filter stage.

Phase 2 of the audio front-end hardening. The *measurement-first* principle: cheap, predictable linear
filters (a high-pass to drop sub-speech rumble, narrow notches for tonal HVAC/fan lines) should run
**before** the post-NR / DFN3 / OM-LSA denoiser, so the neural/spectral stage doesn't spend capacity
suppressing noise a biquad removes for free.

The stage itself is just a second :class:`~conf_pipeline_control.peq.StreamingPeq` placed before
post-NR — that cascade already has ``highpass`` (HPF) and ``bell`` (notch via negative gain) types and
is exact zero-latency IIR. This module only builds the PEQ-format band dicts
(``{"freqHz", "gainDb", "q", "type"}``, the same shape the PEQ model uses), so there is no duplicate
filter math.

**Room-specific tones live in presets / user config, never as global defaults.** :func:`office_ac_preset`
is a *measured-room EXAMPLE* you opt into — re-measure per room (Phase 3's placement check surfaces the
actual tones). Nothing here is applied unless a caller passes the bands to ``pre_nr_bands=…``.
"""
from __future__ import annotations

from typing import Any, List, Optional, Sequence, Tuple, Union

DEFAULT_HPF_Q = 0.707            # Butterworth (maximally flat) 2nd-order high-pass
DEFAULT_NOTCH_Q = 8.0            # fairly narrow — dip the tone, keep the speech either side of it
DEFAULT_NOTCH_DEPTH_DB = 12.0    # notch depth (magnitude); applied as a negative-gain bell

# A notch may be given as a bare frequency, a (freq, q, depth_db) tuple, or a {"freqHz","q","depthDb"} dict.
NotchSpec = Union[float, int, dict, Tuple[Any, ...]]


def hpf_band(freq_hz: float, q: float = DEFAULT_HPF_Q) -> dict:
    """A 2nd-order high-pass PEQ band (RBJ ``highpass`` biquad) — removes rumble below ``freq_hz``."""
    return {"type": "highpass", "freqHz": float(freq_hz), "gainDb": 0.0, "q": float(q)}


def notch_band(freq_hz: float, q: float = DEFAULT_NOTCH_Q,
               depth_db: float = DEFAULT_NOTCH_DEPTH_DB) -> dict:
    """A narrow attenuation at ``freq_hz`` — a negative-gain ``bell`` (a notch for a tonal interferer).

    ``depth_db`` is a magnitude; a notch always attenuates, so the sign is forced negative."""
    return {"type": "bell", "freqHz": float(freq_hz), "gainDb": -abs(float(depth_db)), "q": float(q)}


def build_pre_nr_bands(hpf_hz: Optional[float] = None,
                       notches: Optional[Sequence[NotchSpec]] = None) -> List[dict]:
    """Compose PEQ-format pre-NR bands: an optional high-pass first, then one notch per entry.

    ``notches`` entries may each be a bare frequency (Hz), a ``(freq, q, depth_db)`` tuple, or a
    ``{"freqHz", "q", "depthDb"}`` dict. Returns ``[]`` when nothing is requested (the stage then stays
    a bit-exact no-op)."""
    bands: List[dict] = []
    if hpf_hz:                                   # 0 / None ⇒ no high-pass
        bands.append(hpf_band(float(hpf_hz)))
    for n in (notches or []):
        if isinstance(n, dict):
            bands.append(notch_band(float(n["freqHz"]),
                                    float(n.get("q", DEFAULT_NOTCH_Q)),
                                    float(n.get("depthDb", DEFAULT_NOTCH_DEPTH_DB))))
        elif isinstance(n, (tuple, list)):
            f = float(n[0])
            q = float(n[1]) if len(n) > 1 else DEFAULT_NOTCH_Q
            d = float(n[2]) if len(n) > 2 else DEFAULT_NOTCH_DEPTH_DB
            bands.append(notch_band(f, q, d))
        else:
            bands.append(notch_band(float(n)))
    return bands


def office_ac_preset() -> List[dict]:
    """**MEASURED-ROOM EXAMPLE — opt-in, NOT a global default.** A speech high-pass at 120 Hz plus
    notches at the 102 / 140 / 177 Hz tones from one room's HVAC survey. Re-measure per room; Phase 3's
    placement check reports the actual tones to notch. Pass the result to ``pre_nr_bands=…``."""
    return build_pre_nr_bands(hpf_hz=120.0, notches=[102.0, 140.0, 177.0])
