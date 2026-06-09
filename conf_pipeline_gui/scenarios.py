"""Sample configurations."""
from __future__ import annotations

import conf_pipeline as cp
from conf_pipeline.coverage import create_microphone_array, dynamic_zone, exclusion_zone
from conf_pipeline.model import AecConfig, Point2D, RectShape

from .state import now_iso


def _place(c, did, x, y):
    return cp.set_device_position(c, did, Point2D(x, y))


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
