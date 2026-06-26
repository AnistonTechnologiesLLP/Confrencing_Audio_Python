from conf_pipeline.profiles import get_device_profile, DEVICE_PROFILES


def test_polaris_profile_has_aperture():
    cap = get_device_profile("polaris-8").capabilities
    assert cap.aperture_m == 0.08
    assert abs(cap.element_spacing_m - 0.0306) < 1e-6
    assert cap.max_coverage_zones == 8


def test_existing_profiles_have_no_aperture():
    # no-regression: legacy profiles keep aperture_m None -> legacy 35 deg scoring
    for pid in ("generic-ceiling-array", "generic-table-array"):
        assert get_device_profile(pid).capabilities.aperture_m is None
