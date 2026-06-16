"""Room-aware seat mapping: a detected array-relative azimuth -> nearest room seat.

Pure geometry, no hardware. Convention throughout: bearings are 0deg = +Y, clockwise (the
engine-wide convention; `bearing_to_deg` uses atan2(dx, dy)). Seats are stored as world
coordinates. The array sits at the origin so a seat's world bearing equals its position angle:
(0, 3) -> 0deg (north), (3, 0) -> 90deg (east), (0, -3) -> 180deg, (-3, 0) -> 270deg.
"""
import conf_pipeline as cp
from conf_pipeline.model import Point2D, RoomLayout, RoomObject, SeatAnchor

ORIGIN = Point2D(0.0, 0.0)
# (id, world position) — north at bearing 0, east at bearing 90 from the origin.
NORTH = ("N", SeatAnchor(position=Point2D(0.0, 3.0)))
EAST = ("E", SeatAnchor(position=Point2D(3.0, 0.0)))
SOUTH = ("S", SeatAnchor(position=Point2D(0.0, -3.0)))
WEST = ("W", SeatAnchor(position=Point2D(-3.0, 0.0)))
RING = [NORTH, EAST, SOUTH, WEST]


# --------------------------------------------------------------------------- #
# Low-level nearest_seat
# --------------------------------------------------------------------------- #
def test_nearest_seat_picks_the_seat_the_azimuth_points_at():
    # bearing 0: the array's 0deg reference already points room +Y, so room_az == azimuth.
    assert cp.nearest_seat(0.0, ORIGIN, 0.0, RING).seat_id == "N"
    assert cp.nearest_seat(90.0, ORIGIN, 0.0, RING).seat_id == "E"
    assert cp.nearest_seat(180.0, ORIGIN, 0.0, RING).seat_id == "S"
    assert cp.nearest_seat(270.0, ORIGIN, 0.0, RING).seat_id == "W"
    m = cp.nearest_seat(0.0, ORIGIN, 0.0, RING)
    assert m.separation_deg == 0.0 and m.distance_m == 3.0       # exact hit, 3 m away


def test_array_bearing_rotates_the_azimuth_into_room_coordinates():
    # The SAME array-relative azimuth (0) maps to a DIFFERENT seat as the array is re-mounted.
    assert cp.nearest_seat(0.0, ORIGIN, 0.0, RING).seat_id == "N"    # 0deg ref -> +Y
    assert cp.nearest_seat(0.0, ORIGIN, 90.0, RING).seat_id == "E"   # 0deg ref -> +X (east)
    assert cp.nearest_seat(0.0, ORIGIN, 180.0, RING).seat_id == "S"
    assert cp.nearest_seat(0.0, ORIGIN, 270.0, RING).seat_id == "W"
    # and the rotation wraps: azimuth 350 + bearing 20 == room 10 -> still nearest N.
    assert cp.nearest_seat(350.0, ORIGIN, 20.0, RING).seat_id == "N"


def test_between_seats_returns_none_past_the_gate():
    seats = [NORTH, EAST]                                            # bearings 0 and 90
    assert cp.nearest_seat(20.0, ORIGIN, 0.0, seats).seat_id == "N"  # 20deg off N, within 30deg
    assert cp.nearest_seat(44.0, ORIGIN, 0.0, seats) is None         # 44/46deg off both -> between seats
    # a wider gate accepts it (and still picks the angularly closer one)
    assert cp.nearest_seat(44.0, ORIGIN, 0.0, seats, max_separation_deg=60.0).seat_id == "N"
    assert cp.nearest_seat(46.0, ORIGIN, 0.0, seats, max_separation_deg=60.0).seat_id == "E"


def test_nearest_seat_no_seats_is_none():
    assert cp.nearest_seat(0.0, ORIGIN, 0.0, []) is None


def test_nearest_seat_reports_the_matched_anchor_and_distance():
    far = ("F", SeatAnchor(position=Point2D(0.0, 4.0), facing_deg=180.0))
    m = cp.nearest_seat(0.0, ORIGIN, 0.0, [far])
    assert m.seat_id == "F" and m.anchor.facing_deg == 180.0 and m.distance_m == 4.0


# --------------------------------------------------------------------------- #
# Config-level nearest_seat_for_array + room_seats
# --------------------------------------------------------------------------- #
def _config_with_array_and_seats(bearing=0.0, position=Point2D(0.0, 0.0)):
    c = cp.create_config("rt", "2026-01-01T00:00:00Z")
    c = cp.add_device(c, cp.create_microphone_array("A", "Array", position=position))
    if bearing is not None:
        c = cp.set_array_bearing(c, "A", bearing)
    c.room = RoomLayout(
        vertices=[Point2D(-3, -3), Point2D(3, -3), Point2D(3, 3), Point2D(-3, 3)],
        height=3.0, units="meters",
        objects=[RoomObject(
            id="sofa", kind="sofa", position=Point2D(0.0, 3.0),
            seats=[SeatAnchor(position=Point2D(0.0, 3.0)),     # sofa-seat1 -> bearing 0 (north)
                   SeatAnchor(position=Point2D(3.0, 0.0))],    # sofa-seat2 -> bearing 90 (east)
        )],
    )
    return c


def test_room_seats_ids_match_coverage_sim_room_targets():
    # Non-circular parity check: the mapper's seat ids must equal coverage_sim's seat targets
    # (both 1-based) so a matched seat correlates with a coverage target. Compares the two
    # functions directly, so it FAILS if either drifts.
    c = _config_with_array_and_seats()
    mapper_ids = [sid for sid, _ in cp.room_seats(c)]
    coverage_seat_ids = [t.id for t in cp.room_targets(c) if "-seat" in t.id]
    assert mapper_ids == coverage_seat_ids == ["sofa-seat1", "sofa-seat2"]


def test_nearest_seat_for_array_end_to_end():
    c = _config_with_array_and_seats(bearing=0.0)
    assert cp.nearest_seat_for_array(c, "A", 0.0).seat_id == "sofa-seat1"   # north (first seat)
    assert cp.nearest_seat_for_array(c, "A", 90.0).seat_id == "sofa-seat2"  # east (second seat)
    # re-mounting the array (bearing 90) re-maps the same detected azimuth
    c2 = _config_with_array_and_seats(bearing=90.0)
    assert cp.nearest_seat_for_array(c2, "A", 0.0).seat_id == "sofa-seat2"  # 0 + 90 -> east


def test_nearest_seat_for_array_none_cases():
    # no bearing set -> orientation unknown -> None
    assert cp.nearest_seat_for_array(_config_with_array_and_seats(bearing=None), "A", 0.0) is None
    # no position -> None
    assert cp.nearest_seat_for_array(_config_with_array_and_seats(position=None), "A", 0.0) is None
    # unknown array id -> None
    assert cp.nearest_seat_for_array(_config_with_array_and_seats(), "ZZ", 0.0) is None
    # a device that exists but is not a microphone array -> None
    c = _config_with_array_and_seats()
    c = cp.add_device(c, cp.create_codec("C", "Codec", "dante"))
    assert cp.nearest_seat_for_array(c, "C", 0.0) is None


def test_nearest_seat_for_array_no_room_or_no_seats_is_none():
    c = cp.create_config("rt", "2026-01-01T00:00:00Z")
    c = cp.add_device(c, cp.create_microphone_array("A", "Array", position=Point2D(0.0, 0.0)))
    c = cp.set_array_bearing(c, "A", 0.0)
    assert cp.nearest_seat_for_array(c, "A", 0.0) is None           # no room at all
    c.room = RoomLayout(vertices=[Point2D(0, 0), Point2D(2, 0), Point2D(2, 2), Point2D(0, 2)],
                        height=3.0, units="meters", objects=[])
    assert cp.nearest_seat_for_array(c, "A", 0.0) is None           # room, but no seats


# --------------------------------------------------------------------------- #
# seat_null_azimuths — array-relative bearings of the non-target seats (room-aware nulling)
# --------------------------------------------------------------------------- #
def _az(config, array_id, **kw):
    return [round(a, 3) for a in cp.seat_null_azimuths(config, array_id, **kw)]


def test_seat_null_azimuths_array_relative_and_exclusion():
    # seats: sofa-seat1 at (0,3) → world bearing 0 (north); sofa-seat2 at (3,0) → world bearing 90 (east)
    c = _config_with_array_and_seats(bearing=0.0)
    assert _az(c, "A") == [0.0, 90.0]                                  # bearing 0 → array frame == world
    assert _az(c, "A", exclude_seat_id="sofa-seat1") == [90.0]         # drop the seat being listened to
    # re-mount the array (bearing 90): each seat rotates into the array frame by −90
    c2 = _config_with_array_and_seats(bearing=90.0)
    assert _az(c2, "A") == [270.0, 0.0]                               # north 0−90→270, east 90−90→0


def test_seat_null_azimuths_none_cases():
    assert cp.seat_null_azimuths(_config_with_array_and_seats(bearing=None), "A") == []   # no bearing
    assert cp.seat_null_azimuths(_config_with_array_and_seats(position=None), "A") == []  # no position
    assert cp.seat_null_azimuths(_config_with_array_and_seats(), "ZZ") == []              # unknown array
    c = _config_with_array_and_seats()
    c = cp.add_device(c, cp.create_codec("C", "Codec", "dante"))
    assert cp.seat_null_azimuths(c, "C") == []                        # device is not a microphone array
