"""Dev helper: render the main window offscreen and save a PNG screenshot."""
import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from conf_pipeline_gui.app import MainWindow, build_qss
from conf_pipeline_gui.scenarios import SCENARIOS

app = QApplication.instance() or QApplication([])
app.setStyleSheet(build_qss(sys.argv[2] if len(sys.argv) > 2 else "dark"))
win = MainWindow()
win.resize(1320, 840)
# load the first sample so the canvas has content
_key, _label, builder = SCENARIOS[0]
win.state.set_config(builder())
mode = sys.argv[3] if len(sys.argv) > 3 else "design"
win.state.set_mode(mode)
win.inspector.refresh() if hasattr(win, "inspector") else None
win.show()
app.processEvents()
out = sys.argv[1] if len(sys.argv) > 1 else "shot.png"
win.grab().save(out)
print("saved", out)
