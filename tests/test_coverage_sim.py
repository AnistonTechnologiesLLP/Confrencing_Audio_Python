"""Geometric coverage simulation: mic pickup, camera FOV + occlusion, speaker cone."""

import conf_pipeline as cp
from conf_pipeline import coverage_sim as cs
from conf_pipeline.model import (
    ConferencingCamera,
    Loudspeaker,
    Point2D,
    RoomLayout,
    RoomObject,
    SeatAnchor,
)


def _room(c, w=6.0, d=5.0, h=3.0, objects=None):
    c.room = RoomLayout(
        vertices=[Point2D(0, 0), Point2D(w, 0), Point2D(w, d), Point2D(0, d)],
        height=h, objects=objects or [],
    )
    return c


def _config():
    return cp.create_config("Room", "2026-06-13T00:00:00Z")


# --------------------------------------------------------------------------- #
# Camera FOV + occlusion
# --------------------------------------------------------------------------- #
def test_camera_frames_subject_in_fov():
    c = _room(_config())
    c = cp.add_talker(c, cp.create_talker("t1", "Person", Point2D(3.0, 3.0)))
    # camera at the front wall (y≈0) looking +Y (north) into the room
    cam = ConferencingCamera(id="cam", label="Cam", ports=[], position=Point2D(3.0, 0.2),
                             bearing_deg=0.0, profile_id="generic-wide-camera")
    c.devices = [cam]
    rc = cp.simulate_room_coverage(c)
    assert len(rc.cameras) == 1
    hit = rc.cameras[0].targets[0]
    assert hit.in_coverage is True and hit.blocked is False
    assert rc.cameras[0].framed_pct == 100.0


def test_camera_misses_subject_behind_it():
    c = _room(_config())
    c = cp.add_talker(c, cp.create_talker("t1", "Person", Point2D(3.0, 1.0)))
    # camera at back wall looking +Y, subject is behind it (smaller y)
    cam = ConferencingCamera(id="cam", label="Cam", ports=[], position=Point2D(3.0, 4.5),
                             bearing_deg=0.0, profile_id="generic-ptz-camera")
    c.devices = [cam]
    rc = cp.simulate_room_coverage(c)
    assert rc.cameras[0].targets[0].in_coverage is False


def test_low_camera_blocked_by_screen_but_not_table():
    c = _config()
    # subject straight ahead of a soundbar (low) camera
    subject = Point2D(3.0, 4.0)
    cam_pos = Point2D(3.0, 0.3)
    table = RoomObject(id="tbl", kind="table", position=Point2D(3.0, 2.0), rotation_deg=0.0)
    c = _room(c, objects=[table])
    c = cp.add_talker(c, cp.create_talker("t1", "Person", subject))
    cam = ConferencingCamera(id="cam", label="Bar", ports=[], position=cam_pos,
                             elevation=1.1, bearing_deg=0.0, profile_id="generic-soundbar-camera")
    c.devices = [cam]
    # a table between camera and subject does NOT block (catalog blocks_camera=False)
    assert cp.simulate_room_coverage(c).cameras[0].targets[0].blocked is False

    # swap the table for a tall screen at the same spot → blocks the low camera
    c.room.objects = [RoomObject(id="scr", kind="screen", position=Point2D(3.0, 2.0), rotation_deg=0.0)]
    blocked_hit = cp.simulate_room_coverage(c).cameras[0].targets[0]
    assert blocked_hit.in_coverage is True and blocked_hit.blocked is True


def test_ceiling_camera_sees_over_screen():
    """A high camera looks over a screen the soundbar camera couldn't clear."""
    c = _config()
    c = _room(c, objects=[RoomObject(id="scr", kind="screen", position=Point2D(3.0, 2.0), rotation_deg=0.0)])
    c = cp.add_talker(c, cp.create_talker("t1", "Person", Point2D(3.0, 4.0)))
    cam = ConferencingCamera(id="cam", label="PTZ", ports=[], position=Point2D(3.0, 0.3),
                             elevation=2.6, bearing_deg=0.0, profile_id="generic-ptz-camera")
    c.devices = [cam]
    assert cp.simulate_room_coverage(c).cameras[0].targets[0].blocked is False


# --------------------------------------------------------------------------- #
# Seats become targets
# --------------------------------------------------------------------------- #
def test_furniture_seats_are_targets():
    c = _config()
    sofa = RoomObject(id="sofa", kind="sofa", position=Point2D(3.0, 3.0),
                      seats=[SeatAnchor(position=Point2D(2.5, 3.0)), SeatAnchor(position=Point2D(3.5, 3.0))])
    c = _room(c, objects=[sofa])
    rc = cp.simulate_room_coverage(c)
    seat_ids = {t.id for t in rc.targets}
    assert {"sofa-seat1", "sofa-seat2"} <= seat_ids


# --------------------------------------------------------------------------- #
# Mic pickup
# --------------------------------------------------------------------------- #
def test_unzoned_array_covers_within_circle():
    c = _room(_config(), h=3.0)
    # ceiling array at room centre; talker beneath it is covered, far corner is not
    c = cp.add_device(c, cp.create_microphone_array("A", "Array"))
    c = cp.set_device_position(c, "A", Point2D(3.0, 2.5))
    c = cp.add_talker(c, cp.create_talker("near", "Near", Point2D(3.0, 2.5)))
    c = cp.add_talker(c, cp.create_talker("far", "Far", Point2D(0.1, 0.1)))
    rc = cp.simulate_room_coverage(c)
    mc = rc.mics[0]
    assert mc.radius_m > 0
    by_id = {h.id: h for h in mc.targets}
    assert by_id["near"].in_coverage is True
    assert by_id["far"].in_coverage is False


def test_summary_reports_gaps():
    c = _room(_config())
    c = cp.add_device(c, cp.create_microphone_array("A", "Array"))
    c = cp.set_device_position(c, "A", Point2D(3.0, 2.5))
    c = cp.add_talker(c, cp.create_talker("near", "Near", Point2D(3.0, 2.5)))
    c = cp.add_talker(c, cp.create_talker("far", "Far", Point2D(0.1, 0.1)))
    rc = cp.simulate_room_coverage(c)
    assert rc.summary["target_count"] == 2
    assert "far" in rc.summary["mic_gaps"]
    assert 0.0 <= rc.summary["mic_coverage_pct"] <= 100.0


# --------------------------------------------------------------------------- #
# Speaker dispersion
# --------------------------------------------------------------------------- #
def test_speaker_cone_covers_in_front():
    c = _room(_config())
    c = cp.add_talker(c, cp.create_talker("t1", "Front", Point2D(3.0, 3.0)))
    c = cp.add_talker(c, cp.create_talker("t2", "Behind", Point2D(3.0, 0.1)))
    spk = Loudspeaker(id="s", label="LS", ports=[], position=Point2D(3.0, 0.3),
                      bearing_deg=0.0, profile_id="generic-loudspeaker")
    c.devices = [spk]
    rc = cp.simulate_room_coverage(c)
    by_id = {h.id: h for h in rc.speakers[0].targets}
    assert by_id["t1"].in_coverage is True
    assert by_id["t2"].in_coverage is False


def test_unaimed_speaker_is_omni():
    c = _room(_config())
    c = cp.add_talker(c, cp.create_talker("t1", "Anywhere", Point2D(0.5, 4.5)))
    spk = Loudspeaker(id="s", label="LS", ports=[], position=Point2D(3.0, 2.5),
                      profile_id="generic-loudspeaker")  # no bearing
    c.devices = [spk]
    rc = cp.simulate_room_coverage(c)
    assert rc.speakers[0].wedge.h_half_deg == 180.0
    assert rc.speakers[0].targets[0].in_coverage is True


# --------------------------------------------------------------------------- #
# Occlusion primitive
# --------------------------------------------------------------------------- #
def test_segment_intersects_obb():
    box = cp.obb_corners(Point2D(0, 0), 2.0, 2.0, 0.0)  # ±1 square
    assert cs.segment_intersects_obb(Point2D(-3, 0), Point2D(3, 0), box) is True
    assert cs.segment_intersects_obb(Point2D(-3, 5), Point2D(3, 5), box) is False
