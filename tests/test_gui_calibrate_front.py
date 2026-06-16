"""Regression: 'Calibrate front' must apply the measured bearing to the Front-offset
spin box, wrapping the 0..360° DOA into the box's −180..180° range instead of clamping.

The bug: a front talker the array measured above 180° (common on this front/back-
ambiguous ring) was clamped to 180 by Qt's setValue, so the value never matched the
real bearing and the pickup sector never centred on the talker. Skipped without PySide6.
"""
import os

import pytest

pytest.importorskip("PySide6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@pytest.fixture(scope="module")
def qapp():
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture
def live(qapp):
    from conf_pipeline_gui.app import MainWindow, build_qss
    qapp.setStyleSheet(build_qss("dark"))
    w = MainWindow()
    w.show()
    panel = w.panels["live"]
    yield panel
    w.close()


@pytest.mark.parametrize("az, expected", [
    (0.0, 0),       # front (array 0°) → no offset
    (90.0, 90),     # in-range, unchanged
    (180.0, -180),  # boundary (directly behind)
    (181.0, -179),  # just past 180 → wraps to the negative side, NOT clamped to 180
    (270.0, -90),   # left/rear → −90 (the bug clamped this to 180)
    (350.0, -10),   # near-front from the other side
])
def test_calibrate_front_wraps_doa_into_spinbox_range(live, az, expected):
    live._on_calib_done((az, 12.0))
    assert live.live_front_offset.value() == expected
    # always within the spin box's declared range (never silently clamped)
    assert -180.0 <= live.live_front_offset.value() <= 180.0


def test_calibrate_front_rear_talker_is_not_clamped_to_180(live):
    # the exact failure mode reported: a rear/left talker must not stick at 180
    live.live_front_offset.setValue(0.0)
    live._on_calib_done((300.0, 9.0))
    assert live.live_front_offset.value() == -60      # 300° == −60°, applied (not clamped to 180)
    assert live.live_front_offset.value() != 180.0


def test_calibrate_front_no_talker_leaves_value_untouched(live):
    live.live_front_offset.setValue(42.0)
    live._on_calib_done((None, 0.0))
    assert live.live_front_offset.value() == 42.0
    assert "no clear talker" in live.live_status.text().lower()
