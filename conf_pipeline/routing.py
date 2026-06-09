"""Routing views (Designer's enhanced routing / Dante hub). Pure derived data."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from .model import SystemConfig, Transport, find_device, find_port


@dataclass
class Subscription:
    route_id: str
    transport: Transport
    from_device_id: str
    from_device_label: str
    from_port_id: str
    from_port_label: str
    to_device_id: str
    to_device_label: str
    to_port_id: str
    to_port_label: str


def subscriptions(config: SystemConfig, transport: Optional[Transport] = None) -> list[Subscription]:
    out: list[Subscription] = []
    for r in config.routes:
        frm = find_port(config, r.from_port_id)
        to = find_port(config, r.to_port_id)
        if frm is None or to is None:
            continue
        if transport is not None and frm.transport != transport:
            continue
        fd = find_device(config, frm.device_id)
        td = find_device(config, to.device_id)
        out.append(Subscription(
            route_id=r.id, transport=frm.transport,
            from_device_id=frm.device_id, from_device_label=(fd.label if fd else frm.device_id),
            from_port_id=frm.id, from_port_label=frm.label,
            to_device_id=to.device_id, to_device_label=(td.label if td else to.device_id),
            to_port_id=to.id, to_port_label=to.label,
        ))
    return out


def dante_subscriptions(config: SystemConfig) -> list[Subscription]:
    return subscriptions(config, "dante")


def routing_summary(config: SystemConfig) -> dict[str, int]:
    subs = subscriptions(config)
    dante = sum(1 for s in subs if s.transport == "dante")
    return {"dante": dante, "analog": len(subs) - dante, "total": len(subs)}


def signal_flow_report(config: SystemConfig) -> str:
    lines = [f"[{s.transport}] {s.from_device_label} ({s.from_port_label}) -> {s.to_device_label} ({s.to_port_label})" for s in subscriptions(config)]
    return "\n".join(lines) if lines else "(no routes)"
