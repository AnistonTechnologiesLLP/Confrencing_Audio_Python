# tests/test_aperture_scoring.py
from conf_pipeline.sim.scoring import effective_halfwidth_deg, coverage_score
from conf_pipeline.sim.types import SimParams
from conf_pipeline.model import MicrophoneArray


def _array(profile_id):
    return MicrophoneArray(id="a1", label="A", ports=[], coverage_mode="automatic",
                           zones=[], aec=None, profile_id=profile_id)


def test_polaris_halfwidth_is_wider_than_legacy_when_horizontal():
    p = SimParams()
    polaris = effective_halfwidth_deg(_array("polaris-8"), off_nadir_deg=90.0, params=p)
    legacy = effective_halfwidth_deg(_array("generic-ceiling-array"), off_nadir_deg=90.0, params=p)
    assert legacy == p.lobe_halfwidth_deg          # no aperture -> legacy 35
    assert polaris > legacy                         # 40 mm at table range is coarser


def test_coverage_score_drops_for_polaris_off_axis():
    p = SimParams()
    # a talker 25 deg off the look: a tight (legacy) beam still scores it; the coarse
    # POLARIS beam is so wide the off-axis penalty is smaller -> but a competing close
    # seat is no longer separable (covered in Task 5). Here: same off-axis angle, the
    # POLARIS wide beam yields a HIGHER lobe weight (wider main lobe), proving the
    # halfwidth feeds coverage_score.
    legacy = coverage_score(25.0, True, False, p, halfwidth_deg=35.0)
    coarse = coverage_score(25.0, True, False, p, halfwidth_deg=80.0)
    assert coarse > legacy
