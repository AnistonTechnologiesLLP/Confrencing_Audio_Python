"""Sample configurations (meeting / conference / training / lecture rooms)."""
from __future__ import annotations

import conf_pipeline as cp
from conf_pipeline.coverage import create_microphone_array, dedicated_zone, dynamic_zone, exclusion_zone
from conf_pipeline.model import AecConfig, Point2D, PolygonShape, RectShape

from .state import now_iso


def _place(c, did, x, y):
    return cp.set_device_position(c, did, Point2D(x, y))


def _wire(c, mic_out_ports, codec_id, speaker_ids):
    """Route mics + far-end codec into the processor, feed the speakers, send the
    room mix back to the codec, then ``auto_configure`` AEC references / automixer.

    Mirrors the hand-built boardroom routing but generated from the device lists,
    so every sample validates with the same far-end-reference AEC setup."""
    n = 1
    for port in mic_out_ports:
        c = cp.route(c, port, f"P-in-dante-{n}")
        n += 1
    far_in = f"P-in-dante-{n}"
    c = cp.route(c, f"{codec_id}-out-dante-1", far_in)  # far-end audio into the room
    for sid in speaker_ids:
        c = cp.route(c, "P-out-analog-1", f"{sid}-in-analog-1")
    c = cp.matrix_for(c, "P").route(far_in, "P-out-analog-1")  # far-end -> speakers
    return cp.auto_configure(c)


def boardroom():
    c = cp.create_config("Boardroom", now_iso())
    c = cp.set_room(c, cp.rectangular_room(9, 7, 3))
    c = cp.add_device(c, cp.create_processor("P", "DSP"))
    c = cp.add_device(c, create_microphone_array("A1", "Ceiling Array 1", "automatic"))
    c = cp.add_device(c, create_microphone_array("A2", "Ceiling Array 2", "automatic"))
    c = cp.add_device(c, cp.create_wireless_mic("PM", "Presenter Mic", "dante"))
    c = cp.add_device(c, cp.create_loudspeaker("L1", "Speaker L", "analog"))
    c = cp.add_device(c, cp.create_loudspeaker("L2", "Speaker R", "analog"))
    c = cp.add_device(c, cp.create_codec("C", "Codec", "dante"))
    c = _place(c, "P", 8, 6)
    c = _place(c, "A1", 3, 2.5)
    c = _place(c, "A2", 6, 2.5)
    c = _place(c, "PM", 4.5, 0.8)
    c = _place(c, "L1", 2, 6.2)
    c = _place(c, "L2", 7, 6.2)
    c = _place(c, "C", 8, 1)
    c = cp.route(c, "A1-out-mix", "P-in-dante-1")
    c = cp.route(c, "A2-out-mix", "P-in-dante-2")
    c = cp.route(c, "PM-out-dante-1", "P-in-dante-3")
    c = cp.route(c, "C-out-dante-1", "P-in-dante-4")
    c = cp.route(c, "P-out-analog-1", "L1-in-analog-1")
    c = cp.route(c, "P-out-analog-1", "L2-in-analog-1")
    c = cp.matrix_for(c, "P").route("P-in-dante-4", "P-out-analog-1")
    c = cp.matrix_for(c, "P").route("P-in-dante-3", "P-out-analog-1")
    c = cp.matrix_for(c, "P").route("P-in-dante-1", "P-out-dante-1")
    c = cp.matrix_for(c, "P").route("P-in-dante-2", "P-out-dante-1")
    c = cp.matrix_for(c, "P").route("P-in-dante-3", "P-out-dante-1")
    c = cp.route(c, "P-out-dante-1", "C-in-dante-1")
    c = cp.matrix_for(c, "P").route("P-in-dante-4", "P-out-dante-2")
    c = cp.set_aec(c, "A1", AecConfig(True, "P-out-analog-1"))
    c = cp.set_aec(c, "A2", AecConfig(True, "P-out-analog-1"))
    c = cp.set_aec(c, "PM", AecConfig(True, "P-out-dante-2"))
    c = cp.add_coverage_zone(c, "A1", dynamic_zone("A1-z1", "Table", RectShape(origin=Point2D(1.5, 1), width=5, height=4)))
    c = cp.add_coverage_zone(c, "A1", exclusion_zone("A1-x1", "Doorway", RectShape(origin=Point2D(0.4, 5), width=1.6, height=1.6)))
    c = cp.add_talker(c, cp.create_talker("T1", "Presenter", Point2D(4.5, 1.4), 1.55))
    c = cp.add_talker(c, cp.create_talker("T2", "Attendee", Point2D(3, 3), 1.2))
    return c


def huddle():
    c = cp.create_config("Huddle", now_iso())
    c = cp.set_room(c, cp.rectangular_room(5, 4, 2.7))
    c = cp.add_device(c, cp.create_processor("P", "DSP"))
    c = cp.add_device(c, create_microphone_array("A1", "Table Array", "automatic"))
    c = cp.add_device(c, cp.create_loudspeaker("L1", "Soundbar", "analog"))
    c = cp.add_device(c, cp.create_codec("C", "Room Codec", "dante"))
    c = _place(c, "P", 4, 3.2)
    c = _place(c, "A1", 2.5, 2)
    c = _place(c, "L1", 2.5, 0.6)
    c = _place(c, "C", 4, 0.8)
    c = cp.route(c, "A1-out-mix", "P-in-dante-1")
    c = cp.route(c, "C-out-dante-1", "P-in-dante-2")
    c = cp.route(c, "P-out-analog-1", "L1-in-analog-1")
    c = cp.matrix_for(c, "P").route("P-in-dante-2", "P-out-analog-1")
    return cp.auto_configure(c)


def meeting_room():
    """Small meeting room: one ceiling array over a 4-seat table + soundbar."""
    c = cp.create_config("Meeting Room", now_iso())
    c = cp.set_room(c, cp.rectangular_room(7, 5, 2.8))
    c = cp.add_device(c, cp.create_processor("P", "DSP"))
    c = cp.add_device(c, create_microphone_array("A1", "Ceiling Array", "automatic"))
    c = cp.add_device(c, cp.create_loudspeaker("L1", "Soundbar", "analog"))
    c = cp.add_device(c, cp.create_codec("C", "Room Codec", "dante"))
    c = _place(c, "P", 6, 4.3)
    c = _place(c, "A1", 3.5, 2.5)
    c = _place(c, "L1", 3.5, 0.6)
    c = _place(c, "C", 6, 1)
    c = cp.add_coverage_zone(c, "A1", dynamic_zone("A1-z1", "Table", RectShape(origin=Point2D(1.5, 1.2), width=4, height=2.6)))
    c = cp.add_talker(c, cp.create_talker("T1", "Host", Point2D(2, 2.5), 1.2))
    c = cp.add_talker(c, cp.create_talker("T2", "Guest A", Point2D(3.5, 1.6)))
    c = cp.add_talker(c, cp.create_talker("T3", "Guest B", Point2D(3.5, 3.4)))
    c = cp.add_talker(c, cp.create_talker("T4", "Guest C", Point2D(5, 2.5)))
    return _wire(c, ["A1-out-mix"], "C", ["L1"])


def conference_room():
    """Large conference room: three ceiling arrays down a long boardroom table."""
    c = cp.create_config("Conference Room", now_iso())
    c = cp.set_room(c, cp.rectangular_room(12, 7, 3))
    c = cp.add_device(c, cp.create_processor("P", "DSP"))
    c = cp.add_device(c, create_microphone_array("A1", "Ceiling Array 1", "automatic"))
    c = cp.add_device(c, create_microphone_array("A2", "Ceiling Array 2", "automatic"))
    c = cp.add_device(c, create_microphone_array("A3", "Ceiling Array 3", "automatic"))
    c = cp.add_device(c, cp.create_wireless_mic("PM", "Presenter Mic", "dante"))
    c = cp.add_device(c, cp.create_loudspeaker("L1", "Speaker L", "analog"))
    c = cp.add_device(c, cp.create_loudspeaker("L2", "Speaker R", "analog"))
    c = cp.add_device(c, cp.create_codec("C", "Room Codec", "dante"))
    c = _place(c, "P", 11, 6.2)
    c = _place(c, "A1", 3, 3.5)
    c = _place(c, "A2", 6, 3.5)
    c = _place(c, "A3", 9, 3.5)
    c = _place(c, "PM", 6, 0.8)
    c = _place(c, "L1", 2, 6.5)
    c = _place(c, "L2", 10, 6.5)
    c = _place(c, "C", 11, 1)
    c = cp.add_coverage_zone(c, "A2", dynamic_zone("A2-z1", "Table", RectShape(origin=Point2D(2, 2), width=8, height=3)))
    c = cp.add_coverage_zone(c, "A1", exclusion_zone("A1-x1", "Doorway", RectShape(origin=Point2D(0.4, 5), width=1.6, height=1.6)))
    c = cp.add_talker(c, cp.create_talker("T1", "Chair", Point2D(2.5, 3.5), 1.3))
    c = cp.add_talker(c, cp.create_talker("T2", "Attendee A", Point2D(4.5, 2.4)))
    c = cp.add_talker(c, cp.create_talker("T3", "Attendee B", Point2D(4.5, 4.6)))
    c = cp.add_talker(c, cp.create_talker("T4", "Attendee C", Point2D(7.5, 2.4)))
    c = cp.add_talker(c, cp.create_talker("T5", "Attendee D", Point2D(7.5, 4.6)))
    c = cp.add_talker(c, cp.create_talker("T6", "Presenter", Point2D(9.5, 3.5), 1.55))
    return _wire(c, ["A1-out-mix", "A2-out-mix", "A3-out-mix", "PM-out-dante-1"], "C", ["L1", "L2"])


def training_room():
    """Training room / classroom: front + rear arrays, lectern mic, audience zone."""
    c = cp.create_config("Training Room", now_iso())
    c = cp.set_room(c, cp.rectangular_room(10, 8, 3.2))
    c = cp.add_device(c, cp.create_processor("P", "DSP"))
    c = cp.add_device(c, create_microphone_array("A1", "Front Array", "automatic"))
    c = cp.add_device(c, create_microphone_array("A2", "Rear Array", "automatic"))
    c = cp.add_device(c, cp.create_wireless_mic("LM", "Lectern Mic", "dante"))
    c = cp.add_device(c, cp.create_loudspeaker("L1", "Speaker L", "analog"))
    c = cp.add_device(c, cp.create_loudspeaker("L2", "Speaker R", "analog"))
    c = cp.add_device(c, cp.create_codec("C", "Room Codec", "dante"))
    c = _place(c, "P", 9, 7.2)
    c = _place(c, "A1", 5, 2.5)
    c = _place(c, "A2", 5, 6)
    c = _place(c, "LM", 1.5, 1.5)
    c = _place(c, "L1", 2, 0.5)
    c = _place(c, "L2", 8, 0.5)
    c = _place(c, "C", 9, 1)
    c = cp.add_coverage_zone(c, "A1", dedicated_zone("A1-z1", "Lectern", Point2D(0.8, 0.8)))
    c = cp.add_coverage_zone(c, "A2", dynamic_zone("A2-z1", "Audience", RectShape(origin=Point2D(2, 3.5), width=6, height=4)))
    c = cp.add_coverage_zone(c, "A1", exclusion_zone("A1-x1", "AV Closet", RectShape(origin=Point2D(8.2, 6.2), width=1.6, height=1.6)))
    c = cp.add_talker(c, cp.create_talker("T1", "Instructor", Point2D(1.5, 1.4), 1.6))
    c = cp.add_talker(c, cp.create_talker("T2", "Student 1", Point2D(3, 4.5)))
    c = cp.add_talker(c, cp.create_talker("T3", "Student 2", Point2D(5, 4.5)))
    c = cp.add_talker(c, cp.create_talker("T4", "Student 3", Point2D(3, 6)))
    c = cp.add_talker(c, cp.create_talker("T5", "Student 4", Point2D(5, 6)))
    return _wire(c, ["A1-out-mix", "A2-out-mix", "LM-out-dante-1"], "C", ["L1", "L2"])


def lecture_hall():
    """Lecture hall / auditorium: stage array + two audience arrays, presenter mic."""
    c = cp.create_config("Lecture Hall", now_iso())
    c = cp.set_room(c, cp.rectangular_room(14, 10, 4.2))
    c = cp.add_device(c, cp.create_processor("P", "DSP"))
    c = cp.add_device(c, create_microphone_array("A1", "Stage Array", "automatic"))
    c = cp.add_device(c, create_microphone_array("A2", "Audience Array L", "automatic"))
    c = cp.add_device(c, create_microphone_array("A3", "Audience Array R", "automatic"))
    c = cp.add_device(c, cp.create_wireless_mic("PM", "Presenter Mic", "dante"))
    c = cp.add_device(c, cp.create_loudspeaker("L1", "Line Array L", "analog"))
    c = cp.add_device(c, cp.create_loudspeaker("L2", "Line Array R", "analog"))
    c = cp.add_device(c, cp.create_codec("C", "Stream Codec", "dante"))
    c = _place(c, "P", 13, 9)
    c = _place(c, "A1", 7, 2)
    c = _place(c, "A2", 4.5, 6.5)
    c = _place(c, "A3", 9.5, 6.5)
    c = _place(c, "PM", 7, 1)
    c = _place(c, "L1", 3, 0.6)
    c = _place(c, "L2", 11, 0.6)
    c = _place(c, "C", 13, 1)
    c = cp.add_coverage_zone(c, "A1", dynamic_zone("A1-z1", "Stage", RectShape(origin=Point2D(4, 0.8), width=6, height=2.2)))
    c = cp.add_coverage_zone(c, "A2", dynamic_zone("A2-z1", "Audience", RectShape(origin=Point2D(1.5, 4), width=11, height=5)))
    c = cp.add_talker(c, cp.create_talker("T1", "Presenter", Point2D(7, 1.6), 1.6))
    c = cp.add_talker(c, cp.create_talker("T2", "Q&A (left)", Point2D(4, 6)))
    c = cp.add_talker(c, cp.create_talker("T3", "Q&A (right)", Point2D(10, 6)))
    c = cp.add_talker(c, cp.create_talker("T4", "Audience", Point2D(7, 8)))
    return _wire(c, ["A1-out-mix", "A2-out-mix", "A3-out-mix", "PM-out-dante-1"], "C", ["L1", "L2"])


def u_shape_boardroom():
    """Boardroom with a U-shaped table modelled as a polygon pickup zone."""
    c = cp.create_config("U-Shape Boardroom", now_iso())
    c = cp.set_room(c, cp.rectangular_room(9, 8, 3))
    c = cp.add_device(c, cp.create_processor("P", "DSP"))
    c = cp.add_device(c, create_microphone_array("A1", "Ceiling Array 1", "automatic"))
    c = cp.add_device(c, create_microphone_array("A2", "Ceiling Array 2", "automatic"))
    c = cp.add_device(c, cp.create_loudspeaker("L1", "Speaker L", "analog"))
    c = cp.add_device(c, cp.create_loudspeaker("L2", "Speaker R", "analog"))
    c = cp.add_device(c, cp.create_codec("C", "Room Codec", "dante"))
    c = _place(c, "P", 8, 7)
    c = _place(c, "A1", 4.5, 3)
    c = _place(c, "A2", 4.5, 5.5)
    c = _place(c, "L1", 2, 7.4)
    c = _place(c, "L2", 7, 7.4)
    c = _place(c, "C", 8, 1)
    u_table = PolygonShape(points=[
        Point2D(2, 2), Point2D(7, 2), Point2D(7, 6), Point2D(5.5, 6),
        Point2D(5.5, 3.5), Point2D(3.5, 3.5), Point2D(3.5, 6), Point2D(2, 6),
    ])
    c = cp.add_coverage_zone(c, "A1", dynamic_zone("A1-z1", "U-Table", u_table))
    c = cp.add_talker(c, cp.create_talker("T1", "Chair", Point2D(4.5, 2.4), 1.4))
    c = cp.add_talker(c, cp.create_talker("T2", "Member A", Point2D(2.7, 3)))
    c = cp.add_talker(c, cp.create_talker("T3", "Member B", Point2D(2.7, 5)))
    c = cp.add_talker(c, cp.create_talker("T4", "Member C", Point2D(6.3, 3)))
    c = cp.add_talker(c, cp.create_talker("T5", "Member D", Point2D(6.3, 5)))
    return _wire(c, ["A1-out-mix", "A2-out-mix"], "C", ["L1", "L2"])


# Registry: (key, menu label, builder) — consumed by the GUI scenario picker.
SCENARIOS = [
    ("boardroom", "Boardroom (reference AEC)", boardroom),
    ("huddle", "Huddle (auto-configured)", huddle),
    ("meeting_room", "Meeting room", meeting_room),
    ("conference_room", "Conference room (3 arrays)", conference_room),
    ("training_room", "Training room / classroom", training_room),
    ("lecture_hall", "Lecture hall / auditorium", lecture_hall),
    ("u_shape_boardroom", "U-shape boardroom (polygon table)", u_shape_boardroom),
]
