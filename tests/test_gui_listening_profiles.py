"""Offscreen probe for the Phase-10 LIVE listening-mode flow summary.

Constructs `LivePanel(AppState())` directly (NOT MainWindow, which hangs headless per CLAUDE.md) and
checks that selecting a listening mode updates a read-only processing-flow summary. This is descriptive
only — it does not change the dropdown's existing Connect/apply behaviour. Skipped without PySide6.
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


def _panel():
    from conf_pipeline_gui.panels.live import LivePanel
    from conf_pipeline_gui.state import AppState
    return LivePanel(AppState())


def _select(p, key):
    i = p.live_listening_mode.findData(key)
    assert i >= 0, key
    p.live_listening_mode.setCurrentIndex(i)


def test_flow_summary_widget_exists_and_renders(qapp):
    p = _panel()
    assert hasattr(p, "live_flow_summary")
    txt = p.live_flow_summary.text()
    assert "Flow:" in txt and "→" in txt and "capture" in txt


def test_dropdown_still_has_all_six_modes(qapp):
    p = _panel()
    keys = [p.live_listening_mode.itemData(i) for i in range(p.live_listening_mode.count())]
    assert keys == ["follow", "seat", "table", "clean", "manual", "twokit"]


def test_clean_audio_summary_shows_omlsa(qapp):
    p = _panel()
    _select(p, "clean")
    txt = p.live_flow_summary.text()
    assert "Clean audio" in txt and "OM-LSA" in txt
    assert "dfn3" not in txt.lower()                      # does NOT silently turn on DFN3


def test_follow_summary_shows_auto_steer(qapp):
    p = _panel()
    _select(p, "follow")
    assert "auto-steer ON" in p.live_flow_summary.text()


def test_whole_table_summary_has_no_forced_cleanup(qapp):
    p = _panel()
    _select(p, "table")
    assert "denoise OFF" in p.live_flow_summary.text()    # whole table forces no denoiser


def test_manual_summary_renders(qapp):
    p = _panel()
    _select(p, "manual")
    assert "Manual" in p.live_flow_summary.text()


def test_selecting_every_mode_updates_summary_without_crash(qapp):
    p = _panel()
    last = None
    for key in ("follow", "seat", "table", "clean", "manual", "twokit"):
        _select(p, key)
        txt = p.live_flow_summary.text()
        assert isinstance(txt, str) and txt and txt != last
        last = txt


def test_recommended_cleanup_preticked_in_live(qapp):
    """A fresh LIVE session ships the recommended cleanup pre-ticked (GUI default only; engine/CLI defaults
    stay OFF). AGC was already on; dereverb + OM-LSA denoise + tap-suppression are now pre-ticked too, on
    every path that has them."""
    p = _panel()
    assert p.live_agc.isChecked()                              # AGC (already recommended-on)
    assert not p.live_dereverb.isChecked()                     # dereverb is per-profile (Follow/Clean), NOT global
    assert p.live_beameng_postnr.isChecked()                   # denoise on the A/B-engine path
    assert p.live_beameng_nr_engine.currentData() == "omlsa"
    assert p.live_beameng_transient.isChecked()
    assert p.live_autosteer_clean.currentData() == "omlsa"     # denoise on the auto-steer path
    assert p.live_autosteer_transient.isChecked()
    assert p.live_twokit_clean.currentData() == "omlsa"        # per-kit denoise
    assert p.live_twokit_agc.isChecked()                       # one combined AGC


def test_dereverb_is_not_globally_preticked(qapp):
    """The global 'Reduce room echo (dereverb)' checkbox is OFF on a fresh panel — dereverb is NOT a
    global default (it can colour a dry room). It is recommended only on the Follow / Clean paths."""
    p = _panel()
    assert not p.live_dereverb.isChecked()
    assert not p.live_beameng_dereverb.isChecked()             # the A/B-engine (Lock-to-seat) path stays off
    assert not p.live_autosteer_dereverb.isChecked()           # untouched until a steering mode is picked


def test_follow_and_clean_enable_dereverb_on_autosteer_path(qapp):
    """Picking Follow or Clean enables dereverb ON THE AUTO-STEER PATH ONLY (its own checkbox) — never the
    global switch, so other modes are unaffected."""
    for mode in ("follow", "clean"):
        p = _panel()
        _select(p, mode)
        assert p.live_autosteer_dereverb.isChecked(), mode     # recommended ON for this profile
        assert not p.live_dereverb.isChecked(), mode           # but NOT the global default


def test_table_and_manual_do_not_force_dereverb(qapp):
    """Whole table and Manual never force dereverb: the global switch stays off and the auto-steer dereverb
    is left untouched (in Manual the user's own toggle is the source of truth)."""
    for mode in ("table", "manual"):
        p = _panel()
        _select(p, mode)
        assert not p.live_dereverb.isChecked(), mode
        assert not p.live_autosteer_dereverb.isChecked(), mode


def test_opt_in_only_stages_stay_off(qapp):
    """Recommended-on is not everything-on: AEC (needs a far-end reference) and the voice gate (can clip
    soft speech) stay OFF by default on every path."""
    p = _panel()
    for name in ("live_beameng_aec", "live_beameng_voicegate", "live_autosteer_aec",
                 "live_autosteer_voicegate"):
        assert not getattr(p, name).isChecked(), name


def test_clean_summary_reflects_recommended_on(qapp):
    p = _panel()
    _select(p, "clean")
    txt = p.live_flow_summary.text()
    assert "denoise OM-LSA" in txt and "AGC ON" in txt and "dereverb ON" in txt
