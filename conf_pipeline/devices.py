"""Generic device factories (modeled on common conferencing behavior)."""
from __future__ import annotations

from typing import Optional

from . import matrix as mx
from .model import (
    AecConfig,
    Codec,
    Loudspeaker,
    Port,
    Processor,
    Transport,
    WiredMic,
    WirelessMic,
)
from .profiles import default_profile_id


def _make_port(device_id: str, kind: str, transport: Transport, index: int) -> Port:
    direction = "in" if kind == "input" else "out"
    pretty = "Dante" if transport == "dante" else "Analog"
    pdir = "In" if kind == "input" else "Out"
    return Port(id=f"{device_id}-{direction}-{transport}-{index}", device_id=device_id, kind=kind, transport=transport, label=f"{pretty} {pdir} {index}")


def _make_ports(device_id: str, kind: str, transport: Transport, count: int) -> list[Port]:
    return [_make_port(device_id, kind, transport, i) for i in range(1, count + 1)]


def create_processor(
    id: str,
    label: str,
    dante_inputs: int = 8,
    dante_outputs: int = 8,
    analog_inputs: int = 2,
    analog_outputs: int = 2,
) -> Processor:
    input_ports = _make_ports(id, "input", "dante", dante_inputs) + _make_ports(id, "input", "analog", analog_inputs)
    output_ports = _make_ports(id, "output", "dante", dante_outputs) + _make_ports(id, "output", "analog", analog_outputs)
    matrix = mx.create_matrix(id, input_ports, output_ports)
    return Processor(id=id, label=label, ports=[*input_ports, *output_ports], matrix=matrix, buses=[*matrix.input_buses, *matrix.output_buses], profile_id=default_profile_id("processor"))


def create_wireless_mic(id: str, label: str, transport: Transport = "dante") -> WirelessMic:
    return WirelessMic(id=id, label=label, ports=[_make_port(id, "output", transport, 1)], aec=AecConfig(enabled=False, reference_bus_id=None), profile_id=default_profile_id("wirelessMic"))


def create_wired_mic(id: str, label: str, transport: Transport = "analog") -> WiredMic:
    return WiredMic(id=id, label=label, ports=[_make_port(id, "output", transport, 1)], aec=AecConfig(enabled=False, reference_bus_id=None), profile_id=default_profile_id("wiredMic"))


def create_loudspeaker(id: str, label: str, transport: Transport = "analog") -> Loudspeaker:
    return Loudspeaker(id=id, label=label, ports=[_make_port(id, "input", transport, 1)], profile_id=default_profile_id("loudspeaker"))


def create_codec(id: str, label: str, transport: Transport = "dante") -> Codec:
    return Codec(id=id, label=label, ports=[_make_port(id, "output", transport, 1), _make_port(id, "input", transport, 1)], profile_id=default_profile_id("codec"))
