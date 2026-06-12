import os
os.environ["QT_QPA_PLATFORM"] = "offscreen"
from PySide6.QtWidgets import QApplication, QFrame
from conf_pipeline_gui.theme import DARK_QSS, LIGHT_QSS
from conf_pipeline_gui.viewbar import ViewBar

app = QApplication([])
for name, qss in (("dark", DARK_QSS), ("light", LIGHT_QSS)):
    app.setStyleSheet(qss)
    vb = ViewBar()
    vb.show()
    app.processEvents()
    vb.adjustSize()
    app.processEvents()
    seps = [c for c in vb.children() if isinstance(c, QFrame)]
    for s in seps:
        if s.frameShape() == QFrame.VLine:
            g = s.geometry()
            print(f"{name}: viewbar size={vb.width()}x{vb.height()}  sep geom={g.width()}x{g.height()} at ({g.x()},{g.y()})  maxH={s.maximumHeight()}")
    vb.deleteLater()
