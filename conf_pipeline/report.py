"""Shareable design report (Markdown / HTML), read-only over a ``SystemConfig``.

Markdown is the source of truth; :func:`design_report` ``fmt="html"`` converts it
with a small in-module converter (stdlib ``html.escape`` only — no dependency).
Sections reuse the existing engine functions (routing, validation, coverage).
"""
from __future__ import annotations

import html

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


def _room_section(config: SystemConfig) -> str:
    name = config.metadata.get("name", "Untitled")
    lines = [f"# Design report — {name}", ""]
    room = config.room
    if room is not None and room.vertices:
        from .sim import estimated_rt60  # lazy: keep report import light
        xs = [v.x for v in room.vertices]
        ys = [v.y for v in room.vertices]
        lines.append(f"- Room: {max(xs) - min(xs):.1f} × {max(ys) - min(ys):.1f} × {room.height:.1f} m")
        lines.append(f"- Estimated RT60: {estimated_rt60(config):.2f} s")
    else:
        lines.append("- Room: not defined")
    return "\n".join(lines)


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
