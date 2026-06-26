import conf_pipeline as cp
from conf_pipeline.model import Point2D, bearing_to_deg
from conf_pipeline.seat_mapper import _array_relative_azimuth, learn_bearing


def test_learn_bearing_is_inverse_of_array_relative_azimuth():
    array = Point2D(0.0, 0.0)
    ref = Point2D(2.0, 1.0)
    for true_bearing in (0.0, 37.0, 90.0, 200.0, 359.0):
        measured = _array_relative_azimuth(array, true_bearing, ref)   # what DOA would report
        learned = learn_bearing(array, ref, measured)
        # learned bearing must reproduce the same azimuth (mod 360, within float eps)
        assert abs(((learned - true_bearing + 180.0) % 360.0) - 180.0) < 1e-6


def test_learn_bearing_normalizes_into_0_360():
    b = learn_bearing(Point2D(0.0, 0.0), Point2D(0.0, 1.0), 350.0)   # ref due +Y (bearing_to_deg=0)
    assert 0.0 <= b < 360.0
    assert abs(b - 10.0) < 1e-6     # 0 - 350 = -350 → +10


def test_reference_due_plus_y_zero_measured_gives_zero_bearing():
    # ref straight ahead (+Y), DOA reads 0° → array faces +Y → bearing 0
    b = learn_bearing(Point2D(0.0, 0.0), Point2D(0.0, 2.0), 0.0)
    assert abs(b) < 1e-6
