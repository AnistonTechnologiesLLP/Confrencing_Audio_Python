# tests/test_directivity_calibration.py
"""Calibration test: analytic aperture-beamwidth model vs measured delay-sum beam.

Skipped automatically when the ``[control]`` extra (numpy + sounddevice) is not
installed, so the default numpy-free suite still passes.
"""
import math
import pytest

np = pytest.importorskip("numpy")  # skip when [control] isn't installed

from conf_pipeline.directivity import steered_beamwidth_deg


def _unit_from_az_offnadir(az_deg: float, off_nadir_deg: float) -> tuple[float, float, float]:
    """Convert (azimuth_deg, off_nadir_deg) to a unit vector (x, y, z).

    off_nadir_deg=90 → horizontal plane; 0 → straight down.
    """
    on = math.radians(off_nadir_deg)
    az = math.radians(az_deg)
    s = math.sin(on)
    return (s * math.sin(az), s * math.cos(az), -math.cos(on))


def _measured_half_deg(geom, freq_hz: float, look_az_deg: float, off_nadir_deg: float = 90.0) -> float:
    """3 dB main-lobe half-width (deg) of the delay-sum beam toward (look_az_deg, off_nadir_deg).

    Sweeps azimuth in 1-degree steps from the look direction until the response
    drops 3 dB below the on-axis peak.  Returns 180.0 if it never drops that far.
    """
    import conf_pipeline_control as cc
    from conf_pipeline_control.beamformer import design_from_bearings

    look = (float(look_az_deg), float(off_nadir_deg))
    d = design_from_bearings(
        geom,
        look,
        nulls=(),
        freq_hz=freq_hz,
        mode=cc.MODE_DELAYSUM,
        loading=0.0,
        bands=(),  # skip wideband verification grid — this is a single-frequency probe
    )
    w = list(d.beams[0].weights)
    on_axis_db = cc.response_db(w, geom, _unit_from_az_offnadir(look_az_deg, off_nadir_deg), freq_hz)

    for dphi in range(1, 181):
        u = _unit_from_az_offnadir(look_az_deg + dphi, off_nadir_deg)
        r = cc.response_db(w, geom, u, freq_hz)
        if r <= on_axis_db - 3.0:
            return float(dphi)
    return 180.0


def test_analytic_matches_measured_sensibel8_within_tolerance():
    """Analytic steered_beamwidth_deg must be within ±15° of the measured beam.

    Geometry: sensibel_8 with radius_m=0.040 (aperture = 0.08 m).
    Grid: 3 frequencies × 1 look direction = 3 probes.
    """
    from conf_pipeline_control.geometry import sensibel_8

    geom = sensibel_8(radius_m=0.040)
    aperture_m = 0.08  # matches profiles.py POLARIS entry

    worst = 0.0
    results: list[tuple[float, float, float, float]] = []  # (freq, meas, pred, gap)

    for f in (800.0, 1500.0, 3000.0):
        meas = _measured_half_deg(geom, f, look_az_deg=0.0, off_nadir_deg=90.0)
        pred = steered_beamwidth_deg(aperture_m, f, steer_deg=90.0)
        # Both clamp toward near-omni at low frequency; compare the clamped values.
        gap = abs(min(pred, 90.0) - min(meas, 90.0))
        worst = max(worst, gap)
        results.append((f, meas, pred, gap))

    # Emit a diagnostic table for easy reading on failure.
    header = f"{'freq_hz':>10} {'meas_half°':>12} {'pred_half°':>12} {'gap°':>8}"
    rows = "\n".join(
        f"{f:>10.0f} {m:>12.1f} {p:>12.1f} {g:>8.1f}"
        for f, m, p, g in results
    )
    detail = f"\n{header}\n{rows}\nworst gap = {worst:.1f}°"

    assert worst <= 15.0, (
        f"Analytic beamwidth model off by {worst:.1f}° (tolerance 15°). "
        f"Adjust _BW_K / _ENDFIRE_WIDEN in conf_pipeline/directivity.py.{detail}"
    )
