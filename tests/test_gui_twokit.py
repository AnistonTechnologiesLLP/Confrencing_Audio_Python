"""Offscreen smoke for the "Two kits" LIVE listening mode — the GUI wiring around the
dual-POLARIS :class:`MultiKitController` (a fake stands in, so no sounddevice/hardware)."""
import os

import pytest

pytest.importorskip("PySide6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import conf_pipeline as cp  # noqa: E402
from conf_pipeline.model import Point2D  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture
def win(qapp):
    from conf_pipeline_gui.app import MainWindow, build_qss

    qapp.setStyleSheet(build_qss("dark"))
    w = MainWindow()
    w.show()
    yield w
    w.close()


def _two_array_config():
    c = cp.create_config("TK", "2026-06-11T00:00:00Z")
    c = cp.set_room(c, cp.rectangular_room(8, 6, 3))
    c = cp.add_device(c, cp.create_microphone_array("A1", "Array 1"))
    c = cp.set_device_position(c, "A1", Point2D(2, 3))
    c = cp.add_device(c, cp.create_microphone_array("A2", "Array 2"))
    c = cp.set_device_position(c, "A2", Point2D(6, 3))
    return c


def test_twokit_mode_connect_tick_disconnect(win, monkeypatch):
    import conf_pipeline_gui.panels.live as live_mod

    win.state.set_config(_two_array_config())
    win.state.set_mode("live")
    captured: dict = {}

    class _FakeMK:  # stand-in for cc.MultiKitController — no hardware
        def __init__(self, specs, **kw):
            captured["specs"], captured["kw"] = specs, kw
            self.error = None
        def start(self):
            captured["started"] = True
        def stop(self):
            captured["stopped"] = True
        def set_gain_db(self, g, **k):
            captured["gain"] = g
        def set_mute(self, m, **k):
            captured["mute"] = m
        def read_level(self):
            return 0.3
        @property
        def active_kit(self):
            return 1
        def status(self):
            return [live_mod.cc.KitStatus(0, False, 30.0, 0.2, 0.1, False, None),
                    live_mod.cc.KitStatus(1, True, 120.0, 0.5, 0.8, False, None)]

    monkeypatch.setattr(live_mod.cc, "controls_available", lambda: True)
    monkeypatch.setattr(live_mod.cc, "MultiKitController", _FakeMK)

    panel = win.panels["live"]
    panel.refresh()
    panel.live_listening_mode.setCurrentIndex(panel.live_listening_mode.findData("twokit"))
    # the high-level mode clears the other backends and the arrays populate from the config
    assert not (panel.live_beameng.isChecked() or panel.live_autosteer.isChecked()
                or panel.live_octovox.isChecked())
    assert panel.live_twokit_arr_a.findData("A1") >= 0 and panel.live_twokit_arr_b.findData("A2") >= 0
    panel.live_twokit_arr_a.setCurrentIndex(panel.live_twokit_arr_a.findData("A1"))
    panel.live_twokit_arr_b.setCurrentIndex(panel.live_twokit_arr_b.findData("A2"))
    # force two distinct device bindings (machine-independent)
    for combo, dev in ((panel.live_twokit_dev_a, 101), (panel.live_twokit_dev_b, 202)):
        combo.clear()
        combo.addItem(f"dev{dev}", dev)

    panel._live_toggle_connect()
    assert panel._twokit is not None and captured.get("started")
    assert panel._live_busy() and win.modebar._live_connected            # LIVE dot red
    assert [s.device for s in captured["specs"]] == [101, 202]           # bound to the two devices
    assert [s.array_id for s in captured["specs"]] == ["A1", "A2"]       # and the two room arrays

    panel._tick_live_meter()                                             # per-kit + combined meters, overlay; must not raise
    assert panel.live_meter.level() > 0.0
    win.canvas.repaint()

    panel._live_toggle_connect()                                        # disconnect
    assert panel._twokit is None and captured.get("stopped")
    assert not panel._live_busy() and not win.modebar._live_connected


def test_twokit_rejects_same_device_for_both_kits(win, monkeypatch):
    import conf_pipeline_gui.panels.live as live_mod

    win.state.set_config(_two_array_config())
    win.state.set_mode("live")
    monkeypatch.setattr(live_mod.cc, "controls_available", lambda: True)

    panel = win.panels["live"]
    panel.refresh()
    panel.live_listening_mode.setCurrentIndex(panel.live_listening_mode.findData("twokit"))
    for combo in (panel.live_twokit_dev_a, panel.live_twokit_dev_b):     # SAME device for both kits
        combo.clear()
        combo.addItem("dev7", 7)

    panel._live_toggle_connect()
    assert panel._twokit is None                                        # guard refused to connect
    assert "DISTINCT" in panel.live_twokit_status.text()
