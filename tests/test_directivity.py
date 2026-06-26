# tests/test_directivity.py
import math
from conf_pipeline.directivity import (
    steered_beamwidth_deg, alias_ceiling_hz, separable,
    NEAR_OMNI_HALF_DEG, SIM_SPEECH_FREQ_HZ,
)

POLARIS_AP = 0.08      # 40 mm radius → ~80 mm aperture
POLARIS_SP = 0.0306    # adjacent capsule spacing 2*R*sin(pi/8)

def test_none_or_zero_aperture_is_near_omni():
    assert steered_beamwidth_deg(None, 1500.0, 0.0) == NEAR_OMNI_HALF_DEG
    assert steered_beamwidth_deg(0.0, 1500.0, 0.0) == NEAR_OMNI_HALF_DEG

def test_higher_freq_is_narrower():
    lo = steered_beamwidth_deg(POLARIS_AP, 800.0, 0.0)
    hi = steered_beamwidth_deg(POLARIS_AP, 3400.0, 0.0)
    assert hi < lo  # more directive up high

def test_larger_aperture_is_narrower():
    small = steered_beamwidth_deg(0.08, 2000.0, 0.0)
    big = steered_beamwidth_deg(0.40, 2000.0, 0.0)
    assert big < small

def test_off_broadside_is_wider():
    broad = steered_beamwidth_deg(POLARIS_AP, 3400.0, 0.0)
    endfire = steered_beamwidth_deg(POLARIS_AP, 3400.0, 90.0)
    assert endfire >= broad

def test_polaris_is_near_omni_low_and_coarse_high():
    """40 mm POLARIS: near-omni at low speech frequencies, monotonically narrowing with frequency.

    The high-freq bound is calibrated to the measured sensibel_8 ring beam (Task 6):
    at 3400 Hz broadside the real half-angle is ~17°, not the 30°+ the old linear-aperture
    formula predicted.  We assert the calibrated physics band (10–25°) and that the
    high-freq beam is strictly narrower than the low-freq near-omni value.
    """
    lo = steered_beamwidth_deg(POLARIS_AP, 700.0, 90.0)
    hi = steered_beamwidth_deg(POLARIS_AP, 3400.0, 0.0)
    assert lo >= 80.0               # near-omni at low freq
    assert 10.0 <= hi <= 25.0       # calibrated measured band at 3400 Hz broadside
    assert hi < lo                  # monotonically narrower at high frequency

def test_larger_aperture_gives_tighter_beam():
    """A larger aperture produces a meaningfully tighter beam at the speech centre frequency.

    After calibrating _BW_K to the measured sensibel_8 ring beam (Task 6), a 0.18 m
    aperture gives ~17° at 1500 Hz — NOT the ~35° the old formula predicted.  The
    spec's "ceiling scoring preserved" guarantee is provided by the aperture_m is None →
    35° FALLBACK in the scorer/coverage_sim (tested in test_aperture_scoring.py /
    test_coverage_sim_aperture.py), NOT by the formula matching 35° at 0.18 m.

    This test asserts the physically correct truth: a larger array is tighter.
    """
    small = steered_beamwidth_deg(0.08, SIM_SPEECH_FREQ_HZ, 0.0)
    big = steered_beamwidth_deg(0.18, SIM_SPEECH_FREQ_HZ, 0.0)
    assert big < small                      # larger aperture → tighter beam
    assert 10.0 <= big <= 25.0             # calibrated physics band for 0.18 m at 1500 Hz

def test_alias_ceiling_polaris_about_5p6k():
    assert 5200.0 <= alias_ceiling_hz(POLARIS_SP) <= 6000.0
    assert alias_ceiling_hz(None) == float("inf")

def test_separable_boundary():
    assert separable(120.0, 30.0)        # 120 >= 1.5*2*30? no -> use factor on half: 1.5*30=45 -> 120>=45 True
    assert not separable(20.0, 60.0)     # 20 < 1.5*60=90 -> not separable
