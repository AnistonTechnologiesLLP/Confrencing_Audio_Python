"""Inspector side panel: Build / AEC-DSP / Routing / Issues / Simulate / JSON tabs."""
from __future__ import annotations

import dataclasses
import math

from PySide6.QtCore import QObject, QRunnable, Qt, QThreadPool, QTimer, Signal
from PySide6.QtGui import QColor, QFont
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFormLayout,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSlider,
    QSpinBox,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

import conf_pipeline as cp
import conf_pipeline_control as cc
from conf_pipeline.model import AecConfig, Point2D

from .state import AppState


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
    so the Live tab can auto-detect dead / silent capsules."""

    def __init__(self, device, samplerate, channels, dur=0.6):
        super().__init__()
        self._args = (device, samplerate, channels, dur)
        self.signals = _ProbeSignals()

    def run(self):  # noqa: D401 (Qt override)
        try:
            import numpy as np
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


DEVICE_TYPES = [
    ("Processor (DSP)", "processor"),
    ("Microphone array", "microphoneArray"),
    ("Wireless mic", "wirelessMic"),
    ("Wired mic", "wiredMic"),
    ("Loudspeaker", "loudspeaker"),
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


class NoWheelDoubleSpinBox(QDoubleSpinBox):
    """A spin box that ignores mouse-wheel scrolling.

    Scrolling the inspector then scrolls the panel instead of changing the value
    — and, crucially, never fires valueChanged mid-wheel, which would rebuild the
    selection card and destroy this very widget inside its own event (a crash).
    """

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


class Inspector(QWidget):
    def __init__(self, state: AppState):
        super().__init__()
        self.state = state
        self._refreshing = False
        self._refresh_pending = False
        self._dsp_seq = 1
        self._heat_pending = False
        self._validate_workers = set()  # strong refs to in-flight QRunnables
        self.setMinimumWidth(380)

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # Status banner: live validation state + the single most useful next step,
        # clickable to jump to the relevant tab. Always visible above the tabs.
        self.banner = QLabel("")
        self.banner.setProperty("inspectorBanner", "true")
        self.banner.setWordWrap(True)
        self.banner.setContentsMargins(12, 8, 12, 8)
        self.banner.setTextInteractionFlags(Qt.LinksAccessibleByMouse)
        self.banner.linkActivated.connect(self._banner_link)
        root.addWidget(self.banner)

        self.tabs = QTabWidget()
        root.addWidget(self.tabs, 1)

        # ---- live array-control state (host-side beamforming) ----
        self._live_ctl = None            # MicController while connected
        self._live_design = None         # last cc.BeamDesign built from zones
        self._live_dev_rates = {}        # device index -> native samplerate
        self._probe_workers = set()      # strong refs to capsule-probe runnables
        self._ab_workers = set()         # strong refs to A/B-test runnables
        self._calib_workers = set()      # strong refs to front-calibration runnables
        self._clean_monitor = None       # CleanMonitor while OCTOVOX cleaning is live
        self._autosteer = None           # AutoSteerController while auto-following talkers

        self.tabs.addTab(self._scroll(self._build_tab()), "Build")
        self.tabs.addTab(self._scroll(self._dsp_tab()), "AEC / DSP")
        self.tabs.addTab(self._routing_tab(), "Routing")
        self.tabs.addTab(self._issues_tab(), "Issues")
        self.tabs.addTab(self._scroll(self._simulate_tab()), "Simulate")
        self.tabs.addTab(self._scroll(self._live_tab()), "Live")
        self.tabs.addTab(self._json_tab(), "JSON")

        self._live_timer = QTimer(self)
        self._live_timer.setInterval(60)
        self._live_timer.timeout.connect(self._tick_live_meter)
        self._live_timer.start()

        state.changed.connect(self._schedule_refresh)
        self.refresh()

    def _schedule_refresh(self):
        """Coalesce rebuilds onto the next event-loop tick so the inspector is
        never rebuilt synchronously inside a child widget's input event."""
        if self._refresh_pending:
            return
        self._refresh_pending = True
        QTimer.singleShot(0, self._do_refresh)

    def _do_refresh(self):
        self._refresh_pending = False
        self.refresh()

    def _scroll(self, inner: QWidget) -> QScrollArea:
        """Wrap a tab page so stacked content scrolls instead of forcing a tall
        window minimum (keeps the app usable on small / high-DPI screens)."""
        sa = QScrollArea()
        sa.setWidgetResizable(True)
        sa.setFrameShape(QFrame.NoFrame)
        # As-needed (not Off): if a tab's content is wider than a narrow / high-DPI
        # panel, scroll it inside the panel rather than letting it spill out of the
        # window.
        sa.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        sa.setWidget(inner)
        return sa

    # ------------------------------------------------------------- Build tab
    def _build_tab(self):
        w = QWidget()
        lay = QVBoxLayout(w)

        add = QGroupBox("Add device")
        f = QHBoxLayout(add)
        self.dev_type = QComboBox()
        for label, val in DEVICE_TYPES:
            self.dev_type.addItem(label, val)
        self.dev_transport = QComboBox()
        self.dev_transport.addItems(["dante", "analog"])
        add_btn = QPushButton("Add")
        add_btn.setProperty("accent", "true")
        add_btn.clicked.connect(self._add_device)
        f.addWidget(self.dev_type, 2)
        f.addWidget(self.dev_transport, 1)
        f.addWidget(add_btn)
        lay.addWidget(add)

        self.dev_list = QListWidget()
        self.dev_list.setMaximumHeight(240)
        self.dev_list.itemClicked.connect(self._on_device_click)
        lay.addWidget(QLabel("Devices"))
        lay.addWidget(self.dev_list, 1)
        row = QHBoxLayout()
        rm = QPushButton("Remove selected")
        rm.clicked.connect(self._remove_selected_device)
        row.addWidget(rm)
        lay.addLayout(row)

        cov = QGroupBox("Coverage (selected array)")
        cl = QVBoxLayout(cov)
        modes = QHBoxLayout()
        self.mode_auto = QPushButton("Automatic")
        self.mode_manual = QPushButton("Manual")
        self.mode_auto.clicked.connect(lambda: self._set_mode("automatic"))
        self.mode_manual.clicked.connect(lambda: self._set_mode("manual"))
        modes.addWidget(self.mode_auto)
        modes.addWidget(self.mode_manual)
        cl.addLayout(modes)
        zr = QHBoxLayout()
        self.zone_kind = QComboBox()
        self.zone_kind.addItems(["Records (dynamic)", "Always-on (dedicated)", "No-pickup (exclusion)"])
        self.zone_kind.currentIndexChanged.connect(self._zone_kind_changed)
        addz = QPushButton("+ Zone")
        addz.clicked.connect(self._add_zone_default)
        zr.addWidget(self.zone_kind, 1)
        zr.addWidget(addz)
        cl.addLayout(zr)
        cl.addWidget(QLabel("Tip: pick the Zone tool and drag on the canvas."))
        lay.addWidget(cov)

        people = QGroupBox("People (talkers)")
        pl = QVBoxLayout(people)
        addt = QPushButton("+ Add talker")
        addt.setProperty("accent", "true")
        addt.clicked.connect(self._add_talker)
        pl.addWidget(addt)
        self.talker_list = QListWidget()
        self.talker_list.setMaximumHeight(200)
        self.talker_list.itemClicked.connect(self._on_talker_click)
        pl.addWidget(self.talker_list)
        lay.addWidget(people)

        self.sel_box = QGroupBox("Selection")
        self.sel_layout = QVBoxLayout(self.sel_box)
        lay.addWidget(self.sel_box)

        return w

    def _dsp_tab(self):
        w = QWidget()
        self.dsp_layout = QVBoxLayout(w)
        return w

    def _routing_tab(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        self.routing_summary_lbl = QLabel()
        lay.addWidget(self.routing_summary_lbl)
        lay.addWidget(QLabel("Signal flow (Dante hub / routing):"))
        self.routing_view = QPlainTextEdit()
        self.routing_view.setReadOnly(True)
        self.routing_view.setFont(QFont("Consolas", 10))
        lay.addWidget(self.routing_view, 1)

        # ---- Mute groups (logic / control) ----
        mg = QGroupBox("Mute groups")
        mgl = QVBoxLayout(mg)
        mgl.addWidget(QLabel("A named set of devices that mute together (Designer-style logic control)."))
        self.mute_group_list = QListWidget()
        self.mute_group_list.setMaximumHeight(120)
        mgl.addWidget(self.mute_group_list)
        row = QHBoxLayout()
        self.mute_group_name = QLineEdit()
        self.mute_group_name.setPlaceholderText("New group name…")
        add_mg = QPushButton("+ Group from mute-capable mics")
        add_mg.clicked.connect(self._add_mute_group)
        row.addWidget(self.mute_group_name, 1)
        row.addWidget(add_mg)
        mgl.addLayout(row)
        row2 = QHBoxLayout()
        self.mute_group_toggle = QPushButton("Toggle mute")
        self.mute_group_toggle.clicked.connect(self._toggle_selected_mute_group)
        rm_mg = QPushButton("Remove")
        rm_mg.clicked.connect(self._remove_selected_mute_group)
        row2.addWidget(self.mute_group_toggle)
        row2.addWidget(rm_mg)
        row2.addStretch(1)
        mgl.addLayout(row2)
        lay.addWidget(mg)
        return w

    # ---- mute-group actions ----
    def _selected_mute_group_id(self):
        it = self.mute_group_list.currentItem()
        return it.data(Qt.UserRole) if it is not None else None

    def _add_mute_group(self):
        cfg = self.state.config
        mics = [d for d in cfg.devices if cp.is_mic_device(d) and cp.device_capabilities(d).mute]
        if not mics:
            return self._toast("No mute-capable microphones to group.")
        existing = cfg.control.mute_groups if cfg.control is not None else []
        n = len(existing) + 1
        ids = {g.id for g in existing}
        gid = f"mg{n}"
        while gid in ids:
            n += 1
            gid = f"mg{n}"
        name = self.mute_group_name.text().strip() or f"Mute group {n}"
        try:
            grp = cp.create_mute_group(gid, name, device_ids=[m.id for m in mics])
            self.state.set_config(cp.add_mute_group(cfg, grp))
            self.mute_group_name.clear()
            self._toast(f"Added “{name}” over {len(mics)} mic(s)")
        except Exception as exc:
            self._toast(str(exc))

    def _toggle_selected_mute_group(self):
        gid = self._selected_mute_group_id()
        if gid is None or self.state.config.control is None:
            return
        grp = next((g for g in self.state.config.control.mute_groups if g.id == gid), None)
        if grp is not None:
            self.state.set_config(cp.set_mute_group_muted(self.state.config, gid, not grp.muted))

    def _remove_selected_mute_group(self):
        gid = self._selected_mute_group_id()
        if gid is not None:
            self.state.set_config(cp.remove_mute_group(self.state.config, gid))

    def _refresh_mute_groups(self, cfg):
        if not hasattr(self, "mute_group_list"):
            return
        prev = self._selected_mute_group_id()
        self.mute_group_list.clear()
        groups = cfg.control.mute_groups if cfg.control is not None else []
        for g in groups:
            members = len(g.device_ids) + len(g.zone_refs)
            state = "🔇 muted" if g.muted else "🔊 unmuted"
            it = QListWidgetItem(f"{g.label}  ·  {members} member(s) · {g.trigger} · {state}")
            it.setData(Qt.UserRole, g.id)
            self.mute_group_list.addItem(it)
            if g.id == prev:
                self.mute_group_list.setCurrentItem(it)
        has = bool(groups)
        self.mute_group_toggle.setEnabled(has)

    def _toast(self, msg):
        win = self.window()
        if hasattr(win, "toast"):
            win.toast(msg)

    def _issues_tab(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        self.issue_badge = QLabel()
        lay.addWidget(self.issue_badge)
        self.issue_list = QListWidget()
        self.issue_list.itemClicked.connect(self._on_issue_click)
        lay.addWidget(self.issue_list)
        sep = QFrame()
        sep.setFrameShape(QFrame.HLine)
        lay.addWidget(sep)
        lay.addWidget(QLabel("Coverage (array pickup circles)"))
        self.coverage_lbl = QLabel()
        self.coverage_lbl.setWordWrap(True)
        lay.addWidget(self.coverage_lbl)
        return w

    def _json_tab(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        self.json_view = QPlainTextEdit()
        self.json_view.setReadOnly(True)
        self.json_view.setFont(QFont("Consolas", 10))
        lay.addWidget(self.json_view)
        return w

    # ------------------------------------------------------------- Live tab
    def _live_tab(self):
        """Drive a real array microphone with host-side beamforming.

        The pickup/exclusion zones on the selected array are turned into beam
        weights (steer toward pickup, null exclusions). With the array plugged in
        and the ``[control]`` extra installed this runs live; otherwise a
        simulated controller keeps the UI usable.
        """
        w = QWidget()
        lay = QVBoxLayout(w)

        intro = QLabel(
            "Coverage-area control for an array microphone (e.g. sensiBel 8). "
            "Pickup zones are steered toward; exclusion zones are nulled/attenuated."
        )
        intro.setWordWrap(True)
        lay.addWidget(intro)

        self.live_avail_lbl = QLabel()
        self.live_avail_lbl.setWordWrap(True)
        lay.addWidget(self.live_avail_lbl)

        # --- array + geometry ---
        gb = QGroupBox("Array & geometry")
        gf = QFormLayout(gb)
        gf.setRowWrapPolicy(QFormLayout.WrapLongRows)        # stack label/field when narrow
        gf.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)
        self.live_array = QComboBox()  # populated in _refresh_live
        self.live_array.currentIndexChanged.connect(lambda *_a: None if self._refreshing else self._on_live_array_changed())
        gf.addRow("Array", self.live_array)
        self.live_radius = NoWheelDoubleSpinBox()
        self.live_radius.setRange(0.01, 1.0)
        self.live_radius.setSingleStep(0.005)
        self.live_radius.setDecimals(3)
        self.live_radius.setValue(0.05)
        self.live_radius.setSuffix(" m")
        self.live_radius.setToolTip(
            "Capsule-circle radius of YOUR array. 0.05 m is a placeholder — set the "
            "real radius (centre to a capsule). It sets the beamwidth and the DOA "
            "resolution, so the number matters."
        )
        gf.addRow("Capsule radius", self.live_radius)
        self.live_freq = NoWheelDoubleSpinBox()
        self.live_freq.setRange(200.0, 8000.0)
        self.live_freq.setSingleStep(100.0)
        self.live_freq.setDecimals(0)
        self.live_freq.setValue(cc.DEFAULT_DESIGN_FREQ_HZ)
        self.live_freq.setSuffix(" Hz")
        gf.addRow("Design freq", self.live_freq)
        lay.addWidget(gb)

        # --- active capsules (exclude a dead / non-audio channel) ---
        cap_gb = QGroupBox("Active capsules")
        cap_l = QVBoxLayout(cap_gb)
        cap_row = QHBoxLayout()
        self.live_caps = []
        for i in range(8):
            cb = QCheckBox(str(i + 1))
            cb.setChecked(True)
            cb.toggled.connect(lambda *_a: None if self._refreshing else self._live_active_changed())
            self.live_caps.append(cb)
            cap_row.addWidget(cb)
        cap_row.addStretch(1)
        cap_l.addLayout(cap_row)
        ctl_row = QHBoxLayout()
        self.live_detect = QPushButton("Detect silent capsules")
        self.live_detect.clicked.connect(self._live_detect_silent)
        self.live_active_lbl = QLabel("8/8 active")
        ctl_row.addWidget(self.live_detect)
        ctl_row.addWidget(self.live_active_lbl)
        ctl_row.addStretch(1)
        cap_l.addLayout(ctl_row)
        lay.addWidget(cap_gb)

        # --- beamformer mode (directivity vs robustness) ---
        bf_gb = QGroupBox("Beamformer")
        bf = QFormLayout(bf_gb)
        bf.setRowWrapPolicy(QFormLayout.WrapLongRows)
        bf.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)
        self.live_mode = QComboBox()
        self.live_mode.addItem("Superdirective (rejects background)", cc.MODE_SUPERDIRECTIVE)
        self.live_mode.addItem("Delay-and-sum (most robust)", cc.MODE_DELAYSUM)
        self.live_mode.currentIndexChanged.connect(lambda *_a: None if self._refreshing else self._on_live_mode_changed())
        bf.addRow("Mode", self.live_mode)
        rob_row = QHBoxLayout()
        self.live_robust = QSlider(Qt.Horizontal)
        self.live_robust.setRange(0, 100)
        self.live_robust.setValue(60)          # ≈ 0.05 loading
        self.live_robust.valueChanged.connect(self._on_live_loading_changed)
        self.live_robust_lbl = QLabel()
        rob_row.addWidget(self.live_robust, 1)
        rob_row.addWidget(self.live_robust_lbl)
        bf.addRow("Focus ↔ robust", rob_row)
        self.live_suppress_outside = QCheckBox("Null talkers outside the pickup zone")
        self.live_suppress_outside.setToolTip(
            "Add every placed talker that is not inside a pickup zone as a beam null, "
            "so out-of-area voices are actively subtracted (up to the array's null budget)."
        )
        self.live_suppress_outside.toggled.connect(
            lambda *_a: None if self._refreshing else (self._live_design_from_zones() if self._live_design is not None else None)
        )
        bf.addRow("Out-of-zone", self.live_suppress_outside)
        preset = QPushButton("Aggressive preset (low-noise studio mics)")
        preset.setToolTip(
            "Push the beam to maximum directivity. Safe here because your SBM100B mics "
            "are 80 dBA SNR — the extra self-noise from aggressive superdirectivity stays "
            "below audibility (would hiss on ordinary MEMS). Watch the WNG in the readout."
        )
        preset.clicked.connect(self._live_aggressive_preset)
        bf.addRow("Preset", preset)
        lay.addWidget(bf_gb)

        # --- auto-steer: detect talkers by direction and follow the ones in a sector ---
        as_gb = QGroupBox("Auto-steer — follow talkers in a sector")
        as_gb.setToolTip(
            "Detect who is talking by direction (DOA) in real time and steer a beam at "
            "each talker inside the coverage sector, nulling the ones outside. Best for "
            "a desk array: it adapts as people talk in turn or move. Azimuth only — a "
            "small array resolves bearing, not distance, so the area is an angular arc."
        )
        asf = QFormLayout(as_gb)
        asf.setRowWrapPolicy(QFormLayout.WrapLongRows)
        asf.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)
        self.live_autosteer = QCheckBox("Enable (auto-detect & follow)")
        self.live_autosteer.setToolTip("On Connect, follow detected talkers instead of using a fixed zone design.")
        self.live_autosteer.toggled.connect(lambda *_a: None if self._refreshing else self._on_autosteer_toggled())
        asf.addRow("Auto-steer", self.live_autosteer)
        self.live_sector_center = NoWheelDoubleSpinBox()
        self.live_sector_center.setRange(0.0, 359.0)
        self.live_sector_center.setSingleStep(5.0)
        self.live_sector_center.setDecimals(0)
        self.live_sector_center.setValue(0.0)
        self.live_sector_center.setSuffix("°")
        asf.addRow("Sector centre", self.live_sector_center)
        self.live_sector_width = NoWheelDoubleSpinBox()
        self.live_sector_width.setRange(10.0, 360.0)
        self.live_sector_width.setSingleStep(10.0)
        self.live_sector_width.setDecimals(0)
        self.live_sector_width.setValue(120.0)
        self.live_sector_width.setSuffix("° wide")
        self.live_sector_width.setToolTip("Full arc width (e.g. 120° = centre ±60°).")
        asf.addRow("Sector width", self.live_sector_width)
        self.live_front_offset = NoWheelDoubleSpinBox()
        self.live_front_offset.setRange(-180.0, 180.0)
        self.live_front_offset.setSingleStep(5.0)
        self.live_front_offset.setDecimals(0)
        self.live_front_offset.setValue(0.0)
        self.live_front_offset.setSuffix("°")
        self.live_front_offset.setToolTip("Rotate the array's azimuth-0 to your room/desk 'front'.")
        asf.addRow("Front offset", self.live_front_offset)
        self.live_max_talkers = NoWheelSpinBox()
        self.live_max_talkers.setRange(1, 6)
        self.live_max_talkers.setValue(3)
        self.live_max_talkers.setToolTip("Max simultaneous talkers to track (resolution-limited on a small array).")
        asf.addRow("Max talkers", self.live_max_talkers)
        self.live_autosteer_gate = QCheckBox("Mute output when nobody is in the sector")
        self.live_autosteer_gate.setChecked(True)
        asf.addRow("Gate", self.live_autosteer_gate)
        self.live_calib_btn = QPushButton("Calibrate front (talk from the front, then click)")
        self.live_calib_btn.setToolTip(
            "Records a few seconds while someone talks from your desk's 'front', measures "
            "that bearing, and sets the Front offset so the sector lines up with it. "
            "Disconnect first; needs the [control] extra."
        )
        self.live_calib_btn.clicked.connect(self._live_calibrate_front)
        asf.addRow("Calibrate", self.live_calib_btn)
        self.live_autosteer_view = QLabel("Connect with auto-steer enabled to see detected talkers.")
        self.live_autosteer_view.setWordWrap(True)
        self.live_autosteer_view.setFont(QFont("Consolas", 9))
        asf.addRow(self.live_autosteer_view)
        lay.addWidget(as_gb)
        self._autosteer_widgets = (
            self.live_sector_center, self.live_sector_width, self.live_front_offset,
            self.live_max_talkers, self.live_autosteer_gate, self.live_calib_btn,
        )
        for _w in self._autosteer_widgets:
            _w.setEnabled(False)                 # enabled when auto-steer is ticked
        # adjust the sector live while connected (no reconnect needed)
        for _sp in (self.live_sector_center, self.live_sector_width, self.live_front_offset, self.live_max_talkers):
            _sp.valueChanged.connect(lambda *_a: None if self._refreshing else self._on_autosteer_param_changed())
        self.live_autosteer_gate.toggled.connect(lambda *_a: None if self._refreshing else self._on_autosteer_param_changed())

        design_btn = QPushButton("Design beam from zones")
        design_btn.setProperty("accent", "true")
        design_btn.clicked.connect(self._live_design_from_zones)
        lay.addWidget(design_btn)

        self.live_ab_btn = QPushButton("A/B test — record & compare beamformers")
        self.live_ab_btn.setToolTip(
            "Record a clip from the array, process it omni / delay-sum / superdirective / "
            "aggressive / nulled, and save mono WAVs + a dB report so you can hear and "
            "measure the difference."
        )
        self.live_ab_btn.clicked.connect(self._live_ab_test)
        lay.addWidget(self.live_ab_btn)

        self.live_design_view = QPlainTextEdit()
        self.live_design_view.setReadOnly(True)
        self.live_design_view.setFont(QFont("Consolas", 9))
        self.live_design_view.setMaximumHeight(150)
        self.live_design_view.setPlaceholderText("No beam designed yet.")
        lay.addWidget(self.live_design_view)

        # --- device + transport ---
        dgb = QGroupBox("Audio device")
        df = QFormLayout(dgb)
        df.setRowWrapPolicy(QFormLayout.WrapLongRows)
        df.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)
        self.live_device = QComboBox()
        self.live_device.currentIndexChanged.connect(lambda *_a: None if self._refreshing else self._on_live_device_changed())
        df.addRow("Input", self.live_device)
        self.live_rate = QComboBox()
        for r in ("48000", "44100", "32000", "16000"):
            self.live_rate.addItem(f"{r} Hz", int(r))
        df.addRow("Sample rate", self.live_rate)
        self.live_monitor = QCheckBox("Monitor output (use headphones)")
        self.live_monitor.setToolTip(
            "Play the beamformed output live. Use headphones — monitoring through "
            "room speakers will feed back into the array and howl."
        )
        df.addRow("Monitor", self.live_monitor)
        self.live_out_device = QComboBox()
        df.addRow("Output", self.live_out_device)
        lay.addWidget(dgb)

        # --- clean via OCTOVOX (near-live cleaned monitor) ---
        ov_gb = QGroupBox("Clean via OCTOVOX (near-live)")
        ov_gb.setToolTip(
            "Send rolling chunks of the raw array to a running OCTOVOX server "
            "(beamform + dereverb + DeepFilterNet3), steered by the zone azimuths, "
            "and play the cleaned result back. Delayed by ~chunk + processing; "
            "not real-time talkback."
        )
        ovf = QFormLayout(ov_gb)
        ovf.setRowWrapPolicy(QFormLayout.WrapLongRows)
        ovf.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)
        self.live_octovox = QCheckBox("Enable (use headphones)")
        ovf.addRow("OCTOVOX", self.live_octovox)
        self.live_octovox_url = QLineEdit(cc.OCTOVOX_DEFAULT_URL)
        ovf.addRow("Server", self.live_octovox_url)
        self.live_octovox_steer = QCheckBox("Steer to pickup zone (needs azimuth calibration)")
        self.live_octovox_steer.setToolTip(
            "OFF (default): OCTOVOX auto-finds the voice — reliable on a small/front-back-"
            "ambiguous array. ON: force OCTOVOX to steer at the pickup-zone azimuth; only "
            "use once the Azimuth offset is calibrated, or it can null the voice (noise only)."
        )
        ovf.addRow("Direction", self.live_octovox_steer)
        self.live_az_offset = NoWheelDoubleSpinBox()
        self.live_az_offset.setRange(-180.0, 180.0)
        self.live_az_offset.setSingleStep(5.0)
        self.live_az_offset.setValue(0.0)
        self.live_az_offset.setSuffix("°")
        ovf.addRow("Azimuth offset", self.live_az_offset)
        self.live_chunk = NoWheelDoubleSpinBox()
        self.live_chunk.setRange(1.0, 8.0)
        self.live_chunk.setSingleStep(0.5)
        self.live_chunk.setValue(3.0)
        self.live_chunk.setSuffix(" s")
        ovf.addRow("Chunk", self.live_chunk)
        self.live_octovox_status = QLabel("")
        self.live_octovox_status.setWordWrap(True)
        ovf.addRow(self.live_octovox_status)
        lay.addWidget(ov_gb)

        # --- transport controls ---
        row = QHBoxLayout()
        self.live_connect = QPushButton("Connect")
        self.live_connect.clicked.connect(self._live_toggle_connect)
        self.live_mute = QPushButton("Mute")
        self.live_mute.setCheckable(True)
        self.live_mute.clicked.connect(self._live_toggle_mute)
        row.addWidget(self.live_connect)
        row.addWidget(self.live_mute)
        lay.addLayout(row)

        gainrow = QHBoxLayout()
        gainrow.addWidget(QLabel("Gain"))
        self.live_gain = QSlider(Qt.Horizontal)
        self.live_gain.setRange(-60, 24)
        self.live_gain.setValue(0)
        self.live_gain.valueChanged.connect(self._live_gain_changed)
        self.live_gain_lbl = QLabel("0 dB")
        gainrow.addWidget(self.live_gain, 1)
        gainrow.addWidget(self.live_gain_lbl)
        lay.addLayout(gainrow)

        lay.addWidget(QLabel("Output level"))
        self.live_meter = QProgressBar()
        self.live_meter.setRange(0, 100)
        self.live_meter.setValue(0)
        self.live_meter.setTextVisible(False)
        lay.addWidget(self.live_meter)

        self.live_status = QLabel("Disconnected.")
        self.live_status.setWordWrap(True)
        lay.addWidget(self.live_status)
        lay.addStretch(1)

        # Stop combos from demanding their full content width (long OS device
        # names) — let them fill the column and elide instead of forcing the whole
        # tab wider than a narrow panel.
        for combo in (self.live_array, self.live_device, self.live_rate, self.live_out_device, self.live_mode):
            combo.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
            combo.setMinimumContentsLength(6)
            combo.setMinimumWidth(80)
        self.live_robust_lbl.setText(f"{self._live_loading():.3f}")  # initial loading readout
        return w

    # ---- live helpers ----
    def _live_array_id(self):
        return self.live_array.currentData()

    def _live_busy(self):
        """True if any live session (beamformer, OCTOVOX, or auto-steer) is active."""
        return self._live_ctl is not None or self._clean_monitor is not None or self._autosteer is not None

    def _active_ctl(self):
        """The underlying MicController in use (auto-steer wraps one), or None."""
        if self._autosteer is not None:
            return self._autosteer.ctrl
        return self._live_ctl

    def _on_autosteer_toggled(self):
        """Enable the sector controls only when auto-steer is selected."""
        on = self.live_autosteer.isChecked()
        for w in self._autosteer_widgets:
            w.setEnabled(on)

    def _autosteer_sector(self):
        return cc.SectorConfig(
            center_deg=float(self.live_sector_center.value()),
            half_width_deg=float(self.live_sector_width.value()) / 2.0,
            front_offset_deg=float(self.live_front_offset.value()),
        )

    def _on_autosteer_param_changed(self):
        """Push sector / max-talkers / gate changes to a running session live —
        the controller reads these each control tick, so no reconnect is needed."""
        a = self._autosteer
        if a is None:
            return
        a.sector = self._autosteer_sector()
        a.max_talkers = int(self.live_max_talkers.value())
        a.gate_when_empty = self.live_autosteer_gate.isChecked()
        self.live_mute.setEnabled(not a.gate_when_empty)

    def _live_active_mask(self):
        return [cb.isChecked() for cb in self.live_caps]

    def _live_geometry(self):
        geom = cc.sensibel_8(radius_m=float(self.live_radius.value()))
        mask = self._live_active_mask()
        if any(mask) and not all(mask):
            geom = cc.with_active_channels(geom, mask)
        return geom

    def _live_mode(self):
        return self.live_mode.currentData() or cc.MODE_SUPERDIRECTIVE

    def _live_loading(self):
        # slider 0..100 → diagonal loading 0.001 (max focus) .. 0.5 (max robust), log
        v = self.live_robust.value()
        return round(0.001 * (500.0 ** (v / 100.0)), 4)

    def _on_live_mode_changed(self):
        sd = self._live_mode() == cc.MODE_SUPERDIRECTIVE
        self.live_robust.setEnabled(sd)
        self.live_robust_lbl.setEnabled(sd)
        if self._live_design is not None:
            self._live_design_from_zones()

    def _on_live_loading_changed(self, *_a):
        self.live_robust_lbl.setText(f"{self._live_loading():.3f}")
        if not self._refreshing and self._live_design is not None:
            self._live_design_from_zones()

    def _refresh_live(self):
        # availability banner
        if cc.controls_available():
            self.live_avail_lbl.setText("Live audio ready (numpy + sounddevice detected).")
        else:
            self.live_avail_lbl.setText(
                "Live audio backend not installed — running in simulation. "
                "Install with:  pip install -e \".[control]\""
            )
        # array picker (preserve selection)
        cur = self._live_array_id()
        self.live_array.blockSignals(True)
        self.live_array.clear()
        arrays = [d for d in self.state.config.devices if d.type == "microphoneArray"]
        for a in arrays:
            placed = "" if a.position else "  (no position)"
            self.live_array.addItem(f"{a.label} · {a.id}{placed}", a.id)
        if cur is not None:
            idx = self.live_array.findData(cur)
            if idx >= 0:
                self.live_array.setCurrentIndex(idx)
        self.live_array.blockSignals(False)
        # device picker (only when idle; don't disrupt a live session)
        if not self._live_busy():
            curd = self.live_device.currentData()
            self.live_device.blockSignals(True)
            self.live_device.clear()
            from conf_pipeline_control.audio import list_input_devices
            devs = list_input_devices()
            self._live_dev_rates = {}
            if devs:
                for d in devs:
                    self.live_device.addItem(f"[{d.index}] {d.name} ({d.max_input_channels}ch)", d.index)
                    self._live_dev_rates[d.index] = int(d.default_samplerate)
            else:
                self.live_device.addItem("System default", None)
            if curd is not None:
                i = self.live_device.findData(curd)
                if i >= 0:
                    self.live_device.setCurrentIndex(i)
            self.live_device.blockSignals(False)
            self._on_live_device_changed()  # match the rate to the selected device

            # output devices (for monitoring)
            from conf_pipeline_control.audio import list_output_devices
            curo = self.live_out_device.currentData()
            self.live_out_device.blockSignals(True)
            self.live_out_device.clear()
            self.live_out_device.addItem("System default", None)
            for o in list_output_devices():
                self.live_out_device.addItem(f"[{o.index}] {o.name} ({o.max_output_channels}ch)", o.index)
            if curo is not None:
                i = self.live_out_device.findData(curo)
                if i >= 0:
                    self.live_out_device.setCurrentIndex(i)
            self.live_out_device.blockSignals(False)

    def _on_live_array_changed(self):
        # changing the target array invalidates any prior design
        self._live_design = None
        self.live_design_view.clear()

    def _live_aggressive_preset(self):
        """Max-directivity superdirective — safe thanks to the 80 dBA studio mics."""
        i = self.live_mode.findData(cc.MODE_SUPERDIRECTIVE)
        if i >= 0:
            self.live_mode.setCurrentIndex(i)
        self.live_robust.setValue(26)          # ≈ 0.005 loading (low → aggressive)
        if self._live_array_id():
            self._live_design_from_zones()

    def _live_ab_test(self):
        """Record a clip and compare beamformers → WAVs + report (off the GUI thread)."""
        if not cc.controls_available():
            self.live_status.setText("A/B test needs the [control] extra (numpy + sounddevice).")
            return
        aid = self._live_array_id()
        if not aid:
            self.live_status.setText("Select a placed array first.")
            return
        if self._live_busy():
            self.live_status.setText("Disconnect before running the A/B test.")
            return
        from PySide6.QtWidgets import QFileDialog
        out_dir = QFileDialog.getExistingDirectory(self, "Save A/B WAVs + report to…")
        if not out_dir:
            return
        geom = self._live_geometry()
        sr = self.live_rate.currentData() or 44100
        self.live_ab_btn.setEnabled(False)
        self.live_status.setText("A/B: recording 10 s — speak from the pickup zone…")
        worker = _ABWorker(self.state.config, aid, geom, self.live_device.currentData(),
                           int(sr), 10.0, out_dir, float(self.live_freq.value()))
        worker.signals.done.connect(self._on_ab_done)
        worker.signals.failed.connect(self._on_ab_failed)
        self._ab_workers.add(worker)
        QThreadPool.globalInstance().start(worker)

    def _on_ab_done(self, payload):
        summary, out_dir, n = payload
        self.live_ab_btn.setEnabled(True)
        self._ab_workers.clear()
        self.live_design_view.setPlainText(
            summary + f"\n\nSaved {n} files to:\n{out_dir}\n"
            "Listen: omni.wav vs superdirective_aggressive.wav vs nulled.wav."
        )
        self.live_status.setText(f"A/B done — {n} files in {out_dir}")

    def _on_ab_failed(self, msg):
        self.live_ab_btn.setEnabled(True)
        self._ab_workers.clear()
        self.live_status.setText(f"A/B failed: {msg}")

    def _on_live_device_changed(self):
        """Select the device's native sample rate so Connect doesn't fail on a
        rate the hardware can't open (e.g. a 44100-only array vs a 48000 default)."""
        rate = self._live_dev_rates.get(self.live_device.currentData())
        if not rate:
            return
        i = self.live_rate.findData(rate)
        if i < 0:
            self.live_rate.addItem(f"{rate} Hz", rate)
            i = self.live_rate.findData(rate)
        self.live_rate.setCurrentIndex(i)

    def _live_active_changed(self):
        """A capsule was toggled: update the count, and rebuild + reapply the beam
        (the live runtime designs over only the active capsules)."""
        n = sum(cb.isChecked() for cb in self.live_caps)
        if n == 0:  # never leave the array with no capsules
            for cb in self.live_caps:
                cb.blockSignals(True)
                cb.setChecked(True)
                cb.blockSignals(False)
            n = len(self.live_caps)
            self.live_status.setText("At least one capsule must stay active.")
        self.live_active_lbl.setText(f"{n}/{len(self.live_caps)} active")
        if self._live_design is not None:
            self._live_design_from_zones()  # rebuild with the new mask (+ reapply if connected)

    def _live_detect_silent(self):
        """Capture briefly and auto-uncheck capsules reading near-silence."""
        if self._live_busy():
            self.live_status.setText("Disconnect before detecting capsules (the device is in use).")
            return
        if not cc.controls_available():
            self.live_status.setText("Detect needs the [control] extra (numpy + sounddevice).")
            return
        dev = self.live_device.currentData()
        sr = self.live_rate.currentData() or 48000
        self.live_status.setText("Probing capsules…")
        self.live_detect.setEnabled(False)
        worker = _ProbeWorker(dev, int(sr), len(self.live_caps))
        worker.signals.done.connect(self._on_probe_done)
        worker.signals.failed.connect(self._on_probe_failed)
        self._probe_workers.add(worker)
        QThreadPool.globalInstance().start(worker)

    def _on_probe_done(self, rms):
        self.live_detect.setEnabled(True)
        self._probe_workers.clear()
        dbs = [20.0 * math.log10(r + 1e-12) for r in rms]
        mx = max(dbs) if dbs else -120.0
        # active = within 20 dB of the loudest capsule and above an absolute floor;
        # a dead capsule reads ~30 dB below the others (or true digital silence).
        for i, cb in enumerate(self.live_caps):
            if i < len(dbs):
                live = (dbs[i] > mx - 20.0) and (dbs[i] > -100.0)
                cb.blockSignals(True)
                cb.setChecked(live)
                cb.blockSignals(False)
        n = sum(cb.isChecked() for cb in self.live_caps)
        detail = "  ".join(f"{i+1}:{dbs[i]:.0f}" for i in range(len(dbs)))
        self.live_status.setText(f"{n}/{len(self.live_caps)} capsules live  ({detail} dB)")
        self._live_active_changed()

    def _on_probe_failed(self, msg):
        self.live_detect.setEnabled(True)
        self._probe_workers.clear()
        self.live_status.setText(f"Probe failed: {msg}")

    def _live_calibrate_front(self):
        """Record a 'front' talker and set the Front offset to the measured bearing."""
        if self._live_busy():
            self.live_status.setText("Disconnect before calibrating (the device is in use).")
            return
        if not cc.controls_available():
            self.live_status.setText("Calibrate needs the [control] extra (numpy + sounddevice).")
            return
        geom = self._live_geometry()
        sr = self.live_rate.currentData() or 44100
        self.live_status.setText("Calibrating — have someone talk from the FRONT for ~4 s…")
        self.live_calib_btn.setEnabled(False)
        worker = _CalibWorker(geom, self.live_device.currentData(), int(sr), 90.0)
        worker.signals.done.connect(self._on_calib_done)
        worker.signals.failed.connect(self._on_calib_failed)
        self._calib_workers.add(worker)
        QThreadPool.globalInstance().start(worker)

    def _on_calib_done(self, payload):
        self.live_calib_btn.setEnabled(self.live_autosteer.isChecked())
        self._calib_workers.clear()
        az, sal = payload
        if az is None:
            self.live_status.setText("Calibration: no clear talker detected — try again, louder.")
            return
        self.live_front_offset.setValue(round(az))   # front talker measured here → that's the offset
        self.live_status.setText(f"Front calibrated: azimuth {az:.0f}° ({sal:.0f} dB). Sector centre is now 'front'.")

    def _on_calib_failed(self, msg):
        self.live_calib_btn.setEnabled(self.live_autosteer.isChecked())
        self._calib_workers.clear()
        self.live_status.setText(f"Calibration failed: {msg}")

    def _live_design_from_zones(self):
        aid = self._live_array_id()
        if not aid:
            self.live_design_view.setPlainText("Add a microphone array and place it in the room first.")
            return
        try:
            geom = self._live_geometry()
            design = cc.design_zone_beams(
                self.state.config, aid, geom,
                freq_hz=float(self.live_freq.value()),
                mode=self._live_mode(),
                loading=self._live_loading(),
                suppress_outside_talkers=self.live_suppress_outside.isChecked(),
            )
        except ValueError as exc:
            self.live_design_view.setPlainText(f"Cannot design: {exc}")
            return
        self._live_design = design
        text = design.summary()
        if not design.beams:
            text += "\n\nTip: add Records/dedicated zones on this array to steer pickup, " \
                    "and No-pickup zones to mute areas."
        else:
            # compact azimuth pattern of the first beam, as a text sparkline
            pat = cc.beam_pattern_azimuth(list(design.beams[0].weights), geom, design.freq_hz, steps=36)
            text += "\n\nAzimuth response (beam 1), 0°→350°:\n" + self._sparkline([db for _a, db in pat])
            # per-talker leakage: how loudly each placed person is captured
            if self.state.config.talkers:
                leak = cc.talker_leakage_db(self.state.config, aid, geom, list(design.beams[0].weights), design.freq_hz)
                leak.sort(key=lambda r: -r[2])
                text += "\n\nTalker pickup (beam 1):"
                for _tid, label, gain, in_pk in leak:
                    tag = "pickup" if in_pk else "OUTSIDE"
                    text += f"\n  {label or _tid}: {gain:+.0f} dB  [{tag}]"
        self.live_design_view.setPlainText(text)
        if self._live_ctl is not None:
            try:
                self._live_ctl.apply_design(design)
            except ValueError as exc:
                self.live_status.setText(f"Design not applied: {exc}")

    @staticmethod
    def _sparkline(values_db, floor=-40.0):
        bars = "▁▂▃▄▅▆▇█"
        out = []
        for v in values_db:
            t = max(0.0, min(1.0, (v - floor) / (0.0 - floor)))
            out.append(bars[min(len(bars) - 1, int(t * (len(bars) - 1)))])
        return "".join(out)

    def _live_toggle_connect(self):
        if self._live_busy():
            self._live_disconnect()
            return
        if self.live_autosteer.isChecked():
            self._autosteer_connect()
            return
        if self.live_octovox.isChecked():
            self._octovox_connect()
            return
        aid = self._live_array_id()
        geom = self._live_geometry()
        rate = self.live_rate.currentData() or 48000
        try:
            if cc.controls_available():
                from conf_pipeline_control.live import LiveBeamController
                ctl = LiveBeamController(
                    geom,
                    device=self.live_device.currentData(),
                    samplerate=float(rate),
                    monitor=self.live_monitor.isChecked(),
                    output_device=self.live_out_device.currentData(),
                )
            else:
                ctl = cc.SimulatedMicController(geom)
            if self._live_design is not None:
                try:
                    ctl.apply_design(self._live_design)
                except ValueError:
                    pass
            ctl.set_gain_db(float(self.live_gain.value()))
            ctl.set_mute(self.live_mute.isChecked())
            ctl.connect()
        except Exception as exc:  # hardware/open failure → report, stay disconnected
            self.live_status.setText(f"Connect failed: {exc}")
            return
        self._live_ctl = ctl
        self.live_connect.setText("Disconnect")
        st = ctl.state()
        beams = f", {st.design_zones} beam(s)" if st.design_zones else ""
        mon = ", monitoring" if self.live_monitor.isChecked() and ctl.backend == "live" else ""
        self.live_status.setText(
            f"Connected ({ctl.backend}, {st.active_channels}/{st.n_channels} capsules{beams}{mon})."
        )

    def _octovox_connect(self):
        """Start the near-live OCTOVOX cleaned monitor (raw array → server → play)."""
        if not cc.octovox_deps_available():
            self.live_octovox_status.setText("Needs the [octovox] extra (requests + scipy + sounddevice).")
            return
        aid = self._live_array_id()
        if not aid:
            self.live_octovox_status.setText("Select a placed array first.")
            return
        client = cc.OctovoxClient(self.live_octovox_url.text().strip() or cc.OCTOVOX_DEFAULT_URL)
        if not client.is_up():
            self.live_octovox_status.setText(f"OCTOVOX server not reachable at {client.base_url}. Start it (run.py).")
            return
        # Only force a steering direction when the user opts in (and has calibrated
        # the azimuth offset). Otherwise let OCTOVOX auto-beamform — reliable on a
        # small / front-back-ambiguous array, and never nulls the voice.
        steer = self.live_octovox_steer.isChecked()
        za = cc.zone_azimuths(self.state.config, aid, azimuth_offset_deg=float(self.live_az_offset.value()))
        target_az = za.target_az if steer else None
        interferer_az = za.interferer_az if steer else None
        rate = self.live_rate.currentData() or 44100
        try:
            mon = cc.CleanMonitor(
                client,
                input_device=self.live_device.currentData(),
                samplerate=int(rate),
                chunk_seconds=float(self.live_chunk.value()),
                target_az=target_az,
                interferer_az=interferer_az,
                output_device=self.live_out_device.currentData(),
                nr="dfn",
                active=self._live_active_mask(),  # repair dead capsules for OCTOVOX
            )
            mon.start()
        except Exception as exc:
            self.live_octovox_status.setText(f"Could not start: {exc}")
            return
        self._clean_monitor = mon
        self.live_connect.setText("Disconnect")
        if steer:
            mode = f"steered to {za.target_az:.0f}°, {len(za.interferer_az)} excluded" if za.target_az is not None else "steered (no pickup zone)"
        else:
            mode = "auto-beam (OCTOVOX finds the voice)"
        self.live_status.setText(
            f"OCTOVOX cleaning live · {mode} · ~{self.live_chunk.value():.0f}s delay (headphones)."
        )
        self.live_octovox_status.setText(
            "Auto-beam: OCTOVOX locates the talker. Enable 'Steer to pickup zone' only after calibrating the azimuth offset."
            if not steer else (za.note or "Steering to the pickup-zone azimuth.")
        )

    def _autosteer_connect(self):
        """Start auto-steer: detect talkers by direction and follow those in the sector."""
        if not cc.controls_available():
            self.live_status.setText("Auto-steer needs the [control] extra (numpy + sounddevice).")
            return
        geom = self._live_geometry()
        rate = self.live_rate.currentData() or 44100
        sector = self._autosteer_sector()
        try:
            ctrl = cc.AutoSteerController(
                geom, sector,
                device=self.live_device.currentData(),
                samplerate=float(rate),
                max_talkers=int(self.live_max_talkers.value()),
                freq_hz=float(self.live_freq.value()),
                mode=self._live_mode(),
                loading=self._live_loading(),
                gate_when_empty=self.live_autosteer_gate.isChecked(),
                monitor=self.live_monitor.isChecked(),
                output_device=self.live_out_device.currentData(),
            )
            ctrl.ctrl.set_gain_db(float(self.live_gain.value()))
            ctrl.start()
        except Exception as exc:  # hardware/open failure → report, stay disconnected
            self.live_status.setText(f"Auto-steer connect failed: {exc}")
            return
        self._autosteer = ctrl
        self.live_connect.setText("Disconnect")
        # the gate owns muting while auto-steering; avoid a fight with the manual button
        self.live_mute.setEnabled(not self.live_autosteer_gate.isChecked())
        mon = ", monitoring" if self.live_monitor.isChecked() else ""
        self.live_status.setText(
            f"Auto-steer live · sector {sector.center_deg:.0f}° ±{sector.half_width_deg:.0f}° "
            f"· up to {int(self.live_max_talkers.value())} talker(s){mon} (headphones)."
        )

    def _tick_autosteer(self):
        """Update the meter + the detected-talker readout while auto-steering."""
        a = self._autosteer
        lvl = a.read_level()
        pct = 0 if lvl <= 1e-6 else int(max(0.0, min(100.0, (20.0 * math.log10(lvl) + 60.0) / 60.0 * 100.0)))
        self.live_meter.setValue(pct)
        dets = a.detections()
        if not dets:
            self.live_autosteer_view.setText("· listening — no talker detected ·")
        else:
            parts = [f"{'IN ' if d.in_sector else 'out'} {d.azimuth_deg:.0f}° ({d.salience_db:.0f}dB)" for d in dets]
            n_in = sum(1 for d in dets if d.in_sector)
            self.live_autosteer_view.setText(f"{n_in} in-area  |  " + "   ".join(parts))
        if a.error:
            self.live_status.setText(f"Auto-steer: {a.error[:60]}")

    def _live_disconnect(self):
        if self._autosteer is not None:
            try:
                self._autosteer.stop()
            finally:
                self._autosteer = None
            self.live_mute.setEnabled(True)
            self.live_autosteer_view.setText("Connect with auto-steer enabled to see detected talkers.")
        if self._clean_monitor is not None:
            try:
                self._clean_monitor.stop()
            finally:
                self._clean_monitor = None
        if self._live_ctl is not None:
            try:
                self._live_ctl.disconnect()
            finally:
                self._live_ctl = None
        self.live_connect.setText("Connect")
        self.live_meter.setValue(0)
        self.live_status.setText("Disconnected.")
        self._refresh_live()

    def _live_toggle_mute(self):
        muted = self.live_mute.isChecked()
        self.live_mute.setText("Muted" if muted else "Mute")
        ctl = self._active_ctl()
        if ctl is not None:
            ctl.set_mute(muted)

    def _live_gain_changed(self, v):
        self.live_gain_lbl.setText(f"{v} dB")
        ctl = self._active_ctl()
        if ctl is not None:
            ctl.set_gain_db(float(v))

    def _tick_live_meter(self):
        """Update the level meter on a dB scale (−60 dB → 0 %, 0 dB → 100 %), so
        normal speech picked up by a ceiling array is clearly visible rather than
        a sliver on a linear scale."""
        if self._autosteer is not None:
            self._tick_autosteer()
            return
        if self._clean_monitor is not None:
            st = self._clean_monitor.state()
            # meter shows playback buffer fill (0..~chunk seconds); status shows progress
            self.live_meter.setValue(int(min(1.0, st.buffered_s / max(0.5, self.live_chunk.value())) * 100))
            msg = f"OCTOVOX: {st.chunks_played} cleaned / {st.chunks_sent} sent"
            if st.gated:
                msg += f", {st.gated} silent-gated"
            if st.dropped:
                msg += f", {st.dropped} dropped"
            if st.last_elapsed_s:
                msg += f" · {st.last_elapsed_s:.1f}s/chunk"
            if st.error:
                msg += f" · ERROR: {st.error[:50]}"
            self.live_octovox_status.setText(msg)
            return
        if self._live_ctl is not None and self._live_ctl.connected:
            lvl = self._live_ctl.read_level()  # linear 0..1, post gain + mute
            if lvl <= 1e-6:
                pct = 0
            else:
                db = 20.0 * math.log10(lvl)
                pct = int(max(0.0, min(100.0, (db + 60.0) / 60.0 * 100.0)))
            self.live_meter.setValue(pct)
        elif self.live_meter.value() != 0:
            self.live_meter.setValue(0)

    # ------------------------------------------------------------- Simulate tab
    def _simulate_tab(self):
        w = QWidget()
        lay = QVBoxLayout(w)

        box = QGroupBox("Simulate placement")
        f = QFormLayout(box)
        self.sim_target = QComboBox()  # populated in _refresh_simulate
        f.addRow("Optimise for", self.sim_target)
        self.sim_grid = self._sim_spin(0.1, 2.0, 0.1, self.state.sim_params.grid_step_m)
        self.sim_grid.valueChanged.connect(lambda *_a: None if self._refreshing else self._schedule_heatmap())
        f.addRow("Grid step (m)", self.sim_grid)
        rr = QHBoxLayout()
        self.sim_rt60_auto = QCheckBox("Auto (Sabine)")
        self.sim_rt60_auto.setChecked(True)
        self.sim_rt60 = self._sim_spin(0.1, 2.5, 0.05, 0.6)
        self.sim_rt60.setEnabled(False)
        self.sim_rt60_auto.toggled.connect(self._rt60_auto_changed)
        self.sim_rt60.valueChanged.connect(lambda *_a: None if self._refreshing else self._schedule_heatmap())
        rr.addWidget(self.sim_rt60_auto)
        rr.addWidget(self.sim_rt60, 1)
        f.addRow("RT60 (s)", self._wrap(rr))
        lay.addWidget(box)

        wbox = QGroupBox("Objective weights")
        wl = QFormLayout(wbox)
        self.sim_w = {}
        for key, label, default in [("snr", "Direct SNR", 35), ("drr", "Direct / reverb", 25),
                                    ("cov", "Coverage", 25), ("fair", "Fairness", 15)]:
            sl = QSlider(Qt.Horizontal)
            sl.setRange(0, 100)
            sl.setValue(default)
            sl.valueChanged.connect(lambda *_a: None if self._refreshing else self._schedule_heatmap())
            self.sim_w[key] = sl
            wl.addRow(label, sl)
        lay.addWidget(wbox)

        self.sim_all_arrays = QCheckBox("Consider all arrays (talker served by best-covering array)")
        self.sim_all_arrays.setChecked(self.state.sim_params.consider_all_arrays)
        self.sim_all_arrays.toggled.connect(lambda *_a: None if self._refreshing else self._schedule_heatmap())
        lay.addWidget(self.sim_all_arrays)

        self.sim_seat_table = QCheckBox("Seat talkers at the table (pickup zones)")
        self.sim_seat_table.setChecked(self.state.sim_params.seat_in_pickup_zones)
        self.sim_seat_table.toggled.connect(lambda *_a: None if self._refreshing else self._schedule_heatmap())
        lay.addWidget(self.sim_seat_table)

        self.sim_heat_chk = QCheckBox("Show score heatmap (where to mount the array)")
        self.sim_heat_chk.toggled.connect(self._toggle_heatmap)
        lay.addWidget(self.sim_heat_chk)

        rec_btn = QPushButton("Recommend")
        rec_btn.setProperty("accent", "true")
        rec_btn.clicked.connect(self._run_recommend)
        lay.addWidget(rec_btn)

        res = QGroupBox("Recommendation")
        self.sim_results_layout = QVBoxLayout(res)
        lay.addWidget(res)

        self.sim_apply_btn = QPushButton("Apply to layout")
        self.sim_apply_btn.clicked.connect(self._apply_recommendation)
        lay.addWidget(self.sim_apply_btn)

        vrow = QHBoxLayout()
        self.sim_backend = QComboBox()
        self.sim_validate_btn = QPushButton("Validate top pick")
        self.sim_validate_btn.clicked.connect(self._run_validate)
        vrow.addWidget(self.sim_backend, 1)
        vrow.addWidget(self.sim_validate_btn)
        lay.addLayout(vrow)
        self.sim_backend_hint = QLabel("")
        self.sim_backend_hint.setWordWrap(True)
        self.sim_backend_hint.setStyleSheet("color: #9197ab;")
        lay.addWidget(self.sim_backend_hint)

        lay.addStretch(1)
        return w

    def _sim_spin(self, lo, hi, step, value):
        s = NoWheelDoubleSpinBox()
        s.setRange(lo, hi)
        s.setSingleStep(step)
        s.setDecimals(2)
        s.setValue(value)
        return s

    def _wrap(self, layout):
        box = QFrame()
        layout.setContentsMargins(0, 0, 0, 0)
        box.setLayout(layout)
        return box

    # ---- Simulate: params / targets ----
    def _sim_params(self):
        rt60 = None if self.sim_rt60_auto.isChecked() else self.sim_rt60.value()
        return cp.SimParams(
            grid_step_m=self.sim_grid.value(),
            rt60_s=rt60,
            w_snr=self.sim_w["snr"].value() / 100.0,
            w_drr=self.sim_w["drr"].value() / 100.0,
            w_coverage=self.sim_w["cov"].value() / 100.0,
            w_fairness=self.sim_w["fair"].value() / 100.0,
            consider_all_arrays=self.sim_all_arrays.isChecked(),
            seat_in_pickup_zones=self.sim_seat_table.isChecked(),
        )

    def _sim_array_id(self):
        arrays = [d for d in self.state.config.devices if d.type == "microphoneArray"]
        if not arrays:
            return None
        sel = self.state.selection
        if sel and sel.get("kind") == "device" and any(a.id == sel["id"] for a in arrays):
            return sel["id"]
        return arrays[0].id

    def _rt60_auto_changed(self, on):
        self.sim_rt60.setEnabled(not on)
        if not self._refreshing:
            self._schedule_heatmap()

    # ---- Simulate: actions ----
    def _run_recommend(self):
        aid = self._sim_array_id()
        if not aid:
            return self._toast("Add a microphone array first.")
        params = self._sim_params()
        target = self.sim_target.currentData()
        self.state.sim_params = params
        self.state.sim_target_id = target
        try:
            rec = cp.recommend_placement(self.state.config, aid, talker_id=target, params=params)
        except Exception as exc:
            return self._toast(str(exc))
        self.state.sim_recommendation = rec
        if self.state.sim_show_heatmap:
            self._recompute_heatmap()
        self.state.changed.emit()

    def _toggle_heatmap(self, on):
        self.state.sim_show_heatmap = bool(on)
        if on:
            self._schedule_heatmap()
        else:
            self.state.sim_heatmap = None
            self.state.changed.emit()

    def _schedule_heatmap(self):
        if self._heat_pending:
            return
        self._heat_pending = True
        QTimer.singleShot(60, self._do_heatmap)

    def _do_heatmap(self):
        self._heat_pending = False
        if self.state.sim_show_heatmap:
            self._recompute_heatmap()
            self.state.changed.emit()

    def _recompute_heatmap(self):
        aid = self._sim_array_id()
        if not aid:
            self.state.sim_heatmap = None
            return
        try:
            self.state.sim_heatmap = cp.score_heatmap(self.state.config, aid, params=self._sim_params())
        except Exception:
            self.state.sim_heatmap = None

    def _apply_recommendation(self):
        rec = self.state.sim_recommendation
        if not rec:
            return self._toast("Run a recommendation first.")
        cfg = self.state.config
        if cp.find_device(cfg, rec.array_id) is None:
            return self._toast("The recommended array no longer exists.")
        cfg = cp.set_device_position(cfg, rec.array_id, rec.array_pos)
        cfg = cp.set_device_elevation(cfg, rec.array_id, rec.array_elev)
        if rec.talker_id and rec.talker_pos and cp.find_talker(cfg, rec.talker_id) is not None:
            cfg = cp.set_talker_position(cfg, rec.talker_id, rec.talker_pos)
        self.state.set_config(cfg)  # one undo step
        self._toast("Applied recommendation.")

    def _run_validate(self):
        rec = self.state.sim_recommendation
        if not rec:
            return self._toast("Run a recommendation first.")
        if not cp.available_backends():
            return self._toast("No validation backend installed.")
        backend = self.sim_backend.currentData() or "auto"
        self.sim_validate_btn.setEnabled(False)
        self.sim_validate_btn.setText("Validating…")
        worker = _ValidateWorker(self.state.config, rec, self._sim_params(), backend)
        # pass the worker + the rec being validated back, so we can drop the
        # strong ref when done and only attach the result to *this* recommendation
        worker.signals.done.connect(lambda res, w=worker, r=rec: self._on_validate_done(res, w, r))
        worker.signals.failed.connect(lambda msg, w=worker: self._on_validate_failed(msg, w))
        self._validate_workers.add(worker)  # QThreadPool keeps no strong ref — we must
        QThreadPool.globalInstance().start(worker)

    def _on_validate_done(self, res, worker, validated_rec):
        self._validate_workers.discard(worker)
        if self.state.sim_recommendation is validated_rec:
            self.state.sim_recommendation = dataclasses.replace(validated_rec, validated=res)
            self.state.changed.emit()
            self._toast(f"Validated ({res.backend}): {res.predicted_snr_db:.1f} dB SNR")
        else:
            self._toast("Validation finished, but the recommendation changed — re-run Validate.")
        self._reset_validate_btn()

    def _on_validate_failed(self, msg, worker):
        self._validate_workers.discard(worker)
        self._reset_validate_btn()
        self._toast(f"Validation failed: {msg}")

    def _reset_validate_btn(self):
        busy = bool(self._validate_workers)
        self.sim_validate_btn.setText("Validating…" if busy else "Validate top pick")
        self.sim_validate_btn.setEnabled(
            not busy and bool(cp.available_backends()) and self.state.sim_recommendation is not None
        )

    def _toast(self, msg):
        w = self.window()
        if hasattr(w, "toast"):
            w.toast(msg)

    # ---- Simulate: refresh ----
    def _refresh_simulate(self):
        cur = self.sim_target.currentData()
        self.sim_target.clear()
        self.sim_target.addItem("(array only — all talkers)", None)
        for t in self.state.config.talkers:
            self.sim_target.addItem(t.label, t.id)
        idx = self.sim_target.findData(cur)
        self.sim_target.setCurrentIndex(idx if idx >= 0 else 0)

        backends = cp.available_backends()
        curb = self.sim_backend.currentData()
        self.sim_backend.clear()
        for b in backends:
            self.sim_backend.addItem(b, b)
        has_rec = self.state.sim_recommendation is not None
        if backends:
            bi = self.sim_backend.findData(curb)
            self.sim_backend.setCurrentIndex(bi if bi >= 0 else 0)
            self.sim_backend.setEnabled(True)
            self.sim_validate_btn.setEnabled(has_rec and not self._validate_workers)
            self.sim_backend_hint.setText("")
        else:
            self.sim_backend.setEnabled(False)
            self.sim_validate_btn.setEnabled(False)
            self.sim_backend_hint.setText(
                "Install a physics backend to validate:  pip install conferencing-audio-pipeline[sim]"
            )
        self.sim_apply_btn.setEnabled(has_rec)
        self._render_sim_results()

    def _render_sim_results(self):
        self._clear_layout(self.sim_results_layout)
        rec = self.state.sim_recommendation
        if rec is None:
            self.sim_results_layout.addWidget(QLabel("No recommendation yet — press Recommend."))
            return
        s = rec.score
        lines = [
            f"Array:  ({rec.array_pos.x:.2f}, {rec.array_pos.y:.2f}) m   ·   height {rec.array_elev:.2f} m",
            f"Steer:  {round(rec.steer_off_nadir_deg)}° off-nadir · az {round(rec.steer_az_deg)}°",
        ]
        if rec.talker_pos is not None:
            lines.append(f"Seat:   ({rec.talker_pos.x:.2f}, {rec.talker_pos.y:.2f}) m")
        lines.append(
            f"Score {s.total:.2f}  —  SNR {s.snr:.2f} · DRR {s.drr:.2f} · "
            f"cover {s.coverage:.2f} · fair {s.fairness:.2f}"
        )
        if not math.isnan(s.distance_m):
            extra = f"Distance {s.distance_m:.2f} m · off-axis {round(s.off_axis_deg)}°"
            if s.drr_db is not None:
                extra += f" · DRR {s.drr_db:.1f} dB"
            lines.append(extra)
        if rec.validated is not None:
            val = rec.validated
            drr = f" · DRR {val.predicted_drr_db:.1f} dB" if val.predicted_drr_db is not None else ""
            lines.append(f"Validated [{val.backend}]:  SNR {val.predicted_snr_db:.1f} dB{drr}")
        if rec.note:
            lines.append(f"Note: {rec.note}")
        for ln in lines:
            lbl = QLabel(ln)
            lbl.setWordWrap(True)
            self.sim_results_layout.addWidget(lbl)

    # --------------------------------------------------------------- actions
    def _add_device(self):
        dtype = self.dev_type.currentData()
        transport = self.dev_transport.currentText()
        did = self.state.next_device_id(dtype)
        label = f"{dtype} {did}"
        if dtype == "processor":
            dev = cp.create_processor(did, label)
        elif dtype == "microphoneArray":
            dev = cp.create_microphone_array(did, label, "automatic")
        elif dtype == "wirelessMic":
            dev = cp.create_wireless_mic(did, label, transport)
        elif dtype == "wiredMic":
            dev = cp.create_wired_mic(did, label, transport)
        elif dtype == "loudspeaker":
            dev = cp.create_loudspeaker(did, label, transport)
        else:
            dev = cp.create_codec(did, label, transport)
        cfg = cp.add_device(self.state.config, dev)
        cfg = cp.set_device_position(cfg, did, self._default_placement(cfg))
        self.state.selection = {"kind": "device", "id": did}
        self.state.set_config(cfg)

    def _default_placement(self, cfg):
        n = sum(1 for d in cfg.devices if d.position) + len(cfg.talkers)
        import math
        # spiral around the centre of the current bounds
        pts = [d.position for d in cfg.devices if d.position] + [t.position for t in cfg.talkers]
        if cfg.room:
            pts += cfg.room.vertices
        if pts:
            cx = sum(p.x for p in pts) / len(pts)
            cy = sum(p.y for p in pts) / len(pts)
        else:
            cx, cy = 6, 4.5
        ang = n * 1.35
        rad = 1.0 + n * 0.25
        return Point2D(round((cx + math.cos(ang) * rad) * 4) / 4, round((cy + math.sin(ang) * rad) * 4) / 4)

    def _remove_selected_device(self):
        sel = self.state.selection
        if sel and sel["kind"] == "device":
            self.state.set_config(cp.remove_device(self.state.config, sel["id"]))

    def _on_device_click(self, item):
        self.state.select({"kind": "device", "id": item.data(Qt.UserRole)})

    def _on_talker_click(self, item):
        self.state.select({"kind": "talker", "id": item.data(Qt.UserRole)})

    def _on_issue_click(self, item):
        refs = item.data(Qt.UserRole) or []
        dev = next((r for r in refs if any(d.id == r for d in self.state.config.devices)), None)
        if dev:
            self.state.select({"kind": "device", "id": dev})

    def _selected_array_id(self):
        sel = self.state.selection
        arrays = [d for d in self.state.config.devices if d.type == "microphoneArray"]
        if sel and sel.get("kind") == "device" and any(a.id == sel["id"] for a in arrays):
            return sel["id"]
        return arrays[0].id if arrays else None

    def _set_mode(self, mode):
        aid = self._selected_array_id()
        if aid:
            self.state.set_config(cp.set_coverage_mode(self.state.config, aid, mode))

    def _zone_kind_changed(self, idx):
        self.state.zone_kind = ["dynamic", "dedicated", "exclusion"][idx]

    def _add_zone_default(self):
        aid = self._selected_array_id()
        if not aid:
            return
        zid = self.state.next_zone_id(aid)
        from conf_pipeline.model import RectShape
        kind = self.state.zone_kind
        if kind == "dedicated":
            z = cp.dedicated_zone(zid, f"Always-on {zid}", Point2D(1, 1))
        elif kind == "exclusion":
            z = cp.exclusion_zone(zid, f"No-pickup {zid}", RectShape(origin=Point2D(1, 1), width=2, height=2))
        else:
            z = cp.dynamic_zone(zid, f"Records {zid}", RectShape(origin=Point2D(1, 1), width=2, height=2))
        self.state.set_config(cp.add_coverage_zone(self.state.config, aid, z))

    def _add_talker(self):
        tid = self.state.next_talker_id()
        cfg = cp.add_talker(self.state.config, cp.create_talker(tid, f"Talker {tid}", self._default_placement(self.state.config)))
        self.state.selection = {"kind": "talker", "id": tid}
        self.state.set_config(cfg)

    # --------------------------------------------------------------- refresh
    def _banner_link(self, href: str):
        """Banner hint links jump to a tab (tab:Issues) or trigger an action (act:optimize)."""
        if href.startswith("tab:"):
            name = href.split(":", 1)[1]
            for i in range(self.tabs.count()):
                if self.tabs.tabText(i) == name:
                    self.tabs.setCurrentIndex(i)
                    return
        elif href == "act:build":
            self.tabs.setCurrentIndex(0)

    def _refresh_banner(self, cfg, res):
        """One-line status + next-step hint above the tabs."""
        # Determine the most useful next step from the design state.
        arrays = [d for d in cfg.devices if d.type == "microphoneArray"]
        has_proc = any(d.type == "processor" for d in cfg.devices)
        if not res.ok:
            n = len(res.errors)
            self.banner.setText(
                f"<b style='color:#ff6b81'>✗ {n} error{'s' if n != 1 else ''}</b> — "
                f"<a href='tab:Issues' style='color:#85a0ff'>review in Issues ›</a>"
            )
            self.banner.setProperty("level", "error")
        else:
            # valid — surface the best next action
            hint = None
            if not arrays and not cfg.devices:
                hint = "Add a device in <a href='act:build' style='color:#85a0ff'>Build ›</a>"
            elif arrays and not has_proc:
                hint = "Add a processor (DSP) so AEC & the automixer have somewhere to live"
            elif has_proc and not cfg.routes:
                hint = "Run <b>Optimize room</b> or <b>Auto-Route</b> from the toolbar to wire it up"
            warn = len(res.warnings)
            base = "<b style='color:#3ddc97'>✓ Valid</b>"
            if warn:
                base += f" · <span style='color:#f7c948'>{warn} warning{'s' if warn != 1 else ''}</span> (<a href='tab:Issues' style='color:#85a0ff'>Issues ›</a>)"
            self.banner.setText(base + (f" — {hint}" if hint else ""))
            self.banner.setProperty("level", "ok" if not warn else "warn")
        self.banner.style().unpolish(self.banner)
        self.banner.style().polish(self.banner)

    def refresh(self):
        self._refreshing = True
        cfg = self.state.config
        # devices
        self.dev_list.clear()
        for d in cfg.devices:
            ins = sum(1 for p in d.ports if p.kind == "input")
            outs = sum(1 for p in d.ports if p.kind == "output")
            it = QListWidgetItem(f"{d.label}  ·  {d.type} · {d.id} · {ins}in/{outs}out")
            it.setData(Qt.UserRole, d.id)
            self.dev_list.addItem(it)
        # talkers
        self.talker_list.clear()
        arrays = [d for d in cfg.devices if d.type == "microphoneArray" and d.position]
        for t in cfg.talkers:
            cov = cp.talker_coverage(cfg, t.id)
            badge = "recorded" if cov.captured else ("excluded" if cov.excluded_by else "not covered")
            angs = []
            for a in arrays:
                ang = cp.array_to_talker_angles(cfg, a.id, t.id)
                if ang:
                    angs.append(f"{a.label} {round(ang.off_nadir_deg)}°·{ang.distance:.1f}m")
            ang_txt = "   ".join(angs) if angs else "(no placed array)"
            it = QListWidgetItem(f"{t.label}  [{badge}]\n   {ang_txt}")
            it.setData(Qt.UserRole, t.id)
            self.talker_list.addItem(it)
        # selection box
        self._refresh_selection()
        # dsp
        self._refresh_dsp()
        # issues
        res = cp.validate(cfg)
        self._refresh_banner(cfg, res)
        self.issue_badge.setText(("✓ Valid" if res.ok else f"✗ {len(res.errors)} error(s)") + f" · {len(res.warnings)} warning(s)")
        self.issue_list.clear()
        ic = ISSUE_COLORS[getattr(self.state, "theme", "dark")]
        for i in [*res.errors, *res.warnings]:
            it = QListWidgetItem(f"[{i.code}] {i.message}")
            it.setForeground(QColor(ic["error"] if i.severity == "error" else ic["warning"]))
            it.setData(Qt.UserRole, i.refs)
            self.issue_list.addItem(it)
        # coverage
        rep = cp.coverage_report(cfg)
        if not cfg.talkers:
            self.coverage_lbl.setText("No talkers placed.")
        else:
            label_of = {t.id: t.label for t in cfg.talkers}
            parts = [f"{len(rep.covered)}/{len(cfg.talkers)} talkers covered"]
            if rep.uncovered:
                parts.append("uncovered: " + ", ".join(label_of.get(t, t) for t in rep.uncovered))
            if rep.overlaps:
                parts.append(f"{len(rep.overlaps)} array overlap(s)")
            zrep = cp.zone_coverage_report(cfg)
            if zrep.zones:
                covered_areas = sum(1 for z in zrep.zones if z.centroid_covered)
                parts.append(f"{covered_areas}/{len(zrep.zones)} coverage area(s) in pickup")
                if zrep.contended:
                    parts.append(f"{len(zrep.contended)} area(s) contended")
            self.coverage_lbl.setText("  ·  ".join(parts))
        # routing
        s = cp.routing_summary(cfg)
        self.routing_summary_lbl.setText(f"{s['total']} route(s) · {s['dante']} Dante · {s['analog']} analog")
        self.routing_view.setPlainText(cp.signal_flow_report(cfg))
        # mute groups
        self._refresh_mute_groups(cfg)
        # json
        self.json_view.setPlainText(cp.serialize(cfg, pretty=True))
        # simulate
        self._refresh_simulate()
        # live array control
        self._refresh_live()
        self._refreshing = False

    def _clear_layout(self, lay):
        while lay.count():
            item = lay.takeAt(0)
            wdg = item.widget()
            if wdg is not None:
                wdg.setParent(None)
                wdg.deleteLater()
            else:
                child = item.layout()
                if child is not None:
                    self._clear_layout(child)  # recurse into nested form/row layouts

    def _refresh_selection(self):
        self._clear_layout(self.sel_layout)
        sel = self.state.selection
        cfg = self.state.config
        if not sel:
            self.sel_layout.addWidget(QLabel("Nothing selected."))
            return
        if sel["kind"] == "device":
            d = next((x for x in cfg.devices if x.id == sel["id"]), None)
            if not d:
                return
            self._device_props(d)
        elif sel["kind"] == "talker":
            t = next((x for x in cfg.talkers if x.id == sel["id"]), None)
            if not t:
                return
            self._talker_props(t)
        elif sel["kind"] == "zone":
            self.sel_layout.addWidget(QLabel(f"Zone {sel['zone_id']} on {sel['array_id']}"))
            self._zone_props(sel["array_id"], sel["zone_id"])
            rm = QPushButton("Delete zone")
            rm.clicked.connect(lambda: self.state.set_config(cp.remove_coverage_zone(cfg, sel["array_id"], sel["zone_id"])))
            self.sel_layout.addWidget(rm)

    def _zone_props(self, array_id, zone_id):
        """Per-coverage-area output channel + gain (Designer steerable coverage)."""
        cfg = self.state.config
        arr = next((d for d in cfg.devices if d.id == array_id), None)
        zone = next((z for z in arr.zones if z.id == zone_id), None) if arr else None
        if zone is None or zone.type == "exclusion":
            return  # exclusion zones carry no output channel
        form = QFormLayout()
        ch = QComboBox()
        ch.addItem("— (mixed only)", None)
        for i in range(1, cp.MAX_ZONES_PER_ARRAY + 1):
            ch.addItem(str(i), i)
        cur = 0 if zone.output_channel is None else zone.output_channel
        ch.setCurrentIndex(cur)

        def _set_channel(_idx):
            if self._refreshing:
                return
            val = ch.currentData()
            try:
                self.state.set_config(cp.set_zone_output_channel(self.state.config, array_id, zone_id, val))
            except cp.CoverageError as e:
                ch.setCurrentIndex(0 if zone.output_channel is None else zone.output_channel)
                self.coverage_lbl.setText(f"⚠ {e}")
        ch.currentIndexChanged.connect(_set_channel)
        form.addRow("Output channel", ch)

        gain = NoWheelDoubleSpinBox()
        gain.setRange(cp.ZONE_GAIN_DB_MIN, cp.ZONE_GAIN_DB_MAX)
        gain.setSingleStep(0.5)
        gain.setSuffix(" dB")
        gain.setValue(zone.gain_db if zone.gain_db is not None else 0.0)
        gain.valueChanged.connect(
            lambda v: None if self._refreshing else self.state.set_config(cp.set_zone_gain_db(self.state.config, array_id, zone_id, float(v)))
        )
        form.addRow("Area gain", gain)
        self.sel_layout.addLayout(form)

    def _device_props(self, d):
        form = QFormLayout()
        name = QLineEdit(d.label)
        name.editingFinished.connect(lambda: self.state.set_config(cp.rename_device(self.state.config, d.id, name.text() or d.id)))
        form.addRow("Label", name)
        if d.position:
            sx = self._spin(d.position.x, lambda v: self._set_pos(d.id, v, None))
            sy = self._spin(d.position.y, lambda v: self._set_pos(d.id, None, v))
            form.addRow("X (m)", sx)
            form.addRow("Y (m)", sy)
        from conf_pipeline.model import default_elevation
        elev = d.elevation if d.elevation is not None else default_elevation(d, self.state.config.room.height if self.state.config.room else 3.0)
        sz = self._spin(elev, lambda v: self.state.set_config(cp.set_device_elevation(self.state.config, d.id, v)))
        form.addRow("Z height (m)", sz)
        prof = QComboBox()
        applicable = [p for p in cp.DEVICE_PROFILES.values() if d.type in p.applies_to]
        ids = [p.id for p in applicable]
        if d.profile_id and d.profile_id not in ids:
            prof.addItem(f"{d.profile_id} (mismatch)", d.profile_id)
        for p in applicable:
            prof.addItem(p.label, p.id)
        i = prof.findData(d.profile_id)
        if i >= 0:
            prof.setCurrentIndex(i)
        prof.currentIndexChanged.connect(lambda *_a: None if self._refreshing else self.state.set_config(cp.assign_device_profile(self.state.config, d.id, prof.currentData())))
        form.addRow("Profile", prof)
        caps = cp.device_capabilities(d)
        captxt = " · ".join([s for s, on in [("AEC", caps.aec), ("automix", caps.automix), ("mute", caps.mute)] if on]) or "—"
        form.addRow("Caps", QLabel(captxt))
        self.sel_layout.addLayout(form)
        rm = QPushButton("Delete device")
        rm.clicked.connect(lambda: self.state.set_config(cp.remove_device(self.state.config, d.id)))
        self.sel_layout.addWidget(rm)

    def _set_pos(self, did, x, y):
        d = next(z for z in self.state.config.devices if z.id == did)
        nx = x if x is not None else d.position.x
        ny = y if y is not None else d.position.y
        self.state.set_config(cp.set_device_position(self.state.config, did, Point2D(nx, ny)))

    def _talker_props(self, t):
        form = QFormLayout()
        name = QLineEdit(t.label)
        name.editingFinished.connect(lambda: self.state.set_config(cp.rename_talker(self.state.config, t.id, name.text() or t.id)))
        form.addRow("Label", name)
        sx = self._spin(t.position.x, lambda v: self._set_tpos(t.id, v, None))
        sy = self._spin(t.position.y, lambda v: self._set_tpos(t.id, None, v))
        form.addRow("X (m)", sx)
        form.addRow("Y (m)", sy)
        sz = self._spin(cp.talker_elevation(t), lambda v: self.state.set_config(cp.set_talker_elevation(self.state.config, t.id, v)))
        form.addRow("Mouth Z (m)", sz)
        self.sel_layout.addLayout(form)
        cov = cp.talker_coverage(self.state.config, t.id)
        cap = "✓ recorded" if cov.captured else (f"excluded by {', '.join(cov.excluded_by)}" if cov.excluded_by else "not in any pickup zone")
        self.sel_layout.addWidget(QLabel(f"Capture: {cap}"))
        self.sel_layout.addWidget(QLabel("Steering angle from each array:"))
        for a in [d for d in self.state.config.devices if d.type == "microphoneArray"]:
            ang = cp.array_to_talker_angles(self.state.config, a.id, t.id)
            if ang:
                self.sel_layout.addWidget(QLabel(f"  {a.label}: {ang.distance:.2f} m · az {round(ang.azimuth_deg)}° · tilt {round(ang.downtilt_deg)}° · nadir {round(ang.off_nadir_deg)}°"))
            else:
                self.sel_layout.addWidget(QLabel(f"  {a.label}: array unplaced"))
        rm = QPushButton("Delete talker")
        rm.clicked.connect(lambda: self.state.set_config(cp.remove_talker(self.state.config, t.id)))
        self.sel_layout.addWidget(rm)

    def _spin(self, value, cb):
        s = NoWheelDoubleSpinBox()
        s.setRange(-1000, 1000)
        s.setSingleStep(0.1)
        s.setDecimals(2)
        s.setValue(value)
        s.valueChanged.connect(lambda v: None if self._refreshing else cb(v))
        return s

    def _set_tpos(self, tid, x, y):
        t = next(z for z in self.state.config.talkers if z.id == tid)
        nx = x if x is not None else t.position.x
        ny = y if y is not None else t.position.y
        self.state.set_config(cp.set_talker_position(self.state.config, tid, Point2D(nx, ny)))

    def _refresh_dsp(self):
        self._clear_layout(self.dsp_layout)
        cfg = self.state.config
        proc = cp.get_primary_processor(cfg)
        out_bus_ids = [b.id for b in proc.matrix.output_buses] if proc else []
        mics = [d for d in cfg.devices if cp.is_mic_device(d)]
        self.dsp_layout.addWidget(QLabel("AEC references"))
        if not mics:
            self.dsp_layout.addWidget(QLabel("No microphones."))
        for m in mics:
            box = QFrame()
            row = QHBoxLayout(box)
            chk = QCheckBox(m.label)
            chk.setChecked(m.aec.enabled)
            combo = QComboBox()
            combo.addItem("— none —", None)
            for bid in out_bus_ids:
                combo.addItem(bid, bid)
            if m.aec.reference_bus_id in out_bus_ids:
                combo.setCurrentText(m.aec.reference_bus_id)
            combo.setEnabled(m.aec.enabled)

            def commit(mic_id=m.id, chk=chk, combo=combo):
                if self._refreshing:
                    return
                self.state.set_config(cp.set_aec(self.state.config, mic_id, AecConfig(chk.isChecked(), combo.currentData())))

            chk.toggled.connect(lambda *_args, f=commit: f())
            combo.currentIndexChanged.connect(lambda *_args, f=commit: f())
            row.addWidget(chk)
            row.addWidget(combo, 1)
            self.dsp_layout.addWidget(box)
        if proc:
            self.dsp_layout.addWidget(QLabel("Automixer NLP"))
            nlp = QComboBox()
            nlp.addItems(list(cp.NLP_LEVELS))
            nlp.setCurrentText(cfg.automixer.nlp)
            nlp.currentTextChanged.connect(lambda v: None if self._refreshing else self.state.set_config(cp.configure_automixer(self.state.config, proc.id, cp.set_nlp(self.state.config.automixer, v))))
            self.dsp_layout.addWidget(nlp)
        self._dsp_blocks_section()
        self.dsp_layout.addStretch(1)

    def _dsp_blocks_section(self):
        sep = QFrame()
        sep.setFrameShape(QFrame.HLine)
        self.dsp_layout.addWidget(sep)
        self.dsp_layout.addWidget(QLabel("Processing blocks (selected device)"))
        sel = self.state.selection
        if not (sel and sel["kind"] == "device"):
            self.dsp_layout.addWidget(QLabel("Select a device (Build tab) to edit its chain."))
            return
        d = next((x for x in self.state.config.devices if x.id == sel["id"]), None)
        if not d:
            return
        caps = cp.device_capabilities(d)
        addrow = QFrame()
        h = QHBoxLayout(addrow)
        h.setContentsMargins(0, 0, 0, 0)
        kind = QComboBox()
        for k in caps.supported_blocks:
            kind.addItem(BLOCK_LABELS.get(k, k), k)
        btn = QPushButton("+ Add block")
        btn.clicked.connect(lambda _=0, did=d.id, kc=kind: self._add_block(did, kc.currentData()))
        h.addWidget(kind, 1)
        h.addWidget(btn)
        self.dsp_layout.addWidget(addrow)
        for b in (d.dsp_blocks or []):
            self.dsp_layout.addWidget(self._block_widget(d, b))

    def _add_block(self, device_id, kind):
        if not kind:
            return
        bid = f"{device_id}-{kind}-{self._dsp_seq}"
        self._dsp_seq += 1
        self.state.set_config(cp.add_dsp_block(self.state.config, device_id, cp.create_dsp_block(kind, bid)))

    def _block_widget(self, d, b):
        box = QFrame()
        box.setProperty("card", "true")
        v = QVBoxLayout(box)
        head = QHBoxLayout()
        en = QCheckBox(BLOCK_LABELS.get(b.kind, b.kind))
        en.setChecked(b.enabled)
        en.toggled.connect(lambda val, did=d.id, bid=b.id: None if self._refreshing else self.state.set_config(cp.set_dsp_block_enabled(self.state.config, did, bid, val)))
        head.addWidget(en)
        if d.type == "processor":
            tgt = QComboBox()
            tgt.addItem("whole device", None)
            for bus in d.buses:
                tgt.addItem(bus.id.replace(d.id + "-", ""), bus.id)
            ti = tgt.findData(b.target_bus_id)
            if ti >= 0:
                tgt.setCurrentIndex(ti)
            tgt.currentIndexChanged.connect(lambda *_a, did=d.id, bid=b.id, c=tgt: None if self._refreshing else self.state.set_config(cp.update_dsp_block(self.state.config, did, bid, {"target_bus_id": c.currentData()})))
            head.addWidget(tgt, 1)
        head.addStretch(1)
        rm = QPushButton("✕")
        rm.setMaximumWidth(30)
        rm.clicked.connect(lambda _=0, did=d.id, bid=b.id: self.state.set_config(cp.remove_dsp_block(self.state.config, did, bid)))
        head.addWidget(rm)
        v.addLayout(head)
        if b.kind == "mute":
            mute = QCheckBox("muted")
            mute.setChecked(bool(b.params.get("muted")))
            mute.toggled.connect(lambda val, did=d.id, bid=b.id: None if self._refreshing else self.state.set_config(cp.update_dsp_block(self.state.config, did, bid, {"params": {"muted": val}})))
            v.addWidget(mute)
        elif b.kind == "peq4":
            self._peq_editor(v, d, b)
        else:
            row = QHBoxLayout()
            for key, label, lo, hi, step in BLOCK_PARAM_SCHEMA.get(b.kind, []):
                row.addWidget(QLabel(label))
                sp = NoWheelDoubleSpinBox()
                sp.setRange(lo, hi)
                sp.setSingleStep(step)
                sp.setDecimals(2)
                sp.setValue(float(b.params.get(key, 0)))
                sp.valueChanged.connect(lambda val, did=d.id, bid=b.id, k=key: None if self._refreshing else self.state.set_config(cp.update_dsp_block(self.state.config, did, bid, {"params": {k: val}})))
                row.addWidget(sp)
            v.addLayout(row)
        return box

    def _peq_editor(self, parent, d, b):
        bands = b.params.get("bands", [])
        for i, band in enumerate(bands):
            row = QHBoxLayout()
            for key, lo, hi, dec in [("freqHz", 20, 20000, 0), ("gainDb", -15, 15, 1), ("q", 0.1, 10, 2)]:
                sp = NoWheelDoubleSpinBox()
                sp.setRange(lo, hi)
                sp.setDecimals(dec)
                sp.setValue(float(band.get(key, 0)))
                sp.valueChanged.connect(lambda val, idx=i, k=key, did=d.id, bid=b.id: self._set_band(did, bid, idx, k, val))
                row.addWidget(sp)
            tcombo = QComboBox()
            tcombo.addItems(list(cp.PEQ_BAND_TYPES))
            tcombo.setCurrentText(band.get("type", "bell"))
            tcombo.currentTextChanged.connect(lambda val, idx=i, did=d.id, bid=b.id: self._set_band(did, bid, idx, "type", val))
            row.addWidget(tcombo)
            rmb = QPushButton("✕")
            rmb.setMaximumWidth(28)
            rmb.clicked.connect(lambda _=0, idx=i, did=d.id, bid=b.id: self._remove_band(did, bid, idx))
            row.addWidget(rmb)
            parent.addLayout(row)
        if len(bands) < 4:
            addb = QPushButton("+ band")
            addb.clicked.connect(lambda _=0, did=d.id, bid=b.id: self._add_band(did, bid))
            parent.addWidget(addb)

    def _block(self, did, bid):
        d = next(x for x in self.state.config.devices if x.id == did)
        return next(x for x in d.dsp_blocks if x.id == bid)

    def _set_band(self, did, bid, idx, key, val):
        if self._refreshing:
            return
        bands = [dict(x) for x in self._block(did, bid).params.get("bands", [])]
        bands[idx][key] = val
        self.state.set_config(cp.update_dsp_block(self.state.config, did, bid, {"params": {"bands": bands}}))

    def _add_band(self, did, bid):
        bands = [dict(x) for x in self._block(did, bid).params.get("bands", [])]
        bands.append(cp.default_peq_band())
        self.state.set_config(cp.update_dsp_block(self.state.config, did, bid, {"params": {"bands": bands}}))

    def _remove_band(self, did, bid, idx):
        bands = [dict(x) for i, x in enumerate(self._block(did, bid).params.get("bands", [])) if i != idx]
        self.state.set_config(cp.update_dsp_block(self.state.config, did, bid, {"params": {"bands": bands}}))
