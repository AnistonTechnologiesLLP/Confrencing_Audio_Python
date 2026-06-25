# tests/test_coverage_sim_aperture.py
"""Aperture-aware mic wedge in mic_coverage: polaris-8 gets a wider beam; legacy stays at 35°."""
import conf_pipeline as cp
from conf_pipeline.coverage_sim import mic_coverage, DEFAULT_PICKUP_BEAM_HALF_DEG
from conf_pipeline.model import Point2D, RectShape, RoomLayout
from conf_pipeline.model import CoverageZone


def _cfg_and_array(profile_id: str):
    """Build a minimal config with one mic array at the origin and one pickup zone."""
    c = cp.create_config("Room", "2026-06-25T00:00:00Z")
    c.room = RoomLayout(
        vertices=[Point2D(-3, -3), Point2D(3, -3), Point2D(3, 3), Point2D(-3, 3)],
        height=3.0,
    )
    arr = cp.create_microphone_array("a1", "Array A", position=Point2D(0.0, 0.0))
    # Override profile so we can test different profiles without cp.create_microphone_array
    # knowing about them.
    arr.profile_id = profile_id
    # Add one dedicated pickup zone at (0.5, 0.5) with a 1×1 m rect.
    arr.zones = [CoverageZone(
        id="z1", type="dedicated",
        shape=RectShape(Point2D(0.5, 0.5), 1.0, 1.0),
        always_on=False, label="seat",
    )]
    c = cp.add_device(c, arr)
    return c, arr


def test_polaris_wedge_is_wider_than_legacy():
    """polaris-8 profile has aperture_m set → steered_beamwidth_deg replaces the 35° default."""
    cfg, arr = _cfg_and_array("polaris-8")
    mc = mic_coverage(cfg, arr, targets=[])
    assert mc is not None
    assert len(mc.wedges) == 1
    assert mc.wedges[0].h_half_deg > DEFAULT_PICKUP_BEAM_HALF_DEG  # honest, coarse


def test_legacy_array_wedge_unchanged():
    """Arrays without aperture_m keep the legacy 35° half-angle unchanged."""
    cfg, arr = _cfg_and_array("generic-ceiling-array")
    mc = mic_coverage(cfg, arr, targets=[])
    assert mc is not None
    assert len(mc.wedges) == 1
    assert mc.wedges[0].h_half_deg == DEFAULT_PICKUP_BEAM_HALF_DEG
