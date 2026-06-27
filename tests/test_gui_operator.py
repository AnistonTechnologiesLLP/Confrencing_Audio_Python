"""Offscreen probe for the Phase-6 operator status panel.

Constructs ``OperatorStatusPanel`` directly (a single QWidget) — NOT MainWindow, which hangs headless on
Windows per CLAUDE.md; full GUI behaviour runs in CI. Skipped without PySide6.
"""
import os

import pytest

pytest.importorskip("PySide6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("numpy")

from conf_pipeline_control.operator import OperatorStatus  # noqa: E402
from conf_pipeline_control.placement import STATUS_BAD, PlacementResult  # noqa: E402
from conf_pipeline_control.polaris_beamformer import PolarisBeamformer  # noqa: E402
from conf_pipeline_control.pre_nr import build_pre_nr_bands  # noqa: E402
from conf_pipeline_gui.panels.operator import OperatorStatusPanel  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    yield app


def test_operator_panel_renders_status(qapp):
    eng = PolarisBeamformer(device=None, pre_nr=True, pre_nr_bands=build_pre_nr_bands(hpf_hz=120.0))
    panel = OperatorStatusPanel()
    panel.set_status(OperatorStatus.build(engine=eng).to_dict())
    assert panel.section("pipeline") is not None
    assert panel.section("calibration")["enabled"] is False
    summ = panel.summary()
    assert "Pipeline" in summ and "Calibration" in summ
    # the panel reflects pre-NR being on
    assert "HPF/notch" in summ


def test_operator_panel_empty_is_safe(qapp):
    panel = OperatorStatusPanel()
    assert panel.section("device") is None
    assert "No status" in panel.summary()


def test_operator_panel_surfaces_bad_placement_warning(qapp):
    eng = PolarisBeamformer(device=None)
    r = PlacementResult(status=STATUS_BAD, score=40, reasons=("loud HVAC near the array",))
    panel = OperatorStatusPanel()
    panel.set_status(OperatorStatus.build(engine=eng, placement=r).to_dict())
    assert any("BAD" in w for w in panel.warnings())
