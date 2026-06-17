"""LIVE panel: drive a real array microphone with host-side beamforming.

The old Live tab's wall of controls, restructured into four collapsible cards
(Hardware / Beam / Auto-steer / OCTOVOX) above a pinned transport footer —
meter, Connect, Mute and gain never scroll out of reach. The pickup/exclusion
zones on the selected array are turned into beam weights (steer toward pickup,
null exclusions). With the array plugged in and the ``[control]`` extra
installed this runs live; otherwise a simulated controller keeps the UI usable.
"""
from __future__ import annotations

import math

from PySide6.QtCore import Qt, QThreadPool, QTimer
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QSlider,
    QVBoxLayout,
    QWidget,
)

import conf_pipeline as cp
import conf_pipeline_control as cc

from .common import (
    Card,
    LevelMeter,
    NoWheelDoubleSpinBox,
    NoWheelSpinBox,
    PanelBase,
    set_danger,
    _ABWorker,
    _CalibWorker,
    _ProbeWorker,
)


def _dominant_seat(config, array_id, detections):
    """Map the dominant live detection to a room seat (or ``None``).

    ``detections`` is an iterable of ``(azimuth_deg, salience_db, in_sector)`` in
    the array's own frame — the DOA frame ``current_doa_deg`` reports, exactly what
    :func:`conf_pipeline.nearest_seat_for_array` consumes. Prefers the loudest
    *in-sector* detection (the talker actually being followed); falls back to the
    loudest detection overall if none are flagged in-sector. Returns ``None`` when
    there are no detections, no session array, or the array has no room pose
    (``position`` + ``bearing_deg``) / no seat is close enough.
    """
    if array_id is None:
        return None
    pool = [d for d in detections if d[2]] or list(detections)
    loudest = None
    for az, sal, _in in pool:
        if az is not None and (loudest is None or sal > loudest[1]):
            loudest = (az, sal)
    if loudest is None:
        return None
    try:
        return cp.nearest_seat_for_array(config, array_id, loudest[0])
    except Exception:
        return None


class LivePanel(PanelBase):
    MODE = "live"
    TITLE = "Live"

    def __init__(self, state):
        super().__init__(state)
        # ---- live array-control state (host-side beamforming) ----
        self._live_ctl = None            # MicController while connected
        self._live_design = None         # last cc.BeamDesign built from zones
        self._live_dev_rates = {}        # device index -> native samplerate
        self._probe_workers = set()      # strong refs to capsule-probe runnables
        self._ab_workers = set()         # strong refs to A/B-test runnables
        self._calib_workers = set()      # strong refs to front-calibration runnables
        self._clean_monitor = None       # CleanMonitor while OCTOVOX cleaning is live
        self._autosteer = None           # AutoSteerController while auto-following talkers
        self._beam_engine = None         # BeamEngine while running the steered/grid A/B
        self._beameng_loc = None         # last BeamEngine current_location (for the overlay tick)
        self._session_array_id = None    # the array the running session was started with
        self._live_seat = None           # SeatMatch for the dominant talker (room-aware readout)
        self._beameng_locked_seat = None  # seat_id the steered beam is pinned to (snap-steer), or None
        self._beameng_locked_az = None    # the array-relative azimuth currently pinned (re-pushed if pose moves)
        self._beameng_locked_manual_az = None  # array-relative angle pinned by the manual dial / a map click, or None
        self._canvas = None               # injected by MainWindow so "click to aim" can arm the canvas click_cb

        root = QVBoxLayout(self)
        root.setContentsMargins(10, 8, 10, 8)
        root.setSpacing(6)
        root.addWidget(self._header())
        root.addWidget(self._scroll(self._build_cards()), 1)
        root.addWidget(self._build_transport())

        self._live_timer = QTimer(self)
        self._live_timer.setInterval(60)
        self._live_timer.timeout.connect(self._tick_live_meter)
        self._live_timer.start()
        self.refresh()

    # ------------------------------------------------------------------- cards
    def _build_cards(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setSpacing(8)

        self.live_avail_lbl = QLabel()
        self.live_avail_lbl.setWordWrap(True)
        lay.addWidget(self.live_avail_lbl)

        # --- HARDWARE: array, audio device, capsules ---
        hw = Card("Hardware — array & audio device")
        gf = QFormLayout()
        gf.setRowWrapPolicy(QFormLayout.WrapLongRows)
        gf.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)
        self.live_array = QComboBox()  # populated in refresh()
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
        self.live_device = QComboBox()
        self.live_device.currentIndexChanged.connect(lambda *_a: None if self._refreshing else self._on_live_device_changed())
        gf.addRow("Input", self.live_device)
        self.live_rate = QComboBox()
        for r in ("48000", "44100", "32000", "16000"):
            self.live_rate.addItem(f"{r} Hz", int(r))
        gf.addRow("Sample rate", self.live_rate)
        self.live_monitor = QCheckBox("Monitor output (use headphones)")
        self.live_monitor.setToolTip(
            "Play the beamformed output live. Use headphones — monitoring through "
            "room speakers will feed back into the array and howl."
        )
        gf.addRow("Monitor", self.live_monitor)
        self.live_out_device = QComboBox()
        gf.addRow("Output", self.live_out_device)
        hw.body_lay.addLayout(gf)

        cap_row = QHBoxLayout()
        cap_row.addWidget(QLabel("Capsules"))
        self.live_caps = []
        for i in range(8):
            cb = QCheckBox(str(i + 1))
            cb.setChecked(True)
            cb.toggled.connect(lambda *_a: None if self._refreshing else self._live_active_changed())
            self.live_caps.append(cb)
            cap_row.addWidget(cb)
        cap_row.addStretch(1)
        hw.body_lay.addLayout(cap_row)
        ctl_row = QHBoxLayout()
        self.live_detect = QPushButton("Detect silent capsules")
        self.live_detect.clicked.connect(self._live_detect_silent)
        self.live_active_lbl = QLabel("8/8 active")
        ctl_row.addWidget(self.live_detect)
        ctl_row.addWidget(self.live_active_lbl)
        ctl_row.addStretch(1)
        hw.body_lay.addLayout(ctl_row)
        lay.addWidget(hw)

        # --- BEAM: design + analysis ---
        beam = Card("Beam — directivity & zone design")
        bf = QFormLayout()
        bf.setRowWrapPolicy(QFormLayout.WrapLongRows)
        bf.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)
        self.live_freq = NoWheelDoubleSpinBox()
        self.live_freq.setRange(200.0, 8000.0)
        self.live_freq.setSingleStep(100.0)
        self.live_freq.setDecimals(0)
        self.live_freq.setValue(cc.DEFAULT_DESIGN_FREQ_HZ)
        self.live_freq.setSuffix(" Hz")
        bf.addRow("Design freq", self.live_freq)
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
        self.live_limits_info = QLabel("ⓘ  POLARIS limits")
        self.live_limits_info.setProperty("hintChip", "true")
        self.live_limits_info.setCursor(Qt.WhatsThisCursor)
        self.live_limits_info.setToolTip(
            "<b>POLARIS array — physical limits</b><br>"
            "• <b>Azimuth only</b> — a planar 8-mic ring; it steers left/right but cannot resolve elevation.<br>"
            "• <b>~5.6 kHz</b> spatial-aliasing ceiling (≈40 mm aperture); the beam grates above it.<br>"
            "• <b>Two talkers within ~40–50°</b> merge into one lobe — they can't be separated.<br>"
            "• <b>Front/back ambiguous</b> — one planar ring, so mirrored directions look alike to the DOA."
        )
        bf.addRow("Limits", self.live_limits_info)
        beam.body_lay.addLayout(bf)

        design_btn = QPushButton("Design beam from zones")
        design_btn.setProperty("accent", "true")
        design_btn.clicked.connect(self._live_design_from_zones)
        beam.body_lay.addWidget(design_btn)
        self.live_ab_btn = QPushButton("A/B test — record & compare beamformers")
        self.live_ab_btn.setToolTip(
            "Record a clip from the array, process it omni / delay-sum / superdirective / "
            "aggressive / nulled, and save mono WAVs + a dB report so you can hear and "
            "measure the difference."
        )
        self.live_ab_btn.clicked.connect(self._live_ab_test)
        beam.body_lay.addWidget(self.live_ab_btn)
        self.live_design_view = QPlainTextEdit()
        self.live_design_view.setReadOnly(True)
        self.live_design_view.setFont(QFont("Consolas", 9))
        self.live_design_view.setMaximumHeight(150)
        self.live_design_view.setPlaceholderText("No beam designed yet.")
        beam.body_lay.addWidget(self.live_design_view)
        lay.addWidget(beam)

        # --- AUTO-STEER: detect talkers by direction and follow a sector ---
        steer = Card("Auto-steer — follow talkers in a sector", collapsed=True)
        steer.setToolTip(
            "Detect who is talking by direction (DOA) in real time and steer a beam at "
            "each talker inside the coverage sector, nulling the ones outside. Best for "
            "a desk array: it adapts as people talk in turn or move. Azimuth only — a "
            "small array resolves bearing, not distance, so the area is an angular arc."
        )
        asf = QFormLayout()
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
        steer.body_lay.addLayout(asf)
        lay.addWidget(steer)
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

        # --- POLARIS A/B engine: steered vs grid on one shared stream ---
        eng = Card("POLARIS A/B beamformer — steered ↔ grid", collapsed=True)
        eng.setToolTip(
            "Drive the 8-channel POLARIS board through the unified BeamEngine and switch "
            "live between two strategies on ONE shared input stream: 'steered' (SRP-PHAT DOA "
            "+ a delay-and-sum beam at the dominant talker) and 'grid' (a fixed near-field "
            "virtual-mic grid, loudest selected). A strategy A/B, not a quality ranking — both "
            "share the 40 mm array's ~5–6 kHz limit. The tracked direction is drawn on the "
            "room map; no live monitoring yet."
        )
        ef = QFormLayout()
        ef.setRowWrapPolicy(QFormLayout.WrapLongRows)
        ef.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)
        self.live_beameng = QCheckBox("Use the A/B engine (one POLARIS board)")
        self.live_beameng.setToolTip(
            "On Connect, run BeamEngine instead of the zone-design / auto-steer / OCTOVOX paths."
        )
        self.live_beameng.toggled.connect(lambda *_a: None if self._refreshing else self._on_beameng_toggled())
        ef.addRow("A/B engine", self.live_beameng)
        self.live_beameng_mode = QComboBox()
        self.live_beameng_mode.addItem("Steered (DOA + beam)", "steered")
        self.live_beameng_mode.addItem("Grid (select loudest)", "grid")
        self.live_beameng_mode.setEnabled(False)        # enabled when the engine is ticked
        self.live_beameng_mode.currentIndexChanged.connect(
            lambda *_a: None if self._refreshing else self._on_beameng_mode_changed())
        ef.addRow("Strategy", self.live_beameng_mode)
        self.live_beameng_nullseats = QCheckBox("Null the other (empty) seats")
        self.live_beameng_nullseats.setToolTip(
            "While following the talker (steered), actively null the seats you are NOT listening to "
            "(the matched seat is kept). Needs the array's room bearing set (Design → array) and a "
            "superdirective steered beam — which this enables, so tick it before Connect."
        )
        self.live_beameng_nullseats.setEnabled(False)        # enabled when the engine is ticked
        ef.addRow("Seat nulling", self.live_beameng_nullseats)
        self.live_beameng_lockseat = QComboBox()
        self.live_beameng_lockseat.addItem("Follow talker (DOA)", None)
        self.live_beameng_lockseat.setToolTip(
            "Pin the steered beam to a chosen seat instead of following the loudest talker (snap-steer). "
            "Needs the array's room bearing set (Design → array); re-select 'Follow talker' to resume DOA."
        )
        self.live_beameng_lockseat.setEnabled(False)         # enabled while a connected steered engine runs
        self.live_beameng_lockseat.currentIndexChanged.connect(
            lambda *_a: None if self._refreshing else self._on_beameng_lockseat_changed())
        ef.addRow("Lock to seat", self.live_beameng_lockseat)
        self.live_beameng_angle = NoWheelDoubleSpinBox()
        self.live_beameng_angle.setRange(0.0, 360.0)
        self.live_beameng_angle.setWrapping(True)            # 360° wraps to 0° (a compass dial)
        self.live_beameng_angle.setSingleStep(5.0)
        self.live_beameng_angle.setSuffix("°")
        self.live_beameng_angle.setToolTip(
            "Pin the steered beam to a manual array-relative angle (0° = the array's reference, clockwise). "
            "Active when 'Lock to seat' is 'Manual angle' — or just click a spot on the 2D room map to aim "
            "(that fills this dial). A fixed angle: it does not follow the talker or re-resolve if the array moves."
        )
        self.live_beameng_angle.setEnabled(False)            # enabled when steered + 'Manual angle' is selected
        self.live_beameng_angle.valueChanged.connect(
            lambda v: None if self._refreshing else self._on_beameng_angle_changed(v))
        ef.addRow("Manual angle", self.live_beameng_angle)
        self.live_beameng_view = QLabel("Connect with the A/B engine to compare steered vs grid live.")
        self.live_beameng_view.setWordWrap(True)
        self.live_beameng_view.setFont(QFont("Consolas", 9))
        ef.addRow(self.live_beameng_view)
        eng.body_lay.addLayout(ef)
        lay.addWidget(eng)

        # --- OCTOVOX: near-live cleaned monitor ---
        ov = Card("Clean via OCTOVOX (near-live)", collapsed=True)
        ov.setToolTip(
            "Send rolling chunks of the raw array to a running OCTOVOX server "
            "(beamform + dereverb + DeepFilterNet3), steered by the zone azimuths, "
            "and play the cleaned result back. Delayed by ~chunk + processing; "
            "not real-time talkback."
        )
        ovf = QFormLayout()
        ovf.setRowWrapPolicy(QFormLayout.WrapLongRows)
        ovf.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)
        self.live_octovox = QCheckBox("Enable (use headphones)")
        self.live_octovox.toggled.connect(
            lambda on: None if self._refreshing else (on and self.live_beameng.setChecked(False)))
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
        ov.body_lay.addLayout(ovf)
        lay.addWidget(ov)

        lay.addStretch(1)

        # Stop combos from demanding their full content width (long OS device
        # names) — let them fill the column and elide instead of forcing the
        # whole panel wider.
        for combo in (self.live_array, self.live_device, self.live_rate, self.live_out_device,
                      self.live_mode, self.live_beameng_mode):
            combo.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
            combo.setMinimumContentsLength(6)
            combo.setMinimumWidth(80)
        self.live_robust_lbl.setText(f"{self._live_loading():.3f}")  # initial loading readout
        return w

    # --------------------------------------------------------------- transport
    def _build_transport(self) -> QFrame:
        bar = QFrame()
        bar.setProperty("transport", "true")
        v = QVBoxLayout(bar)
        v.setContentsMargins(10, 8, 10, 8)
        v.setSpacing(6)

        self.live_meter = LevelMeter()
        v.addWidget(self.live_meter)

        row = QHBoxLayout()
        self.live_connect = QPushButton("Connect")
        self.live_connect.setProperty("accent", "true")
        self.live_connect.clicked.connect(self._live_toggle_connect)
        self.live_mute = QPushButton("Mute")
        self.live_mute.setCheckable(True)
        self.live_mute.setToolTip("Mute the monitor playback. For the A/B engine this needs 'Monitor output "
                                  "(use headphones)' on — otherwise there's no playback to mute.")
        self.live_mute.clicked.connect(self._live_toggle_mute)
        row.addWidget(self.live_connect)
        row.addWidget(self.live_mute)
        row.addWidget(QLabel("Gain"))
        self.live_gain = QSlider(Qt.Horizontal)
        self.live_gain.setRange(-60, 24)
        self.live_gain.setValue(0)
        self.live_gain.setToolTip("Trim the monitor playback gain. For the A/B engine this needs 'Monitor "
                                  "output (use headphones)' on.")
        self.live_gain.valueChanged.connect(self._live_gain_changed)
        self.live_gain_lbl = QLabel("0 dB")
        row.addWidget(self.live_gain, 1)
        row.addWidget(self.live_gain_lbl)
        v.addLayout(row)

        self.live_status = QLabel("Disconnected.")
        self.live_status.setWordWrap(True)
        v.addWidget(self.live_status)
        return bar

    # ---- live helpers ----
    def _live_array_id(self):
        return self.live_array.currentData()

    def _live_busy(self):
        """True if any live session (beamformer, OCTOVOX, auto-steer, or A/B engine) is active."""
        return (self._live_ctl is not None or self._clean_monitor is not None
                or self._autosteer is not None or self._beam_engine is not None)

    def _active_ctl(self):
        """The active session's mute/gain control surface — the A/B engine (duck-typed:
        ``set_mute``/``set_gain_db``/``read_level``), the auto-steer controller, or the live zone
        controller; ``None`` if none is connected. Sessions are mutually exclusive."""
        if self._beam_engine is not None:
            return self._beam_engine
        if self._autosteer is not None:
            return self._autosteer.ctrl
        return self._live_ctl

    def _on_autosteer_toggled(self):
        """Enable the sector controls only when auto-steer is selected."""
        on = self.live_autosteer.isChecked()
        for w in self._autosteer_widgets:
            w.setEnabled(on)
        if on:                               # session modes are mutually exclusive
            self.live_beameng.setChecked(False)

    # ---- POLARIS A/B engine (BeamEngine: steered ↔ grid on one stream) ----
    def _beameng_mode(self):
        return self.live_beameng_mode.currentData() or "steered"

    def _on_beameng_toggled(self):
        """Enable the strategy picker when the engine is selected, and keep the
        session modes mutually exclusive."""
        on = self.live_beameng.isChecked()
        self.live_beameng_mode.setEnabled(on)
        self.live_beameng_nullseats.setEnabled(on)
        if on:
            self.live_autosteer.setChecked(False)
            self.live_octovox.setChecked(False)

    def _on_beameng_mode_changed(self):
        """Switch a running engine's strategy live (glitch-free crossfade); otherwise
        the picker just sets the mode the next Connect starts in."""
        e = self._beam_engine
        if e is None:
            return
        steered = self._beameng_mode() == "steered"
        self.live_beameng_lockseat.setEnabled(steered)   # snap-steer / manual angle only apply to the steered beam
        if not steered:
            self.live_beameng_angle.setEnabled(False)
        try:
            e.set_mode(self._beameng_mode())
            if steered and self.live_beameng_lockseat.currentData() is not None:
                self._on_beameng_lockseat_changed()   # re-pin seat / manual: set_mode's reset_transient cleared _steered_az
        except Exception as exc:
            self.live_status.setText(f"A/B switch failed: {exc}")

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
        # DOA reports 0..360°, but the Front-offset spin box is −180..180°, so a rear/left talker
        # (az > 180, common on this front/back-ambiguous ring) would clamp to 180 instead of being
        # applied. Wrap into (−180, 180]; the sector gate is wrap-aware, so it steers identically.
        off = int(((round(az) + 180) % 360) - 180)
        self.live_front_offset.setValue(off)
        self.live_status.setText(
            f"Front calibrated: heard at {az:.0f}° → front offset {off:+d}° ({sal:.0f} dB). "
            "Sector centre is now 'front'.")

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
            # broadband verification: DI / beamwidth as a function of frequency
            curves = cc.frequency_curves(design)
            if curves:
                text += "\n\n" + curves[0].table()
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
        if self.live_beameng.isChecked():
            self._beameng_connect()
            return
        if self.live_autosteer.isChecked():
            self._autosteer_connect()
            return
        if self.live_octovox.isChecked():
            self._octovox_connect()
            return
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
        self._session_array_id = self._live_array_id()
        self.live_connect.setText("Disconnect")
        st = ctl.state()
        beams = f", {st.design_zones} beam(s)" if st.design_zones else ""
        mon = ", monitoring" if self.live_monitor.isChecked() and ctl.backend == "live" else ""
        self.live_status.setText(
            f"Connected ({ctl.backend}, {st.active_channels}/{st.n_channels} capsules{beams}{mon})."
        )
        self._notify_session_changed()

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
        self._session_array_id = aid
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
        self._notify_session_changed()

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
        self._session_array_id = self._live_array_id()
        self.live_connect.setText("Disconnect")
        # the gate owns muting while auto-steering; avoid a fight with the manual button
        self.live_mute.setEnabled(not self.live_autosteer_gate.isChecked())
        mon = ", monitoring" if self.live_monitor.isChecked() else ""
        self.live_status.setText(
            f"Auto-steer live · sector {sector.center_deg:.0f}° ±{sector.half_width_deg:.0f}° "
            f"· up to {int(self.live_max_talkers.value())} talker(s){mon} (headphones)."
        )
        self._notify_session_changed()

    def _beameng_connect(self):
        """Start the BeamEngine A/B: steered + grid back-ends on one shared POLARIS stream."""
        if not cc.controls_available():
            self.live_status.setText("The A/B engine needs the [control] extra (numpy + sounddevice).")
            return
        rate = self.live_rate.currentData() or 44100
        mask = self._live_active_mask()
        cfg: dict = {"radius_m": float(self.live_radius.value())}
        if any(mask) and not all(mask):
            cfg["active_mask"] = list(mask)          # exclude the dead capsule on both back-ends
        steered_cfg = dict(cfg)
        if self.live_beameng_nullseats.isChecked():
            steered_cfg["mode"] = cc.MODE_SUPERDIRECTIVE   # nulls need a frequency-domain steered beam
        monitor_on = self.live_monitor.isChecked()
        try:
            eng = cc.BeamEngine(
                device=self.live_device.currentData(),
                fs=float(rate),
                mode=self._beameng_mode(),
                steered_cfg=steered_cfg,
                grid_cfg=dict(cfg),
                assumed_range_m=2.0,                 # gives steered mode an (x, y) too, for parity
                monitor=monitor_on,                  # play the A/B output on headphones (if ticked)
                output_device=self.live_out_device.currentData(),
            )
            eng.start()
        except Exception as exc:                     # hardware/open failure → report, stay disconnected
            self.live_status.setText(f"A/B engine connect failed: {exc}")
            return
        self._beam_engine = eng
        self._beameng_loc = None
        self._session_array_id = self._live_array_id()
        self.live_connect.setText("Disconnect")
        # Mute/Gain trim the monitor playback — only usable when monitoring is on.
        self.live_mute.setEnabled(monitor_on)
        self.live_gain.setEnabled(monitor_on)
        if monitor_on:                               # apply the current control state to the new engine
            eng.set_gain_db(float(self.live_gain.value()))
            eng.set_mute(self.live_mute.isChecked())
        self.live_beameng_nullseats.setEnabled(False)   # the steered beam mode is fixed at Connect
        self._refresh_beameng_lockseat()                # populate Follow / Manual angle / seats
        steered = self._beameng_mode() == "steered"
        self.live_beameng_lockseat.setEnabled(steered)  # snap-steer / manual angle only apply to the steered beam
        self.live_beameng_angle.setEnabled(False)       # enabled when 'Manual angle' is selected
        if self._canvas is not None:
            self._canvas.click_cb = self._on_canvas_click_live   # arm "click the map to aim"
        mon = "monitoring (headphones)" if monitor_on else "no monitor — tick Monitor for Mute/Gain"
        self.live_status.setText(
            f"A/B engine live · {self._beameng_mode()} · switch strategy from the picker ({mon})."
        )
        self._notify_session_changed()

    def _seat_suffix(self):
        """Trailing '· seat <id> (<sep>° off)' for the readouts, or '' if unmapped."""
        m = self._live_seat
        if m is None:
            return ""
        return f"   ·   seat {m.seat_id} ({m.separation_deg:.0f}° off)"

    def _refresh_beameng_lockseat(self):
        """Rebuild the Lock-to-seat combo: 'Follow talker' head, then 'Manual angle', then the room's seats.
        Resets the lock to follow. Signals are blocked so this never auto-steers."""
        c = self.live_beameng_lockseat
        c.blockSignals(True)
        c.clear()
        c.addItem("Follow talker (DOA)", None)
        c.addItem("Manual angle", "__manual__")     # pin to the angle dial / a clicked map point
        try:
            seats = cp.room_seats(self.state.config)
        except Exception:
            seats = []
        for seat_id, _anchor in seats:
            c.addItem(f"Seat {seat_id}", seat_id)
        c.blockSignals(False)
        self._beameng_locked_seat = None
        self._beameng_locked_manual_az = None

    def _on_beameng_lockseat_changed(self):
        """Pin the steered beam to the chosen seat (snap-steer), a manual angle, or resume DOA-follow on
        'Follow talker'. The angle dial is enabled only while 'Manual angle' is selected."""
        e = self._beam_engine
        if e is None:
            return
        data = self.live_beameng_lockseat.currentData()
        is_manual = data == "__manual__"
        self.live_beameng_angle.setEnabled(is_manual)
        if is_manual:                                         # pin to the manual dial (or a clicked map point)
            self._beameng_locked_seat = None
            self._beameng_locked_az = None
            az = float(self.live_beameng_angle.value())
            self._beameng_locked_manual_az = az
            try: e.set_steering(az)
            except Exception: pass
            return
        self._beameng_locked_manual_az = None                 # leaving manual: drop the angle lock
        seat_id = data                                        # None = follow, else a seat id
        az = None
        if seat_id is not None and self._session_array_id:
            try:
                az = cp.seat_azimuth_for_array(self.state.config, self._session_array_id, seat_id)
            except Exception:
                az = None
        if seat_id is not None and az is None:                # seat has no resolvable bearing → can't lock
            self.live_status.setText("Lock to seat needs the array's room bearing (Design → array).")
            self._beameng_locked_seat = None
            self._beameng_locked_az = None
            try: e.set_steering(None)                          # stay following the talker
            except Exception: pass
            return
        self._beameng_locked_seat = seat_id                   # None = follow, else the locked seat id
        self._beameng_locked_az = az                          # tracked so _push_locked_steering re-pins on pose change
        try:
            e.set_steering(az)                                # az None → resume DOA-follow; else pin the look
        except Exception:
            pass

    def _on_beameng_angle_changed(self, value):
        """Pin the steered beam to the manual angle dial. Only acts while 'Manual angle' is the selected
        lock (the dial is disabled otherwise), so seat / follow locks are never disturbed."""
        e = self._beam_engine
        if e is None or self.live_beameng_lockseat.currentData() != "__manual__":
            return
        az = float(value)
        self._beameng_locked_manual_az = az
        try: e.set_steering(az)
        except Exception: pass

    def _on_canvas_click_live(self, point) -> bool:
        """Canvas 'click to aim': turn a clicked room point into a manual lock on the steered beam, by
        seeding the angle dial + switching the lock selector to 'Manual angle', then driving the manual
        lock through the one handler. Returns True when it consumed the click (aimed), False so a click
        it can't act on still falls through to normal selection."""
        # The A/B session keeps running when the user leaves Live mode (the app only toasts), but the canvas
        # is shared across modes — so stay inert unless Live is the active view, or we'd hijack Design/etc. clicks.
        if getattr(self.state, "mode", None) != "live":
            return False
        e = self._beam_engine
        if e is None or self._beameng_mode() != "steered" or not self._session_array_id:
            return False
        try:
            az = cp.azimuth_for_array_point(self.state.config, self._session_array_id, point)
        except Exception:
            az = None
        if az is None:                                        # array has no position / room bearing
            self.live_status.setText("Click-to-aim needs the array's position + room bearing (Design → array).")
            return False
        c = self.live_beameng_lockseat
        prev = self._refreshing
        self._refreshing = True                               # seed the dial + combo silently...
        try:
            self.live_beameng_angle.setValue(round(az, 1))
            c.setCurrentIndex(c.findData("__manual__"))
        finally:
            self._refreshing = prev
        self._on_beameng_lockseat_changed()                   # ...then pin once via the manual branch (clears any seat lock)
        return True

    def _push_locked_steering(self):
        """Snap-steer upkeep: while locked (steered), re-resolve the seat's azimuth from the CURRENT
        config each tick and re-pin ONLY if it changed (e.g. the array's pose/bearing was edited in
        Design mid-session) — keeping the look consistent with the live seat-null geometry. No-op unless
        the angle actually moved (so no needless per-tick re-solve)."""
        e = self._beam_engine
        if (e is None or self._beameng_locked_seat is None or self._beameng_mode() != "steered"
                or not self._session_array_id):
            return
        try:
            az = cp.seat_azimuth_for_array(self.state.config, self._session_array_id, self._beameng_locked_seat)
        except Exception:
            az = None
        if az is not None and az != self._beameng_locked_az:
            self._beameng_locked_az = az
            try: e.set_steering(az)
            except Exception: pass

    def _manual_lock_seat_id(self):
        """When manual-angle-locked, the seat nearest our manual aim — so 'Null other seats' keeps OUR
        look (the seat we are aimed at) instead of nulling it. None when not manual-locked / no match."""
        if self._beameng_locked_manual_az is None or not self._session_array_id:
            return None
        try:
            m = cp.nearest_seat_for_array(self.state.config, self._session_array_id,
                                          self._beameng_locked_manual_az)
        except Exception:
            return None
        return m.seat_id if m is not None else None

    def _push_seat_nulls(self) -> int:
        """Room-aware seat nulling: while the engine runs steered, push the OTHER seats' bearings to the
        steered beam as nulls, keeping the TARGET seat (the locked seat if snap-steered, the seat nearest a
        manual aim, else the matched talker's seat). Returns the count pushed (0 = none / cleared); the
        beam's null-budget composer handles dedupe + the M−1 budget."""
        e = self._beam_engine
        if e is None:
            return 0
        target = self._beameng_locked_seat or self._manual_lock_seat_id() \
            or (self._live_seat.seat_id if self._live_seat is not None else None)
        az: list = []
        if (self.live_beameng_nullseats.isChecked() and self._beameng_mode() == "steered"
                and target is not None and self._session_array_id):
            try:
                az = cp.seat_null_azimuths(self.state.config, self._session_array_id,
                                           exclude_seat_id=target)
            except Exception:
                az = []
        try:
            e.set_nulls(az or None)
            return len(e.active_nulls)   # the count ACTUALLY applied (budget/look-filtered; 0 if not freq-domain)
        except Exception:
            return 0

    def _tick_beameng(self):
        """Update the meter + the location readout while the A/B engine runs."""
        e = self._beam_engine
        lvl = e.read_level()
        pct = 0 if lvl <= 1e-6 else int(max(0.0, min(100.0, (20.0 * math.log10(lvl) + 60.0) / 60.0 * 100.0)))
        self.live_meter.set_level(pct / 100.0)
        loc = e.current_location
        self._beameng_loc = loc                      # cached for _publish_overlay
        if loc.angle_deg is not None:                # room-aware: map the tracked bearing to a seat
            self._live_seat = _dominant_seat(self.state.config, self._session_array_id,
                                             [(loc.angle_deg, 1.0, True)])
        self._push_locked_steering()                 # snap-steer: re-pin the locked seat if its bearing moved
        n_null = self._push_seat_nulls()             # room-aware: null the other seats (if enabled)
        if loc.angle_deg is None and loc.xy is None:
            self.live_beameng_view.setText(f"[{loc.mode}] · listening — no source localized ·")
        else:
            ang = "  -- " if loc.angle_deg is None else f"{loc.angle_deg:5.0f}°"
            xy = "" if loc.xy is None else f"  ({loc.xy[0]:+.2f}, {loc.xy[1]:+.2f}) m"
            null_s = f"   ·   nulling {n_null} seat(s)" if n_null else ""
            if self._beameng_mode() != "steered":        # only the steered beam honours a lock
                lock_s = ""
            elif self._beameng_locked_seat:
                lock_s = f"   ·   locked → seat {self._beameng_locked_seat}"
            elif self._beameng_locked_manual_az is not None:
                lock_s = f"   ·   locked → {self._beameng_locked_manual_az:.0f}°"
            else:
                lock_s = ""
            self.live_beameng_view.setText(
                f"[{loc.mode}] {ang}{xy}  ·  conf {loc.confidence:.0%}{self._seat_suffix()}{lock_s}{null_s}")
        if e.error:
            self.live_status.setText(f"A/B engine: {e.error[:60]}")

    def _tick_autosteer(self):
        """Update the meter + the detected-talker readout while auto-steering."""
        a = self._autosteer
        lvl = a.read_level()
        pct = 0 if lvl <= 1e-6 else int(max(0.0, min(100.0, (20.0 * math.log10(lvl) + 60.0) / 60.0 * 100.0)))
        self.live_meter.set_level(pct / 100.0)
        dets = a.detections()
        self._live_seat = _dominant_seat(
            self.state.config, self._session_array_id,
            [(d.azimuth_deg, d.salience_db, d.in_sector) for d in dets],
        )
        if not dets:
            self.live_autosteer_view.setText("· listening — no talker detected ·")
        else:
            parts = [f"{'IN ' if d.in_sector else 'out'} {d.azimuth_deg:.0f}° ({d.salience_db:.0f}dB)" for d in dets]
            n_in = sum(1 for d in dets if d.in_sector)
            self.live_autosteer_view.setText(f"{n_in} in-area  |  " + "   ".join(parts) + self._seat_suffix())
        if a.error:
            self.live_status.setText(f"Auto-steer: {a.error[:60]}")

    def _live_disconnect(self):
        if self._beam_engine is not None:
            try:
                self._beam_engine.stop()
            finally:
                self._beam_engine = None
                self._beameng_loc = None
            self.live_mute.setEnabled(True)
            self.live_gain.setEnabled(True)
            self.live_beameng_nullseats.setEnabled(self.live_beameng.isChecked())   # re-enable for next Connect
            self.live_beameng_lockseat.setEnabled(False)        # snap-steer needs a running engine
            self.live_beameng_lockseat.blockSignals(True)
            self.live_beameng_lockseat.setCurrentIndex(0)       # back to "Follow talker"
            self.live_beameng_lockseat.blockSignals(False)
            self.live_beameng_angle.setEnabled(False)
            self.live_beameng_angle.blockSignals(True)
            self.live_beameng_angle.setValue(0.0)
            self.live_beameng_angle.blockSignals(False)
            self._beameng_locked_seat = None
            self._beameng_locked_az = None
            self._beameng_locked_manual_az = None
            if self._canvas is not None:
                self._canvas.click_cb = None                    # disarm "click to aim"
            self.live_beameng_view.setText("Connect with the A/B engine to compare steered vs grid live.")
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
        self._session_array_id = None
        self._live_seat = None
        self.live_connect.setText("Connect")
        self.live_meter.reset()
        self.live_status.setText("Disconnected.")
        self._notify_session_changed()
        self._publish_overlay()  # clear the canvas operations view promptly
        self.refresh()

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

    def _notify_session_changed(self):
        """Tell the shell (ModeBar live dot) the session state flipped, and mark the Connect/Disconnect
        button destructive while a session runs (it reads 'Disconnect' then)."""
        set_danger(self.live_connect, self._live_busy())
        w = self.window()
        if hasattr(w, "_live_session_changed"):
            w._live_session_changed(self._live_busy())

    def _publish_overlay(self):
        """Feed the canvas LIVE operations view (sector wedge, DOA rays, halo)."""
        if not self._live_busy():
            if self.state.live_overlay is not None:
                self.state.set_live_overlay(None)
            return
        sector = None
        detections = []
        if self._autosteer is not None:
            s = self._autosteer.sector
            sector = (s.center_deg, s.half_width_deg, s.front_offset_deg)
            try:
                detections = [(d.azimuth_deg, d.salience_db, d.in_sector) for d in self._autosteer.detections()]
            except Exception:
                detections = []
        elif self._beam_engine is not None and self._beameng_loc is not None:
            # one ray at the tracked bearing — steered DOA, or the grid's atan2(x, y) selection
            loc = self._beameng_loc
            if loc.angle_deg is not None:
                detections = [(loc.angle_deg, max(0.0, loc.confidence) * 12.0, True)]
        m = self._live_seat                          # resolved by the DOA ticks above
        seat = None if m is None else {"id": m.seat_id, "x": m.anchor.position.x, "y": m.anchor.position.y}
        # the array's room mounting heading: rotates the whole overlay (rays + sector) out of
        # the array's own frame into room coordinates, so the DOA rays agree with the seat ring
        # (and the seat dots on the map). 0 when unset → the overlay renders exactly as before.
        arr = next((d for d in self.state.config.devices if d.id == self._session_array_id), None)
        bearing = getattr(arr, "bearing_deg", None) or 0.0
        # the committed/locked steer (manual-angle dial or snap-steer seat) — drawn as a distinct
        # solid arrow, separate from the dashed talker DOA. None while following the talker (the DOA
        # rays already show that direction). Array-relative, lifted to room by `bearing` in the canvas.
        # Only the STEERED beam honours a lock — in grid mode the lock state persists (for the switch-back
        # re-pin) but the beam follows the loudest cell, so suppress the arrow to avoid a stale look.
        steer_az = None
        if self._beameng_mode() == "steered":
            steer_az = self._beameng_locked_manual_az
            if steer_az is None and self._beameng_locked_seat is not None:
                steer_az = self._beameng_locked_az
        self.state.set_live_overlay({
            # pinned at connect — the combo may show another room's arrays
            "array_id": self._session_array_id,
            "sector": sector,
            "detections": detections,
            "seat": seat,
            "bearing": bearing,
            "level": self.live_meter.level(),
            "steer_az": steer_az,
            "connected": True,
        })

    def _tick_live_meter(self):
        """Update the level meter on a dB scale (−60 dB → 0 %, 0 dB → 100 %), so
        normal speech picked up by a ceiling array is clearly visible rather than
        a sliver on a linear scale."""
        self._live_seat = None               # only the DOA paths below re-resolve a seat
        if self._beam_engine is not None:
            self._tick_beameng()
            self._publish_overlay()
            return
        if self._autosteer is not None:
            self._tick_autosteer()
            self._publish_overlay()
            return
        if self._clean_monitor is not None:
            st = self._clean_monitor.state()
            # meter shows playback buffer fill (0..~chunk seconds); status shows progress
            self.live_meter.set_level(min(1.0, st.buffered_s / max(0.5, self.live_chunk.value())), meter=False)
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
            self._publish_overlay()
            return
        if self._live_ctl is not None and self._live_ctl.connected:
            lvl = self._live_ctl.read_level()  # linear 0..1, post gain + mute
            if lvl <= 1e-6:
                pct = 0
            else:
                db = 20.0 * math.log10(lvl)
                pct = int(max(0.0, min(100.0, (db + 60.0) / 60.0 * 100.0)))
            self.live_meter.set_level(pct / 100.0)
        elif self.live_meter.level() != 0:
            self.live_meter.reset()
        self._publish_overlay()

    # --------------------------------------------------------------- refresh
    def refresh(self):
        super().refresh()
        self._refreshing = True
        try:
            # availability banner
            if cc.controls_available():
                self.live_avail_lbl.setText("Live audio ready (numpy + sounddevice detected).")
            else:
                self.live_avail_lbl.setText(
                    "Live audio backend not installed — running in simulation. "
                    "Install with:  pip install -e \".[control]\""
                )
            # pickers rebuild only when idle; a running session keeps its combos
            # stable (the room switcher may have swapped in another room's devices)
            if not self._live_busy():
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
                if self.live_device.currentData() != curd:
                    self._on_live_device_changed()  # device changed — match its native rate
                # (an unchanged device keeps the user's manually chosen rate)

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
        finally:
            self._refreshing = False
