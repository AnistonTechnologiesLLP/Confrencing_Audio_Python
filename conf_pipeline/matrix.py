"""Pure crosspoint-matrix engine. Every operation returns a NEW MatrixMixer."""
from __future__ import annotations

import copy

from .model import Bus, Crosspoint, MatrixMixer, Port

DEFAULT_CROSSPOINT = Crosspoint(enabled=True, gain_db=0.0)


def create_matrix(processor_id: str, input_ports: list[Port], output_ports: list[Port]) -> MatrixMixer:
    input_buses = [Bus(id=p.id, processor_id=processor_id, kind="input", port_id=p.id, label=p.label) for p in input_ports]
    output_buses = [Bus(id=p.id, processor_id=processor_id, kind="output", port_id=p.id, label=p.label) for p in output_ports]
    return MatrixMixer(processor_id=processor_id, input_buses=input_buses, output_buses=output_buses, cells={})


def has_input_bus(matrix: MatrixMixer, input_bus_id: str) -> bool:
    return any(b.id == input_bus_id for b in matrix.input_buses)


def has_output_bus(matrix: MatrixMixer, output_bus_id: str) -> bool:
    return any(b.id == output_bus_id for b in matrix.output_buses)


def _assert_buses(matrix: MatrixMixer, input_bus_id: str, output_bus_id: str) -> None:
    if not has_input_bus(matrix, input_bus_id):
        raise ValueError(f"Unknown matrix input bus: {input_bus_id}")
    if not has_output_bus(matrix, output_bus_id):
        raise ValueError(f"Unknown matrix output bus: {output_bus_id}")


def _clone_cells(cells: dict[str, dict[str, Crosspoint]]) -> dict[str, dict[str, Crosspoint]]:
    return {row: dict(cols) for row, cols in cells.items()}


def set_crosspoint(matrix: MatrixMixer, input_bus_id: str, output_bus_id: str, crosspoint: Crosspoint) -> MatrixMixer:
    _assert_buses(matrix, input_bus_id, output_bus_id)
    cells = _clone_cells(matrix.cells)
    row = cells.setdefault(input_bus_id, {})
    row[output_bus_id] = copy.copy(crosspoint)
    return MatrixMixer(matrix.processor_id, list(matrix.input_buses), list(matrix.output_buses), cells)


def route(matrix: MatrixMixer, input_bus_id: str, output_bus_id: str, gain_db: float = 0.0) -> MatrixMixer:
    return set_crosspoint(matrix, input_bus_id, output_bus_id, Crosspoint(enabled=True, gain_db=gain_db))


def clear(matrix: MatrixMixer, input_bus_id: str, output_bus_id: str) -> MatrixMixer:
    _assert_buses(matrix, input_bus_id, output_bus_id)
    if matrix.cells.get(input_bus_id, {}).get(output_bus_id) is None:
        return matrix
    cells = _clone_cells(matrix.cells)
    row = cells.get(input_bus_id)
    if row:
        row.pop(output_bus_id, None)
        if not row:
            cells.pop(input_bus_id, None)
    return MatrixMixer(matrix.processor_id, list(matrix.input_buses), list(matrix.output_buses), cells)


def get(matrix: MatrixMixer, input_bus_id: str, output_bus_id: str) -> Crosspoint | None:
    return matrix.cells.get(input_bus_id, {}).get(output_bus_id)


def is_active(matrix: MatrixMixer, input_bus_id: str, output_bus_id: str) -> bool:
    cp = get(matrix, input_bus_id, output_bus_id)
    return cp is not None and cp.enabled


def inputs_for_output(matrix: MatrixMixer, output_bus_id: str) -> list[str]:
    result = []
    for in_id, cols in matrix.cells.items():
        cp = cols.get(output_bus_id)
        if cp is not None and cp.enabled:
            result.append(in_id)
    return result


def outputs_for_input(matrix: MatrixMixer, input_bus_id: str) -> list[str]:
    cols = matrix.cells.get(input_bus_id)
    if not cols:
        return []
    return [out_id for out_id, cp in cols.items() if cp.enabled]


def active_crosspoints(matrix: MatrixMixer) -> list[tuple[str, str, Crosspoint]]:
    out = []
    for in_id, cols in matrix.cells.items():
        for out_id, cp in cols.items():
            if cp.enabled:
                out.append((in_id, out_id, cp))
    return out
