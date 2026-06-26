"""DESIGN panel: room, devices, coverage zones, talkers, selection editor.

The old Build tab plus the room/floor-plan actions that used to live in the
toolbar. The zone *kind* now comes from the tool rail's flyout (state.zone_kind).
"""
from __future__ import annotations

import math

from PySide6.QtCore import Qt, QThreadPool
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

import conf_pipeline as cp
from conf_pipeline.model import Point2D, RectShape

from .common import DEVICE_TYPES, NoWheelDoubleSpinBox, PanelBase, _CalibWorker, clear_layout


class DesignPanel(PanelBase):
    MODE = "design"
    TITLE = "Design"

    def __init__(self, state):
        super().__init__(state)
        self._calib_workers: set = set()   # strong refs to bearing-learn runnables
        root = QVBoxLayout(self)
        root.setContentsMargins(10, 8, 10, 8)
        root.addWidget(self._header())

        optimize = QPushButton("✨ Optimize room")
        optimize.setProperty("accent", "true")
        optimize.setToolTip("One click: place arrays, assign coverage channels, route everything")
        optimize.clicked.connect(lambda: self._win("_optimize_room"))
        root.addWidget(optimize)

        root.addWidget(self._scroll(self._build_body()), 1)
        self.refresh()

    def _build_body(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)

        room = QGroupBox("Room")
        rl = QHBoxLayout(room)
        rect = QPushButton("Rect room")
        rect.setToolTip("Drop a rectangular room you can resize")
        rect.clicked.connect(lambda: self._win("_rect_room"))
        plan = QPushButton("Floor plan…")
        plan.setToolTip("Place a floor-plan image under the room")
        plan.clicked.connect(lambda: self._win("_import_floor_plan"))
        calib = QPushButton("Calibrate…")
        calib.setToolTip("Drag a known distance to set the floor-plan scale")
        calib.clicked.connect(lambda: self._win("_calibrate_scale"))
        rl.addWidget(rect)
        rl.addWidget(plan)
        rl.addWidget(calib)
        lay.addWidget(room)

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
        rm.setProperty("danger", "true")
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
        addz = QPushButton("+ Zone")
        addz.setToolTip("Add a default zone of the kind picked on the Zone tool's flyout")
        addz.clicked.connect(self._add_zone_default)
        cl.addWidget(addz)
        cl.addWidget(QLabel("Tip: pick the Zone tool (Z) and drag on the canvas."))
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
        lay.addStretch(1)
        return w

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
        elif dtype == "camera":
            dev = cp.create_camera(did, label)
        else:
            dev = cp.create_codec(did, label, transport)
        cfg = cp.add_device(self.state.config, dev)
        cfg = cp.set_device_position(cfg, did, self._default_placement(cfg))
        self.state.selection = {"kind": "device", "id": did}
        self.state.set_config(cfg)

    def _default_placement(self, cfg):
        n = sum(1 for d in cfg.devices if d.position) + len(cfg.talkers)
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

    def _add_zone_default(self):
        aid = self._selected_array_id()
        if not aid:
            return
        zid = self.state.next_zone_id(aid)
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
    def refresh(self):
        super().refresh()
        self._refreshing = True
        try:
            cfg = self.state.config
            self.dev_list.clear()
            for d in cfg.devices:
                ins = sum(1 for p in d.ports if p.kind == "input")
                outs = sum(1 for p in d.ports if p.kind == "output")
                it = QListWidgetItem(f"{d.label}  ·  {d.type} · {d.id} · {ins}in/{outs}out")
                it.setData(Qt.UserRole, d.id)
                self.dev_list.addItem(it)
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
            self._refresh_selection()
        finally:
            self._refreshing = False

    # ---- selection property editor ----
    def _refresh_selection(self):
        clear_layout(self.sel_layout)
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
            rm.setProperty("danger", "true")
            rm.clicked.connect(lambda: self.state.set_config(cp.remove_coverage_zone(cfg, sel["array_id"], sel["zone_id"])))
            self.sel_layout.addWidget(rm)

    def _toggle_zone_cut(self, checked: bool) -> None:
        """Flip the selected zone between dynamic and exclusion (one undo step)."""
        sel = self.state.selection
        if not sel or sel.get("kind") != "zone":
            return
        array_id = sel["array_id"]
        zone_id = sel["zone_id"]
        new_type = "exclusion" if checked else "dynamic"
        try:
            self.state.set_config(cp.set_zone_type(self.state.config, array_id, zone_id, new_type))
        except cp.CoverageError as exc:
            self._toast(f"⚠ {exc}")

    def _zone_props(self, array_id, zone_id):
        """Per-coverage-area output channel + gain (Designer steerable coverage)."""
        cfg = self.state.config
        arr = next((d for d in cfg.devices if d.id == array_id), None)
        zone = next((z for z in arr.zones if z.id == zone_id), None) if arr else None
        if zone is None:
            return

        # "Cut (no pickup)" checkbox — available for all zone types
        cut_cb = QCheckBox("Cut (no pickup)")
        cut_cb.setToolTip("Exclude this area: no audio is picked up here")
        cut_cb.setChecked(zone.type == "exclusion")
        cut_cb.toggled.connect(lambda checked: None if self._refreshing else self._toggle_zone_cut(checked))
        self.sel_layout.addWidget(cut_cb)

        if zone.type == "exclusion":
            return  # exclusion zones carry no output channel or gain controls

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
                self._toast(f"⚠ {e}")
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
        # aim — bearing is the mounting heading (0°=+Y): cameras/loudspeakers steer their FOV/dispersion
        # cone; a microphone ARRAY's bearing rotates its room frame, enabling room-aware steering
        # (snap-steer / click-to-aim / seat-nulling). The planar array has no tilt (off-nadir fixed).
        if d.type in ("camera", "loudspeaker", "microphoneArray"):
            bearing = self._spin(float(getattr(d, "bearing_deg", 0.0) or 0.0), lambda v: self._set_bearing(d, v))
            bearing.setRange(0, 360)
            if d.type == "microphoneArray":
                bearing_row = QHBoxLayout()
                bearing_row.addWidget(bearing, 1)
                learn_btn = QPushButton("Learn bearing…")
                learn_btn.setToolTip(
                    "Records a reference talker at a KNOWN point and infers the array bearing.\n"
                    "Have someone stand at the reference point and talk for ~4 s, then click.\n"
                    "Needs the [control] extra (numpy + sounddevice) and a connected device."
                )
                _did = d.id
                learn_btn.clicked.connect(lambda _checked=False, did=_did: self._start_learn_bearing(did))
                self._learn_btn = learn_btn
                bearing_row.addWidget(learn_btn)
                form.addRow("Bearing (°)", bearing_row)
            else:
                form.addRow("Bearing (°)", bearing)
            if d.type in ("camera", "loudspeaker"):
                tilt = self._spin(float(getattr(d, "tilt_deg", 0.0) or 0.0), lambda v: self._set_tilt(d, v))
                tilt.setRange(-90, 90)
                form.addRow("Tilt (°)", tilt)
        caps = cp.device_capabilities(d)
        captxt = " · ".join([s for s, on in [("AEC", caps.aec), ("automix", caps.automix), ("mute", caps.mute)] if on]) or "—"
        if caps.camera is not None:
            captxt = f"FOV {caps.camera.fov_h_deg:.0f}°×{caps.camera.fov_v_deg:.0f}° · {caps.camera.max_range_m:.0f} m"
        elif caps.speaker is not None:
            captxt = f"dispersion {caps.speaker.dispersion_h_deg:.0f}°×{caps.speaker.dispersion_v_deg:.0f}°"
        form.addRow("Caps", QLabel(captxt))
        self.sel_layout.addLayout(form)
        if cp.is_mic_device(d) or d.type == "processor":
            dsp = QPushButton("Edit DSP chain →")
            dsp.setToolTip("Open the Route panel's processing chain for this device")
            dsp.clicked.connect(lambda: self._win("_goto_mode", "route"))
            self.sel_layout.addWidget(dsp)
        rm = QPushButton("Delete device")
        rm.setProperty("danger", "true")
        rm.clicked.connect(lambda: self.state.set_config(cp.remove_device(self.state.config, d.id)))
        self.sel_layout.addWidget(rm)

    def _set_pos(self, did, x, y):
        d = next(z for z in self.state.config.devices if z.id == did)
        nx = x if x is not None else d.position.x
        ny = y if y is not None else d.position.y
        self.state.set_config(cp.set_device_position(self.state.config, did, Point2D(nx, ny)))

    def _set_bearing(self, d, v):
        if d.type == "camera":
            fn = cp.set_camera_bearing
        elif d.type == "microphoneArray":
            fn = cp.set_array_bearing
        else:
            fn = cp.set_speaker_bearing
        self.state.set_config(fn(self.state.config, d.id, float(v)))

    def _set_tilt(self, d, v):
        fn = cp.set_camera_tilt if d.type == "camera" else cp.set_speaker_tilt
        self.state.set_config(fn(self.state.config, d.id, float(v)))

    # ---- learn-bearing: pure solve + capture wiring ----

    def _apply_learned_bearing(self, array_id: str, ref_point: Point2D, measured_az_deg: float) -> None:
        """Apply the bearing inferred from a DOA measurement of a reference at ``ref_point``.

        Pure, testable apply path — called by the GUI capture callback (hardware) AND
        directly by tests (no audio). One undo step. Guards if the array has no position.
        """
        arr = next((d for d in self.state.config.devices if d.id == array_id), None)
        if arr is None:
            return
        if arr.position is None:
            self._toast("Place the array on the floor-plan first (position required).")
            return
        bearing = cp.learn_bearing(arr.position, ref_point, measured_az_deg)
        self.state.set_config(cp.set_array_bearing(self.state.config, array_id, bearing))

    def _start_learn_bearing(self, array_id: str) -> None:
        """Launch the DOA capture worker; on completion call ``_apply_learned_bearing``.

        Uses the same _CalibWorker path as the Live panel's 'Calibrate front' button.
        Prompts for a reference point (2 m straight ahead of the array in its current
        room +Y) if the array has no room seat within range; uses Point2D(0,2) relative
        shift as a sensible default when no explicit input is given (operator can refine
        via the Bearing spin after). Hardware — not unit-tested.
        """
        import conf_pipeline_control as cc  # noqa: PLC0415 (lazy import — control extra optional)

        if not cc.controls_available():
            self._toast("Learn bearing needs the [control] extra (numpy + sounddevice).")
            return
        arr = next((d for d in self.state.config.devices if d.id == array_id), None)
        if arr is None or arr.position is None:
            self._toast("Place the array on the floor-plan first (position required).")
            return

        # Default reference: 2 m in the room +Y direction from the array.
        # The operator stands at this point and talks for ~4 s.
        ref_point = Point2D(arr.position.x, arr.position.y + 2.0)

        # Reuse the same geometry the Live panel uses: default 8-capsule POLARIS ring,
        # capsule 5 masked off (the known dead-capsule default), 35 mm radius.
        DEAD_CAPSULE = 5
        POLARIS_RADIUS_M = 0.035
        geom = cc.sensibel_8(radius_m=POLARIS_RADIUS_M)
        geom = cc.with_active_channels(geom, [i != DEAD_CAPSULE for i in range(8)])

        # device: try to find a connected input device; fall back to 0 (will fail loudly).
        try:
            from conf_pipeline_control.audio import list_input_devices
            devs = list_input_devices()
            polaris = next(
                (d for d in devs if "POLARIS" in (d.name or "").upper() or "SB-" in (d.name or "").upper()),
                devs[0] if devs else None,
            )
            device = polaris.index if polaris else 0
        except Exception:
            device = 0

        self._toast("Learning bearing — talk from 2 m in front of the array for ~4 s…")
        btn = getattr(self, "_learn_btn", None)
        if btn is not None:
            btn.setEnabled(False)
        self._learn_ref_point = ref_point
        self._learn_array_id = array_id
        worker = _CalibWorker(geom, device, 44100, 90.0)
        worker.signals.done.connect(self._on_learn_bearing_done)
        worker.signals.failed.connect(self._on_learn_bearing_failed)
        self._calib_workers.add(worker)
        QThreadPool.globalInstance().start(worker)

    def _on_learn_bearing_done(self, payload) -> None:
        self._calib_workers.clear()
        btn = getattr(self, "_learn_btn", None)
        if btn is not None:
            btn.setEnabled(True)
        az, sal = payload
        if az is None:
            self._toast("Learn bearing: no clear talker detected — try again, louder / closer.")
            return
        ref_point = getattr(self, "_learn_ref_point", Point2D(0.0, 2.0))
        array_id = getattr(self, "_learn_array_id", None)
        if array_id is None:
            return
        self._apply_learned_bearing(array_id, ref_point, az)
        arr = next((d for d in self.state.config.devices if d.id == array_id), None)
        bearing = getattr(arr, "bearing_deg", None) if arr else None
        self._toast(
            f"Bearing learned: DOA {az:.0f}° at reference → bearing set to {bearing:.0f}° "
            f"({sal:.0f} dB)."
        )

    def _on_learn_bearing_failed(self, msg: str) -> None:
        self._calib_workers.clear()
        btn = getattr(self, "_learn_btn", None)
        if btn is not None:
            btn.setEnabled(True)
        self._toast(f"Learn bearing failed: {msg}")

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
        rm.setProperty("danger", "true")
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
