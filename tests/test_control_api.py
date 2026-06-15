"""Local HTTP control API: request/response tests against a live stdlib server
on an ephemeral localhost port (no external deps — urllib only)."""
import json
import urllib.error
import urllib.request

import pytest

import conf_pipeline as cp
from conf_pipeline.model import Point2D, RectShape


def _config():
    c = cp.create_config("Boardroom", "2026-06-12T00:00:00Z")
    c = cp.add_device(c, cp.create_microphone_array("A", "Array"))
    c = cp.add_coverage_zone(c, "A", cp.CoverageZone("z1", "dynamic", RectShape(Point2D(1, 1), 2, 2), False, "Table"))
    c = cp.add_mute_group(c, cp.create_mute_group("mg1", "Room mute", device_ids=["A"]))
    scene = cp.create_scene(
        "lecture", "Lecture",
        mute_states={"mg1": True},
        zone_states=[cp.SceneZoneState("A", "z1", gain_db=-3.0, active=True)],
        steer=[cp.SceneSteer("A", 90.0, 75.0)],
    )
    return cp.add_scene(c, scene)


@pytest.fixture
def served():
    holder = cp.ConfigHolder(_config())
    with cp.ControlApiServer(holder.get, holder.apply) as srv:
        yield srv, holder


def _get(url):
    with urllib.request.urlopen(url, timeout=10) as r:
        return r.status, json.loads(r.read().decode())


def _post(url, body=None):
    data = json.dumps(body).encode() if body is not None else b""
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode())


def test_status_reports_groups_scenes_and_version(served):
    srv, _ = served
    code, d = _get(srv.url + "/api/status")
    assert code == 200
    assert d["name"] == "Boardroom"
    assert d["configVersion"] == cp.CONFIG_VERSION
    assert d["muteGroups"] == [{"id": "mg1", "label": "Room mute", "muted": False}]
    assert d["scenes"] == [{"id": "lecture", "label": "Lecture"}]
    assert d["deployment"] is None


def test_scenes_listing(served):
    srv, _ = served
    code, d = _get(srv.url + "/api/scenes")
    assert code == 200
    assert d["scenes"] == [{"id": "lecture", "label": "Lecture", "muteStates": 1, "zoneStates": 1, "steer": 1}]


def test_recall_applies_config_and_returns_live_hints(served):
    srv, holder = served
    code, d = _post(srv.url + "/api/scenes/lecture/recall")
    assert code == 200 and d["ok"] is True and d["recalled"] == "lecture"
    # config-side effects landed in the holder
    cfg = holder.get()
    assert cfg.control.mute_groups[0].muted is True
    assert cp.find_device(cfg, "A").zones[0].gain_db == -3.0
    # the response carries the config-inert hints for the caller's beamformer
    assert d["steer"] == [{"arrayId": "A", "azimuthDeg": 90.0, "offNadirDeg": 75.0}]
    assert d["activeZones"] == [{"arrayId": "A", "zoneId": "z1", "gainDb": -3.0, "active": True}]


def test_recall_unknown_scene_is_404(served):
    srv, holder = served
    code, d = _post(srv.url + "/api/scenes/ghost/recall")
    assert code == 404 and "Unknown scene" in d["error"]
    assert holder.get().control.mute_groups[0].muted is False   # nothing applied


def test_mute_group_set_and_clear(served):
    srv, holder = served
    code, d = _post(srv.url + "/api/mute-groups/mg1", {"muted": True})
    assert code == 200 and d == {"ok": True, "id": "mg1", "muted": True}
    assert holder.get().control.mute_groups[0].muted is True
    code, _ = _post(srv.url + "/api/mute-groups/mg1", {"muted": False})
    assert code == 200
    assert holder.get().control.mute_groups[0].muted is False


def test_mute_group_error_paths(served):
    srv, _ = served
    code, d = _post(srv.url + "/api/mute-groups/ghost", {"muted": True})
    assert code == 404 and "Unknown mute group" in d["error"]
    code, d = _post(srv.url + "/api/mute-groups/mg1", {"muted": "yes"})
    assert code == 400 and "boolean" in d["error"]
    code, d = _post(srv.url + "/api/mute-groups/mg1")                # no body
    assert code == 400


def test_unknown_routes_are_404(served):
    srv, _ = served
    with pytest.raises(urllib.error.HTTPError) as exc:
        _get(srv.url + "/api/nope")
    assert exc.value.code == 404
    code, _ = _post(srv.url + "/api/scenes")                         # POST on a GET route
    assert code == 404


def test_sequential_requests_share_one_consistent_config(served):
    srv, holder = served
    for i in range(6):
        want = i % 2 == 0
        _post(srv.url + "/api/mute-groups/mg1", {"muted": want})
        _, d = _get(srv.url + "/api/status")
        assert d["muteGroups"][0]["muted"] is want
    assert holder.get().control.mute_groups[0].muted is False


def test_server_lifecycle_and_ephemeral_port():
    holder = cp.ConfigHolder(_config())
    srv = cp.ControlApiServer(holder.get, holder.apply)
    assert not srv.running
    srv.start()
    try:
        assert srv.running and srv.port > 0
        assert srv.url.startswith("http://127.0.0.1:")
        srv.start()                                                  # idempotent
        code, _ = _get(srv.url + "/api/status")
        assert code == 200
    finally:
        srv.stop()
    assert not srv.running
    srv.stop()                                                       # idempotent
    with pytest.raises(urllib.error.URLError):
        _get(f"http://127.0.0.1:{srv.port}/api/status")              # truly down
