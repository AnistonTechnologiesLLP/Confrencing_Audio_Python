import pytest

import conf_pipeline as cp
from conf_pipeline.model import AecConfig
from conf_pipeline.persistence import DeserializeError


def rich():
    c = cp.create_config("round-trip", "2026-01-01T00:00:00Z")
    c = cp.add_device(c, cp.create_processor("P", "Processor", dante_inputs=4, dante_outputs=4))
    c = cp.add_device(c, cp.create_wireless_mic("M", "Mic", "dante"))
    c = cp.add_device(c, cp.create_codec("C", "Codec", "dante"))
    c = cp.route(c, "M-out-dante-1", "P-in-dante-1")
    c = cp.route(c, "C-out-dante-1", "P-in-dante-2")
    c = cp.matrix_for(c, "P").route("P-in-dante-2", "P-out-dante-1", -3)
    c = cp.set_aec(c, "M", AecConfig(True, "P-out-dante-1"))
    return c


def test_round_trip_lossless():
    c = rich()
    restored = cp.deserialize(cp.serialize(c))
    assert cp.serialize(restored) == cp.serialize(c)


def test_round_trip_pretty():
    c = rich()
    assert cp.serialize(cp.deserialize(cp.serialize(c, pretty=True))) == cp.serialize(c)


def test_camelcase_schema():
    c = rich()
    import json
    d = json.loads(cp.serialize(c))
    assert d["version"] == cp.CONFIG_VERSION
    proc = next(x for x in d["devices"] if x["type"] == "processor")
    assert "inputBuses" in proc["matrix"] and "processorId" in proc["matrix"]
    mic = next(x for x in d["devices"] if x["id"] == "M")
    assert mic["aec"]["referenceBusId"] == "P-out-dante-1"  # nullable key present
    assert "deviceId" in proc["ports"][0]


def test_array_bearing_round_trip():
    import json
    c = cp.create_config("rt", "2026-01-01T00:00:00Z")
    c = cp.add_device(c, cp.create_microphone_array("A", "Array"))
    arr0 = next(x for x in json.loads(cp.serialize(c))["devices"] if x["id"] == "A")
    assert "bearingDeg" not in arr0                          # omit-when-None (unaimed array)
    c = cp.set_array_bearing(c, "A", 45.0)
    d = json.loads(cp.serialize(c))
    assert d["version"] == cp.CONFIG_VERSION == 5
    arr1 = next(x for x in d["devices"] if x["id"] == "A")
    assert arr1["bearingDeg"] == 45.0                        # serialized as camelCase
    assert cp.serialize(cp.deserialize(cp.serialize(c))) == cp.serialize(c)   # lossless round-trip


def test_v4_config_migrates_to_v5_losslessly():
    import json
    c = cp.create_config("m", "2026-01-01T00:00:00Z")
    c = cp.add_device(c, cp.create_microphone_array("A", "Array"))
    obj = json.loads(cp.serialize(c))
    obj["version"] = 4                                       # a genuine v4 file: array carries no bearingDeg
    restored = cp.deserialize(json.dumps(obj))              # migrate v4 -> v5
    out = json.loads(cp.serialize(restored))
    assert out["version"] == 5
    arr = next(x for x in out["devices"] if x["id"] == "A")
    assert "bearingDeg" not in arr                          # additive migration adds nothing


def test_array_bearing_setter_rejects_non_array():
    c = cp.create_config("x", "2026-01-01T00:00:00Z")
    c = cp.add_device(c, cp.create_codec("C", "Codec", "dante"))
    with pytest.raises(ValueError, match="not a microphone array"):
        cp.set_array_bearing(c, "C", 10.0)


def test_reject_malformed():
    with pytest.raises(DeserializeError):
        cp.deserialize("{ not json")


def test_reject_wrong_version():
    import json
    obj = json.loads(cp.serialize(rich()))
    obj["version"] = 999
    with pytest.raises(DeserializeError, match="version"):
        cp.deserialize(json.dumps(obj))


def test_reject_missing_fields():
    import json
    with pytest.raises(DeserializeError, match="Missing required"):
        cp.deserialize(json.dumps({"version": 1}))
