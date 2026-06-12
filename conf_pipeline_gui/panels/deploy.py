"""DEPLOY panel: pre-flight checklist, deploy + inline diff, export, raw JSON.

The old Issues-tab coverage report becomes a checklist row, the JSON debug view
drops out of primary navigation into a collapsed Data card, and the deploy diff
renders inline instead of vanishing into a toast.
"""
from __future__ import annotations

from PySide6.QtGui import QFont, QGuiApplication
from PySide6.QtWidgets import (
    QCheckBox,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

import conf_pipeline as cp

from .common import Card, PanelBase


class DeployPanel(PanelBase):
    MODE = "deploy"
    TITLE = "Deploy"

    def __init__(self, state):
        super().__init__(state)
        root = QVBoxLayout(self)
        root.setContentsMargins(10, 8, 10, 8)
        root.addWidget(self._header())
        root.addWidget(self._scroll(self._build_body()), 1)
        self.refresh()

    def _build_body(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)

        check = QGroupBox("Pre-flight")
        cl = QVBoxLayout(check)
        vrow = QHBoxLayout()
        self.check_valid = QLabel("")
        self.check_valid.setWordWrap(True)
        review = QPushButton("Review →")
        review.setToolTip("Open the issues drawer")
        review.clicked.connect(lambda: self._win("_show_issues"))
        vrow.addWidget(self.check_valid, 1)
        vrow.addWidget(review)
        cl.addLayout(vrow)
        self.check_coverage = QLabel("")
        self.check_coverage.setWordWrap(True)
        cl.addWidget(self.check_coverage)
        nrow = QHBoxLayout()
        self.check_naming = QLabel("Naming scheme — apply a consistent device naming")
        self.check_naming.setWordWrap(True)
        name_btn = QPushButton("Apply")
        name_btn.clicked.connect(lambda: self._win("_auto_name"))
        nrow.addWidget(self.check_naming, 1)
        nrow.addWidget(name_btn)
        cl.addLayout(nrow)
        lay.addWidget(check)

        online = QGroupBox("Online room (simulated)")
        ol = QVBoxLayout(online)
        orow = QHBoxLayout()
        self.online_btn = QPushButton("Go online")
        self.online_btn.setToolTip(
            "Open the simulated room: discover and connect its devices.\n"
            "The room contains whatever was last deployed (or, before any deploy, the current design)."
        )
        self.online_btn.clicked.connect(self._toggle_online)
        self.online_summary = QLabel("Offline — device status appears here while online.")
        self.online_summary.setWordWrap(True)
        orow.addWidget(self.online_btn)
        orow.addWidget(self.online_summary, 1)
        ol.addLayout(orow)
        self.device_rows = QVBoxLayout()
        self.device_rows.setSpacing(2)
        ol.addLayout(self.device_rows)
        self.push_btn = QPushButton("⇪ Deploy to online devices")
        self.push_btn.setProperty("accent", "true")
        self.push_btn.setToolTip(
            "Push the design to every online device, read each back, and show the\n"
            "reconcile diff (device-reported vs designed). The deploy snapshot only\n"
            "updates when everything pushed and matches."
        )
        self.push_btn.clicked.connect(self._push_online)
        self.push_btn.setEnabled(False)
        ol.addWidget(self.push_btn)
        lay.addWidget(online)

        deploy_btn = QPushButton("⇪ Deploy — snapshot this design")
        deploy_btn.setProperty("accent", "true")
        deploy_btn.setToolTip("Mark the current design as deployed and show the diff since the last deploy")
        deploy_btn.clicked.connect(self._deploy)
        lay.addWidget(deploy_btn)
        self.deploy_info = QLabel("")
        self.deploy_info.setWordWrap(True)
        lay.addWidget(self.deploy_info)

        share = QGroupBox("Share / files")
        sl = QHBoxLayout(share)
        for label, action, tip in [
            ("Import…", "_import", "Load a config from JSON"),
            ("Export…", "_export", "Save this config to JSON"),
            ("Report…", "_export_report", "Export a shareable Markdown/HTML design report"),
        ]:
            b = QPushButton(label)
            b.setToolTip(tip)
            b.clicked.connect(lambda _c=False, a=action: self._win(a))
            sl.addWidget(b)
        lay.addWidget(share)

        self.data_card = Card("Data (raw config JSON)", collapsed=True)
        self.json_view = QPlainTextEdit()
        self.json_view.setReadOnly(True)
        self.json_view.setFont(QFont("Consolas", 10))
        self.json_view.setMinimumHeight(220)
        self.data_card.body_lay.addWidget(self.json_view)
        copy_btn = QPushButton("Copy JSON")
        copy_btn.clicked.connect(self._copy_json)
        self.data_card.body_lay.addWidget(copy_btn)
        self.data_card.header.clicked.connect(self._maybe_fill_json)
        lay.addWidget(self.data_card)

        lay.addStretch(1)
        return w

    # ---- actions ----
    def _toggle_online(self):
        if self.state.online:
            self.state.go_offline()
        else:
            self.state.go_online()

    def _rebuild_device_rows(self):
        while self.device_rows.count():
            item = self.device_rows.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()
        rows = self.state.device_status()
        for r in rows:
            roww = QWidget()
            rl = QHBoxLayout(roww)
            rl.setContentsMargins(2, 0, 2, 0)
            dot = "●" if r.online else "○"
            tags = []
            if not r.online:
                tags.append("offline")
            elif r.connected:
                tags.append("connected")
            if r.new_since_deploy:
                tags.append("new since deploy")
            elif r.changed_since_deploy:
                tags.append("changed since deploy")
            lab = QLabel(f"{dot} {r.device_id} — {r.label}" + (f"  ·  {', '.join(tags)}" if tags else ""))
            lab.setWordWrap(True)
            rl.addWidget(lab, 1)
            sim = QCheckBox("offline")
            sim.setToolTip("Simulate unplugging this device from the room")
            sim.setChecked(not r.online)
            sim.toggled.connect(lambda on, did=r.device_id: self.state.simulate_device_offline(did, on))
            rl.addWidget(sim)
            self.device_rows.addWidget(roww)

    def _push_online(self):
        report = self.state.push_online()
        if report is None:
            self.deploy_info.setText("Go online first — there is no room to push to.")
            return
        self.deploy_info.setText(report.summary())
        if report.complete and report.clean:
            self._toast("Pushed — room is in sync with the design")
        else:
            self._toast("Pushed with issues — see the report")

    def _deploy(self):
        diff = self.state.deploy()
        if diff.identical:
            self.deploy_info.setText("Deployed — no changes since the last deploy.")
            return self._toast("Deployed — no changes since last deploy")
        lines = []
        for label, items in [("Added", diff.devices_added), ("Removed", diff.devices_removed),
                             ("Changed", diff.devices_changed)]:
            if items:
                lines.append(f"{label} devices: " + ", ".join(items))
        if diff.routes_added:
            lines.append(f"Routes added: {len(diff.routes_added)}")
        if diff.routes_removed:
            lines.append(f"Routes removed: {len(diff.routes_removed)}")
        self.deploy_info.setText("Deployed.\n" + "\n".join(f"• {ln}" for ln in lines))
        self._toast("Deployed")

    def _copy_json(self):
        QGuiApplication.clipboard().setText(cp.serialize(self.state.config, pretty=True))
        self._toast("Config JSON copied")

    def _maybe_fill_json(self):
        if self.data_card.body.isVisible():
            self.json_view.setPlainText(cp.serialize(self.state.config, pretty=True))

    # ---- refresh ----
    def refresh(self):
        super().refresh()
        self._refreshing = True
        try:
            cfg = self.state.config
            res = cp.validate(cfg)
            self.check_valid.setText(
                "✓ Validation — no errors" + (f", {len(res.warnings)} warning(s)" if res.warnings else "")
                if res.ok else f"✗ Validation — {len(res.errors)} error(s), {len(res.warnings)} warning(s)"
            )
            rep = cp.coverage_report(cfg)
            if not cfg.talkers:
                self.check_coverage.setText("Coverage — no talkers placed.")
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
                self.check_coverage.setText("Coverage — " + "  ·  ".join(parts))
            deployed = self.state.rooms[self.state.active_room].get("last_deployed")
            if deployed is not None and not self.deploy_info.text():
                self.deploy_info.setText("Previously deployed — Deploy again to snapshot the latest changes.")
            # online-room status
            self.online_btn.setText("Go offline" if self.state.online else "Go online")
            self.push_btn.setEnabled(self.state.online)
            rows = self.state.device_status()
            if not self.state.online:
                self.online_summary.setText("Offline — device status appears here while online.")
            elif not rows:
                self.online_summary.setText("Online — no devices in the design yet.")
            else:
                n_on = sum(1 for r in rows if r.online)
                n_pending = sum(1 for r in rows if r.changed_since_deploy or r.new_since_deploy)
                parts = [f"{n_on}/{len(rows)} online"]
                if n_pending:
                    parts.append(f"{n_pending} changed/new since deploy")
                self.online_summary.setText("Online — " + " · ".join(parts))
            self._rebuild_device_rows()
            # serialize only when the user actually has the card open
            if self.data_card.body.isVisible():
                self.json_view.setPlainText(cp.serialize(cfg, pretty=True))
        finally:
            self._refreshing = False
