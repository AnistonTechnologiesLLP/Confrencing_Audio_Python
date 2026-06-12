"""Local HTTP control API — scene recall / mute / status (pure stdlib).

A tiny JSON-over-HTTP surface for room-control integrations (Crestron-style
scene recall, mute, status polling), in the ``/api/…`` style of the OCTOVOX
bridge. Built on :mod:`http.server` — **no dependency at all**, not even an
optional extra — and bound to localhost by default.

The server owns no configuration. The host application supplies two callables:

- ``get_config() -> SystemConfig`` — read the current config;
- ``apply(transform) -> SystemConfig`` — run ``transform(config)`` and make the
  result current, returning it. The owner decides how to make that safe
  (:class:`ConfigHolder` is the thread-safe headless/test implementation; a
  GUI would marshal onto its main thread).

Routes:

- ``GET  /api/status``                → name, schema version, deployment state,
  mute groups (id/label/muted), scenes (id/label)
- ``GET  /api/scenes``                → scene list with section counts
- ``POST /api/scenes/<id>/recall``    → recall; the response carries the
  scene's config-inert live-layer hints (``steer``, ``activeZones``) so the
  caller can aim the beamformer
- ``POST /api/mute-groups/<id>``      → body ``{"muted": true|false}``

Errors are JSON too: ``{"error": …}`` with 400/404/405 status codes.
"""
from __future__ import annotations

import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Optional

from .api import recall_scene, set_mute_group_muted
from .model import CONFIG_VERSION, SystemConfig, to_jsonable

Transform = Callable[[SystemConfig], SystemConfig]

DEFAULT_HOST = "127.0.0.1"


class ConfigHolder:
    """Thread-safe config owner for headless and test use."""

    def __init__(self, config: SystemConfig) -> None:
        self._config = config
        self._lock = threading.Lock()

    def get(self) -> SystemConfig:
        with self._lock:
            return self._config

    def apply(self, transform: Transform) -> SystemConfig:
        with self._lock:
            self._config = transform(self._config)
            return self._config


def _status_payload(config: SystemConfig) -> dict[str, Any]:
    ctrl = config.control
    return {
        "name": config.metadata.get("name", ""),
        "configVersion": CONFIG_VERSION,
        "deployment": config.deployment.status if config.deployment is not None else None,
        "muteGroups": [
            {"id": g.id, "label": g.label, "muted": g.muted}
            for g in (ctrl.mute_groups if ctrl is not None else [])
        ],
        "scenes": [
            {"id": s.id, "label": s.label}
            for s in (ctrl.scenes if ctrl is not None else [])
        ],
        "schedules": [
            {"id": s.id, "sceneId": s.scene_id, "time": s.time, "days": list(s.days), "enabled": s.enabled}
            for s in (ctrl.schedules if ctrl is not None else [])
        ],
    }


def _scenes_payload(config: SystemConfig) -> dict[str, Any]:
    ctrl = config.control
    return {
        "scenes": [
            {
                "id": s.id,
                "label": s.label,
                "muteStates": len(s.mute_states),
                "zoneStates": len(s.zone_states),
                "steer": len(s.steer),
            }
            for s in (ctrl.scenes if ctrl is not None else [])
        ]
    }


class _ApiHttpServer(ThreadingHTTPServer):
    """ThreadingHTTPServer carrying the config callables for the handler."""

    daemon_threads = True

    def __init__(self, address: tuple[str, int], get_config: Callable[[], SystemConfig], apply: Callable[[Transform], SystemConfig]):
        super().__init__(address, _Handler)
        self.get_config = get_config
        self.apply = apply


_RECALL_RE = re.compile(r"^/api/scenes/([^/]+)/recall$")
_MUTE_RE = re.compile(r"^/api/mute-groups/([^/]+)$")


class _Handler(BaseHTTPRequestHandler):
    server: _ApiHttpServer  # narrowed for the route handlers

    # silence per-request stderr logging — this runs inside an app
    def log_message(self, fmt: str, *args: Any) -> None:
        pass

    def _send(self, code: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json_body(self) -> Optional[dict[str, Any]]:
        try:
            length = int(self.headers.get("Content-Length", "0"))
            parsed = json.loads(self.rfile.read(length).decode("utf-8")) if length else {}
        except (ValueError, json.JSONDecodeError):
            return None
        return parsed if isinstance(parsed, dict) else None

    # ---- routes ----
    def do_GET(self) -> None:  # noqa: N802 (http.server API)
        if self.path == "/api/status":
            self._send(200, _status_payload(self.server.get_config()))
        elif self.path == "/api/scenes":
            self._send(200, _scenes_payload(self.server.get_config()))
        else:
            self._send(404, {"error": f"Unknown route: GET {self.path}"})

    def do_POST(self) -> None:  # noqa: N802 (http.server API)
        m = _RECALL_RE.match(self.path)
        if m:
            return self._recall(m.group(1))
        m = _MUTE_RE.match(self.path)
        if m:
            return self._set_mute(m.group(1))
        self._send(404, {"error": f"Unknown route: POST {self.path}"})

    def _recall(self, scene_id: str) -> None:
        try:
            new = self.server.apply(lambda c: recall_scene(c, scene_id))
        except ValueError as exc:
            return self._send(404, {"error": str(exc)})
        ctrl = new.control
        scene = next((s for s in (ctrl.scenes if ctrl is not None else []) if s.id == scene_id), None)
        assert scene is not None  # recall would have raised otherwise
        self._send(200, {
            "ok": True,
            "recalled": scene_id,
            # config-inert live-layer hints, for the caller to act on
            "steer": to_jsonable(scene.steer),
            "activeZones": to_jsonable([z for z in scene.zone_states if z.active is not None]),
        })

    def _set_mute(self, group_id: str) -> None:
        body = self._read_json_body()
        if body is None or not isinstance(body.get("muted"), bool):
            return self._send(400, {"error": 'Body must be JSON with a boolean "muted".'})
        muted = body["muted"]
        config = self.server.get_config()
        groups = config.control.mute_groups if config.control is not None else []
        if not any(g.id == group_id for g in groups):
            return self._send(404, {"error": f"Unknown mute group: {group_id}"})
        self.server.apply(lambda c: set_mute_group_muted(c, group_id, muted))
        self._send(200, {"ok": True, "id": group_id, "muted": muted})


class ControlApiServer:
    """The local control surface. ``port=0`` picks an ephemeral port (read
    :attr:`port` after :meth:`start`). Usable as a context manager."""

    def __init__(
        self,
        get_config: Callable[[], SystemConfig],
        apply: Callable[[Transform], SystemConfig],
        *,
        host: str = DEFAULT_HOST,
        port: int = 0,
    ) -> None:
        self._get_config = get_config
        self._apply = apply
        self.host = host
        self._requested_port = port
        self._server: Optional[_ApiHttpServer] = None
        self._thread: Optional[threading.Thread] = None

    @property
    def running(self) -> bool:
        return self._server is not None

    @property
    def port(self) -> int:
        if self._server is None:
            return self._requested_port
        return self._server.server_address[1]

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}"

    def start(self) -> None:
        if self._server is not None:
            return
        self._server = _ApiHttpServer((self.host, self._requested_port), self._get_config, self._apply)
        self._thread = threading.Thread(target=self._server.serve_forever, name="control-api", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if self._server is None:
            return
        self._server.shutdown()
        self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)
        self._server = None
        self._thread = None

    def __enter__(self) -> "ControlApiServer":
        self.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.stop()
