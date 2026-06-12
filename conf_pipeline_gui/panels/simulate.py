"""SIMULATE panel: placement heatmap, recommendation, physics validation.

Near-verbatim move of the old inspector Simulate tab; the heatmap checkbox is
two-way-synced with the canvas view bar's overlays popover by the shell.
"""
from __future__ import annotations

import dataclasses
import math

from PySide6.QtCore import Qt, QThreadPool, QTimer
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFormLayout,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSlider,
    QVBoxLayout,
    QWidget,
)

import conf_pipeline as cp

from .common import NoWheelDoubleSpinBox, PanelBase, _ValidateWorker, clear_layout


class SimulatePanel(PanelBase):
    MODE = "simulate"
    TITLE = "Simulate"

    def __init__(self, state):
        super().__init__(state)
        self._heat_pending = False
        self._validate_workers = set()  # strong refs to in-flight QRunnables

        root = QVBoxLayout(self)
        root.setContentsMargins(10, 8, 10, 8)
        root.addWidget(self._header())
        root.addWidget(self._scroll(self._build_body()), 1)
        self.refresh()

    def _build_body(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)

        box = QGroupBox("Simulate placement")
        f = QFormLayout(box)
        self.sim_target = QComboBox()  # populated in refresh()
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

    # ---- params / targets ----
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

    # ---- actions ----
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
        if self._refreshing:
            return
        self.state.sim_show_heatmap = bool(on)
        if on:
            self._schedule_heatmap()
        else:
            self.state.sim_heatmap = None
            self.state.changed.emit()

    def set_heatmap(self, on: bool):
        """Shell entry point (view-bar overlays popover)."""
        if self.sim_heat_chk.isChecked() != bool(on):
            self.sim_heat_chk.setChecked(bool(on))  # toggled slot does the rest

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

    # ---- refresh ----
    def refresh(self):
        super().refresh()
        self._refreshing = True
        try:
            cur = self.sim_target.currentData()
            self.sim_target.clear()
            self.sim_target.addItem("(array only — all talkers)", None)
            for t in self.state.config.talkers:
                self.sim_target.addItem(t.label, t.id)
            idx = self.sim_target.findData(cur)
            self.sim_target.setCurrentIndex(idx if idx >= 0 else 0)

            if self.sim_heat_chk.isChecked() != self.state.sim_show_heatmap:
                self.sim_heat_chk.setChecked(self.state.sim_show_heatmap)

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
        finally:
            self._refreshing = False

    def _render_sim_results(self):
        clear_layout(self.sim_results_layout)
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
