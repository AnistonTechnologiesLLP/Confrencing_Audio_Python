"""Optional physics validation of a single recommended geometry (pluggable).

The heuristic engine (``scoring`` / ``search``) is pure stdlib. This module is
the *only* place that touches numerical/acoustics libraries, and only inside the
functions — importing :mod:`conf_pipeline.sim` never imports numpy. Two backends:

* ``farfield``         — anechoic plane-wave delay-and-sum SNR over a uniform
  circular array (numpy only). The model/recipe is lifted (not imported) from
  the OCTOVOX ``steering_vector`` work; lightweight, no reverberation.
* ``pyroomacoustics``  — image-source room impulse responses: physical DRR from
  the measured RIR plus a beamformer SNR through the real transfer functions.

Install the extras to enable them::

    pip install conferencing-audio-pipeline[sim]      # numpy -> farfield
    pip install conferencing-audio-pipeline[sim-rir]  # + pyroomacoustics

Use :func:`available_backends` to discover what is installed and
:func:`validate_recommendation` (``backend="auto"``) to run the best available.
"""
from __future__ import annotations

import importlib.util
import math

from ..model import Point2D, SystemConfig, find_talker
from .scoring import _critical_distance, drr_db
from .types import Recommendation, SimParams, ValidationResult

_SPEED_SOUND = 343.0
_NOISE_FLOOR = 1e-4  # ~ -40 dB sensor / diffuse-noise floor at the array


# --------------------------------------------------------------------------- #
# capability probing
# --------------------------------------------------------------------------- #
def _have(module: str) -> bool:
    try:
        return importlib.util.find_spec(module) is not None
    except (ImportError, ValueError):
        return False


def numpy_available() -> bool:
    return _have("numpy")


def available_backends() -> list[str]:
    backends: list[str] = []
    if numpy_available():
        backends.append("farfield")
        if _have("pyroomacoustics"):
            backends.append("pyroomacoustics")
    return backends


# --------------------------------------------------------------------------- #
# shared geometry (numpy-free) helpers used by both backends
# --------------------------------------------------------------------------- #
def _resolve_target(config: SystemConfig, rec: Recommendation) -> tuple[Point2D, float]:
    """The point the array is being validated against (the seat, or, in
    array-only mode, the worst-served existing talker)."""
    params = SimParams()
    if rec.talker_pos is not None:
        elev = params.talker_height_m
        if rec.talker_id is not None:
            t = find_talker(config, rec.talker_id)
            if t is not None and t.elevation is not None:
                elev = t.elevation
        return rec.talker_pos, elev
    if not config.talkers:
        raise ValueError("Nothing to validate: the recommendation has no seat and there are no talkers.")
    # worst-served talker from the per-talker breakdown, else the first talker
    worst_id = None
    if rec.per_talker:
        worst_id = min(rec.per_talker, key=lambda k: rec.per_talker[k].total)
    target = find_talker(config, worst_id) if worst_id else config.talkers[0]
    target = target or config.talkers[0]
    elev = target.elevation if target.elevation is not None else params.talker_height_m
    return target.position, elev


def _interferer_point(config: SystemConfig, rec: Recommendation, target: Point2D) -> tuple[Point2D, float]:
    """Dominant competing source: the nearest *other* talker, else a generic
    off-axis position 2 m to the side of the target."""
    others = [
        t for t in config.talkers
        if not (t.position.x == target.x and t.position.y == target.y)
    ]
    if others:
        nearest = min(others, key=lambda t: math.hypot(t.position.x - target.x, t.position.y - target.y))
        elev = nearest.elevation if nearest.elevation is not None else SimParams().talker_height_m
        return nearest.position, elev
    return Point2D(target.x + 2.0, target.y), SimParams().talker_height_m


# --------------------------------------------------------------------------- #
# backend: far-field plane-wave delay-and-sum (numpy)
# --------------------------------------------------------------------------- #
def _make_uca(np, radius_m: float = 0.04, n_mics: int = 8):
    """Planar uniform circular array, MIC1 at top (90 deg), clockwise."""
    pos = []
    for i in range(n_mics):
        angle = math.radians(90.0 - i * 360.0 / n_mics)
        pos.append([radius_m * math.cos(angle), radius_m * math.sin(angle), 0.0])
    return np.array(pos, dtype=float)


def _steering_vector(np, direction_unit, fs: int, nfft: int, mic_pos):
    """(F, C) far-field plane-wave steering vector (ported from OCTOVOX)."""
    freqs = np.fft.rfftfreq(nfft, 1.0 / fs)
    delays = -mic_pos @ direction_unit / _SPEED_SOUND
    return np.exp(-1j * 2 * np.pi * freqs[:, None] * delays[None, :])


def _unit(np, array_pos: Point2D, array_elev: float, pos: Point2D, elev: float):
    d = np.array([pos.x - array_pos.x, pos.y - array_pos.y, elev - array_elev], dtype=float)
    n = float(np.linalg.norm(d)) or 1.0
    return d / n


def _beam_snr(np, H_t, H_i, w):
    """Output SNR (dB) and interferer leakage (dB) for beam weights ``w`` over
    target / interferer channel responses ``H_t`` / ``H_i`` (each ``(F, C)``)."""
    out_t = np.sum(np.conj(w) * H_t, axis=1)
    out_i = np.sum(np.conj(w) * H_i, axis=1)
    p_t = float(np.mean(np.abs(out_t) ** 2))
    p_i = float(np.mean(np.abs(out_i) ** 2))
    snr_db = 10.0 * math.log10(p_t / (p_i + _NOISE_FLOOR)) if p_t > 0 else -120.0
    leak_db = 10.0 * math.log10(p_i + 1e-12)
    return snr_db, leak_db


def _validate_farfield(config: SystemConfig, rec: Recommendation, params: SimParams) -> ValidationResult:
    import numpy as np

    fs, nfft = 16000, 512
    geom = _make_uca(np)
    target, t_elev = _resolve_target(config, rec)
    intf, i_elev = _interferer_point(config, rec, target)

    a_t = _steering_vector(np, _unit(np, rec.array_pos, rec.array_elev, target, t_elev), fs, nfft, geom)
    a_i = _steering_vector(np, _unit(np, rec.array_pos, rec.array_elev, intf, i_elev), fs, nfft, geom)
    w = a_t / geom.shape[0]  # delay-and-sum steered at the target
    snr_db, leak_db = _beam_snr(np, a_t, a_i, w)

    distance = math.dist((rec.array_pos.x, rec.array_pos.y, rec.array_elev), (target.x, target.y, t_elev))
    drr = drr_db(distance, _critical_distance(config, params))
    return ValidationResult(
        backend="farfield",
        method="far-field plane-wave UCA delay-and-sum (anechoic); DRR from Sabine model",
        predicted_snr_db=snr_db,
        predicted_drr_db=drr,
        beam_off_axis_db=leak_db,
        n_mics=geom.shape[0],
    )


# --------------------------------------------------------------------------- #
# backend: pyroomacoustics image-source RIR
# --------------------------------------------------------------------------- #
def _validate_pyroom(config: SystemConfig, rec: Recommendation, params: SimParams) -> ValidationResult:
    import numpy as np
    import pyroomacoustics as pra

    if config.room is None or len(config.room.vertices) < 3:
        raise ValueError("pyroomacoustics validation needs a room polygon; none is defined.")

    fs = 16000
    height = config.room.height
    target, t_elev = _resolve_target(config, rec)
    intf, i_elev = _interferer_point(config, rec, target)

    corners = np.array([[v.x, v.y] for v in config.room.vertices], dtype=float).T  # (2, N)
    material = pra.Material(max(0.01, min(0.99, params.absorption)))
    try:
        room = pra.Room.from_corners(corners, fs=fs, max_order=12, materials=material)
        room.extrude(height, materials=material)
        geom = _make_uca(np).T  # (3, C)
        R = geom + np.array([[rec.array_pos.x], [rec.array_pos.y], [rec.array_elev]])
        room.add_microphone_array(pra.MicrophoneArray(R, fs))
        room.add_source([target.x, target.y, t_elev])
        room.add_source([intf.x, intf.y, i_elev])
        room.compute_rir()
    except Exception as exc:  # geometry / solver failure -> let the caller fall back
        raise RuntimeError(f"pyroomacoustics could not simulate this room: {exc}") from exc

    rir_t0 = np.asarray(room.rir[0][0], dtype=float)
    drr_physical = _drr_from_rir(np, rir_t0, fs)

    nfft = 1
    max_len = max(len(room.rir[c][0]) for c in range(R.shape[1]))
    max_len = max(max_len, max(len(room.rir[c][1]) for c in range(R.shape[1])))
    while nfft < max_len:
        nfft *= 2
    nfft = min(nfft, 8192)

    C = R.shape[1]
    H_t = np.zeros((nfft // 2 + 1, C), dtype=complex)
    H_i = np.zeros((nfft // 2 + 1, C), dtype=complex)
    for c in range(C):
        H_t[:, c] = np.fft.rfft(np.asarray(room.rir[c][0], dtype=float), n=nfft)
        H_i[:, c] = np.fft.rfft(np.asarray(room.rir[c][1], dtype=float), n=nfft)

    geom_local = _make_uca(np)
    a_t = _steering_vector(np, _unit(np, rec.array_pos, rec.array_elev, target, t_elev), fs, nfft, geom_local)
    w = a_t / C  # geometric DAS steered at the target, applied to the real transfer functions
    snr_db, leak_db = _beam_snr(np, H_t, H_i, w)

    return ValidationResult(
        backend="pyroomacoustics",
        method="image-source RIR (max_order=12); physical DRR + DAS beamformer SNR through measured transfer functions",
        predicted_snr_db=snr_db,
        predicted_drr_db=drr_physical,
        beam_off_axis_db=leak_db,
        n_mics=C,
    )


def _drr_from_rir(np, rir, fs: int, direct_ms: float = 2.5) -> float:
    """Direct-to-reverberant ratio (dB) from an impulse response."""
    if rir.size == 0:
        return 0.0
    peak = int(np.argmax(np.abs(rir)))
    half = int(direct_ms * 1e-3 * fs)
    lo, hi = max(0, peak - half), min(len(rir), peak + half + 1)
    direct = float(np.sum(rir[lo:hi] ** 2))
    reverb = float(np.sum(rir ** 2) - direct)
    return 10.0 * math.log10(direct / (reverb + 1e-12)) if direct > 0 else -60.0


# --------------------------------------------------------------------------- #
# public dispatch
# --------------------------------------------------------------------------- #
_BACKENDS = {
    "farfield": _validate_farfield,
    "pyroomacoustics": _validate_pyroom,
}


def validate_recommendation(
    config: SystemConfig,
    rec: Recommendation,
    params: SimParams = SimParams(),
    backend: str = "auto",
) -> ValidationResult:
    """Physically validate one recommended geometry with the chosen backend.

    ``backend="auto"`` prefers pyroomacoustics (true RIR) when installed and
    falls back to the far-field model. Raises :class:`RuntimeError` when no
    backend is available, with an install hint.
    """
    avail = available_backends()
    if not avail:
        raise RuntimeError(
            "No validation backend available. Install one with "
            "`pip install conferencing-audio-pipeline[sim]` (far-field) or "
            "`[sim-rir]` (pyroomacoustics)."
        )
    if backend == "auto":
        backend = "pyroomacoustics" if "pyroomacoustics" in avail else "farfield"
    if backend not in _BACKENDS:
        raise ValueError(f"Unknown validation backend {backend!r}; choose from {sorted(_BACKENDS)}.")
    if backend not in avail:
        raise RuntimeError(f"Backend {backend!r} is not installed. Available: {avail}.")
    return _BACKENDS[backend](config, rec, params)
