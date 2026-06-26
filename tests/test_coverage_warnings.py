# tests/test_coverage_warnings.py
"""Task 5: separability + grating-lobe caveats for aperture-limited arrays."""
import conf_pipeline as cp
from conf_pipeline.coverage_sim import coverage_caveats, simulate_room_coverage
from conf_pipeline.model import Point2D, RectShape, RoomLayout, CoverageZone


def _cfg_two_close_seats(profile_id: str):
    """Two seats 0.6 m apart (±0.3 m on X), array at origin — close enough that
    a polaris-8's coarse beam cannot resolve them."""
    c = cp.create_config("Room", "2026-06-25T00:00:00Z")
    c.room = RoomLayout(
        vertices=[Point2D(-3, -3), Point2D(3, -3), Point2D(3, 3), Point2D(-3, 3)],
        height=3.0,
    )
    arr = cp.create_microphone_array("a1", "Array A", position=Point2D(0.0, 0.0))
    arr.profile_id = profile_id
    arr.elevation = 0.85
    arr.zones = [
        CoverageZone(
            id="z1", type="dedicated",
            shape=RectShape(Point2D(-0.55, 0.95), 0.5, 0.5),
            always_on=False, label="seat1",
        ),
        CoverageZone(
            id="z2", type="dedicated",
            shape=RectShape(Point2D(0.05, 0.95), 0.5, 0.5),
            always_on=False, label="seat2",
        ),
    ]
    c = cp.add_device(c, arr)
    return c


def test_polaris_close_seats_flag_unseparable():
    """polaris-8 with two zones only 0.6 m apart should warn about separability."""
    cfg = _cfg_two_close_seats("polaris-8")
    caveats = coverage_caveats(cfg)
    assert any("separat" in c.lower() for c in caveats), (
        f"Expected a separability warning; got: {caveats}"
    )


def test_polaris_grating_lobe_note():
    """polaris-8 element_spacing_m=0.0306 → alias ceiling ≈5.6 kHz < 8 kHz → grating-lobe note."""
    cfg = _cfg_two_close_seats("polaris-8")
    caveats = coverage_caveats(cfg)
    assert any("kHz" in c or "khz" in c.lower() for c in caveats), (
        f"Expected a grating-lobe kHz warning; got: {caveats}"
    )


def test_legacy_array_no_aperture_warnings():
    """generic-ceiling-array has no aperture_m → no separability or grating-lobe caveats."""
    cfg = _cfg_two_close_seats("generic-ceiling-array")
    caveats = coverage_caveats(cfg)
    assert not any("separat" in c.lower() for c in caveats), (
        f"Legacy array should not get separability warnings; got: {caveats}"
    )
    assert not any("grating" in c.lower() for c in caveats), (
        f"Legacy array should not get grating-lobe warnings; got: {caveats}"
    )


def test_room_coverage_caveats_integrated():
    """simulate_room_coverage for a polaris-8 close-seat config includes a separability line."""
    cfg = _cfg_two_close_seats("polaris-8")
    rc = simulate_room_coverage(cfg)
    assert any("separat" in c.lower() for c in rc.caveats), (
        f"RoomCoverage.caveats should include separability warning; got: {rc.caveats}"
    )
