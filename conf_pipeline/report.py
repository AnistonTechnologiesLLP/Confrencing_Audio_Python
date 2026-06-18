"""Shareable design report (Markdown / HTML), read-only over a ``SystemConfig``.

Markdown is the source of truth; :func:`design_report` ``fmt="html"`` converts it
with a small in-module converter (stdlib ``html.escape`` only — no dependency).
Sections reuse the existing engine functions (routing, validation, coverage).
"""
from __future__ import annotations

import html
from dataclasses import dataclass

from .api import talker_coverage
from .coverage_check import coverage_report, zone_coverage_report
from .model import SystemConfig, is_mic_device, is_pickup_zone
from .routing import routing_summary, signal_flow_report
from .validation import validate


def design_report(config: SystemConfig, fmt: str = "markdown") -> str:
    if fmt == "markdown":
        return _markdown(config)
    if fmt == "html":
        return _md_to_html(_markdown(config))
    raise ValueError(f"Unknown report format: {fmt!r} (expected 'markdown' or 'html').")


# --------------------------------------------------------------------------- #
# Commissioning / as-built report (design report + measured live state)
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class CommissioningInfo:
    """Runtime / measured state captured during commissioning, layered onto the
    pure :class:`SystemConfig` design report. Every field is optional — a report
    built from a default ``CommissioningInfo()`` is just the as-built config plus
    a sign-off checklist (the honest "we never went live" case). The GUI fills
    these from the running engine; the pure library never reads a clock or device.

    ``silent_capsules`` is ``None`` when capsule health was never probed, ``()``
    when probed and all live, or the 1-based indices found silent. Latency is
    always framed as *estimated* (summed from stage constants, not measured)."""
    site: str = ""
    commissioned_by: str = ""
    date: str = ""
    notes: str = ""
    listening_mode: str = ""
    estimated_latency_ms: float | None = None
    latency_target_ms: float = 150.0
    active_cleaning_stages: str = ""
    aec_ref_source: str = ""
    aec_erle_db: float | None = None
    bed_reduction_db: float | None = None      # A/B proof: how much quieter the background got
    rms_reduction_db: float | None = None      # A/B proof: broadband level delta
    front_offset_deg: float | None = None
    silent_capsules: tuple[int, ...] | None = None


def commissioning_report(
    config: SystemConfig, info: CommissioningInfo | None = None, fmt: str = "markdown"
) -> str:
    """An integrator deliverable: the as-built configuration plus the measured
    live state (estimated latency, AEC/ERLE, A/B noise-bed proof, capsule health,
    front calibration) and a derived pass/fail sign-off checklist."""
    info = info or CommissioningInfo()
    md = _commission_markdown(config, info)
    if fmt == "markdown":
        return md
    if fmt == "html":
        return _md_to_html(md)
    raise ValueError(f"Unknown report format: {fmt!r} (expected 'markdown' or 'html').")


def _commission_markdown(config: SystemConfig, info: CommissioningInfo) -> str:
    return "\n\n".join(
        s for s in (
            _commission_header(config, info),
            _commission_room(config),
            _device_section(config),
            _routing_section(config),
            _aec_section(config),
            _coverage_section(config),
            _coverage_areas_section(config),
            _control_section(config),
            _commission_measurements(info),
            _commission_health(info),
            _validation_section(config),
            _commission_signoff(config, info),
        ) if s
    )


def _commission_header(config: SystemConfig, info: CommissioningInfo) -> str:
    name = config.metadata.get("name", "Untitled")
    lines = [f"# Commissioning report — {name}", ""]
    for label, val in (("Site", info.site), ("Commissioned by", info.commissioned_by), ("Date", info.date)):
        lines.append(f"- {label}: {val or '—'}")
    if info.listening_mode:
        lines.append(f"- Listening mode: {info.listening_mode}")
    if info.notes:
        lines.append(f"- Notes: {info.notes}")
    return "\n".join(lines)


def _commission_room(config: SystemConfig) -> str:
    return "\n".join(["## Room", *_room_facts(config)])


def _commission_measurements(info: CommissioningInfo) -> str:
    lines = []
    if info.estimated_latency_ms is not None:
        within = "within" if info.estimated_latency_ms <= info.latency_target_ms else "ABOVE"
        lines.append(
            f"- Estimated end-to-end latency: ~{info.estimated_latency_ms:.0f} ms "
            f"({within} the ≤ {info.latency_target_ms:.0f} ms target)"
        )
    if info.active_cleaning_stages:
        lines.append(f"- Active cleaning: {info.active_cleaning_stages}")
    if info.aec_ref_source:
        lines.append(f"- AEC reference source: {info.aec_ref_source}")
    if info.aec_erle_db is not None:
        lines.append(f"- AEC echo reduction (ERLE): {info.aec_erle_db:.1f} dB")
    if info.bed_reduction_db is not None:
        lines.append(f"- Background-noise reduction (A/B proof): {info.bed_reduction_db:.1f} dB quieter")
    if info.rms_reduction_db is not None:
        lines.append(f"- Broadband level change (A/B proof): {info.rms_reduction_db:.1f} dB")
    if not lines:
        return ""
    return "\n".join(["## Live measurements", *lines])


def _commission_health(info: CommissioningInfo) -> str:
    lines = []
    if info.front_offset_deg is not None:
        lines.append(f"- Front calibration offset: {info.front_offset_deg:+.0f}°")
    if info.silent_capsules is not None:
        if info.silent_capsules:
            lines.append("- Silent / disabled capsules: " + ", ".join(str(c) for c in info.silent_capsules))
        else:
            lines.append("- Capsule health: all capsules active")
    if not lines:
        return ""
    return "\n".join(["## Health & calibration", *lines])


def _commission_signoff(config: SystemConfig, info: CommissioningInfo) -> str:
    """A derived pass/fail checklist — the integrator's at-a-glance acceptance
    view — plus a hand-signed form. Each check is computed from the config /
    validation / measured info, never assumed passing."""
    res = validate(config)
    mics = [d for d in config.devices if is_mic_device(d)]
    checks: list[tuple[bool, str]] = [
        (config.room is not None and bool(config.room.vertices), "Room geometry defined"),
        (bool(mics), "At least one microphone / array present"),
    ]
    if config.talkers:
        rep = coverage_report(config)
        checks.append((not rep.uncovered, f"All talkers within coverage ({len(rep.covered)}/{len(config.talkers)})"))
    aec_mics = [m for m in mics if m.aec.enabled]
    if aec_mics:
        checks.append((all(m.aec.reference_bus_id for m in aec_mics),
                       "AEC reference assigned for every echo-cancelling mic"))
    checks.append((not res.errors,
                   f"No configuration errors ({len(res.errors)} error(s), {len(res.warnings)} warning(s))"))
    if info.estimated_latency_ms is not None:
        checks.append((info.estimated_latency_ms <= info.latency_target_ms,
                       f"Estimated latency within target (~{info.estimated_latency_ms:.0f} ms ≤ {info.latency_target_ms:.0f} ms)"))
    if info.bed_reduction_db is not None:
        checks.append((info.bed_reduction_db > 0,
                       f"Noise reduction verified by A/B proof ({info.bed_reduction_db:.1f} dB quieter)"))
    if info.silent_capsules is not None:
        checks.append((not info.silent_capsules, "All capsules active (no silent capsules)"))

    passed = sum(1 for ok, _ in checks if ok)
    lines = ["## Commissioning sign-off", ""]
    lines += [f"- [{'x' if ok else ' '}] {label}" for ok, label in checks]
    lines += ["", f"Checks passing: {passed}/{len(checks)}.", ""]
    lines += [
        "| Field | |",
        "| --- | --- |",
        f"| Commissioned by | {info.commissioned_by or '________________'} |",
        "| Signature | ________________ |",
        f"| Date | {info.date or '________________'} |",
        "| Customer sign-off | ________________ |",
    ]
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Markdown sections
# --------------------------------------------------------------------------- #
def _markdown(config: SystemConfig) -> str:
    return "\n\n".join(
        s for s in (
            _room_section(config),
            _device_section(config),
            _routing_section(config),
            _aec_section(config),
            _coverage_section(config),
            _coverage_areas_section(config),
            _control_section(config),
            _validation_section(config),
        ) if s
    )


def _room_facts(config: SystemConfig) -> list[str]:
    room = config.room
    if room is not None and room.vertices:
        from .sim import estimated_rt60  # lazy: keep report import light
        xs = [v.x for v in room.vertices]
        ys = [v.y for v in room.vertices]
        return [
            f"- Room: {max(xs) - min(xs):.1f} × {max(ys) - min(ys):.1f} × {room.height:.1f} m",
            f"- Estimated RT60: {estimated_rt60(config):.2f} s",
        ]
    return ["- Room: not defined"]


def _room_section(config: SystemConfig) -> str:
    name = config.metadata.get("name", "Untitled")
    return "\n".join([f"# Design report — {name}", "", *_room_facts(config)])


def _device_section(config: SystemConfig) -> str:
    lines = [
        "## Devices", "",
        "| ID | Label | Type | Profile | Ports | Elev (m) | AEC ref |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for d in config.devices:
        elev = "" if d.elevation is None else f"{d.elevation:.2f}"
        aec = ""
        if is_mic_device(d):
            aec = (d.aec.reference_bus_id or "(none)") if d.aec.enabled else "off"
        lines.append(f"| {d.id} | {d.label} | {d.type} | {d.profile_id or '—'} | {len(d.ports)} | {elev} | {aec} |")
    return "\n".join(lines)


def _routing_section(config: SystemConfig) -> str:
    s = routing_summary(config)
    flow = signal_flow_report(config) or "(no routes)"
    return (
        "## Routing\n\n"
        f"- {s['total']} route(s) · {s['dante']} Dante · {s['analog']} analog\n\n"
        "```\n" + flow + "\n```"
    )


def _aec_section(config: SystemConfig) -> str:
    mics = [d for d in config.devices if is_mic_device(d)]
    if not mics:
        return ""
    lines = ["## AEC references"]
    for m in mics:
        lines.append(f"- {m.label}: {m.aec.reference_bus_id or '(none)'}" if m.aec.enabled else f"- {m.label}: disabled")
    return "\n".join(lines)


def _coverage_section(config: SystemConfig) -> str:
    if not config.talkers:
        return ""
    label_of = {t.id: t.label for t in config.talkers}
    rep = coverage_report(config)
    lines = [
        "## Coverage",
        f"- {len(rep.covered)}/{len(config.talkers)} talkers within an array coverage circle",
    ]
    if rep.uncovered:
        lines.append("- Uncovered: " + ", ".join(label_of.get(t, t) for t in rep.uncovered))
    if rep.overlaps:
        lines.append("- Overlapping arrays: " + ", ".join(f"{a}/{b}" for a, b in rep.overlaps))
    for t in config.talkers:
        cov = talker_coverage(config, t.id)
        status = "captured" if cov.captured else ("excluded" if cov.excluded_by else "not in a pickup zone")
        lines.append(f"- {t.label}: {status}")
    return "\n".join(lines)


def _coverage_areas_section(config: SystemConfig) -> str:
    """Per-array coverage areas with their output channel / gain trim, plus the
    zone-vs-coverage findings (Designer-style steerable-coverage view)."""
    arrays = [d for d in config.devices if d.type == "microphoneArray"]
    pickup_zones = [z for a in arrays for z in a.zones if is_pickup_zone(z)]
    if not pickup_zones:
        return ""
    lines = [
        "## Coverage areas",
        "",
        "| Array | Area | Type | Out channel | Gain (dB) |",
        "| --- | --- | --- | --- | --- |",
    ]
    for a in arrays:
        for z in a.zones:
            if not is_pickup_zone(z):
                continue
            ch = str(z.output_channel) if z.output_channel is not None else "—"
            gain = f"{z.gain_db:+.1f}" if z.gain_db is not None else "0.0"
            lines.append(f"| {a.label} | {z.label} | {z.type} | {ch} | {gain} |")
    zrep = zone_coverage_report(config)
    if zrep.zones:
        covered = sum(1 for z in zrep.zones if z.centroid_covered)
        lines.append("")
        lines.append(f"- {covered}/{len(zrep.zones)} coverage area(s) inside their array's pickup circle")
        if zrep.uncovered:
            lines.append("- Outside coverage: " + ", ".join(f"{z.zone_label} ({z.array_id})" for z in zrep.uncovered))
        if zrep.partial:
            lines.append("- Partially covered (edges outside): " + ", ".join(f"{z.zone_label} ({z.array_id})" for z in zrep.partial))
        if zrep.contended:
            lines.append("- Lobe contention (2+ arrays cover the area): " + ", ".join(f"{z.zone_label}" for z in zrep.contended))
    return "\n".join(lines)


def _control_section(config: SystemConfig) -> str:
    if config.control is None or not config.control.mute_groups:
        return ""
    lines = ["## Mute groups"]
    for g in config.control.mute_groups:
        members = list(g.device_ids) + [f"{r.array_id}/{r.zone_id}" for r in g.zone_refs]
        state = "muted" if g.muted else "unmuted"
        lines.append(f"- {g.label} [{g.trigger}, {state}]: {', '.join(members) or '(empty)'}")
    return "\n".join(lines)


def _validation_section(config: SystemConfig) -> str:
    res = validate(config)
    lines = ["## Validation"]
    if not res.errors and not res.warnings:
        lines.append("- No issues")
        return "\n".join(lines)
    for e in res.errors:
        lines.append(f"- ERROR [{e.code}] {e.message}")
    for w in res.warnings:
        lines.append(f"- WARN [{w.code}] {w.message}")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Minimal Markdown -> HTML (only the constructs we emit)
# --------------------------------------------------------------------------- #
_CSS = (
    "body{font-family:Segoe UI,Arial,sans-serif;margin:24px;color:#1a1a1a}"
    "h1{font-size:20px}h2{font-size:15px;margin-top:22px;border-bottom:1px solid #ddd;padding-bottom:3px}"
    "table{border-collapse:collapse;font-size:13px;margin:6px 0}"
    "th,td{border:1px solid #ccc;padding:3px 8px;text-align:left}th{background:#f2f2f2}"
    "pre{background:#f6f6f6;border:1px solid #ddd;padding:8px;font-size:12px;overflow:auto}"
    "ul{margin:4px 0}li{margin:2px 0}"
)


def _is_separator(cells: list[str]) -> bool:
    return all(c.strip() and set(c) <= set("-: ") for c in cells)


def _html_table(rows: list[str]) -> str:
    parsed = [[c.strip() for c in r.strip().strip("|").split("|")] for r in rows]
    parsed = [r for r in parsed if not _is_separator(r)]
    if not parsed:
        return ""
    head, *body = parsed
    th = "".join(f"<th>{html.escape(c)}</th>" for c in head)
    trs = "".join("<tr>" + "".join(f"<td>{html.escape(c)}</td>" for c in r) + "</tr>" for r in body)
    return f"<table><thead><tr>{th}</tr></thead><tbody>{trs}</tbody></table>"


def _md_to_html(md: str) -> str:
    lines = md.split("\n")
    out = [f"<!doctype html><html><head><meta charset='utf-8'><style>{_CSS}</style></head><body>"]
    i, n = 0, len(lines)
    in_code = False
    while i < n:
        line = lines[i]
        if line.strip().startswith("```"):
            out.append("<pre>" if not in_code else "</pre>")
            in_code = not in_code
            i += 1
            continue
        if in_code:
            out.append(html.escape(line))
            i += 1
            continue
        if line.startswith("## "):
            out.append(f"<h2>{html.escape(line[3:])}</h2>")
        elif line.startswith("# "):
            out.append(f"<h1>{html.escape(line[2:])}</h1>")
        elif line.startswith("|"):
            rows = []
            while i < n and lines[i].startswith("|"):
                rows.append(lines[i])
                i += 1
            out.append(_html_table(rows))
            continue
        elif line.startswith("- "):
            out.append("<ul>")
            while i < n and lines[i].startswith("- "):
                out.append(f"<li>{html.escape(lines[i][2:])}</li>")
                i += 1
            out.append("</ul>")
            continue
        elif line.strip():
            out.append(f"<p>{html.escape(line)}</p>")
        i += 1
    if in_code:
        out.append("</pre>")
    out.append("</body></html>")
    return "\n".join(out)
