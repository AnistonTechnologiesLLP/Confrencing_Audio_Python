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
    # 40 mm array: essentially omni at low speech freq, still coarse (>30 deg half) up high
    assert steered_beamwidth_deg(POLARIS_AP, 700.0, 90.0) >= 80.0
    assert steered_beamwidth_deg(POLARIS_AP, 3400.0, 0.0) >= 30.0

def test_ceiling_reference_aperture_is_about_35_deg():
    # a ~0.10 m aperture at the speech centre lands near the legacy 35 deg half-angle
    h = steered_beamwidth_deg(0.10, SIM_SPEECH_FREQ_HZ, 0.0)
    assert 28.0 <= h <= 45.0

def test_alias_ceiling_polaris_about_5p6k():
    assert 5200.0 <= alias_ceiling_hz(POLARIS_SP) <= 6000.0
    assert alias_ceiling_hz(None) == float("inf")

def test_separable_boundary():
    assert separable(120.0, 30.0)        # 120 >= 1.5*2*30? no -> use factor on half: 1.5*30=45 -> 120>=45 True
    assert not separable(20.0, 60.0)     # 20 < 1.5*60=90 -> not separable
