"""Tests for conf_pipeline.seat_zones.derived_room_seats (Task 1).

The _room() helper builds a minimal but valid SystemConfig — SystemConfig has
many required positional fields (version, devices, routes, matrix, automixer,
mute_links, talkers, metadata) plus an optional room kwarg.
"""
from conf_pipeline.model import (
    AutomixerConfig,
    MatrixMixer,
    Point2D,
    RoomLayout,
    RoomObject,
    SeatAnchor,
    SystemConfig,
)
from conf_pipeline.seat_zones import derived_room_seats


def _room(objects):
    return SystemConfig(
        version=5,
        devices=[],
        routes=[],
        matrix=MatrixMixer(processor_id=""),
        automixer=AutomixerConfig(processor_id=""),
        mute_links=[],
        talkers=[],
        metadata={},
        room=RoomLayout(
            vertices=[Point2D(-3, -3), Point2D(3, -3), Point2D(3, 3), Point2D(-3, 3)],
            height=3.0,
            objects=objects,
        ),
    )


def test_chair_yields_one_seat_at_its_position():
    cfg = _room([RoomObject(id="c1", kind="chair", position=Point2D(1.0, 2.0))])
    seats = derived_room_seats(cfg)
    assert len(seats) == 1
    sid, anchor = seats[0]
    assert sid == "c1-seat1"
    assert abs(anchor.position.x - 1.0) < 1e-9 and abs(anchor.position.y - 2.0) < 1e-9


def test_sofa_yields_capacity_seats_spread_across_width():
    # sofa catalog: width 2.0 m, capacity 3, no rotation → 3 seats along +X centred on position
    cfg = _room([RoomObject(id="s1", kind="sofa", position=Point2D(0.0, 0.0))])
    seats = derived_room_seats(cfg)
    assert [sid for sid, _ in seats] == ["s1-seat1", "s1-seat2", "s1-seat3"]
    xs = sorted(a.position.x for _, a in seats)
    # evenly spaced across width 2.0 → at -2/3, 0, +2/3 (fractions 1/6, 3/6, 5/6)
    assert abs(xs[0] - (-2.0 / 3.0)) < 1e-6
    assert abs(xs[1] - 0.0) < 1e-6
    assert abs(xs[2] - (2.0 / 3.0)) < 1e-6
    assert all(abs(a.position.y) < 1e-9 for _, a in seats)


def test_explicit_seats_used_verbatim_not_derived():
    obj = RoomObject(id="t1", kind="table", position=Point2D(0.0, 0.0),
                     seats=[SeatAnchor(position=Point2D(0.0, -0.6)), SeatAnchor(position=Point2D(0.0, 0.6))])
    seats = derived_room_seats(_room([obj]))
    assert [sid for sid, _ in seats] == ["t1-seat1", "t1-seat2"]
    assert abs(seats[0][1].position.y - (-0.6)) < 1e-9


def test_bare_table_yields_no_seats():
    cfg = _room([RoomObject(id="t1", kind="table", position=Point2D(0.0, 0.0))])
    assert derived_room_seats(cfg) == []


def test_rotated_sofa_spreads_along_rotated_width():
    # 90° clockwise: local +X maps to -Y (x'=lx*cos+ly*sin, y'=-lx*sin+ly*cos; cos90=0, sin90=1)
    cfg = _room([RoomObject(id="s1", kind="sofa", position=Point2D(0.0, 0.0), rotation_deg=90.0)])
    seats = derived_room_seats(cfg)
    ys = sorted(a.position.y for _, a in seats)
    assert all(abs(a.position.x) < 1e-6 for _, a in seats)  # spread now along Y
    assert abs(ys[0] - (-2.0 / 3.0)) < 1e-6 and abs(ys[2] - (2.0 / 3.0)) < 1e-6
