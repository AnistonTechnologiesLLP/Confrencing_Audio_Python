"""ROUTE panel: auto-routing, signal flow, AEC references, DSP chains, mute groups.

Merges the old Routing and AEC/DSP inspector tabs — they are one job (wire the
signal chain). The mute-group editor moves verbatim (it is smoke-tested by
attribute and method name).
"""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

import conf_pipeline as cp
from conf_pipeline.model import AecConfig

from .common import BLOCK_LABELS, BLOCK_PARAM_SCHEMA, NoWheelDoubleSpinBox, PanelBase, clear_layout


class RoutePanel(PanelBase):
    MODE = "route"
    TITLE = "Route"

    def __init__(self, state):
        super().__init__(state)
        self._dsp_seq = 1
        root = QVBoxLayout(self)
        root.setContentsMargins(10, 8, 10, 8)
        root.addWidget(self._header())

        btns = QHBoxLayout()
        auto_route = QPushButton("⚡ Auto-Route")
        auto_route.setProperty("accent", "true")
        auto_route.setToolTip("AEC references + automixer + far-end & near-end routing")
        auto_route.clicked.connect(lambda: self._win("_auto_route"))
        auto = QPushButton("Auto-configure")
        auto.setToolTip("AEC + automixer buses only (no reinforcement routing)")
        auto.clicked.connect(lambda: self._win("_auto"))
        btns.addWidget(auto_route)
        btns.addWidget(auto)
        root.addLayout(btns)

        root.addWidget(self._scroll(self._build_body()), 1)
        self.refresh()

    def _build_body(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)

        self.routing_summary_lbl = QLabel()
        lay.addWidget(self.routing_summary_lbl)
        lay.addWidget(QLabel("Signal flow (Dante hub / routing):"))
        self.routing_view = QPlainTextEdit()
        self.routing_view.setReadOnly(True)
        self.routing_view.setFont(QFont("Consolas", 10))
        self.routing_view.setMinimumHeight(160)
        lay.addWidget(self.routing_view, 1)

        proc = QGroupBox("Processing (AEC · automixer · DSP chain)")
        self.dsp_layout = QVBoxLayout(proc)
        lay.addWidget(proc)

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
        lay.addStretch(1)
        return w

    # ---- mute-group actions (verbatim; smoke-tested by name) ----
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
        self.mute_group_toggle.setEnabled(bool(groups))

    # ---- AEC / automixer / DSP chain (moved verbatim) ----
    def _refresh_dsp(self):
        clear_layout(self.dsp_layout)
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

    def _dsp_blocks_section(self):
        sep = QFrame()
        sep.setFrameShape(QFrame.HLine)
        self.dsp_layout.addWidget(sep)
        self.dsp_layout.addWidget(QLabel("Processing blocks (selected device)"))
        sel = self.state.selection
        if not (sel and sel["kind"] == "device"):
            self.dsp_layout.addWidget(QLabel("Select a device (canvas or Design panel) to edit its chain."))
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

    # ---- refresh ----
    def refresh(self):
        super().refresh()
        self._refreshing = True
        try:
            cfg = self.state.config
            s = cp.routing_summary(cfg)
            self.routing_summary_lbl.setText(f"{s['total']} route(s) · {s['dante']} Dante · {s['analog']} analog")
            self.routing_view.setPlainText(cp.signal_flow_report(cfg))
            self._refresh_mute_groups(cfg)
            self._refresh_dsp()
        finally:
            self._refreshing = False
