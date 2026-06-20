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

from PySide6.QtCore import QSettings, Qt, QThreadPool, QTimer
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

# "Cleaning amount" per Strength step (Gentle / Medium / Aggressive index → post_nr_amount): how much of the
# cleaner is applied vs the original voice blended back. Lower = gentler / less muffling. The shared
# level-preserving makeup (always on) keeps the voice from coming out weak at any setting.
_CLEANING_AMOUNTS = (0.5, 0.75, 1.0)
_CLEANING_AMOUNT_DEFAULT = _CLEANING_AMOUNTS[1]    # Medium


def _clean_amount(depth_combo) -> float:
    """Map a Strength combo (Gentle / Medium / Aggressive) to a post_nr cleaning amount."""
    return _CLEANING_AMOUNTS[max(0, min(len(_CLEANING_AMOUNTS) - 1, depth_combo.currentIndex()))]

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


class _ArrayRow:
    """The widgets of one array row in the Hardware card — a 'Use' checkbox (is this array in the
    session?), that array's OWN input device, and a 'device gone' flag. One row per configured
    microphone array: tick one → a single-array session, tick several → a combined room capture."""

    __slots__ = ("widget", "array_id", "use", "device", "missing")

    def __init__(self, widget, array_id, use, device, missing):
        self.widget, self.array_id, self.use = widget, array_id, use
        self.device, self.missing = device, missing


class LivePanel(PanelBase):
    MODE = "live"
    TITLE = "Live"

    def __init__(self, state):
        super().__init__(state)
        # ---- live array-control state (host-side beamforming) ----
        self._live_ctl = None            # MicController while connected
        self._live_design = None         # last cc.BeamDesign built from zones
        self._live_dev_rates = {}        # device index -> native samplerate
        self._array_device = {}          # array_id -> chosen input-device index (each array keeps its own)
        self._probe_workers = set()      # strong refs to capsule-probe runnables
        self._ab_workers = set()         # strong refs to A/B-test runnables
        self._calib_workers = set()      # strong refs to front-calibration runnables
        self._clean_monitor = None       # CleanMonitor while OCTOVOX cleaning is live
        self._autosteer = None           # AutoSteerController while auto-following talkers
        self._beam_engine = None         # BeamEngine while running the steered/grid A/B
        self._twokit = None              # MultiKitController while running the dual-POLARIS automix
        self._multibeam = None           # MultiBeamController while capturing everyone (multi-talker automix)
        self._multibeam_rec = None       # MultiTrackRecorder while "Record tracks" is armed
        self._multiroom = None           # MultiRoomController while combining ≥2 arrays into one room capture
        self._array_rows = []            # per-array Hardware rows ([Use] + the array's own input device), one per config array
        self._array_use = {}             # array_id -> bool: is this array enabled for the session (persisted across refresh)
        self._beameng_loc = None         # last BeamEngine current_location (for the overlay tick)
        self._ab_cap = None              # armed ABCapture during a running A/B proof, or None
        self._ab_obj = None              # the live object (engine/autosteer/ctrl) the A/B capture is on
        self._ab_last = None             # last finalized ABProofResult (for the commissioning report)
        self._caps_probed = False        # True once Detect-silent has measured the capsules this session
        # --- first-run setup-guide state (the checklist banner reasons over these) ---
        self._listening_mode_touched = False  # user explicitly picked a listening mode (not the default)
        self._front_calibrated = False        # Calibrate-front succeeded (NOT inferred from the offset value)
        self._heard_ack = False               # manual "Got it, I can hear it" fallback
        self._guide_dismissed = False         # hidden for this session (does not set the persistent flag)
        self._guide_autoshown = False         # already auto-revealed once this session
        self._guide_done_persisted = False    # the QSettings done-flag has been written this session
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

        lay.addWidget(self._build_guide_banner())   # first-run checklist (hidden until first LIVE entry)

        # --- LISTENING MODE: one high-level selector that drives the live mode + sensible defaults and
        # collapses the irrelevant cards ("invisible by default"). "Manual (advanced)" reveals every card.
        # Default "Whole table" maps to today's zone default, so it changes nothing until the user picks. ---
        lm_row = QHBoxLayout()
        self.live_listening_mode = QComboBox()
        self.live_listening_mode.addItem("Follow the room (auto-steer)", "follow")
        self.live_listening_mode.addItem("Lock to a seat", "seat")
        self.live_listening_mode.addItem("Whole table", "table")
        self.live_listening_mode.addItem("Clean audio (hands-off)", "clean")
        self.live_listening_mode.addItem("Manual (advanced)", "manual")
        self.live_listening_mode.addItem("Two kits (combined room)", "twokit")
        self.live_listening_mode.addItem("Capture everyone (all talkers)", "multibeam")
        self.live_listening_mode.setToolTip(
            "Pick how the room is heard; the panel selects the right engine + sensible defaults and hides the "
            "rest. 'Clean audio (hands-off)' follows talkers and turns on AI voice cleaning. "
            "'Manual (advanced)' shows every control. Choose before Connect."
        )
        self.live_listening_mode.setCurrentIndex(2)          # "Whole table" = today's default (no behaviour change)
        self.live_listening_mode.currentIndexChanged.connect(
            lambda *_a: None if self._refreshing else self._on_listening_mode_changed())
        lm_row.addWidget(QLabel("Listening mode"))
        lm_row.addWidget(self.live_listening_mode, 1)
        lay.addLayout(lm_row)

        # --- HARDWARE: arrays (per-array Use + device), audio settings, capsules ---
        hw = Card("Hardware — arrays & audio device")
        # ARRAYS: one row per configured microphone array — [Use] + that array's OWN input device.
        # Tick one → a single-array session; tick two or more → a combined room capture (populated in refresh()).
        self._arrays_section = QWidget()
        asx = QVBoxLayout(self._arrays_section)
        asx.setContentsMargins(0, 0, 0, 0)
        as_intro = QLabel("Arrays — tick the ones to use. One ticked = single-array session; two or more "
                          "= a combined room capture (each its OWN input device; each seat is handled by "
                          "its nearest array).")
        as_intro.setWordWrap(True)
        asx.addWidget(as_intro)
        self._array_rows_lay = QVBoxLayout()
        asx.addLayout(self._array_rows_lay)
        self.live_arrays_status = QLabel("")
        self.live_arrays_status.setWordWrap(True)
        asx.addWidget(self.live_arrays_status)
        hw.body_lay.addWidget(self._arrays_section)
        gf = QFormLayout()
        gf.setRowWrapPolicy(QFormLayout.WrapLongRows)
        gf.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)
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

        # --- MIC INPUT: software level trim before the beamformer ---
        mic = Card("Mic input — level trim")
        mf = QFormLayout()
        mf.setRowWrapPolicy(QFormLayout.WrapLongRows)
        mf.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)
        self.live_preamp_gain = NoWheelDoubleSpinBox()
        self.live_preamp_gain.setRange(-24.0, 24.0)          # software range; control clamps to ±(60, 24) dB
        self.live_preamp_gain.setSingleStep(0.5)
        self.live_preamp_gain.setDecimals(1)
        self.live_preamp_gain.setValue(0.0)
        self.live_preamp_gain.setSuffix(" dB")
        self.live_preamp_gain.setToolTip(
            "Manual gain on the mic input, applied uniformly to all capsules before the beamformer. A "
            "software level trim (for input metering / a healthy operating level) — it does NOT improve "
            "SNR: it scales signal and noise together and the output AGC re-levels it. This POLARIS "
            "exposes no boostable hardware gain (its capture volume only attenuates). 0 dB = off."
        )
        self.live_preamp_gain.valueChanged.connect(
            lambda v: None if self._refreshing else self._on_preamp_gain_changed(v))
        mf.addRow("Input gain", self.live_preamp_gain)
        mic.body_lay.addLayout(mf)
        lay.addWidget(mic)

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
        self.live_autosteer_clean = QComboBox()              # OCTOVOX voice cleaning on the auto-steer output
        self.live_autosteer_clean.addItem("Off", None)
        self.live_autosteer_clean.addItem("AI voice cleaning (OM-LSA)", "omlsa")
        self.live_autosteer_clean.addItem("DeepFilterNet3 (AI, ~60 ms)", "dfn3")
        self.live_autosteer_clean.addItem("Light gate (fast)", "gate")
        self.live_autosteer_clean.setCurrentIndex(0)         # opt-in (Off by default, like the A/B engine)
        self.live_autosteer_clean.setToolTip(
            "Clean the followed talker's voice on the auto-steer output: suppress steady background noise "
            "(fans / AC / HVAC) learned by minimum statistics — no silence needed — without muting speech. "
            "'OCTOVOX cleaner' is the decision-directed OM-LSA denoiser ported from OCTOVOX (more natural, "
            "better on non-stationary noise); 'Light gate' is the lighter spectral gate. Fixed at Connect."
        )
        self.live_autosteer_clean.setEnabled(False)          # enabled when auto-steer is ticked (pre-connect)
        asf.addRow("Clean voice", self.live_autosteer_clean)
        self.live_autosteer_depth = QComboBox()              # (post_nr_floor_db, post_nr_oversub)
        self.live_autosteer_depth.addItem("Gentle", (-9.0, 1.2))
        self.live_autosteer_depth.addItem("Medium", (-15.0, 1.5))
        self.live_autosteer_depth.addItem("Aggressive", (-22.0, 2.0))
        self.live_autosteer_depth.setCurrentIndex(1)         # Medium
        self.live_autosteer_depth.setToolTip(
            "How much cleaning is applied. Aggressive cuts deepest; Gentle/Medium blend more of the original "
            "voice back in so it stays natural (less muffled). The voice level is preserved automatically at "
            "every setting, so cleaning never makes you sound weak. Only applies when 'Clean voice' is on."
        )
        self.live_autosteer_depth.setEnabled(False)
        asf.addRow("Strength", self.live_autosteer_depth)
        self.live_autosteer_dereverb = QCheckBox("Reduce room echo (dereverb)")
        self.live_autosteer_dereverb.setToolTip(
            "Real-time dereverberation on the followed talker: suppress the late-reverberation tail (room "
            "echo) so the voice sounds closer and drier. Runs before the cleaner. Fixed at Connect."
        )
        self.live_autosteer_dereverb.setEnabled(False)       # enabled when auto-steer is ticked (pre-connect)
        asf.addRow("Dereverb", self.live_autosteer_dereverb)
        self.live_autosteer_aec = QCheckBox("Cancel echo (needs far-end playout)")
        self.live_autosteer_aec.setToolTip(
            "Cancel the room's loudspeaker echo from the followed talker using the PC's playback (the far-end "
            "/ Zoom-Teams downlink) as the reference — captured automatically via WASAPI loopback or Stereo "
            "Mix. Only helps when the room plays far-end audio through speakers; otherwise it's a no-op. "
            "Fixed at Connect."
        )
        self.live_autosteer_aec.setEnabled(False)
        asf.addRow("Echo cancel", self.live_autosteer_aec)
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
        self.live_beameng_postnr = QCheckBox("Suppress steady noise (fans/AC)")
        self.live_beameng_postnr.setToolTip(
            "Reduce steady background noise (fans, AC, HVAC hum) on the beam output: it continuously learns "
            "the noise floor by minimum statistics — no silence needed — and attenuates it without muting "
            "(speech sits above the learned floor). Pick the engine with 'Cleaner' below. Fixed at Connect, "
            "so tick it before connecting."
        )
        self.live_beameng_postnr.setEnabled(False)           # enabled when the engine is ticked
        ef.addRow("Noise gate", self.live_beameng_postnr)
        self.live_beameng_nr_depth = QComboBox()             # (post_nr_floor_db, post_nr_oversub)
        self.live_beameng_nr_depth.addItem("Gentle", (-9.0, 1.2))
        self.live_beameng_nr_depth.addItem("Medium", (-15.0, 1.5))
        self.live_beameng_nr_depth.addItem("Aggressive", (-22.0, 2.0))
        self.live_beameng_nr_depth.setCurrentIndex(1)        # Medium (= the engine default)
        self.live_beameng_nr_depth.setToolTip(
            "How much cleaning is applied. Aggressive cuts the fan/AC deepest; Gentle/Medium blend more of the "
            "original voice back in so it stays natural (less muffled). The voice level is preserved "
            "automatically at every setting, so cleaning never makes you sound weak. Only applies when "
            "'Suppress steady noise' is on."
        )
        self.live_beameng_nr_depth.setEnabled(False)         # enabled when the engine is ticked
        ef.addRow("Noise depth", self.live_beameng_nr_depth)
        self.live_beameng_nr_engine = QComboBox()            # post_nr engine: AI cleaner (OM-LSA) vs the light gate
        self.live_beameng_nr_engine.addItem("AI voice cleaning (OM-LSA)", "omlsa")
        self.live_beameng_nr_engine.addItem("DeepFilterNet3 (AI, ~60 ms)", "dfn3")
        self.live_beameng_nr_engine.addItem("Light gate (fast)", "gate")
        self.live_beameng_nr_engine.setCurrentIndex(0)       # default: the OCTOVOX-derived decision-directed cleaner
        self.live_beameng_nr_engine.setToolTip(
            "Which noise reducer runs on the beam output. 'OCTOVOX cleaner' is the decision-directed OM-LSA "
            "denoiser ported from OCTOVOX (more natural, better on non-stationary noise); 'Light gate' is the "
            "lighter single-pole spectral gate. Only applies when 'Suppress steady noise' is on. Fixed at Connect."
        )
        self.live_beameng_nr_engine.setEnabled(False)        # enabled when the engine is ticked
        ef.addRow("Cleaner", self.live_beameng_nr_engine)
        self.live_beameng_dereverb = QCheckBox("Reduce room echo (dereverb)")
        self.live_beameng_dereverb.setToolTip(
            "Real-time dereverberation on the beam output: suppress the late-reverberation tail (room echo) "
            "so the voice sounds closer and drier. Runs before the noise reducer. Fixed at Connect."
        )
        self.live_beameng_dereverb.setEnabled(False)         # enabled when the engine is ticked
        ef.addRow("Dereverb", self.live_beameng_dereverb)
        self.live_beameng_aec = QCheckBox("Cancel echo (needs far-end playout)")
        self.live_beameng_aec.setToolTip(
            "Cancel the room's loudspeaker echo from the beam output using the PC's playback (the far-end / "
            "Zoom-Teams downlink) as the reference — captured automatically via WASAPI loopback or Stereo Mix. "
            "Only helps when the room plays far-end audio through speakers; otherwise it's a no-op. Fixed at "
            "Connect."
        )
        self.live_beameng_aec.setEnabled(False)              # enabled when the engine is ticked
        ef.addRow("Echo cancel", self.live_beameng_aec)
        self.live_beameng_adaptnull = QCheckBox("Adaptive null (learn room noise)")
        self.live_beameng_adaptnull.setToolTip(
            "Make the steered beam data-adaptive (MVDR): measure the room's noise field during pauses and "
            "steer a null onto it (a directional fan / projector / duct), plus auto-null detected "
            "interferers. Falls back to superdirective during speech. Fixed at Connect — tick before connecting."
        )
        self.live_beameng_adaptnull.setEnabled(False)        # enabled when the engine is ticked
        ef.addRow("Adaptive null", self.live_beameng_adaptnull)
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

        # --- TWO KITS: dual-POLARIS combined-room automix (one output, follows the active talker) ---
        twokit = Card("Two kits — combined room coverage")
        intro = QLabel("Two POLARIS kits cover one room. The app outputs whichever kit currently has the "
                       "talker (one stream — not two people at once) and cross-fades on the hand-off. "
                       "Each kit needs its OWN input device.")
        intro.setWordWrap(True)
        twokit.body_lay.addWidget(intro)
        tkf = QFormLayout()
        tkf.setRowWrapPolicy(QFormLayout.WrapLongRows)
        tkf.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)
        self.live_twokit_dev_a = QComboBox()
        tkf.addRow("Kit A input", self.live_twokit_dev_a)
        self.live_twokit_arr_a = QComboBox()
        tkf.addRow("Kit A array", self.live_twokit_arr_a)
        self.live_twokit_meter_a = LevelMeter()
        tkf.addRow("Kit A level", self.live_twokit_meter_a)
        self.live_twokit_dev_b = QComboBox()
        tkf.addRow("Kit B input", self.live_twokit_dev_b)
        self.live_twokit_arr_b = QComboBox()
        tkf.addRow("Kit B array", self.live_twokit_arr_b)
        self.live_twokit_meter_b = LevelMeter()
        tkf.addRow("Kit B level", self.live_twokit_meter_b)
        self.live_twokit_out = QComboBox()
        tkf.addRow("Output (headphones)", self.live_twokit_out)
        self.live_twokit_clean = QComboBox()
        self.live_twokit_clean.addItem("Off", None)
        self.live_twokit_clean.addItem("AI voice cleaning (OM-LSA)", "omlsa")
        self.live_twokit_clean.addItem("DeepFilterNet3 (AI, ~60 ms)", "dfn3")
        self.live_twokit_clean.addItem("Light gate (fast)", "gate")
        self.live_twokit_clean.setToolTip("Per-kit voice cleaning (fans/AC) on each kit's stream; applied to "
                                          "both. The selected kit is what you hear.")
        tkf.addRow("Clean voice", self.live_twokit_clean)
        self.live_twokit_agc = QCheckBox("Normalize output loudness (AGC)")
        self.live_twokit_agc.setToolTip("One target-loudness AGC on the combined output so a near vs far "
                                        "talker land at a consistent level.")
        tkf.addRow("Loudness", self.live_twokit_agc)
        twokit.body_lay.addLayout(tkf)
        self.live_twokit_status = QLabel("Pick a DISTINCT input device for each kit, then Connect.")
        self.live_twokit_status.setWordWrap(True)
        twokit.body_lay.addWidget(self.live_twokit_status)
        lay.addWidget(twokit)

        # --- CAPTURE EVERYONE: one array, a beam per talker, NOM-automix + per-person tracks ---
        multibeam = Card("Capture everyone — all talkers at once")
        mb_intro = QLabel("Form a beam per person (detected and snapped to room seats), mix them into one "
                          "feed, and optionally record a separate track per person. Honest limit: this "
                          "~40 mm array separates 2-3 well-spaced talkers; closer people merge into one beam.")
        mb_intro.setWordWrap(True)
        multibeam.body_lay.addWidget(mb_intro)
        mbf = QFormLayout()
        mbf.setRowWrapPolicy(QFormLayout.WrapLongRows)
        mbf.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)
        self.live_mb_beams = NoWheelDoubleSpinBox()
        self.live_mb_beams.setRange(1, 3)
        self.live_mb_beams.setDecimals(0)
        self.live_mb_beams.setValue(3)
        self.live_mb_beams.setToolTip("Max simultaneous beams. 2-3 is the small array's realistic ceiling.")
        mbf.addRow("Beams", self.live_mb_beams)
        self.live_mb_snap = QCheckBox("Snap each beam to the nearest room seat")
        self.live_mb_snap.setChecked(True)
        self.live_mb_snap.setToolTip("Hybrid aim: lock a beam to a defined seat for a stable, jitter-free "
                                     "pickup; fall back to free direction-finding where no seat is near.")
        mbf.addRow("Aim", self.live_mb_snap)
        self.live_mb_record = QPushButton("Record per-person tracks")
        self.live_mb_record.setCheckable(True)
        self.live_mb_record.setToolTip("While capturing, record each beam to its own WAV (named by seat) "
                                       "plus the mixed feed; choose a folder when you stop.")
        self.live_mb_record.clicked.connect(self._on_multibeam_record_toggled)
        mbf.addRow("Tracks", self.live_mb_record)
        multibeam.body_lay.addLayout(mbf)
        self.live_mb_status = QLabel("Pick this mode, then Connect. Speak from different seats.")
        self.live_mb_status.setWordWrap(True)
        multibeam.body_lay.addWidget(self.live_mb_status)
        lay.addWidget(multibeam)

        # --- TONE: parametric EQ on the cleaned output (after the cleaners, before the AGC) ---
        peq = Card("Tone — parametric EQ", collapsed=True)
        peq.setToolTip("Shape the cleaned voice's tone with up to 4 bands (bell / shelf / pass). Applied "
                       "AFTER noise cleaning and BEFORE the output AGC — so a big boost is re-leveled by "
                       "the AGC as loudness, not tone. Adjustable live while connected.")
        pf = QFormLayout()
        pf.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)
        self.live_peq_enable = QCheckBox("Enable tone shaping")
        self.live_peq_enable.toggled.connect(lambda *_a: None if self._refreshing else self._on_peq_changed())
        pf.addRow("PEQ", self.live_peq_enable)
        peq.body_lay.addLayout(pf)
        hdr = QHBoxLayout()
        for _t, _w in (("On", 24), ("Type", 92), ("Freq", 78), ("Gain", 72), ("Q", 56)):
            _lb = QLabel(_t)
            _lb.setMinimumWidth(_w)
            hdr.addWidget(_lb)
        hdr.addStretch(1)
        peq.body_lay.addLayout(hdr)
        self._peq_rows = []
        for _ in range(cc.PEQ_MAX_BANDS if hasattr(cc, "PEQ_MAX_BANDS") else 4):
            rl = QHBoxLayout()
            on = QCheckBox()
            typ = QComboBox()
            for _label, _data in (("Bell", "bell"), ("Low shelf", "lowShelf"), ("High shelf", "highShelf"),
                                  ("High-pass", "highpass"), ("Low-pass", "lowpass")):
                typ.addItem(_label, _data)
            freq = NoWheelDoubleSpinBox()
            freq.setRange(20.0, 20000.0)
            freq.setDecimals(0)
            freq.setValue(1000.0)
            freq.setSuffix(" Hz")
            gain = NoWheelDoubleSpinBox()
            gain.setRange(-15.0, 15.0)
            gain.setDecimals(1)
            gain.setValue(0.0)
            gain.setSuffix(" dB")
            qspin = NoWheelDoubleSpinBox()
            qspin.setRange(0.1, 10.0)
            qspin.setDecimals(2)
            qspin.setSingleStep(0.1)
            qspin.setValue(1.0)
            r = {"on": on, "type": typ, "freq": freq, "gain": gain, "q": qspin}
            on.toggled.connect(lambda *_a: None if self._refreshing else self._on_peq_changed())
            typ.currentIndexChanged.connect(lambda *_a: None if self._refreshing else self._on_peq_changed())
            for _sp in (freq, gain, qspin):
                _sp.valueChanged.connect(lambda *_a: None if self._refreshing else self._on_peq_changed())
            for _w in (on, typ, freq, gain, qspin):
                rl.addWidget(_w)
            rl.addStretch(1)
            self._peq_rows.append(r)
            peq.body_lay.addLayout(rl)
        hum = QPushButton("Hum notch (50 Hz)")
        hum.setToolTip("One-click preset: narrow notches at 50 Hz mains hum + its in-band harmonics "
                       "(50/100/150/200 Hz, Q≈10, −12 dB) — more transparent on tonal hum than the broadband "
                       "noise cleaner (no musical-noise artefacts).")
        hum.clicked.connect(self._peq_hum_notch_preset)
        peq.body_lay.addWidget(hum)
        lay.addWidget(peq)

        # keep card refs so the Listening-mode selector can collapse the irrelevant ones
        self._live_cards = {"hw": hw, "mic": mic, "beam": beam, "steer": steer, "eng": eng, "ov": ov,
                            "twokit": twokit, "multibeam": multibeam, "peq": peq}

        lay.addStretch(1)

        # Stop combos from demanding their full content width (long OS device
        # names) — let them fill the column and elide instead of forcing the
        # whole panel wider.
        for combo in (self.live_rate, self.live_out_device,
                      self.live_mode, self.live_beameng_mode,
                      self.live_twokit_dev_a, self.live_twokit_dev_b, self.live_twokit_arr_a,
                      self.live_twokit_arr_b, self.live_twokit_out, self.live_twokit_clean):
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

        self.live_abproof_btn = QPushButton("Capture A/B proof (raw vs cleaned)")
        self.live_abproof_btn.setToolTip(
            "Record ~8 s of the beam BOTH ways at once — raw vs the live cleaners (AEC / dereverb / AI "
            "cleaner) — measure how much quieter the background got (dB), and export both clips + the "
            "numbers. Transparent proof you can run in the customer's own room. Needs a live beam connected "
            "(A/B engine, auto-steer, or zone)."
        )
        self.live_abproof_btn.setEnabled(False)
        self.live_abproof_btn.clicked.connect(self._capture_ab_proof)
        v.addWidget(self.live_abproof_btn)

        self.live_status = QLabel("Disconnected.")
        self.live_status.setWordWrap(True)
        v.addWidget(self.live_status)
        return bar

    # ---- live helpers ----
    def _live_array_id(self):
        """The primary array = the first ticked array (single-array modes read this); None if none ticked."""
        for r in self._array_rows:
            if r.use.isChecked():
                return r.array_id
        return None

    def _live_device(self):
        """The primary (first-ticked) array's input device; None if no array is ticked."""
        for r in self._array_rows:
            if r.use.isChecked():
                return r.device.currentData()
        return None

    def _active_arrays(self):
        """The ticked arrays as (array_id, device, radius_m) — the session's array set. One entry = a
        single-array session; two or more = a combined room capture. Radius is the shared capsule radius."""
        rad = float(self.live_radius.value())
        return [(r.array_id, r.device.currentData(), rad) for r in self._array_rows if r.use.isChecked()]

    def _live_busy(self):
        """True if any live session (beamformer, OCTOVOX, auto-steer, or A/B engine) is active."""
        return (self._live_ctl is not None or self._clean_monitor is not None
                or self._autosteer is not None or self._beam_engine is not None
                or self._twokit is not None or self._multibeam is not None
                or self._multiroom is not None)

    def _active_ctl(self):
        """The active session's mute/gain control surface — the A/B engine (duck-typed:
        ``set_mute``/``set_gain_db``/``read_level``), the auto-steer controller, or the live zone
        controller; ``None`` if none is connected. Sessions are mutually exclusive."""
        if self._twokit is not None:
            return self._twokit
        if self._multiroom is not None:
            return self._multiroom
        if self._multibeam is not None:
            return self._multibeam
        if self._beam_engine is not None:
            return self._beam_engine
        if self._autosteer is not None:
            return self._autosteer.ctrl
        return self._live_ctl

    def _on_listening_mode_changed(self) -> None:
        """Drive the live mode + sensible defaults from the single high-level selector, and collapse the
        cards that don't apply. A convenience facade over the existing mode checkboxes — 'Manual (advanced)'
        leaves the checkboxes alone and reveals every card. Ignored mid-session (modes are fixed at Connect)."""
        self._listening_mode_touched = True   # a genuine user pick (this slot is gated off programmatic refresh)
        if self._live_busy():
            return
        mode = self.live_listening_mode.currentData()
        # set the underlying mode checkbox(es); their toggled handlers enforce mutual exclusion + enabling
        if mode in ("follow", "clean"):
            self.live_autosteer.setChecked(True)
        elif mode == "seat":
            self.live_beameng.setChecked(True)
            i = self.live_beameng_mode.findData("steered")
            if i >= 0:
                self.live_beameng_mode.setCurrentIndex(i)
        elif mode in ("table", "twokit", "multibeam"):
            self.live_autosteer.setChecked(False)
            self.live_beameng.setChecked(False)
            self.live_octovox.setChecked(False)
        # "Clean audio (hands-off)" = follow the room + AI voice cleaning on
        if mode == "clean":
            i = self.live_autosteer_clean.findData("omlsa")
            if i >= 0:
                self.live_autosteer_clean.setCurrentIndex(i)
        # show only the cards relevant to the chosen mode ("manual" reveals every card)
        show = {
            "follow": {"hw", "steer", "peq"},
            "clean": {"hw", "steer", "peq"},
            "seat": {"hw", "eng", "peq"},
            "table": {"hw", "beam", "peq"},
            "twokit": {"twokit"},
            "multibeam": {"hw", "mic", "multibeam"},
        }.get(mode, {"hw", "beam"})
        if mode == "manual":
            show = set(self._live_cards)          # advanced: every card (stays correct as cards are added)
        for key, card in self._live_cards.items():
            card.set_open(key in show)

    def _sync_autosteer_nr_enabled(self) -> None:
        """Enable auto-steer's own OCTOVOX-cleaning controls when auto-steer is selected and not yet
        connected (the cleaner is built at Connect, like the A/B engine's)."""
        on = self.live_autosteer.isChecked() and self._autosteer is None
        self.live_autosteer_clean.setEnabled(on)
        self.live_autosteer_depth.setEnabled(on)
        self.live_autosteer_dereverb.setEnabled(on)
        self.live_autosteer_aec.setEnabled(on)

    def _on_autosteer_toggled(self):
        """Enable the sector controls only when auto-steer is selected."""
        on = self.live_autosteer.isChecked()
        for w in self._autosteer_widgets:
            w.setEnabled(on)
        if on:                               # session modes are mutually exclusive
            self.live_beameng.setChecked(False)
        self._sync_autosteer_nr_enabled()    # auto-steer has its own OCTOVOX-cleaning controls

    # ---- POLARIS A/B engine (BeamEngine: steered ↔ grid on one stream) ----
    def _beameng_mode(self):
        return self.live_beameng_mode.currentData() or "steered"

    def _on_beameng_toggled(self):
        """Enable the strategy picker when the engine is selected, and keep the
        session modes mutually exclusive."""
        on = self.live_beameng.isChecked()
        self.live_beameng_mode.setEnabled(on)
        self.live_beameng_nullseats.setEnabled(on)
        self.live_beameng_postnr.setEnabled(on)
        self.live_beameng_nr_depth.setEnabled(on)
        self.live_beameng_nr_engine.setEnabled(on)
        self.live_beameng_dereverb.setEnabled(on)
        self.live_beameng_aec.setEnabled(on)
        self.live_beameng_adaptnull.setEnabled(on)
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

    def _on_array_use_toggled(self, row):
        """A 'Use' checkbox flipped: remember it, invalidate any prior zone design (the array set changed),
        match the shared sample rate to the new primary array's device, and update the status hint."""
        self._array_use[row.array_id] = row.use.isChecked()
        self._live_design = None
        self.live_design_view.clear()
        dev = self._live_device()
        if dev is not None:
            self._match_device_rate(dev)              # primary array's device → shared sample rate
        self._update_arrays_status()

    def _on_array_device_changed(self, row):
        """A per-array device picker changed: remember it for that array, and (when it is the primary/ticked
        array driving the session) match the shared sample rate to it."""
        self._array_device[row.array_id] = row.device.currentData()
        if self._live_array_id() == row.array_id:
            self._match_device_rate(row.device.currentData())
        self._update_arrays_status()

    def _pick_unused_device(self, devs, used):
        """The first available input device not already assigned to another array (or the first device if
        all are taken / none free), so two arrays don't default to the same input."""
        for d in devs:
            if d.index not in used:
                return d.index
        return devs[0].index if devs else None

    def _match_device_rate(self, dev):
        """Select the device's native sample rate so Connect doesn't fail on a rate the hardware can't open
        (e.g. a 44100-only POLARIS vs a 48000 default)."""
        rate = self._live_dev_rates.get(dev)
        if not rate:
            return
        i = self.live_rate.findData(rate)
        if i < 0:
            self.live_rate.addItem(f"{rate} Hz", rate)
            i = self.live_rate.findData(rate)
        self.live_rate.setCurrentIndex(i)

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
        worker = _ABWorker(self.state.config, aid, geom, self._live_device(),
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
        dev = self._live_device()
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
        self._caps_probed = True
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
        worker = _CalibWorker(geom, self._live_device(), int(sr), 90.0)
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
        self._front_calibrated = True          # flag the SUCCESS (never infer from the offset value: 0° is valid)
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
        if self.live_listening_mode.currentData() == "twokit":
            self._twokit_connect()                     # two separate kits — its own device/array pickers
            return
        active = self._active_arrays()
        if not active:
            self.live_status.setText("Tick at least one array to use (Hardware card).")
            return
        if len(active) >= 2:
            self._multiroom_connect()                  # ≥2 arrays ticked → combined room capture (overrides the mode)
            return
        if self.live_listening_mode.currentData() == "multibeam":
            self._multibeam_connect()
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
                    device=self._live_device(),
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
        self._push_preamp_gain()
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
                input_device=self._live_device(),
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
        clean = self.live_autosteer_clean.currentData()                     # None | "omlsa" | "gate"
        nr_floor_db, nr_oversub = self.live_autosteer_depth.currentData()   # Gentle / Medium / Aggressive
        try:
            ctrl = cc.AutoSteerController(
                geom, sector,
                device=self._live_device(),
                samplerate=float(rate),
                max_talkers=int(self.live_max_talkers.value()),
                freq_hz=float(self.live_freq.value()),
                mode=self._live_mode(),
                loading=self._live_loading(),
                gate_when_empty=self.live_autosteer_gate.isChecked(),
                monitor=self.live_monitor.isChecked(),
                output_device=self.live_out_device.currentData(),
                post_nr=clean is not None,                              # AI cleaning on the auto-steer output
                post_nr_engine=clean or "gate",
                post_nr_floor_db=nr_floor_db, post_nr_oversub=nr_oversub,
                post_nr_amount=_clean_amount(self.live_autosteer_depth),  # cleaning amount (keeps the voice full-bodied)
                dereverb=self.live_autosteer_dereverb.isChecked(),      # real-time room-echo suppression
                aec=self.live_autosteer_aec.isChecked(),                # cancel far-end loudspeaker echo
            )
            ctrl.ctrl.set_gain_db(float(self.live_gain.value()))
            ctrl.start()
        except Exception as exc:  # hardware/open failure → report, stay disconnected
            self.live_status.setText(f"Auto-steer connect failed: {exc}")
            return
        self._autosteer = ctrl
        self._push_preamp_gain()
        self._sync_autosteer_nr_enabled()    # fixed at Connect: disable the cleaning controls
        self._session_array_id = self._live_array_id()
        self.live_connect.setText("Disconnect")
        # the gate owns muting while auto-steering; avoid a fight with the manual button
        self.live_mute.setEnabled(not self.live_autosteer_gate.isChecked())
        mon = ", monitoring" if self.live_monitor.isChecked() else ""
        nr = {"omlsa": " · OCTOVOX cleaner", "gate": " · noise-gate"}.get(clean, "")
        self.live_status.setText(
            f"Auto-steer live · sector {sector.center_deg:.0f}° ±{sector.half_width_deg:.0f}°{nr} "
            f"· up to {int(self.live_max_talkers.value())} talker(s){mon} (headphones)."
        )
        self._notify_session_changed()

    def _beameng_steered_cfg(self, base: dict) -> dict:
        """The steered back-end's config from the A/B-card noise options (fixed at Connect). Adaptive-null
        ⇒ data-adaptive MVDR (+ auto-null); seat-nulling alone ⇒ superdirective (both are frequency-domain,
        so seat nulls still apply under MVDR); the post-beam noise gate is independent of the mode."""
        cfg = dict(base)
        if self.live_beameng_adaptnull.isChecked():
            cfg["mode"] = cc.MODE_MVDR                # data-adaptive: null the measured room noise field
            cfg["auto_null"] = True
        elif self.live_beameng_nullseats.isChecked():
            cfg["mode"] = cc.MODE_SUPERDIRECTIVE      # seat nulls need a frequency-domain steered beam
        if self.live_beameng_postnr.isChecked():
            cfg["post_nr"] = True                     # noise reducer on the output (steady fans/AC)
            floor_db, oversub = self.live_beameng_nr_depth.currentData()   # Gentle / Medium / Aggressive
            cfg["post_nr_floor_db"], cfg["post_nr_oversub"] = floor_db, oversub
            cfg["post_nr_amount"] = _clean_amount(self.live_beameng_nr_depth)   # cleaning amount (full-bodied voice)
            cfg["post_nr_engine"] = self.live_beameng_nr_engine.currentData()   # AI OM-LSA cleaner vs light gate
        if self.live_beameng_dereverb.isChecked():
            cfg["dereverb"] = True                    # real-time late-reverb suppression before the cleaner
        if self.live_beameng_aec.isChecked():
            cfg["aec"] = True                         # cancel far-end loudspeaker echo (loopback reference)
        return cfg

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
        steered_cfg = self._beameng_steered_cfg(cfg)
        monitor_on = self.live_monitor.isChecked()
        try:
            eng = cc.BeamEngine(
                device=self._live_device(),
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
        self._push_preamp_gain()
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
        self.live_beameng_postnr.setEnabled(False)      # NR / adaptive mode are fixed at Connect too
        self.live_beameng_nr_depth.setEnabled(False)
        self.live_beameng_nr_engine.setEnabled(False)
        self.live_beameng_dereverb.setEnabled(False)
        self.live_beameng_aec.setEnabled(False)
        self.live_beameng_adaptnull.setEnabled(False)
        self._refresh_beameng_lockseat()                # populate Follow / Manual angle / seats
        steered = self._beameng_mode() == "steered"
        self.live_beameng_lockseat.setEnabled(steered)  # snap-steer / manual angle only apply to the steered beam
        self.live_beameng_angle.setEnabled(False)       # enabled when 'Manual angle' is selected
        if self._canvas is not None:
            self._canvas.click_cb = self._on_canvas_click_live   # arm "click the map to aim"
        mon = "monitoring (headphones)" if monitor_on else "no monitor — tick Monitor for Mute/Gain"
        _nr_label = "OCTOVOX cleaner" if self.live_beameng_nr_engine.currentData() == "omlsa" else "noise-gate"
        nr = [n for n, on in (("adaptive-null", self.live_beameng_adaptnull.isChecked()),
                              (_nr_label, self.live_beameng_postnr.isChecked())) if on]
        nr_s = f" · {' + '.join(nr)}" if nr else ""
        self.live_status.setText(
            f"A/B engine live · {self._beameng_mode()}{nr_s} · switch strategy from the picker ({mon})."
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
        aec_s = f"   ·   AEC {e.aec_erle_db:+.0f} dB" if self.live_beameng_aec.isChecked() else ""
        lat = getattr(e, "estimated_latency_ms", None)       # honest estimated end-to-end DSP latency
        if lat is not None:
            aec_s += f"   ·   ~{lat:.0f} ms"
        if loc.angle_deg is None and loc.xy is None:
            self.live_beameng_view.setText(f"[{loc.mode}] · listening — no source localized ·{aec_s}")
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
                f"[{loc.mode}] {ang}{xy}  ·  conf {loc.confidence:.0%}{self._seat_suffix()}{lock_s}{null_s}{aec_s}")
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
        aec_s = f"   ·   AEC {a.aec_erle_db:+.0f} dB" if self.live_autosteer_aec.isChecked() else ""
        lat = getattr(a, "estimated_latency_ms", None)       # honest estimated end-to-end DSP latency
        if lat is not None:
            aec_s += f"   ·   ~{lat:.0f} ms"
        if not dets:
            self.live_autosteer_view.setText("· listening — no talker detected ·" + aec_s)
        else:
            parts = [f"{'IN ' if d.in_sector else 'out'} {d.azimuth_deg:.0f}° ({d.salience_db:.0f}dB)" for d in dets]
            n_in = sum(1 for d in dets if d.in_sector)
            self.live_autosteer_view.setText(f"{n_in} in-area  |  " + "   ".join(parts) + self._seat_suffix() + aec_s)
        if a.error:
            self.live_status.setText(f"Auto-steer: {a.error[:60]}")

    def _twokit_connect(self):
        """Start the dual-POLARIS automix: two kits → select the active talker → one combined output."""
        if not cc.controls_available():
            self.live_twokit_status.setText("Two-kit mode needs the [control] extra (numpy + sounddevice).")
            return
        dev_a = self.live_twokit_dev_a.currentData()
        dev_b = self.live_twokit_dev_b.currentData()
        if dev_a is not None and dev_a == dev_b:
            self.live_twokit_status.setText("Pick a DISTINCT input device for each kit (two POLARIS = two devices).")
            return
        clean = self.live_twokit_clean.currentData()
        # 2-kit has one clean combo (no Strength row) → default to the Medium cleaning amount; the always-on
        # level-preserving makeup keeps each kit's voice full-bodied (not weak).
        cfg: dict = ({"post_nr": True, "post_nr_engine": clean, "post_nr_amount": _CLEANING_AMOUNT_DEFAULT}
                     if clean is not None else {})
        specs = [
            cc.KitSpec(device=dev_a, array_id=self.live_twokit_arr_a.currentData(), radius_m=0.04, cfg=dict(cfg)),
            cc.KitSpec(device=dev_b, array_id=self.live_twokit_arr_b.currentData(), radius_m=0.04, cfg=dict(cfg)),
        ]
        agc_db = -20.0 if self.live_twokit_agc.isChecked() else None
        try:
            ctrl = cc.MultiKitController(specs, output_device=self.live_twokit_out.currentData(),
                                         sample_rate=44100.0, agc_target_db=agc_db)
            ctrl.set_gain_db(float(self.live_gain.value()))
            ctrl.set_mute(self.live_mute.isChecked())
            ctrl.start()
        except Exception as exc:                      # distinct-device guard / hardware open → report, stay disconnected
            self.live_twokit_status.setText(f"Two-kit connect failed: {exc}")
            return
        self._twokit = ctrl
        self._push_preamp_gain()
        self._session_array_id = self.live_twokit_arr_a.currentData()
        self.live_connect.setText("Disconnect")
        self.live_mute.setEnabled(True)
        self.live_gain.setEnabled(True)
        self.live_twokit_status.setText("Two kits connected — speak in each area; the active kit is output.")
        self._notify_session_changed()

    # ---- per-array Hardware rows ([Use] + the array's own input device) ----
    def _build_array_row(self, array_id, label) -> _ArrayRow:
        row = QWidget()
        h = QHBoxLayout(row)
        h.setContentsMargins(0, 0, 0, 0)
        use = QCheckBox(label)
        use.setToolTip("Use this array in the session. Tick one for a single-array session, or several to "
                       "combine them into one room capture (each needs its OWN input device).")
        device = QComboBox()
        device.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
        device.setMinimumContentsLength(6)
        device.setMinimumWidth(80)
        missing = QLabel("")
        kr = _ArrayRow(row, array_id, use, device, missing)
        use.toggled.connect(lambda *_a, r=kr: None if self._refreshing else self._on_array_use_toggled(r))
        device.currentIndexChanged.connect(
            lambda *_a, r=kr: None if self._refreshing else self._on_array_device_changed(r))
        h.addWidget(use)
        h.addWidget(device, 1)
        h.addWidget(missing)
        return kr

    def _refresh_array_rows(self, arrays, devs) -> None:
        """Rebuild the per-array rows from the configured arrays (one each), preserving every array's Use
        state + chosen device by id. A new array defaults to a DISTINCT unused device; the first array is
        ticked by default so a fresh config is a single-array session. Idle-only (signals blocked)."""
        for kr in self._array_rows:                       # tear down the old rows
            self._array_rows_lay.removeWidget(kr.widget)
            kr.widget.setParent(None)
            kr.widget.deleteLater()
        self._array_rows = []
        assigned: set = set()                             # devices already handed to an earlier row this pass
        for n, a in enumerate(arrays):
            kr = self._build_array_row(a.id, f"{a.label} · {a.id}")
            kr.use.blockSignals(True)
            kr.device.blockSignals(True)
            if devs:
                for d in devs:
                    kr.device.addItem(f"[{d.index}] {d.name} ({d.max_input_channels}ch)", d.index)
            else:
                kr.device.addItem("System default", None)
            remembered = self._array_device.get(a.id)
            gone = remembered is not None and kr.device.findData(remembered) < 0
            kr.missing.setText("device gone" if gone else "")
            if remembered is not None and not gone:
                want = remembered
            else:                                         # new array OR its device vanished → a distinct, still-free device
                want = self._pick_unused_device(devs, assigned)
            ix = kr.device.findData(want) if want is not None else -1
            if kr.device.count():
                kr.device.setCurrentIndex(max(0, ix))
            chosen = kr.device.currentData()
            self._array_device[a.id] = chosen
            if chosen is not None:
                assigned.add(chosen)
            on = self._array_use.get(a.id, n == 0)        # default: first array on → a single-array session
            kr.use.setChecked(on)
            self._array_use[a.id] = on
            kr.use.blockSignals(False)
            kr.device.blockSignals(False)
            self._array_rows.append(kr)
            self._array_rows_lay.addWidget(kr.widget)
        self._update_arrays_status()

    def _active_distinct(self) -> bool:
        devs = [d for (_a, d, _r) in self._active_arrays() if d is not None]
        return len(devs) == len(set(devs))

    def _update_arrays_status(self) -> None:
        n = len(self._active_arrays())
        if not self._array_rows:
            self.live_arrays_status.setText("No arrays configured — add one in DESIGN.")
        elif n == 0:
            self.live_arrays_status.setText("Tick at least one array to use.")
        elif n == 1:
            self.live_arrays_status.setText("1 array — single-array session.")
        elif not self._active_distinct():
            self.live_arrays_status.setText("Two ticked arrays share a device — pick a DISTINCT input per array.")
        else:
            self.live_arrays_status.setText(f"{n} arrays — combined room capture (each seat → its nearest array).")

    def _multibeam_connect(self):
        """Start capture-everyone on a SINGLE array (a beam per talker, snapped to seats, NOM-automixed).
        Ticking ≥2 arrays routes to the combined room capture instead (see _live_toggle_connect)."""
        if not cc.controls_available():
            self.live_mb_status.setText("Capture-everyone needs the [control] extra (numpy + sounddevice).")
            return
        array_id = self._live_array_id()
        if array_id is None:
            self.live_mb_status.setText("Pick an array on the Hardware card first.")
            return
        mask = self._live_active_mask()
        rate = float(self.live_rate.currentData() or 44100)
        n_beams = int(self.live_mb_beams.value())
        try:
            ctrl = cc.MultiBeamController(
                self.state.config, array_id, device=self._live_device(),
                radius_m=float(self.live_radius.value()), sample_rate=rate, n_beams=n_beams,
                snap=self.live_mb_snap.isChecked(),
                active_mask=(list(mask) if (any(mask) and not all(mask)) else None))
            ctrl.set_gain_db(float(self.live_gain.value()))
            ctrl.set_mute(self.live_mute.isChecked())
            if self.live_mb_record.isChecked():
                rec = cc.MultiTrackRecorder(n_beams, rate)
                rec.start()
                ctrl.set_recorder(rec)
                self._multibeam_rec = rec
            ctrl.start()
        except Exception as exc:                          # hardware open / config → report, stay disconnected
            self.live_mb_status.setText(f"Capture-everyone connect failed: {exc}")
            self._multibeam_rec = None
            return
        self._multibeam = ctrl
        self._push_preamp_gain()
        self._session_array_id = array_id
        self.live_connect.setText("Disconnect")
        self.live_mute.setEnabled(True)
        self.live_gain.setEnabled(True)
        self.live_mb_status.setText("Capturing — speak from different seats; each talker gets its own beam.")
        self._notify_session_changed()

    def _multiroom_connect(self):
        """Combine the ticked arrays (≥2) into one room-wide capture (each array owns its nearest seats)."""
        if not cc.controls_available():
            self.live_arrays_status.setText("Capture-everyone needs the [control] extra (numpy + sounddevice).")
            return
        if not self._active_distinct():
            self.live_arrays_status.setText("Each ticked array needs a DISTINCT input device — fix it and Connect.")
            return
        rate = float(self.live_rate.currentData() or 44100)
        n_beams = int(self.live_mb_beams.value())
        specs = [cc.RoomKitSpec(device=dev, array_id=aid, radius_m=rad)
                 for (aid, dev, rad) in self._active_arrays()]
        try:
            ctrl = cc.MultiRoomController(self.state.config, specs, sample_rate=rate, n_beams=n_beams,
                                          snap=self.live_mb_snap.isChecked())
            ctrl.set_gain_db(float(self.live_gain.value()))
            ctrl.set_mute(self.live_mute.isChecked())
            if self.live_mb_record.isChecked():
                self._multibeam_rec = "multiroom"             # sentinel: the room controller owns the recorders
            ctrl.start()
            if self.live_mb_record.isChecked():
                ctrl.record_tracks(True)
        except Exception as exc:                              # hardware open / distinct-device → report, stay off
            self.live_arrays_status.setText(f"Room capture connect failed: {exc}")
            self._multibeam_rec = None
            return
        self._multiroom = ctrl
        self._session_array_id = specs[0].array_id
        self.live_connect.setText("Disconnect")
        self.live_mute.setEnabled(True)
        self.live_gain.setEnabled(True)
        self.live_arrays_status.setText(f"Room capture — {len(specs)} arrays; each seat goes to its nearest array.")
        self._notify_session_changed()

    def _on_multibeam_record_toggled(self):
        """Arm/disarm per-person recording. Mid-session it attaches/detaches the recorder live; when the
        session ends (or recording is turned off) the tracks are written via the folder picker."""
        if self._multibeam is None:
            return                                        # pre-connect: just remembers the armed state
        if self.live_mb_record.isChecked():
            rec = cc.MultiTrackRecorder(int(self.live_mb_beams.value()), self._multibeam.sample_rate)
            rec.start()
            self._multibeam.set_recorder(rec)
            self._multibeam_rec = rec
            self.live_mb_status.setText("Recording per-person tracks …")
        else:
            self._multibeam.set_recorder(None)
            self._write_multibeam_tracks()

    def _write_multibeam_tracks(self):
        """Flush the armed recorder to a user-chosen folder (no-op if nothing was recorded)."""
        rec, self._multibeam_rec = self._multibeam_rec, None
        if rec is None:
            return
        rec.stop()
        from PySide6.QtWidgets import QFileDialog

        out = QFileDialog.getExistingDirectory(self, "Save per-person tracks to …")
        if not out:
            return
        paths = rec.write(out, prefix="capture")
        self.live_mb_status.setText(f"Wrote {len(paths)} track(s) to {out}")

    def _live_disconnect(self):
        if self._multiroom is not None:
            mr, armed = self._multiroom, (self._multibeam_rec == "multiroom")
            try:
                mr.stop()
            finally:
                self._multiroom = None
            self._multibeam_rec = None
            self.live_mute.setEnabled(True)
            self.live_gain.setEnabled(True)
            if armed:
                from PySide6.QtWidgets import QFileDialog
                out = QFileDialog.getExistingDirectory(self, "Save per-person tracks to …")
                if out:
                    paths = mr.write_tracks(out, prefix="room")
                    self.live_arrays_status.setText(f"Wrote {len(paths)} track(s) to {out}")
            self.live_mb_record.setChecked(False)
        if self._multibeam is not None:
            try:
                self._multibeam.stop()
            finally:
                self._multibeam = None
            self.live_mute.setEnabled(True)
            self.live_gain.setEnabled(True)
            self._write_multibeam_tracks()                # flush any armed recording
            self.live_mb_record.setChecked(False)
            self.live_mb_status.setText("Disconnected.")
        if self._twokit is not None:
            try:
                self._twokit.stop()
            finally:
                self._twokit = None
            self.live_mute.setEnabled(True)
            self.live_gain.setEnabled(True)
            self.live_twokit_meter_a.reset()
            self.live_twokit_meter_b.reset()
            self.live_twokit_status.setText("Disconnected.")
        if self._beam_engine is not None:
            try:
                self._beam_engine.stop()
            finally:
                self._beam_engine = None
                self._beameng_loc = None
            self.live_mute.setEnabled(True)
            self.live_gain.setEnabled(True)
            self.live_beameng_nullseats.setEnabled(self.live_beameng.isChecked())   # re-enable for next Connect
            self.live_beameng_postnr.setEnabled(self.live_beameng.isChecked())
            self.live_beameng_nr_depth.setEnabled(self.live_beameng.isChecked())
            self.live_beameng_nr_engine.setEnabled(self.live_beameng.isChecked())
            self.live_beameng_dereverb.setEnabled(self.live_beameng.isChecked())
            self.live_beameng_aec.setEnabled(self.live_beameng.isChecked())
            self.live_beameng_adaptnull.setEnabled(self.live_beameng.isChecked())
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
            self._sync_autosteer_nr_enabled()                   # re-enable the cleaning controls for next Connect
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

    def _push_preamp_gain(self):
        """Push the current mic-input gain to the active session (duck-typed — SimulatedMicController
        and OCTOVOX have no preamp, so guard with hasattr). A software level trim, not an SNR change."""
        ctl = self._active_ctl()
        if ctl is not None and hasattr(ctl, "set_preamp_gain_db"):
            ctl.set_preamp_gain_db(float(self.live_preamp_gain.value()))

    def _on_preamp_gain_changed(self, _v):
        self._push_preamp_gain()

    # ---- parametric EQ (tone) ----
    def _peq_bands(self):
        """The enabled PEQ bands as model dicts ([] when tone shaping is off / no band ticked)."""
        if not self.live_peq_enable.isChecked():
            return []
        out = []
        for r in self._peq_rows:
            if r["on"].isChecked():
                out.append({"freqHz": float(r["freq"].value()), "gainDb": float(r["gain"].value()),
                            "q": float(r["q"].value()), "type": r["type"].currentData()})
        return out

    def _push_peq(self):
        """Push the current PEQ bands to the active session (duck-typed — capture-everyone / 2-kit have no
        PEQ stage, so guard with hasattr). Re-tunable live, like the monitor gain."""
        ctl = self._active_ctl()
        if ctl is not None and hasattr(ctl, "set_peq_bands"):
            ctl.set_peq_bands(self._peq_bands() or None)

    def _on_peq_changed(self):
        self._push_peq()

    def _peq_hum_notch_preset(self):
        """Fill the bands with 50 Hz mains-hum notches (50/100/150/200 Hz, Q=10, −12 dB bells) + enable."""
        self._refreshing = True
        try:
            for r, f in zip(self._peq_rows, (50.0, 100.0, 150.0, 200.0)):
                r["on"].setChecked(True)
                i = r["type"].findData("bell")
                if i >= 0:
                    r["type"].setCurrentIndex(i)
                r["freq"].setValue(f)
                r["gain"].setValue(-12.0)
                r["q"].setValue(10.0)
            self.live_peq_enable.setChecked(True)
        finally:
            self._refreshing = False
        self._push_peq()

    def _notify_session_changed(self):
        """Tell the shell (ModeBar live dot) the session state flipped, and mark the Connect/Disconnect
        button destructive while a session runs (it reads 'Disconnect' then)."""
        self._push_peq()                  # apply the current tone (PEQ) to a freshly-connected session
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
        # 2-kit mode: both kits' arrays on the one map (each its own bearing + dominant DOA), with the
        # active (currently-output) kit flagged. None in every other mode → the canvas single-array path.
        kits = None
        if self._twokit is not None:
            kits = []
            active_k = self._twokit.active_kit
            specs = self._twokit.kits
            for s in self._twokit.status():
                aid = specs[s.index].array_id if s.index < len(specs) else None
                karr = next((d for d in self.state.config.devices if d.id == aid), None)
                kits.append({
                    "array_id": aid,
                    "bearing": getattr(karr, "bearing_deg", None) or 0.0,
                    "doa": s.doa_deg,
                    "level": s.level,
                    "active": s.index == active_k,
                })
        self.state.set_live_overlay({
            # pinned at connect — the combo may show another room's arrays
            "array_id": self._session_array_id,
            "sector": sector,
            "detections": detections,
            "seat": seat,
            "bearing": bearing,
            "level": self.live_meter.level(),
            "steer_az": steer_az,
            "kits": kits,
            "connected": True,
        })

    # ---- first-run setup guide (the LIVE getting-started checklist) ----
    def _build_guide_banner(self):
        """A compact, dismissible 'Getting started' checklist that ticks off as the user picks a
        mode, connects, probes, calibrates and hears the array — reusing the real controls (it never
        re-runs DSP). Deliberately NOT one of self._live_cards, so the listening-mode card-collapse
        never owns or hides it. All gate logic lives in the pure ``first_run`` module."""
        card = Card("Getting started")
        self._guide_card = card
        self._guide_label = QLabel()
        self._guide_label.setWordWrap(True)
        self._guide_label.setTextFormat(Qt.RichText)
        card.body_lay.addWidget(self._guide_label)
        row = QHBoxLayout()
        self._guide_ack_btn = QPushButton("Got it, I can hear it")
        self._guide_ack_btn.setToolTip("Tick the final step by hand (e.g. a quiet room where the meter stays low).")
        self._guide_ack_btn.clicked.connect(self._guide_ack)
        self._guide_ack_btn.setVisible(False)
        row.addWidget(self._guide_ack_btn)
        row.addStretch(1)
        hide_btn = QPushButton("Hide")
        hide_btn.setToolTip("Hide for now — reopen from the menu: 'Show LIVE getting-started'.")
        hide_btn.clicked.connect(self._guide_hide)
        row.addWidget(hide_btn)
        dont_btn = QPushButton("Don't show again")
        dont_btn.clicked.connect(self._guide_dont_show_again)
        row.addWidget(dont_btn)
        card.body_lay.addLayout(row)
        card.setVisible(False)                       # revealed on first LIVE entry / via the menu
        return card

    @staticmethod
    def _guide_settings() -> QSettings:
        # Explicit org/app so the flag is stable even if the app didn't set them (e.g. under tests).
        return QSettings("Aniston", "RoomDesigner")

    def _guide_seen(self) -> bool:
        from .first_run import GUIDE_DONE_SETTING
        return bool(self._guide_settings().value(GUIDE_DONE_SETTING, False, type=bool))

    def show_first_run_guide(self, force: bool = False) -> None:
        """Reveal the checklist. ``force`` (the menu) always shows; otherwise show only on a first
        run (no done-flag), at most once per session, and never when dismissed this session."""
        card = getattr(self, "_guide_card", None)
        if card is None:
            return
        if force:
            self._guide_dismissed = False
        else:
            if self._guide_seen() or self._guide_autoshown or self._guide_dismissed:
                return
            self._guide_autoshown = True
        card.setVisible(True)
        card.set_open(True)
        self._refresh_guide()

    def _build_guide_snapshot(self):
        from .first_run import GuideSnapshot
        meter = getattr(self, "live_meter", None)
        return GuideSnapshot(
            listening_mode=self.live_listening_mode.currentData() or "table",
            listening_mode_touched=self._listening_mode_touched,
            has_array=self._live_array_id() is not None,
            controls_available=cc.controls_available(),
            busy=self._live_busy(),
            caps_probed=self._caps_probed,
            front_calibrated=self._front_calibrated,
            monitor_on=self.live_monitor.isChecked(),
            meter_level=(meter.level() if meter is not None else 0.0),
            heard_ack=self._heard_ack,
        )

    def _guide_hint(self, step_id, snap) -> str:
        from . import first_run as fr
        if step_id == fr.STEP_MODE:
            return "choose how the room is heard, above."
        if step_id == fr.STEP_CONNECT:
            return "add a microphone array in DESIGN first." if not snap.has_array else "click Connect in the footer below."
        if step_id == fr.STEP_DETECT:
            if not snap.controls_available:
                return "(simulation — optional)."
            return "disconnect to re-check capsules (optional)." if snap.busy else "click 'Detect silent capsules' in Hardware."
        if step_id == fr.STEP_CALIBRATE:
            return "disconnect to calibrate (optional)." if snap.busy else "click 'Calibrate front' and talk from the front (optional)."
        if step_id == fr.STEP_HEAR:
            return "tick Monitor, Connect, and watch the meter — or click 'Got it'."
        return ""

    def _refresh_guide(self) -> None:
        """Rebuild the checklist rows from a fresh snapshot (main-thread, cheap). Called from the
        live meter tick, so it tracks state changes without threading refreshes through handlers."""
        card = getattr(self, "_guide_card", None)
        if card is None or not card.isVisible():
            return
        from . import first_run as fr
        snap = self._build_guide_snapshot()
        active = fr.active_step(snap)
        done, total = fr.progress(snap)
        rows = []
        for s in fr.GUIDE_STEPS:
            if not fr.step_relevant(s.id, snap):
                continue
            mark = "✓" if fr.step_done(s.id, snap) else ("▶" if s.id == active else "○")
            title = s.title + (" (optional)" if s.optional else "")
            rows.append(f"<b>{mark} {title}</b> — {self._guide_hint(s.id, snap)}" if s.id == active else f"{mark} {title}")
        head = "All set — you're live! ✓" if active is None else f"Getting started — {done}/{total} done"
        self._guide_label.setText(f"<b>{head}</b><br>" + "<br>".join(rows))
        self._guide_ack_btn.setVisible(active == fr.STEP_HEAR)
        if fr.required_done(snap) and not self._guide_done_persisted:
            self._mark_guide_done()                 # persist once; the banner stays until hidden

    def _mark_guide_done(self) -> None:
        from .first_run import GUIDE_DONE_SETTING
        self._guide_settings().setValue(GUIDE_DONE_SETTING, True)
        self._guide_done_persisted = True

    def _guide_ack(self) -> None:
        self._heard_ack = True
        self._refresh_guide()

    def _guide_hide(self) -> None:
        self._guide_dismissed = True
        if self._guide_card is not None:
            self._guide_card.setVisible(False)

    def _guide_dont_show_again(self) -> None:
        self._mark_guide_done()
        self._guide_hide()

    # ---- commissioning snapshot (for the as-built report) ----
    def commissioning_info(self):
        """Snapshot the live / measured state for a commissioning report (control
        thread). Reads the running beam's estimated latency, AEC/ERLE + reference,
        the last A/B noise proof, the front-offset calibration, and capsule health.
        Any field with no live source is left None so the report omits it honestly."""
        from datetime import datetime

        from conf_pipeline.report import CommissioningInfo

        obj = self._ab_target()                               # running beam (A/B engine / auto-steer / zone), or None
        latency = getattr(obj, "estimated_latency_ms", None) if obj is not None else None
        stages = obj.active_cleaning_stages() if obj is not None else ""
        ref = getattr(obj, "aec_ref_source", "") if obj is not None else ""
        erle = getattr(obj, "aec_erle_db", None) if obj is not None else None
        if erle is not None and erle <= 0.0:                  # 0 dB ERLE == AEC idle / no echo — not worth reporting
            erle = None
        last = self._ab_last
        silent = None
        if self._caps_probed:
            silent = tuple(i + 1 for i, cb in enumerate(self.live_caps) if not cb.isChecked())
        return CommissioningInfo(
            date=datetime.now().strftime("%Y-%m-%d"),
            listening_mode=self.live_listening_mode.currentText(),
            estimated_latency_ms=latency,
            active_cleaning_stages=stages,
            aec_ref_source=ref,
            aec_erle_db=erle,
            bed_reduction_db=(last.bed_reduction_db if last is not None else None),
            rms_reduction_db=(last.rms_reduction_db if last is not None else None),
            front_offset_deg=float(self.live_front_offset.value()),
            silent_capsules=silent,
        )

    # ---- A/B proof (raw beam vs cleaned) ----
    def _ab_target(self):
        """The live object that can capture an A/B proof (A/B engine / auto-steer / zone controller).
        The OCTOVOX CleanMonitor path has no live cleaners to tap, so it's excluded."""
        for obj in (self._beam_engine, self._autosteer, self._live_ctl):
            if obj is not None and hasattr(obj, "start_ab_capture"):
                return obj
        return None

    def _capture_ab_proof(self):
        """Arm a ~8 s raw-vs-cleaned capture on the running beam; the meter tick finalizes + exports it."""
        obj = self._ab_target()
        if obj is None:
            self.live_status.setText("Connect a beam (A/B engine / auto-steer / zone) first, with cleaning on.")
            return
        self._ab_obj = obj
        self._ab_cap = obj.start_ab_capture(8.0)
        self.live_abproof_btn.setEnabled(False)
        self.live_status.setText("A/B proof: capturing ~8 s of raw-vs-cleaned — let the room run / speak…")

    def _poll_ab_proof(self):
        cap = self._ab_cap
        if cap is None or not cap.done:
            return
        obj, self._ab_cap = self._ab_obj, None
        self._ab_obj = None
        self.live_abproof_btn.setEnabled(self._ab_target() is not None)
        res = cap.finalize(erle_db=getattr(obj, "aec_erle_db", 0.0),
                           stages=obj.active_cleaning_stages() if obj is not None else "")
        self._ab_last = res              # keep the latest proof for the commissioning report
        out_dir = QFileDialog.getExistingDirectory(self, f"A/B proof: {res.headline()} — save clips + numbers to…")
        if not out_dir:
            self.live_status.setText(f"A/B proof: {res.headline()} (not saved).")
            return
        import conf_pipeline_control as cc

        cc.write_ab_proof(res, out_dir)
        self.live_status.setText(f"A/B proof saved · {res.headline()} → {out_dir}")

    def _tick_twokit(self):
        """Drive the combined + per-kit meters and the status readout for the 2-kit automix."""
        tk = self._twokit
        if tk is None:
            return

        def _pct(lvl):
            if lvl <= 1e-6:
                return 0.0
            return max(0.0, min(1.0, (20.0 * math.log10(lvl) + 60.0) / 60.0))

        self.live_meter.set_level(_pct(tk.read_level()))
        meters = (self.live_twokit_meter_a, self.live_twokit_meter_b)
        parts = []
        for s in tk.status():
            if s.index < len(meters):
                meters[s.index].set_level(_pct(s.level))
            tag = "●" if s.active else "○"
            doa = f"{s.doa_deg:.0f}°" if s.doa_deg is not None else "—"
            err = f"  ⚠ {s.error}" if s.error else ""
            parts.append(f"{tag} Kit {s.index + 1}: DOA {doa}{err}")
        self.live_twokit_status.setText("     ".join(parts))

    def _tick_multibeam(self):
        """Drive the level meter + per-beam 'who's talking' readout for capture-everyone."""
        mb = self._multibeam
        if mb is None:
            return

        def _pct(lvl):
            return 0.0 if lvl <= 1e-6 else max(0.0, min(1.0, (20.0 * math.log10(lvl) + 60.0) / 60.0))

        self.live_meter.set_level(_pct(mb.read_level()))
        live = [b for b in mb.status() if b.active]
        if not live:
            self.live_mb_status.setText("Capturing — waiting for talkers …")
        else:
            cells = "   ".join(f"● {b.seat_id or f'{b.azimuth_deg:.0f}°'}" for b in live)
            self.live_mb_status.setText(f"{len(live)} talking:   {cells}")

    def _tick_multiroom(self):
        """Drive the meter + per-kit 'which seats are talking' readout for the room-wide capture."""
        mr = self._multiroom
        if mr is None:
            return

        def _pct(lvl):
            return 0.0 if lvl <= 1e-6 else max(0.0, min(1.0, (20.0 * math.log10(lvl) + 60.0) / 60.0))

        self.live_meter.set_level(_pct(mr.read_level()))
        parts = []
        for k in mr.status():
            tag = "⚠" if k.dead else "●"
            seats = ",".join(b.seat_id or f"{b.azimuth_deg:.0f}°" for b in k.beams if b.active) or "—"
            parts.append(f"{tag} {k.array_id or f'kit{k.index + 1}'}: {seats}")
        self.live_arrays_status.setText("    ".join(parts))

    def _tick_live_meter(self):
        """Update the level meter on a dB scale (−60 dB → 0 %, 0 dB → 100 %), so
        normal speech picked up by a ceiling array is clearly visible rather than
        a sliver on a linear scale."""
        self._poll_ab_proof()                # finalize + export an A/B proof once its capture completes
        self.live_abproof_btn.setEnabled(self._ab_cap is None and self._ab_target() is not None)
        self._refresh_guide()                # keep the first-run checklist in step with live state (cheap; no-op when hidden)
        self._live_seat = None               # only the DOA paths below re-resolve a seat
        if self._twokit is not None:
            self._tick_twokit()
            self._publish_overlay()
            return
        if self._multiroom is not None:
            self._tick_multiroom()
            return
        if self._multibeam is not None:
            self._tick_multibeam()
            return
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

    def _refresh_twokit_pickers(self, arrays, devs):
        """Populate the 2-kit device / array / output combos (idle-only, called from refresh).
        Defaults the two kits to two DISTINCT input devices so Connect works out of the box."""
        for combo, default_ix in ((self.live_twokit_dev_a, 0), (self.live_twokit_dev_b, 1)):
            cur = combo.currentData()
            combo.blockSignals(True)
            combo.clear()
            if devs:
                for d in devs:
                    combo.addItem(f"[{d.index}] {d.name} ({d.max_input_channels}ch)", d.index)
            else:
                combo.addItem("System default", None)
            ix = combo.findData(cur) if cur is not None else -1
            if ix < 0:
                ix = min(default_ix, combo.count() - 1)
            combo.setCurrentIndex(max(0, ix))
            combo.blockSignals(False)
        for combo, default_ix in ((self.live_twokit_arr_a, 0), (self.live_twokit_arr_b, 1)):
            cur = combo.currentData()
            combo.blockSignals(True)
            combo.clear()
            for a in arrays:
                combo.addItem(f"{a.label} · {a.id}", a.id)
            ix = combo.findData(cur) if cur is not None else -1
            if ix < 0:
                ix = min(default_ix, combo.count() - 1)
            if combo.count():
                combo.setCurrentIndex(max(0, ix))
            combo.blockSignals(False)
        from conf_pipeline_control.audio import list_output_devices
        cur = self.live_twokit_out.currentData()
        self.live_twokit_out.blockSignals(True)
        self.live_twokit_out.clear()
        self.live_twokit_out.addItem("System default", None)
        for o in list_output_devices():
            self.live_twokit_out.addItem(f"[{o.index}] {o.name} ({o.max_output_channels}ch)", o.index)
        ix = self.live_twokit_out.findData(cur) if cur is not None else -1
        self.live_twokit_out.setCurrentIndex(max(0, ix))
        self.live_twokit_out.blockSignals(False)

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
                arrays = [d for d in self.state.config.devices if d.type == "microphoneArray"]
                from conf_pipeline_control.audio import list_input_devices
                devs = list_input_devices()
                self._live_dev_rates = {d.index: int(d.default_samplerate) for d in devs}
                # per-array rows: one [Use]+device row per configured array (preserves Use + device by id;
                # a new array defaults to a distinct device, the first array ticked → single-array session)
                self._refresh_array_rows(arrays, devs)
                dev = self._live_device()                    # match the shared sample rate to the primary array's device
                if dev is not None:
                    self._match_device_rate(dev)

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

                self._refresh_twokit_pickers(arrays, devs)   # 2-kit device/array/output pickers
        finally:
            self._refreshing = False
