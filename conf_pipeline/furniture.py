"""Furniture catalog: default geometry / acoustics per room-object kind (v4).

Like :data:`conf_pipeline.profiles.DEVICE_PROFILES`, this is a static, **non-
serialized** factory of defaults. A :class:`conf_pipeline.model.RoomObject` stores
only a ``kind`` plus optional per-instance overrides; the resolvers here combine
those overrides with the catalog so the coverage simulator, the validator, and the
GUI all agree on a piece of furniture's footprint, height, occlusion behaviour, and
acoustic absorption.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from .model import Point2D, RoomObject, obb_corners


@dataclass(frozen=True)
class FurnitureType:
    kind: str
    label: str
    width: float            # m, along local +X (before rotation)
    depth: float            # m, along local +Y
    height: float           # m
    absorption: float       # 0..1 Sabine coefficient
    blocks_camera: bool     # opaque to a camera's line of sight
    blocks_audio: bool      # tall/solid enough to shadow sound (coarse)
    seat_capacity: int = 0


# Sensible defaults; all dimensions in metres. blocks_* reflect whether a typical
# item is tall/solid enough to occlude a seated person's face (camera) or shadow
# sound (audio). A table/desk does NOT block a standing/seated face; a screen,
# cabinet, or sofa-back can.
FURNITURE_CATALOG: dict[str, FurnitureType] = {
    "table":   FurnitureType("table", "Table", 2.4, 1.0, 0.74, 0.10, blocks_camera=False, blocks_audio=False),
    "desk":    FurnitureType("desk", "Desk", 1.4, 0.7, 0.74, 0.10, blocks_camera=False, blocks_audio=False),
    "chair":   FurnitureType("chair", "Chair", 0.55, 0.55, 0.90, 0.30, blocks_camera=False, blocks_audio=False, seat_capacity=1),
    "seat":    FurnitureType("seat", "Seat", 0.55, 0.55, 0.50, 0.35, blocks_camera=False, blocks_audio=False, seat_capacity=1),
    "sofa":    FurnitureType("sofa", "Sofa", 2.0, 0.9, 0.85, 0.45, blocks_camera=True, blocks_audio=False, seat_capacity=3),
    "cabinet": FurnitureType("cabinet", "Cabinet", 1.2, 0.5, 1.60, 0.15, blocks_camera=True, blocks_audio=True),
    "screen":  FurnitureType("screen", "Display / screen", 1.8, 0.10, 1.40, 0.12, blocks_camera=True, blocks_audio=True),
    "door":    FurnitureType("door", "Door", 0.9, 0.10, 2.10, 0.06, blocks_camera=False, blocks_audio=False),
    "window":  FurnitureType("window", "Window", 1.4, 0.10, 1.40, 0.05, blocks_camera=False, blocks_audio=False),
    "plant":   FurnitureType("plant", "Plant", 0.6, 0.60, 1.50, 0.20, blocks_camera=False, blocks_audio=False),
}

DEFAULT_FURNITURE_KIND = "table"
FURNITURE_KINDS: tuple[str, ...] = tuple(FURNITURE_CATALOG.keys())


def furniture_type(kind: str) -> Optional[FurnitureType]:
    return FURNITURE_CATALOG.get(kind)


# ---- resolvers: per-instance override → catalog default → hard fallback ---- #
def resolved_dimensions(obj: RoomObject) -> tuple[float, float, float]:
    """(width, depth, height) in metres for a room object, honouring overrides."""
    ft = FURNITURE_CATALOG.get(obj.kind)
    w = obj.width if obj.width is not None else (ft.width if ft else 0.8)
    d = obj.depth if obj.depth is not None else (ft.depth if ft else 0.8)
    h = obj.height if obj.height is not None else (ft.height if ft else 0.8)
    return w, d, h


def resolved_absorption(obj: RoomObject) -> float:
    ft = FURNITURE_CATALOG.get(obj.kind)
    return obj.absorption if obj.absorption is not None else (ft.absorption if ft else 0.10)


def blocks_camera(obj: RoomObject) -> bool:
    if obj.blocks_camera is not None:
        return obj.blocks_camera
    ft = FURNITURE_CATALOG.get(obj.kind)
    return ft.blocks_camera if ft else True


def blocks_audio(obj: RoomObject) -> bool:
    if obj.blocks_audio is not None:
        return obj.blocks_audio
    ft = FURNITURE_CATALOG.get(obj.kind)
    return ft.blocks_audio if ft else False


def furniture_corners(obj: RoomObject) -> list[Point2D]:
    """Oriented footprint (4 corners) of a room object on the floor plane."""
    w, d, _h = resolved_dimensions(obj)
    return obb_corners(obj.position, w, d, obj.rotation_deg or 0.0)
