"""Steering-angle geometry for coverage planning (pure trigonometry).

Points are 3D ``(x, y, z)`` where ``(x, y)`` is the floor plane and ``z`` is
elevation above the floor, all in metres. No acoustic modelling.
"""
from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass
class Point3D:
    x: float
    y: float
    z: float


@dataclass
class SteeringAngles:
    distance: float
    horizontal_distance: float
    azimuth_deg: float
    downtilt_deg: float
    off_nadir_deg: float


def steering_angles(source: Point3D, target: Point3D) -> SteeringAngles:
    """Angles/distances from ``source`` (e.g. a ceiling array) to ``target``.

    azimuth: compass-style bearing on the floor, ``[0, 360)``, clockwise from +Y
    (so +Y = 0 deg, +X = 90 deg). down-tilt: angle below horizontal, positive when
    the target is below the source (90 deg = straight down). off-nadir: angle from
    straight-down to the ray (0 deg = directly beneath); equals ``90 - downtilt``.
    """
    dx = target.x - source.x
    dy = target.y - source.y
    dz = target.z - source.z

    horizontal = math.hypot(dx, dy)
    distance = math.sqrt(dx * dx + dy * dy + dz * dz)

    azimuth = math.degrees(math.atan2(dx, dy))
    if azimuth < 0:
        azimuth += 360.0

    downtilt = math.degrees(math.atan2(source.z - target.z, horizontal))
    off_nadir = 90.0 - downtilt

    return SteeringAngles(
        distance=distance,
        horizontal_distance=horizontal,
        azimuth_deg=azimuth,
        downtilt_deg=downtilt,
        off_nadir_deg=off_nadir,
    )
