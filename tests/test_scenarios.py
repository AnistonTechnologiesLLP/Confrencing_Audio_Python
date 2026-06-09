"""Sample-configuration tests: every built-in scenario must be valid, round-trip
losslessly, and be a usable input to the placement-simulation engine.

Scenarios live in the GUI package (sample *data*), so this needs PySide6 — it is
skipped cleanly when the GUI extra isn't installed.
"""
import pytest

pytest.importorskip("PySide6")

import conf_pipeline as cp  # noqa: E402
from conf_pipeline_gui.scenarios import SCENARIOS  # noqa: E402

_IDS = [s[0] for s in SCENARIOS]


def test_registry_is_unique_and_nonempty():
    keys = [s[0] for s in SCENARIOS]
    assert len(keys) == len(set(keys)) and len(keys) >= 5


@pytest.mark.parametrize("key,label,builder", SCENARIOS, ids=_IDS)
def test_scenario_valid_and_roundtrips(key, label, builder):
    c = builder()
    res = cp.validate(c)
    assert not res.errors, f"{key} has errors: {[e.code for e in res.errors]}"
    assert cp.serialize(cp.deserialize(cp.serialize(c))) == cp.serialize(c)


@pytest.mark.parametrize("key,label,builder", SCENARIOS, ids=_IDS)
def test_scenario_recommendation_runs(key, label, builder):
    c = builder()
    arrays = [d for d in c.devices if d.type == "microphoneArray"]
    if not arrays:
        pytest.skip("scenario has no microphone array")
    target = c.talkers[0].id if c.talkers else None
    rec = cp.recommend_placement(c, arrays[0].id, talker_id=target)
    assert rec.array_pos is not None
    # heatmap is well-formed for every sample
    hm = cp.score_heatmap(c, arrays[0].id)
    assert len(hm.values) == hm.nx * hm.ny
