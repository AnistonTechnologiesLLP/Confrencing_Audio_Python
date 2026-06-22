"""Offscreen GUI: the per-stage activity strip (honest data path) + the live-panel wiring
(stage-meter tick + RAW-bypass button). Painting is pragma-no-cover; here we assert the data
mapping (idle != off, bipolar AGC) and the button forwarding. Skipped without PySide6.

Constructs StageStrip / LivePanel directly — NOT MainWindow (which hangs headless on Windows per
CLAUDE.md); full MainWindow GUI behaviour runs in CI.
"""
import os

import pytest

pytest.importorskip("PySide6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from conf_pipeline_control._stage_metrics import StageActivity, ZERO_ACTIVITY  # noqa: E402
from conf_pipeline_gui.panels.common import StageStrip  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    yield app


def _act(**kw):
    base = dict(aec_erle_db=0.0, aec_on=False, aec_farend_active=False, dereverb_db=0.0,
                dereverb_on=False, denoise_db=0.0, denoise_on=False, agc_gain_db=0.0, agc_on=False)
    base.update(kw)
    return StageActivity(**base)


# --------------------------------------------------------------------------- #
# StageStrip — the honest data mapping
# --------------------------------------------------------------------------- #
def test_stage_strip_reflects_snapshot(qapp):
    s = StageStrip()
    s.set_activity(_act(aec_on=True, aec_farend_active=True, aec_erle_db=12.0,
                        denoise_on=True, denoise_db=8.0, agc_on=True, agc_gain_db=-6.0))
    assert s.cell("aec")["on"] and not s.cell("aec")["idle"] and s.cell("aec")["db"] == 12.0
    assert s.cell("denoise")["on"] and s.cell("denoise")["db"] == 8.0
    assert s.cell("agc")["on"] and s.cell("agc")["db"] == -6.0 and s.cell("agc")["bipolar"] is True
    assert s.cell("dereverb")["on"] is False        # off → greyed


def test_stage_strip_aec_idle_distinct_from_off(qapp):
    s = StageStrip()
    s.set_activity(_act(aec_on=True, aec_farend_active=False, aec_erle_db=0.0))
    assert s.cell("aec")["on"] is True and s.cell("aec")["idle"] is True   # ON but idle — NOT greyed
    s.set_activity(_act(aec_on=False))
    assert s.cell("aec")["on"] is False and s.cell("aec")["idle"] is False  # off


def test_stage_strip_zero_activity_greys_all(qapp):
    s = StageStrip()
    s.set_activity(_act(denoise_on=True, denoise_db=5.0))
    s.set_activity(ZERO_ACTIVITY)
    assert all(s.cell(k)["on"] is False for k in ("aec", "dereverb", "denoise", "agc"))


def test_stage_strip_paints_without_error(qapp):
    s = StageStrip()
    s.resize(240, 36)
    s.set_activity(_act(aec_on=True, aec_farend_active=True, aec_erle_db=10.0,
                        agc_on=True, agc_gain_db=4.0, denoise_on=True, denoise_db=20.0))
    s.grab()                                          # forces paintEvent (bipolar + unipolar branches)


# --------------------------------------------------------------------------- #
# LivePanel wiring — stage-meter tick + RAW-bypass forwarding
# --------------------------------------------------------------------------- #
@pytest.fixture
def panel(qapp):
    from conf_pipeline_gui.panels.live import LivePanel
    from conf_pipeline_gui.state import AppState
    p = LivePanel(AppState())
    yield p
    p.deleteLater()


class _FakeEngine:
    """Stands in for a live session exposing the transparency surface."""
    def __init__(self):
        self.stage_activity = _act(denoise_on=True, denoise_db=9.0, agc_on=True, agc_gain_db=3.0)
        self.bypassed = None
    def set_bypass(self, on):
        self.bypassed = bool(on)


def test_live_panel_tick_updates_strip_and_enables_bypass(panel):
    panel._beam_engine = _FakeEngine()
    panel._tick_stage_meters()
    assert panel.live_stage_strip.cell("denoise")["db"] == 9.0
    assert panel.live_stage_strip.cell("agc")["db"] == 3.0
    assert panel.live_bypass_btn.isEnabled()


def test_live_panel_bypass_button_forwards(panel):
    fake = _FakeEngine()
    panel._beam_engine = fake
    panel._tick_stage_meters()                        # gate the button on
    panel._live_toggle_bypass(True)
    assert fake.bypassed is True
    panel._live_toggle_bypass(False)
    assert fake.bypassed is False


def test_live_panel_bypass_disabled_and_unchecked_with_no_session(panel):
    fake = _FakeEngine()
    panel._beam_engine = fake
    panel._tick_stage_meters()
    panel.live_bypass_btn.setChecked(True)
    panel._beam_engine = None                         # session ended
    panel._tick_stage_meters()
    assert panel.live_bypass_btn.isEnabled() is False
    assert panel.live_bypass_btn.isChecked() is False   # stale RAW state dropped
    assert panel.live_stage_strip.cell("denoise")["on"] is False   # strip greyed
