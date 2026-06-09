"""Placement simulation & recommendation.

Given a room, a microphone array, and talkers, recommend the best array pose
(position + steer) and the best seat for a talker, by sweeping a fast geometric
scoring model (``scoring``) with a coarse-to-fine joint search (``search``). An
optional, pluggable physics backend (``validate``) checks the single top pick.

The heuristic engine is pure stdlib (no numpy); only the optional validator
touches numerical/acoustics libraries, behind availability gates.
"""
from __future__ import annotations

from .scoring import estimated_rt60, score_placement  # noqa: F401
from .search import recommend_placement, score_heatmap  # noqa: F401
from .types import (  # noqa: F401
    Candidate,
    Heatmap,
    PlacementScore,
    Recommendation,
    SimParams,
    ValidationResult,
)
from .validate import (  # noqa: F401
    available_backends,
    numpy_available,
    validate_recommendation,
)

__all__ = [
    "Candidate",
    "Heatmap",
    "PlacementScore",
    "Recommendation",
    "SimParams",
    "ValidationResult",
    "available_backends",
    "estimated_rt60",
    "numpy_available",
    "recommend_placement",
    "score_heatmap",
    "score_placement",
    "validate_recommendation",
]
