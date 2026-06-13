"""Shared panel infrastructure: base class, cards, spinboxes, off-thread workers.

Moved from the old monolithic inspector. The crash-class guards travel with the
code: spinboxes ignore the wheel (a mid-wheel valueChanged would rebuild the
selection card and destroy the widget inside its own event), and panel rebuilds
are coalesced onto the next event-loop tick.
"""
from __future__ import annotations

from PySide6.QtCore import QObject, QRunnable, Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QDoubleSpinBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

import conf_pipeline as cp

from .. import workflow

DEVICE_TYPES = [
    ("Processor (DSP)", "processor"),
    ("Microphone array", "microphoneArray"),
    ("Wireless mic", "wirelessMic"),
    ("Wired mic", "wiredMic"),
    ("Loudspeaker", "loudspeaker"),
    ("Camera", "camera"),
    ("Codec (far-end)", "codec"),
]

ISSUE_COLORS = {
    "dark": {"error": "#ff6b81", "warning": "#f7c948"},
    "light": {"error": "#e23b59", "warning": "#b8860b"},
}
BLOCK_LABELS = {"gain": "Gain", "mute": "Mute", "peq4": "PEQ (4-band)", "agc": "AGC",
                "compressor": "Compressor", "delay": "Delay", "noiseReduction": "Noise reduction", "deverb": "Dereverb"}
BLOCK_PARAM_SCHEMA = {
    "gain": [("gainDb", "Gain dB", -60, 12, 0.5)],
    "agc": [("targetDb", "Target dB", -40, 0, 1), ("maxGainDb", "Max gain", 0, 30, 1)],
    "compressor": [("thresholdDb", "Thresh", -60, 0, 1), ("ratio", "Ratio", 1, 20, 0.5),
                   ("attackMs", "Atk ms", 0, 200, 1), ("releaseMs", "Rel ms", 10, 2000, 10), ("makeupDb", "Makeup", 0, 24, 0.5)],
    "delay": [("delayMs", "Delay ms", 0, 500, 1)],
    "noiseReduction": [("amountDb", "Amount dB", 0, 30, 1)],
    "deverb": [("amount", "Amount", 0, 1, 0.05)],
}


class _ValidateSignals(QObject):
    done = Signal(object)
    failed = Signal(str)


class _ValidateWorker(QRunnable):
    """Runs the (potentially slow) physics validation off the GUI thread."""

    def __init__(self, config, rec, params, backend):
        super().__init__()
        self._args = (config, rec, params, backend)
        self.signals = _ValidateSignals()

    def run(self):  # noqa: D401 (Qt override)
        try:
            self.signals.done.emit(cp.validate_recommendation(*self._args))
        except Exception as exc:  # surface to the GUI thread
            self.signals.failed.emit(str(exc))


class _ProbeSignals(QObject):
    done = Signal(object)   # list[float]: per-channel RMS
    failed = Signal(str)


class _ProbeWorker(QRunnable):
    """Briefly captures the array off the GUI thread and reports per-capsule RMS,
    so the Live panel can auto-detect dead / silent capsules."""

    def __init__(self, device, samplerate, channels, dur=0.6):
        super().__init__()
        self._args = (device, samplerate, channels, dur)
        self.signals = _ProbeSignals()

    def run(self):  # noqa: D401 (Qt override)
        try:
            import numpy as np  # noqa: F401 (validates the extra is present)
            import sounddevice as sd

            device, sr, ch, dur = self._args
            rec = sd.rec(int(dur * sr), samplerate=sr, channels=ch, device=device, dtype="float32")
            sd.wait()
            rms = [float((rec[:, i] ** 2).mean() ** 0.5) for i in range(ch)]
            self.signals.done.emit(rms)
        except Exception as exc:
            self.signals.failed.emit(str(exc))


class _ABWorker(QRunnable):
    """Record a clip and run the A/B beamformer comparison off the GUI thread."""

    def __init__(self, config, array_id, geom, device, sr, seconds, out_dir, freq):
        super().__init__()
        self._args = (config, array_id, geom, device, sr, seconds, out_dir, freq)
        self.signals = _ProbeSignals()  # done(object) / failed(str)

    def run(self):  # noqa: D401 (Qt override)
        try:
            import conf_pipeline_control as cc

            config, array_id, geom, device, sr, seconds, out_dir, freq = self._args
            y8 = cc.record_clip(device, sr, seconds, channels=geom.n_channels)
            report = cc.ab_compare(config, array_id, geom, y8, sr, freq_hz=freq)
            paths = cc.save_ab_report(report, out_dir)
            self.signals.done.emit((report.summary, out_dir, len(paths)))
        except Exception as exc:
            self.signals.failed.emit(str(exc))


class _CalibWorker(QRunnable):
    """Record a few seconds and report the dominant talker bearing, off the GUI
    thread — used to set the auto-steer Front offset from a known 'front' talker."""

    def __init__(self, geom, device, sr, off_nadir, seconds=4.0):
        super().__init__()
        self._args = (geom, device, sr, off_nadir, seconds)
        self.signals = _ProbeSignals()  # done((az|None, salience_db)) / failed(str)

    def run(self):  # noqa: D401 (Qt override)
        try:
            import conf_pipeline_control as cc

            geom, device, sr, off_nadir, seconds = self._args
            y8 = cc.record_clip(device, sr, seconds, channels=geom.n_channels)
            res = cc.detect_offline(y8, sr, geom, off_nadir_deg=off_nadir, max_talkers=1)
            if res.detections:
                d = res.detections[0]
                self.signals.done.emit((d.azimuth_deg, d.salience_db))
            else:
                self.signals.done.emit((None, 0.0))
        except Exception as exc:
            self.signals.failed.emit(str(exc))


class NoWheelDoubleSpinBox(QDoubleSpinBox):
    """A spin box that ignores mouse-wheel scrolling.

    Scrolling the panel then scrolls the panel instead of changing the value
    — and, crucially, never fires valueChanged mid-wheel, which would rebuild
    the selection card and destroy this very widget inside its own event."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.setFocusPolicy(Qt.StrongFocus)

    def wheelEvent(self, event):  # noqa: N802 (Qt override)
        event.ignore()


class NoWheelSpinBox(QSpinBox):
    """Integer spin box that ignores the mouse wheel (see NoWheelDoubleSpinBox)."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.setFocusPolicy(Qt.StrongFocus)

    def wheelEvent(self, event):  # noqa: N802 (Qt override)
        event.ignore()


def clear_layout(lay):
    while lay.count():
        item = lay.takeAt(0)
        wdg = item.widget()
        if wdg is not None:
            wdg.setParent(None)
            wdg.deleteLater()
        else:
            child = item.layout()
            if child is not None:
                clear_layout(child)  # recurse into nested form/row layouts


class Card(QFrame):
    """A collapsible section card — folds the Live panel's wall of controls."""

    def __init__(self, title: str, collapsed: bool = False):
        super().__init__()
        self.setProperty("card", "true")
        self._title = title
        outer = QVBoxLayout(self)
        outer.setContentsMargins(8, 4, 8, 8)
        outer.setSpacing(2)
        self.header = QPushButton()
        self.header.setProperty("cardHeader", "true")
        self.header.setCursor(Qt.PointingHandCursor)
        self.header.clicked.connect(self.toggle)
        outer.addWidget(self.header)
        self.body = QWidget()
        self.body_lay = QVBoxLayout(self.body)
        self.body_lay.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(self.body)
        self.body.setVisible(not collapsed)
        self._render_header()

    def _render_header(self):
        self.header.setText(("▾  " if self.body.isVisible() else "▸  ") + self._title)

    def toggle(self):
        self.body.setVisible(not self.body.isVisible())
        self._render_header()

    def set_open(self, on: bool):
        self.body.setVisible(on)
        self._render_header()


class PanelBase(QWidget):
    """A mode panel: title + next-step hint chip, coalesced visible-only refresh.

    Rebuilds are coalesced onto the next event-loop tick so the panel is never
    rebuilt synchronously inside a child widget's input event; hidden panels
    skip the work and catch up on ``showEvent``.
    """

    MODE = ""     # workflow mode key, for the hint chip
    TITLE = ""

    def __init__(self, state):
        super().__init__()
        self.state = state
        self._refreshing = False
        self._refresh_pending = False
        self._stale = True
        state.changed.connect(self._schedule_refresh)

    # ---- refresh plumbing ----
    def _schedule_refresh(self):
        if self._refresh_pending:
            return
        self._refresh_pending = True
        QTimer.singleShot(0, self._do_refresh)

    def _do_refresh(self):
        self._refresh_pending = False
        if self.isVisible():
            self.refresh()
        else:
            self._stale = True

    def showEvent(self, event):  # noqa: N802 (Qt override)
        super().showEvent(event)
        if self._stale:
            # coalesce with the mode-switch's own changed-signal refresh so a
            # mode entry costs one rebuild, not two
            self._schedule_refresh()

    def refresh(self):
        """Override; first call ``super().refresh()`` to clear staleness and
        update the hint chip, then rebuild with ``self._refreshing`` guards."""
        self._stale = False
        if hasattr(self, "hint_chip"):
            hint = workflow.next_hint(self.state, self.MODE)
            self.hint_chip.setText(hint)
            self.hint_chip.setVisible(bool(hint))

    # ---- shared UI helpers ----
    def _header(self) -> QWidget:
        box = QWidget()
        lay = QVBoxLayout(box)
        lay.setContentsMargins(0, 0, 0, 4)
        lay.setSpacing(5)
        title = QLabel(self.TITLE)
        title.setProperty("panelTitle", "true")
        lay.addWidget(title)
        self.hint_chip = QLabel("")
        self.hint_chip.setProperty("hintChip", "true")
        self.hint_chip.setWordWrap(True)
        self.hint_chip.setVisible(False)
        lay.addWidget(self.hint_chip)
        return box

    def _scroll(self, inner: QWidget) -> QScrollArea:
        """Wrap panel content so stacked controls scroll instead of forcing a
        tall window minimum (keeps the app usable on small / high-DPI screens)."""
        sa = QScrollArea()
        sa.setWidgetResizable(True)
        sa.setFrameShape(QFrame.NoFrame)
        sa.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        sa.setWidget(inner)
        return sa

    def _row(self, *widgets) -> QHBoxLayout:
        row = QHBoxLayout()
        for w in widgets:
            row.addWidget(w)
        return row

    def _win(self, name: str, *args):
        """Invoke a MainWindow action by name (panels stay window-agnostic)."""
        w = self.window()
        fn = getattr(w, name, None)
        if callable(fn):
            return fn(*args)

    def _toast(self, msg):
        w = self.window()
        if hasattr(w, "toast"):
            w.toast(msg)
