"""Inspector side panel: Build / AEC-DSP / Issues / JSON tabs."""
from __future__ import annotations

from PySide6.QtCore import Qt, QTimer
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
    QPushButton,
    QScrollArea,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

import conf_pipeline as cp
from conf_pipeline.model import AecConfig, Point2D

from .state import AppState

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


class Inspector(QWidget):
    def __init__(self, state: AppState):
        super().__init__()
        self.state = state
        self._refreshing = False
        self._refresh_pending = False
        self._dsp_seq = 1
        self.setMinimumWidth(380)

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        self.tabs = QTabWidget()
        root.addWidget(self.tabs)

        self.tabs.addTab(self._scroll(self._build_tab()), "Build")
        self.tabs.addTab(self._scroll(self._dsp_tab()), "AEC / DSP")
        self.tabs.addTab(self._routing_tab(), "Routing")
        self.tabs.addTab(self._issues_tab(), "Issues")
        self.tabs.addTab(self._json_tab(), "JSON")

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
        sa.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
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
        lay.addWidget(self.routing_view)
        return w

    def _issues_tab(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        self.issue_badge = QLabel()
        lay.addWidget(self.issue_badge)
        self.issue_list = QListWidget()
        self.issue_list.itemClicked.connect(self._on_issue_click)
        lay.addWidget(self.issue_list)
        return w

    def _json_tab(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        self.json_view = QPlainTextEdit()
        self.json_view.setReadOnly(True)
        self.json_view.setFont(QFont("Consolas", 10))
        lay.addWidget(self.json_view)
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
        self.issue_badge.setText(("✓ Valid" if res.ok else f"✗ {len(res.errors)} error(s)") + f" · {len(res.warnings)} warning(s)")
        self.issue_list.clear()
        ic = ISSUE_COLORS[getattr(self.state, "theme", "dark")]
        for i in [*res.errors, *res.warnings]:
            it = QListWidgetItem(f"[{i.code}] {i.message}")
            it.setForeground(QColor(ic["error"] if i.severity == "error" else ic["warning"]))
            it.setData(Qt.UserRole, i.refs)
            self.issue_list.addItem(it)
        # routing
        s = cp.routing_summary(cfg)
        self.routing_summary_lbl.setText(f"{s['total']} route(s) · {s['dante']} Dante · {s['analog']} analog")
        self.routing_view.setPlainText(cp.signal_flow_report(cfg))
        # json
        self.json_view.setPlainText(cp.serialize(cfg, pretty=True))
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
            rm = QPushButton("Delete zone")
            rm.clicked.connect(lambda: self.state.set_config(cp.remove_coverage_zone(cfg, sel["array_id"], sel["zone_id"])))
            self.sel_layout.addWidget(rm)

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
