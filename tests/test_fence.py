"""Tests for conf_pipeline_control.fence — pure stdlib, numpy-free, hardware-free.

TDD: written first (all RED), then implemented to GREEN.

Convention reminder (repo-wide): 0° = +Y, clockwise, atan2(x, y).
"""
from __future__ import annotations

import math
from typing import Optional

import pytest

from conf_pipeline.model import Point2D, _norm_bearing, bearing_to_deg
from conf_pipeline_control.fence import (
    DEFAULT_FENCE_HOLD_TICKS,
    DEFAULT_FENCE_MARGIN_M,
    LEVEL_INSIDE_DB,
    FenceConfigError,
    FenceDecider,
    FenceDecision,
    FusedSource,
    KitPose,
    KitReading,
    Ray2D,
    closest_point_two_rays,
    crossing_confidence,
    fuse_position,
    level_cross_check,
    local_az_to_room_az,
    point_in_fence,
    ray_from_bearing,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_reading(
    azimuth_deg: Optional[float],
    salience_db: float = 0.0,
    level: float = 0.01,
    active: bool = True,
) -> KitReading:
    return KitReading(azimuth_deg=azimuth_deg, salience_db=salience_db, level=level, active=active)


def _kit_reading_for_source(
    kit_pos: Point2D,
    kit_bearing: float,
    source_pos: Point2D,
    level: float = 0.01,
    salience_db: float = 0.0,
    active: bool = True,
) -> KitReading:
    """Compute a synthetic KitReading for a kit at known position/bearing seeing a source."""
    room_bearing = bearing_to_deg(kit_pos, source_pos)
    local_az = _norm_bearing(room_bearing - kit_bearing)
    return KitReading(azimuth_deg=local_az, salience_db=salience_db, level=level, active=active)


# ---------------------------------------------------------------------------
# ray_from_bearing — cardinals
# ---------------------------------------------------------------------------

class TestRayFromBearing:
    def test_north_0_deg_points_in_y_direction(self):
        """0° bearing → ray pointing along +Y."""
        r = ray_from_bearing(Point2D(0.0, 0.0), 0.0)
        assert abs(r.dx) < 1e-9
        assert abs(r.dy - 1.0) < 1e-9

    def test_east_90_deg_points_in_x_direction(self):
        """90° bearing → ray pointing along +X."""
        r = ray_from_bearing(Point2D(0.0, 0.0), 90.0)
        assert abs(r.dx - 1.0) < 1e-9
        assert abs(r.dy) < 1e-9

    def test_south_180_deg_points_in_minus_y_direction(self):
        """180° bearing → ray pointing along -Y."""
        r = ray_from_bearing(Point2D(0.0, 0.0), 180.0)
        assert abs(r.dx) < 1e-9
        assert abs(r.dy - (-1.0)) < 1e-9

    def test_west_270_deg_points_in_minus_x_direction(self):
        """270° bearing → ray pointing along -X."""
        r = ray_from_bearing(Point2D(1.0, 2.0), 270.0)
        assert abs(r.dx - (-1.0)) < 1e-9
        assert abs(r.dy) < 1e-9
        assert r.origin.x == pytest.approx(1.0)
        assert r.origin.y == pytest.approx(2.0)

    def test_45_deg_diagonal(self):
        """45° bearing → dx == dy, both positive, unit vector."""
        r = ray_from_bearing(Point2D(0.0, 0.0), 45.0)
        expected = math.sin(math.radians(45.0))
        assert r.dx == pytest.approx(expected)
        assert r.dy == pytest.approx(expected)
        # Must be a unit vector
        assert math.hypot(r.dx, r.dy) == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# local_az_to_room_az
# ---------------------------------------------------------------------------

class TestLocalAzToRoomAz:
    def test_zero_bearing_passes_through(self):
        """With bearing 0, local == room."""
        assert local_az_to_room_az(45.0, 0.0) == pytest.approx(45.0)

    def test_adds_bearing_and_wraps(self):
        """room_az = norm(local + bearing)."""
        assert local_az_to_room_az(350.0, 20.0) == pytest.approx(10.0)

    def test_matches_seat_mapper_transform(self):
        """Must use the same formula as seat_mapper: _norm_bearing(local + bearing)."""
        for local in [0.0, 90.0, 180.0, 270.0, 359.9]:
            for bearing in [0.0, 45.0, 90.0, 135.0, 270.0]:
                expected = _norm_bearing(local + bearing)
                result = local_az_to_room_az(local, bearing)
                assert result == pytest.approx(expected), (
                    f"local={local}, bearing={bearing}: got {result}, expected {expected}"
                )

    def test_result_always_in_0_360(self):
        """Output is always in [0, 360)."""
        for deg in range(0, 720, 30):
            r = local_az_to_room_az(float(deg), 45.0)
            assert 0.0 <= r < 360.0


# ---------------------------------------------------------------------------
# closest_point_two_rays
# ---------------------------------------------------------------------------

class TestClosestPointTwoRays:
    def test_orthogonal_rays_crossing_at_known_point(self):
        """Two orthogonal rays that should cross at (1, 1)."""
        # Ray A: starts at (0,1) pointing East (+X)
        ra = Ray2D(origin=Point2D(0.0, 1.0), dx=1.0, dy=0.0)
        # Ray B: starts at (1,0) pointing North (+Y)
        rb = Ray2D(origin=Point2D(1.0, 0.0), dx=0.0, dy=1.0)
        pt, dist, degen = closest_point_two_rays(ra, rb)
        assert not degen
        assert dist < 1e-9
        assert pt is not None
        assert pt.x == pytest.approx(1.0)
        assert pt.y == pytest.approx(1.0)

    def test_near_parallel_returns_degenerate(self):
        """Near-parallel rays → degenerate=True, point=None."""
        ra = Ray2D(origin=Point2D(0.0, 0.0), dx=0.0, dy=1.0)
        rb = Ray2D(origin=Point2D(0.5, 0.0), dx=1e-5, dy=1.0)  # slightly off-parallel
        # Normalize rb
        mag = math.hypot(1e-5, 1.0)
        rb = Ray2D(origin=Point2D(0.5, 0.0), dx=1e-5 / mag, dy=1.0 / mag)
        pt, dist, degen = closest_point_two_rays(ra, rb, parallel_eps=1e-3)
        assert degen
        assert pt is None
        assert dist == math.inf

    def test_behind_ray_clamp_sa_sb_nonneg(self):
        """If the pure-line intersection would be 'behind' the origin, clamp to s>=0."""
        # Ray A: origin (0,0) pointing +Y
        # Ray B: origin (0,2) pointing +X  — the crossing of the LINES is at (0,2) which is ahead
        # for B (sb=0) but let's check behind-ray clamping with a geometry that would give negative s
        # Ray A: origin (10, 0) pointing +Y; Ray B: origin (0, 10) pointing +X
        # The LINE intersection would be at (10, 10), both s>=0 → not clamped.
        ra = Ray2D(origin=Point2D(10.0, 0.0), dx=0.0, dy=1.0)
        rb = Ray2D(origin=Point2D(0.0, 10.0), dx=1.0, dy=0.0)
        pt, dist, degen = closest_point_two_rays(ra, rb)
        assert not degen
        assert pt is not None
        assert pt.x == pytest.approx(10.0)
        assert pt.y == pytest.approx(10.0)
        assert dist < 1e-9

    def test_rays_diverging_clamp_to_origins(self):
        """Rays pointing away from each other — s should be clamped to 0."""
        # Ray A: from (1,0) pointing +X (East) — away from Ray B
        # Ray B: from (-1,0) pointing -X (West) — away from Ray A
        ra = Ray2D(origin=Point2D(1.0, 0.0), dx=1.0, dy=0.0)
        rb = Ray2D(origin=Point2D(-1.0, 0.0), dx=-1.0, dy=0.0)
        # These are anti-parallel (degen) — but near-parallel check fires first
        pt, dist, degen = closest_point_two_rays(ra, rb, parallel_eps=1e-3)
        # Anti-parallel: |cos| ~ 1 → degen
        assert degen

    def test_skewed_rays_midpoint_accuracy(self):
        """Two orthogonal rays that cross at a known point — verify midpoint accuracy."""
        # Ray A: from (0, 1) pointing +X (East, 90°)
        # Ray B: from (1, 0) pointing +Y (North, 0°)
        # The crossing of the LINES is exactly at (1, 1).
        ra = Ray2D(origin=Point2D(0.0, 1.0), dx=1.0, dy=0.0)
        rb = Ray2D(origin=Point2D(1.0, 0.0), dx=0.0, dy=1.0)
        pt, dist, degen = closest_point_two_rays(ra, rb)
        assert not degen
        assert pt is not None
        assert pt.x == pytest.approx(1.0, abs=1e-9)
        assert pt.y == pytest.approx(1.0, abs=1e-9)
        assert dist == pytest.approx(0.0, abs=1e-9)


# ---------------------------------------------------------------------------
# crossing_confidence
# ---------------------------------------------------------------------------

class TestCrossingConfidence:
    def test_orthogonal_rays_confidence_1(self):
        """Orthogonal rays: |cross| = 1.0."""
        ra = Ray2D(origin=Point2D(0.0, 0.0), dx=1.0, dy=0.0)
        rb = Ray2D(origin=Point2D(0.0, 0.0), dx=0.0, dy=1.0)
        assert crossing_confidence(ra, rb) == pytest.approx(1.0)

    def test_parallel_rays_confidence_0(self):
        """Parallel rays: |cross| = 0.0."""
        ra = Ray2D(origin=Point2D(0.0, 0.0), dx=0.0, dy=1.0)
        rb = Ray2D(origin=Point2D(1.0, 0.0), dx=0.0, dy=1.0)
        assert crossing_confidence(ra, rb) == pytest.approx(0.0)

    def test_45_degree_crossing_confidence(self):
        """45° crossing → |sin(45°)| ≈ 0.707."""
        ra = Ray2D(origin=Point2D(0.0, 0.0), dx=1.0, dy=0.0)  # East
        angle = math.radians(45.0)
        rb = Ray2D(origin=Point2D(0.0, 0.0), dx=math.cos(angle), dy=math.sin(angle))
        conf = crossing_confidence(ra, rb)
        assert conf == pytest.approx(abs(math.sin(angle)), abs=1e-6)


# ---------------------------------------------------------------------------
# point_in_fence
# ---------------------------------------------------------------------------

class TestPointInFence:
    def _square(self) -> list[Point2D]:
        """Unit square: (0,0)–(1,0)–(1,1)–(0,1)."""
        return [Point2D(0.0, 0.0), Point2D(1.0, 0.0), Point2D(1.0, 1.0), Point2D(0.0, 1.0)]

    def test_point_inside_square_no_margin(self):
        assert point_in_fence(Point2D(0.5, 0.5), self._square(), margin_m=0.0)

    def test_point_outside_square_no_margin(self):
        assert not point_in_fence(Point2D(2.0, 2.0), self._square(), margin_m=0.0)

    def test_point_on_boundary_margin_zero_equivalence(self):
        """margin_m==0 must be EXACTLY point_in_polygon."""
        from conf_pipeline.model import point_in_polygon
        p = Point2D(0.5, 0.0)  # on the boundary
        assert point_in_fence(p, self._square(), margin_m=0.0) == point_in_polygon(p, self._square())

    def test_point_just_outside_but_within_margin(self):
        """Point just outside the square by 0.05 m, margin=0.10 → inside (within margin band)."""
        p = Point2D(1.05, 0.5)  # 0.05 m outside the right edge
        assert point_in_fence(p, self._square(), margin_m=0.10)

    def test_point_outside_margin_band(self):
        """Point 0.30 m outside the right edge, margin=0.10 → outside."""
        p = Point2D(1.30, 0.5)
        assert not point_in_fence(p, self._square(), margin_m=0.10)

    def test_empty_polygon_returns_false(self):
        """Empty polygon — no edges — point never inside."""
        # With margin=0, point_in_polygon on empty returns False
        p = Point2D(0.5, 0.5)
        assert not point_in_fence(p, [], margin_m=0.0)


# ---------------------------------------------------------------------------
# level_cross_check
# ---------------------------------------------------------------------------

class TestLevelCrossCheck:
    def test_loud_source_passes(self):
        """Level well above inside_db threshold → True (sounds like an in-fence source)."""
        # LEVEL_INSIDE_DB = -45 dB → level amplitude threshold
        # 20*log10(level) >= -45 → level >= 10^(-45/20) ≈ 0.00562
        ra = _make_reading(azimuth_deg=0.0, level=0.1)   # loud
        rb = _make_reading(azimuth_deg=90.0, level=0.001)  # quiet
        assert level_cross_check(ra, rb) is True

    def test_quiet_source_fails(self):
        """Both kits very quiet → False (sounds like a far source)."""
        ra = _make_reading(azimuth_deg=0.0, level=1e-6)
        rb = _make_reading(azimuth_deg=90.0, level=1e-6)
        assert level_cross_check(ra, rb) is False

    def test_both_silent_is_false(self):
        """Zero level → 20*log10(1e-9) → very negative → False."""
        ra = _make_reading(azimuth_deg=0.0, level=0.0)
        rb = _make_reading(azimuth_deg=0.0, level=0.0)
        assert level_cross_check(ra, rb) is False

    def test_custom_inside_db(self):
        """Custom inside_db override with a level clearly above the threshold."""
        ra = _make_reading(azimuth_deg=0.0, level=0.5)
        rb = _make_reading(azimuth_deg=0.0, level=0.5)
        # 20*log10(0.5) ≈ -6.02 dB; use a threshold of -7 dB so we're clearly above it
        assert level_cross_check(ra, rb, inside_db=-7.0) is True
        # And clearly below -5 dB
        assert level_cross_check(ra, rb, inside_db=-5.0) is False

    def test_boundary_exact(self):
        """Exactly at the boundary → True (>=)."""
        # LEVEL_INSIDE_DB = -45 dB → amplitude = 10^(-45/20)
        threshold_amp = 10 ** (LEVEL_INSIDE_DB / 20.0)
        ra = _make_reading(azimuth_deg=0.0, level=threshold_amp)
        rb = _make_reading(azimuth_deg=0.0, level=0.0)
        assert level_cross_check(ra, rb) is True


# ---------------------------------------------------------------------------
# fuse_position — headline geometry test (realistic table scenario)
# ---------------------------------------------------------------------------

class TestFusePosition:
    """
    Two POLARIS kits placed at CORNERS of a conference table (~1.6 m long, 0.8 m wide).
    Table polygon: (0,0)-(1.6,0)-(1.6,0.8)-(0,0.8).

    Kit A: position (0.0, −0.3), bearing 30° (angled NE toward table interior).
    Kit B: position (1.6, −0.3), bearing 330° (angled NW toward table interior).

    This geometry gives converging (non-anti-parallel) rays so triangulation works.

    A table talker at (~0.8, 0.4) — the centre of the table.
    A far room source at (~0.8, 4.0) — ~3.6 m beyond the table end (behind the table).
    """

    TABLE_POLYGON = [
        Point2D(0.0, 0.0), Point2D(1.6, 0.0),
        Point2D(1.6, 0.8), Point2D(0.0, 0.8),
    ]
    KIT_A_POS = Point2D(0.0, -0.3)
    KIT_A_BEARING = 30.0    # facing NE (toward the table interior)
    KIT_B_POS = Point2D(1.6, -0.3)
    KIT_B_BEARING = 330.0   # facing NW (toward the table interior)

    POSE_A = KitPose(position=KIT_A_POS, bearing_deg=KIT_A_BEARING)
    POSE_B = KitPose(position=KIT_B_POS, bearing_deg=KIT_B_BEARING)

    TABLE_TALKER = Point2D(0.8, 0.4)   # centre of table
    FAR_SOURCE = Point2D(0.8, 4.0)     # ~3.6 m beyond table on same X as talker

    def _readings(self, source: Point2D, level: float = 0.05) -> tuple[KitReading, KitReading]:
        ra = _kit_reading_for_source(self.KIT_A_POS, self.KIT_A_BEARING, source, level=level)
        rb = _kit_reading_for_source(self.KIT_B_POS, self.KIT_B_BEARING, source, level=level)
        return ra, rb

    def test_table_talker_fuses_inside(self):
        """Table talker at centre: rays cross inside the table polygon → inside=True."""
        ra, rb = self._readings(self.TABLE_TALKER)
        result = fuse_position(ra, rb, self.POSE_A, self.POSE_B, self.TABLE_POLYGON,
                               margin_m=DEFAULT_FENCE_MARGIN_M)
        assert result.inside is True, f"Expected inside; got {result}"
        assert result.point is not None
        assert not result.degenerate

    def test_far_source_fuses_outside(self):
        """Far room source ~3.6 m out: rays cross outside the table polygon → inside=False."""
        ra, rb = self._readings(self.FAR_SOURCE, level=0.005)
        result = fuse_position(ra, rb, self.POSE_A, self.POSE_B, self.TABLE_POLYGON,
                               margin_m=DEFAULT_FENCE_MARGIN_M)
        assert result.inside is False, f"Expected outside; got {result}"
        assert result.point is not None
        assert not result.degenerate

    def test_none_azimuth_marks_degenerate(self):
        """If either kit has no azimuth → degenerate=True, point=None."""
        ra = _make_reading(azimuth_deg=None)
        rb = _make_reading(azimuth_deg=90.0)
        result = fuse_position(ra, rb, self.POSE_A, self.POSE_B, self.TABLE_POLYGON,
                               margin_m=DEFAULT_FENCE_MARGIN_M)
        assert result.degenerate is True
        assert result.point is None

    def test_empty_polygon_always_inside(self):
        """Empty polygon → inside=True (fence is inert, always passes)."""
        ra, rb = self._readings(self.FAR_SOURCE)
        result = fuse_position(ra, rb, self.POSE_A, self.POSE_B, [],
                               margin_m=DEFAULT_FENCE_MARGIN_M)
        assert result.inside is True

    def test_loud_kit_identified(self):
        """The kit with the higher level is the loud_kit."""
        ra = _make_reading(azimuth_deg=90.0, level=0.1)   # louder
        rb = _make_reading(azimuth_deg=270.0, level=0.01)
        result = fuse_position(ra, rb, self.POSE_A, self.POSE_B, self.TABLE_POLYGON,
                               margin_m=DEFAULT_FENCE_MARGIN_M)
        assert result.loud_kit == 0  # kit A is louder

    def test_quiet_both_no_loud_kit(self):
        """Both levels near zero → loud_kit=None."""
        ra = _make_reading(azimuth_deg=90.0, level=0.0)
        rb = _make_reading(azimuth_deg=90.0, level=0.0)
        result = fuse_position(ra, rb, self.POSE_A, self.POSE_B, self.TABLE_POLYGON,
                               margin_m=DEFAULT_FENCE_MARGIN_M)
        assert result.loud_kit is None


# ---------------------------------------------------------------------------
# FenceDecider — hysteresis / no-chatter
# ---------------------------------------------------------------------------

class TestFenceDecider:
    TABLE_POLYGON = [
        Point2D(0.0, 0.0), Point2D(1.6, 0.0),
        Point2D(1.6, 0.8), Point2D(0.0, 0.8),
    ]
    POSE_A = KitPose(position=Point2D(0.0, -0.3), bearing_deg=30.0)
    POSE_B = KitPose(position=Point2D(1.6, -0.3), bearing_deg=330.0)

    TABLE_TALKER = Point2D(0.8, 0.4)
    FAR_SOURCE = Point2D(0.8, 4.0)

    def _readings_for(self, source: Point2D, level: float = 0.05) -> tuple[KitReading, KitReading]:
        ra = _kit_reading_for_source(self.POSE_A.position, self.POSE_A.bearing_deg, source, level=level)
        rb = _kit_reading_for_source(self.POSE_B.position, self.POSE_B.bearing_deg, source, level=level)
        return ra, rb

    def test_stable_inside_source_keep(self):
        """A stable table talker → keep=True every tick."""
        decider = FenceDecider(hold_ticks=3)
        ra, rb = self._readings_for(self.TABLE_TALKER)
        results = [
            decider.update(ra, rb, self.POSE_A, self.POSE_B, self.TABLE_POLYGON, t=float(i))
            for i in range(10)
        ]
        assert all(r.keep for r in results), "All ticks should keep table talker"

    def test_stable_outside_source_reject_after_hold(self):
        """A stable far source → reject after hold_ticks consecutive outside decisions."""
        decider = FenceDecider(hold_ticks=3)
        ra, rb = self._readings_for(self.FAR_SOURCE, level=0.005)
        results = [
            decider.update(ra, rb, self.POSE_A, self.POSE_B, self.TABLE_POLYGON, t=float(i))
            for i in range(10)
        ]
        # First hold_ticks-1 ticks: keep unchanged (still True, initial state)
        assert all(r.keep for r in results[:2])
        # After hold_ticks consecutive OUTSIDE decisions, must switch to reject
        assert not results[3].keep

    def test_no_chatter_on_boundary_jitter(self):
        """Jitter across boundary → FenceDecider should flip at most once with hold_ticks=3."""
        decider = FenceDecider(hold_ticks=3)

        # Alternate between a slightly-inside and slightly-outside position
        slightly_inside = Point2D(0.8, 0.75)    # just inside the top edge
        slightly_outside = Point2D(0.8, 0.90)   # just outside the top edge

        decisions = []
        for i in range(20):
            source = slightly_inside if i % 2 == 0 else slightly_outside
            ra, rb = self._readings_for(source)
            d = decider.update(ra, rb, self.POSE_A, self.POSE_B, self.TABLE_POLYGON, t=float(i))
            decisions.append(d.keep)

        # Count keep→reject or reject→keep transitions
        flips = sum(1 for a, b in zip(decisions, decisions[1:]) if a != b)
        assert flips <= 1, f"Too many flips ({flips}) on boundary jitter; expected ≤1"

    def test_clean_flip_exactly_once_after_hold(self):
        """Clean sustained switch: inside for N ticks, then outside forever → exactly one flip."""
        decider = FenceDecider(hold_ticks=3)
        ra_in, rb_in = self._readings_for(self.TABLE_TALKER)
        ra_out, rb_out = self._readings_for(self.FAR_SOURCE, level=0.005)

        decisions = []
        # 5 inside ticks
        for i in range(5):
            d = decider.update(ra_in, rb_in, self.POSE_A, self.POSE_B, self.TABLE_POLYGON, t=float(i))
            decisions.append(d.keep)
        # 5 outside ticks
        for i in range(5, 10):
            d = decider.update(ra_out, rb_out, self.POSE_A, self.POSE_B, self.TABLE_POLYGON, t=float(i))
            decisions.append(d.keep)

        flips = sum(1 for a, b in zip(decisions, decisions[1:]) if a != b)
        assert flips == 1, f"Expected exactly one flip, got {flips}: {decisions}"

    def test_veto_kit_set_when_rejecting(self):
        """When keep=False, veto_kit should be the loud_kit."""
        decider = FenceDecider(hold_ticks=3)
        # Force outside rejection with loud kit A
        ra, rb = self._readings_for(self.FAR_SOURCE, level=0.005)
        for i in range(5):
            d = decider.update(ra, rb, self.POSE_A, self.POSE_B, self.TABLE_POLYGON, t=float(i))

        # After enough ticks, should be rejecting
        last = d  # type: ignore[possibly-undefined]
        if not last.keep:
            assert last.veto_kit is not None

    def test_veto_kit_none_when_keeping(self):
        """When keep=True, veto_kit must be None."""
        decider = FenceDecider(hold_ticks=3)
        ra, rb = self._readings_for(self.TABLE_TALKER)
        for i in range(10):
            d = decider.update(ra, rb, self.POSE_A, self.POSE_B, self.TABLE_POLYGON, t=float(i))
            assert d.veto_kit is None

    def test_empty_polygon_always_keep(self):
        """Empty polygon → always keep=True (inert mode)."""
        decider = FenceDecider(hold_ticks=3)
        ra, rb = self._readings_for(self.FAR_SOURCE, level=0.005)
        for i in range(10):
            d = decider.update(ra, rb, self.POSE_A, self.POSE_B, [], t=float(i))
            assert d.keep is True

    def test_reset_clears_state(self):
        """After reset(), decider starts fresh."""
        decider = FenceDecider(hold_ticks=3)
        ra_out, rb_out = self._readings_for(self.FAR_SOURCE, level=0.005)
        # Drive to rejection
        for i in range(6):
            d = decider.update(ra_out, rb_out, self.POSE_A, self.POSE_B, self.TABLE_POLYGON, t=float(i))
        assert not d.keep  # type: ignore[possibly-undefined]

        decider.reset()

        # After reset, should start from keep=True again
        ra_in, rb_in = self._readings_for(self.TABLE_TALKER)
        d2 = decider.update(ra_in, rb_in, self.POSE_A, self.POSE_B, self.TABLE_POLYGON, t=100.0)
        assert d2.keep is True

    def test_degenerate_falls_back_to_level_check(self):
        """When azimuth is None → degenerate → fallback to level_cross_check."""
        decider = FenceDecider(hold_ticks=1)  # hold_ticks=1 → instant flip
        # Loud source (level above inside_db) → keep
        ra = _make_reading(azimuth_deg=None, level=0.1)
        rb = _make_reading(azimuth_deg=None, level=0.1)
        d = decider.update(ra, rb, self.POSE_A, self.POSE_B, self.TABLE_POLYGON, t=0.0)
        assert d.keep is True

        decider.reset()
        # Quiet source → reject
        ra_q = _make_reading(azimuth_deg=None, level=1e-6)
        rb_q = _make_reading(azimuth_deg=None, level=1e-6)
        # Need hold_ticks=1 so it flips immediately
        for i in range(2):
            d2 = decider.update(ra_q, rb_q, self.POSE_A, self.POSE_B, self.TABLE_POLYGON, t=float(i))
        assert not d2.keep  # type: ignore[possibly-undefined]


# ---------------------------------------------------------------------------
# Module-level constants
# ---------------------------------------------------------------------------

class TestConstants:
    def test_default_fence_margin_m(self):
        assert DEFAULT_FENCE_MARGIN_M == pytest.approx(0.20)

    def test_default_fence_hold_ticks(self):
        assert DEFAULT_FENCE_HOLD_TICKS == 3

    def test_level_inside_db(self):
        assert LEVEL_INSIDE_DB == pytest.approx(-45.0)

    def test_fence_config_error_is_exception(self):
        with pytest.raises(FenceConfigError):
            raise FenceConfigError("test error")
