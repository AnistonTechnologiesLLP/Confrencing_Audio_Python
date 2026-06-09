"""Shareable design report (Markdown / HTML), read-only over a ``SystemConfig``.

Markdown is the source of truth; :func:`design_report` ``fmt="html"`` converts it
with a small in-module converter (stdlib ``html.escape`` only — no dependency).
Sections reuse the existing engine functions (routing, validation, coverage).
"""
from __future__ import annotations

import html

from .api import talker_coverage
from .coverage_check import coverage_report
from .model import SystemConfig, is_mic_device
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
