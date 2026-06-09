"""Deployment workflow (configuration-only; no network/hardware I/O)."""
from __future__ import annotations

import copy
from dataclasses import dataclass, field

from .model import DeploymentState, DeploymentStatus, SystemConfig


def set_deployment_status(config: SystemConfig, status: DeploymentStatus, last_deployed_at: str | None = None) -> SystemConfig:
    new = copy.copy(config)
    new.deployment = DeploymentState(status=status, last_deployed_at=last_deployed_at)
    return new


def mark_deployed(config: SystemConfig, timestamp: str) -> SystemConfig:
    return set_deployment_status(config, "deployed", timestamp)


@dataclass
class DeploymentDiff:
    devices_added: list[str] = field(default_factory=list)
    devices_removed: list[str] = field(default_factory=list)
    devices_changed: list[str] = field(default_factory=list)
    routes_added: list[str] = field(default_factory=list)
    routes_removed: list[str] = field(default_factory=list)
    identical: bool = True


def _fingerprint(d) -> str:
    ports = ",".join(sorted(p.id for p in d.ports))
    blocks = ",".join(sorted(f"{b.id}:{b.kind}:{b.enabled}" for b in (getattr(d, "dsp_blocks", []) or [])))
    return f"{d.label}|{getattr(d, 'profile_id', '') or ''}|{ports}|{blocks}"


def deployment_diff(base: SystemConfig, target: SystemConfig) -> DeploymentDiff:
    base_devs = {d.id: d for d in base.devices}
    target_devs = {d.id: d for d in target.devices}
    added = sorted(i for i in target_devs if i not in base_devs)
    removed = sorted(i for i in base_devs if i not in target_devs)
    changed = sorted(i for i in target_devs if i in base_devs and _fingerprint(base_devs[i]) != _fingerprint(target_devs[i]))
    base_routes = {r.id for r in base.routes}
    target_routes = {r.id for r in target.routes}
    routes_added = sorted(r for r in target_routes if r not in base_routes)
    routes_removed = sorted(r for r in base_routes if r not in target_routes)
    identical = not (added or removed or changed or routes_added or routes_removed)
    return DeploymentDiff(added, removed, changed, routes_added, routes_removed, identical)
