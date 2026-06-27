"""Shared panel infrastructure: base class, cards, spinboxes, off-thread workers.

Moved from the old monolithic inspector. The crash-class guards travel with the
code: spinboxes ignore the wheel (a mid-wheel valueChanged would rebuild the
selection card and destroy the widget inside its own event), and panel rebuilds
are coalesced onto the next event-loop tick.
"""
from __future__ import annotations

from PySide6.QtCore import QObject, QPointF, QRectF, QRunnable, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPainterPath, QPen
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
from ..theme import PALETTES, SPACE, palette as _palette

DEVICE_TYPES = [
    ("Processor (DSP)", "processor"),
    ("Microphone array", "microphoneArray"),
    ("Wireless mic", "wirelessMic"),
    ("Wired mic", "wiredMic"),
    ("Loudspeaker", "loudspeaker"),
    ("Camera", "camera"),
    ("Codec (far-end)", "codec"),
]

# Sourced from the theme palette (err/warn) so issue colours never drift from the rest
# of the UI — identical values to the old literals, one source now.
ISSUE_COLORS = {t: {"error": PALETTES[t]["err"], "warning": PALETTES[t]["warn"]} for t in PALETTES}
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


def set_danger(widget, on: bool = True) -> None:
    """Mark/unmark a button as destructive (the QSS ``[danger="true"]`` variant) and re-polish so
    the restyle takes on an already-shown widget (e.g. the Connect/Disconnect toggle)."""
    widget.setProperty("danger", "true" if on else None)
    widget.style().unpolish(widget)
    widget.style().polish(widget)


class LevelMeter(QWidget):
    """A prominent horizontal output meter: green/amber/red zones, a falling peak-hold marker, and a
    latching clip/hot flag — the read-out an operator watches constantly. ``set_level(frac)`` takes the
    0..1 display fraction the live ticks already compute (dB-mapped). Click to clear a latched clip.
    Pure painting, no behaviour — it replaces a flat QProgressBar in the LIVE transport footer."""

    _PEAK_DECAY = 0.012     # the peak marker falls ~this much per update (~10-20 Hz tick) ≈ 2-4 s to floor
    _CLIP_FRAC = 0.985      # >= this display fraction (~ −0.9 dB) latches the clip/hot flag
    _AMBER = 0.80           # zone thresholds on the same dB-mapped fraction (~ −12 dB / −5 dB)
    _RED = 0.92

    def __init__(self, parent=None):
        super().__init__(parent)
        self._level = 0.0
        self._peak = 0.0
        self._clip = False
        self.setMinimumHeight(18)
        self.setMinimumWidth(120)
        self.setCursor(Qt.PointingHandCursor)
        self.setToolTip("Output level (dB: −60 → left, 0 dBFS → right). Peak-hold marker + clip flag; click to clear.")

    def set_level(self, frac: float, meter: bool = True) -> None:
        """Set the bar to ``frac`` (0..1). With ``meter`` (audio level) also advance the peak-hold and
        latch the clip flag; ``meter=False`` is a plain fill (e.g. the OCTOVOX buffer gauge)."""
        f = 0.0 if frac is None else max(0.0, min(1.0, float(frac)))
        self._level = f
        if meter:
            self._peak = f if f >= self._peak else max(f, self._peak - self._PEAK_DECAY)
            if f >= self._CLIP_FRAC:
                self._clip = True
        else:
            self._peak = 0.0
        self.update()

    def level(self) -> float:
        return self._level

    def reset(self) -> None:
        self._level = self._peak = 0.0
        self._clip = False
        self.update()

    def mousePressEvent(self, e):  # acknowledge / clear a latched clip
        self._clip = False
        self.update()

    def _zone(self, f: float, pal: dict) -> str:
        return pal["err"] if f >= self._RED else (pal["warn"] if f >= self._AMBER else pal["ok"])

    def paintEvent(self, e):  # pragma: no cover - pure painting
        st = getattr(self.window(), "state", None)        # adapt to the live theme (mirrors canvas.py)
        pal = _palette(getattr(st, "theme", "dark") if st is not None else "dark")
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        r = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(pal["surface"]))
        p.drawRoundedRect(r, 4, 4)
        if self._level > 0:                                  # filled bar, coloured by the level's zone
            fill = QRectF(r.x(), r.y(), r.width() * self._level, r.height())
            p.setBrush(QColor(self._zone(self._level, pal)))
            p.drawRoundedRect(fill, 4, 4)
        p.setPen(QPen(QColor(pal["border_strong"]), 1))      # zone-boundary ticks
        for thr in (self._AMBER, self._RED):
            x = r.x() + r.width() * thr
            p.drawLine(QPointF(x, r.y() + 2), QPointF(x, r.bottom() - 2))
        if self._peak > 0:                                   # falling peak-hold marker
            px = r.x() + r.width() * self._peak
            p.setPen(QPen(QColor(self._zone(self._peak, pal)), 2))
            p.drawLine(QPointF(px, r.y() + 1), QPointF(px, r.bottom() - 1))
        if self._clip:                                       # latched clip / hot flag
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(pal["err"]))
            p.drawRoundedRect(QRectF(r.right() - 7, r.y(), 7, r.height()), 2, 2)
        p.end()


class LobePreview(QWidget):
    """A minimal, non-blocking **top-down PREVIEW** of the beamformer lobe: the array at centre, a main-lobe
    wedge toward the main angle (its spread hints the width preset), an optional dashed null line, and a seat
    dot. Schematic only — NOT to scale and NOT a measured beam pattern — it paints from cached state set by
    :meth:`set_lobe` and runs no DSP. Always labelled 'preview'. Azimuth 0° = up, clockwise.

    It is also a **drag-to-aim dial**: press/drag inside it and it emits :attr:`aimed` (the azimuth from the
    centre to the cursor) so the operator aims by dragging on screen — no sidebar dial / degree typing."""

    _HALF = {"wide": 60.0, "medium": 38.0, "narrow": 22.0}     # display half-angles (schematic, not measured)

    aimed = Signal(float)     # emitted on press/drag: the aimed azimuth (deg, -180..180; 0° = up, clockwise)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumHeight(120)
        self.setMinimumWidth(150)
        self._angle = 0.0
        self._width = "medium"
        self._null: "float | None" = None
        self._mode = "table"
        self._auto = False
        self.setToolTip("Lobe preview — DRAG to aim (0° = up, clockwise). Schematic pickup pattern, "
                        "not to scale and not a measured beam.")
        self.setCursor(Qt.CrossCursor)

    def set_lobe(self, *, angle_deg: float = 0.0, width: str = "medium", null_deg=None,
                 mode: str = "table", auto_steer: bool = False) -> None:
        self._angle = float(angle_deg)
        self._width = str(width)
        self._null = None if null_deg is None else float(null_deg)
        self._mode = str(mode)
        self._auto = bool(auto_steer)
        self.update()

    # --- drag-to-aim ---
    def _az_for_point(self, x: float, y: float) -> float:
        """Azimuth (deg, 0° = up, clockwise) from the widget centre to a point — the geometry the drag uses
        to aim. Returns 0° at the exact centre."""
        import math
        r = self.rect()
        dx = float(x) - r.center().x()
        dy = float(y) - r.center().y()
        if dx == 0.0 and dy == 0.0:
            return 0.0
        a = math.degrees(math.atan2(dx, -dy))
        if a > 180.0:
            a -= 360.0
        elif a <= -180.0:
            a += 360.0
        return a

    def _emit_aim(self, e) -> None:
        try:
            pos = e.position()
            self.aimed.emit(self._az_for_point(pos.x(), pos.y()))
        except Exception:
            pass

    def mousePressEvent(self, e):
        self._emit_aim(e)

    def mouseMoveEvent(self, e):
        if e.buttons():
            self._emit_aim(e)

    def paintEvent(self, _):  # pragma: no cover - pure painting
        import math
        st = getattr(self.window(), "state", None)
        pal = _palette(getattr(st, "theme", "dark") if st is not None else "dark")
        accent = QColor(pal.get("accent", pal["ok"]))
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        r = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(pal["surface"]))
        p.drawRoundedRect(r, 4, 4)
        cx, cy = r.center().x(), r.center().y()
        rad = min(r.width(), r.height()) * 0.42

        def pt(deg: float, rr: float) -> QPointF:
            a = math.radians(deg)
            return QPointF(cx + rr * math.sin(a), cy - rr * math.cos(a))

        if self._mode == "follow" or self._auto:               # auto: the look moves ⇒ broad/ambiguous
            half, center = 72.0, self._angle
        elif self._mode == "table":                            # whole table ⇒ broad
            half, center = 78.0, 0.0
        else:                                                  # fixed / seat ⇒ width preset
            half, center = self._HALF.get(self._width, 38.0), self._angle
        path = QPainterPath(QPointF(cx, cy))
        steps = 24
        for i in range(steps + 1):
            path.lineTo(pt(center - half + (2 * half) * i / steps, rad))
        path.lineTo(QPointF(cx, cy))
        fill = QColor(accent)
        fill.setAlpha(70)
        p.setBrush(fill)
        p.setPen(QPen(accent, 1))
        p.drawPath(path)
        p.setPen(QPen(accent, 2))                              # main direction line
        p.drawLine(QPointF(cx, cy), pt(center, rad))
        if self._mode == "seat":                               # seat target dot
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(pal.get("text", "#ffffff")))
            p.drawEllipse(pt(center, rad), 4, 4)
        if self._null is not None:                             # null line (dashed)
            pen = QPen(QColor(pal["warn"]), 2)
            pen.setStyle(Qt.DashLine)
            p.setPen(pen)
            p.drawLine(QPointF(cx, cy), pt(self._null, rad))
        p.setPen(Qt.NoPen)                                     # array at centre
        p.setBrush(QColor(pal.get("text_dim", pal["text"])))
        p.drawEllipse(QPointF(cx, cy), 3, 3)
        f = QFont()
        f.setPointSize(7)
        p.setFont(f)
        p.setPen(QPen(QColor(pal.get("faint", pal.get("text_dim", pal["text"]))), 1))
        p.drawText(r.adjusted(4, 2, -4, -2), int(Qt.AlignTop | Qt.AlignLeft), "preview")
        p.end()


class StageStrip(QWidget):
    """A compact live read-out of what each cleaning stage is doing *right now* — Echo (AEC), Dereverb,
    Denoise, Auto gain — as four small labelled bars. This is the per-stage half of the transparency
    story: an integrator can SEE each stage acting, the thing a black-box "AI mic" can't show.

    Honest by construction: a stage that is ON but has nothing to do shows a lit-but-near-empty bar,
    NOT greyed — greyed means the stage is OFF. Echo shows 'idle' when there's no far-end signal to
    cancel (so its ERLE isn't read as a misleading 0 dB). Auto gain is bipolar (boost up / cut down),
    because it's a normalizer, not a suppressor. Fed a :class:`StageActivity` each tick via
    :meth:`set_activity`; pure painting otherwise."""

    _CELLS = (("aec", "Echo"), ("dereverb", "Dereverb"), ("denoise", "Denoise"), ("agc", "Auto gain"))
    _SCALE_DB = 24.0      # a full bar = this many dB of ERLE / attenuation / |gain|

    def __init__(self, parent=None):
        super().__init__(parent)
        self._cells = {k: {"label": lbl, "on": False, "idle": False, "db": 0.0, "bipolar": False}
                       for k, lbl in self._CELLS}
        self.setMinimumHeight(36)
        self.setMinimumWidth(220)
        self.setToolTip(
            "Live cleaning-stage activity. A lit but near-empty bar = the stage is on with little to do "
            "right now (honest idle); a greyed bar = the stage is off. 'Echo' shows 'idle' when there's "
            "no far-end to cancel; 'Auto gain' is bipolar (boost up / cut down)."
        )

    def set_activity(self, a) -> None:
        """Update the four cells from a :class:`StageActivity` snapshot (pass ZERO_ACTIVITY to grey all)."""
        c = self._cells
        c["aec"].update(on=bool(a.aec_on), idle=bool(a.aec_on and not a.aec_farend_active),
                        db=float(a.aec_erle_db))
        c["dereverb"].update(on=bool(a.dereverb_on), db=float(a.dereverb_db))
        c["denoise"].update(on=bool(a.denoise_on), db=float(a.denoise_db))
        c["agc"].update(on=bool(a.agc_on), db=float(a.agc_gain_db), bipolar=True)
        self.update()

    def cell(self, key: str) -> dict:
        """The live state of one cell (``on``/``idle``/``db``/``bipolar``) — for tests + introspection."""
        return self._cells[key]

    def _value_text(self, cell: dict) -> str:
        if not cell["on"]:
            return "off"
        if cell["idle"]:
            return "idle"
        db = cell["db"]
        if cell["bipolar"]:
            return f"{db:+.0f} dB"
        return f"{db:.0f} dB"

    def paintEvent(self, e):  # pragma: no cover - pure painting
        st = getattr(self.window(), "state", None)
        pal = _palette(getattr(st, "theme", "dark") if st is not None else "dark")
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        f = QFont(self.font())
        f.setPointSizeF(max(7.0, f.pointSizeF() - 1.0))
        p.setFont(f)
        n = len(self._CELLS)
        w = self.width() / n
        cap_h = 13.0
        for i, (key, _lbl) in enumerate(self._CELLS):
            cell = self._cells[key]
            x0 = i * w + 3
            cw = w - 6
            on = cell["on"]
            cap_col = pal["text"] if on else pal["faint"]
            p.setPen(QPen(QColor(cap_col), 1))
            p.drawText(QRectF(x0, 0, cw, cap_h), int(Qt.AlignLeft | Qt.AlignVCenter), cell["label"])
            bar = QRectF(x0, cap_h + 1, cw, self.height() - cap_h - 3)
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(pal["surface"]))
            p.drawRoundedRect(bar, 3, 3)
            frac = min(1.0, abs(cell["db"]) / self._SCALE_DB)
            if on and not cell["idle"] and frac > 0.001:
                p.setBrush(QColor(pal["accent"] if cell["bipolar"] else pal["ok"]))
                if cell["bipolar"]:                               # centred: boost grows right, cut grows left
                    mid = bar.x() + bar.width() / 2.0
                    half = bar.width() / 2.0 * frac
                    fr = QRectF(mid, bar.y(), half, bar.height()) if cell["db"] >= 0 \
                        else QRectF(mid - half, bar.y(), half, bar.height())
                    p.drawRoundedRect(fr, 2, 2)
                    p.setPen(QPen(QColor(pal["border_strong"]), 1))  # centre tick
                    p.drawLine(QPointF(mid, bar.y() + 1), QPointF(mid, bar.bottom() - 1))
                    p.setPen(Qt.NoPen)
                else:
                    p.drawRoundedRect(QRectF(bar.x(), bar.y(), bar.width() * frac, bar.height()), 2, 2)
            p.setPen(QPen(QColor(pal["text_dim"] if on else pal["faint"]), 1))   # value / idle / off text
            p.drawText(bar, int(Qt.AlignCenter), self._value_text(cell))
        p.end()


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
        outer.setContentsMargins(SPACE["sm"], SPACE["xs"], SPACE["sm"], SPACE["sm"])
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
