"""Dev helper: render the main window offscreen and save a PNG screenshot."""
import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from conf_pipeline_gui.app import MainWindow, build_qss
from conf_pipeline_gui.scenarios import SCENARIOS

app = QApplication.instance() or QApplication([])
theme = sys.argv[2] if len(sys.argv) > 2 else "dark"
app.setStyleSheet(build_qss(theme))
win = MainWindow()
win.state.theme = theme
win.resize(1320, 840)
# load the first sample so the canvas has content
_key, _label, builder = SCENARIOS[0]
win.state.set_config(builder())
mode = sys.argv[3] if len(sys.argv) > 3 else "design"
win.state.set_mode(mode)
if mode == "live":
    win.panels["live"]._live_timer.stop()  # keep the injected overlay alive for the grab
    aid = next((d.id for d in win.state.config.devices if d.type == "microphoneArray" and d.position), None)
    win.state.set_live_overlay({
        "array_id": aid,
        "sector": (0.0, 60.0, 0.0),
        "detections": [(15.0, 14.0, True), (-40.0, 9.0, True), (160.0, 7.0, False)],
        "level": 0.65,
        "connected": True,
    })
win.show()
app.processEvents()
out = sys.argv[1] if len(sys.argv) > 1 else "shot.png"
win.grab().save(out)
print("saved", out)
