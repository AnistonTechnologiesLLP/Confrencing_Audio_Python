import json

import pytest

import conf_pipeline as cp
from conf_pipeline.model import AecConfig, DspBlock, MuteLink


def codes(issues):
    return [i.code for i in issues]


def test_factories_assign_default_profile_and_empty_chain():
    c = cp.create_config("p", "x")
    c = cp.add_device(c, cp.create_processor("P", "DSP"))
    c = cp.add_device(c, cp.create_microphone_array("A", "Array"))
    c = cp.add_device(c, cp.create_codec("C", "Codec"))
    assert cp.find_device(c, "P").profile_id == "generic-hardware-dsp"
    assert cp.find_device(c, "P").dsp_blocks == []
    assert cp.find_device(c, "A").profile_id == "generic-ceiling-array"
    assert cp.default_profile_id("codec") == "generic-codec"


def test_profile_lookup_and_capabilities():
    assert cp.get_device_profile("generic-codec") is cp.DEVICE_PROFILES["generic-codec"]
    assert cp.get_device_profile("nope") is None
    caps = cp.device_capabilities(cp.create_loudspeaker("L", "Spk"))
    assert caps.aec is False and caps.mute is True


def test_unknown_profile_and_mismatch():
    c = cp.create_config("p", "x")
    c = cp.add_device(c, cp.create_loudspeaker("L", "Spk"))
    c = cp.assign_device_profile(c, "L", "nope")
    assert "DEVICE_PROFILE_UNKNOWN" in codes(cp.validate(c).errors)
    c = cp.assign_device_profile(c, "L", "generic-ceiling-array")
    assert "DEVICE_CAPABILITY_MISMATCH" in codes(cp.validate(c).errors)


def test_dsp_block_crud():
    c = cp.create_config("d", "x")
    c = cp.add_device(c, cp.create_processor("P", "DSP"))
    c = cp.add_dsp_block(c, "P", cp.create_dsp_block("gain", "g1"))
    assert len(cp.find_device(c, "P").dsp_blocks) == 1
    c = cp.update_dsp_block(c, "P", "g1", {"params": {"gainDb": -6}})
    assert cp.find_device(c, "P").dsp_blocks[0].params["gainDb"] == -6
    c = cp.set_dsp_block_enabled(c, "P", "g1", False)
    assert cp.find_device(c, "P").dsp_blocks[0].enabled is False
    c = cp.remove_dsp_block(c, "P", "g1")
    assert cp.find_device(c, "P").dsp_blocks == []
    assert cp.validate(c).ok


def test_duplicate_block_id():
    c = cp.create_config("d", "x")
    c = cp.add_device(c, cp.create_processor("P", "DSP"))
    c = cp.add_dsp_block(c, "P", cp.create_dsp_block("gain", "g1"))
    with pytest.raises(ValueError, match="Duplicate"):
        cp.add_dsp_block(c, "P", cp.create_dsp_block("mute", "g1"))


def test_unsupported_block():
    c = cp.create_config("d", "x")
    c = cp.add_device(c, cp.create_codec("C", "Codec"))
    c = cp.add_dsp_block(c, "C", cp.create_dsp_block("peq4", "q1"))
    assert "DSP_BLOCK_UNSUPPORTED" in codes(cp.validate(c).errors)


def test_invalid_params():
    c = cp.create_config("d", "x")
    c = cp.add_device(c, cp.create_processor("P", "DSP"))
    c = cp.add_dsp_block(c, "P", DspBlock("g1", "gain", True, {"gainDb": 999}))
    assert "DSP_BLOCK_INVALID" in codes(cp.validate(c).errors)


def test_target_bus_resolution():
    c = cp.create_config("d", "x")
    c = cp.add_device(c, cp.create_processor("P", "DSP"))
    c = cp.add_dsp_block(c, "P", DspBlock("g1", "gain", True, {"gainDb": 0}, target_bus_id="nope"))
    assert "DSP_TARGET_UNRESOLVED" in codes(cp.validate(c).errors)
    c = cp.update_dsp_block(c, "P", "g1", {"target_bus_id": "P-out-dante-1"})
    assert "DSP_TARGET_UNRESOLVED" not in codes(cp.validate(c).errors)


def test_valid_multiblock_chain():
    c = cp.create_config("d", "x")
    c = cp.add_device(c, cp.create_processor("P", "DSP"))
    for k, i in [("gain", "g1"), ("peq4", "q1"), ("delay", "d1"), ("noiseReduction", "n1")]:
        c = cp.add_dsp_block(c, "P", cp.create_dsp_block(k, i))
    assert cp.validate(c).errors == []


def test_commissioning_warnings():
    # DSP chain with no gain/mute
    c = cp.create_config("w", "x")
    c = cp.add_device(c, cp.create_processor("P", "DSP"))
    c = cp.add_dsp_block(c, "P", cp.create_dsp_block("delay", "d1"))
    assert "DSP_CHAIN_NO_LEVEL" in codes(cp.validate(c).warnings)
    # automix output unset with mics
    c2 = cp.create_config("w", "x")
    c2 = cp.add_device(c2, cp.create_processor("P", "DSP"))
    c2 = cp.add_device(c2, cp.create_wireless_mic("M", "Mic"))
    assert "AUTOMIX_OUTPUT_UNSET" in codes(cp.validate(c2).warnings)
    # AEC enabled with no far end
    c3 = cp.create_config("w", "x")
    c3 = cp.add_device(c3, cp.create_processor("P", "DSP"))
    c3 = cp.add_device(c3, cp.create_wireless_mic("M", "Mic"))
    c3 = cp.set_aec(c3, "M", AecConfig(True, None))
    assert "AEC_NO_FAR_END" in codes(cp.validate(c3).warnings)


def test_v1_migration():
    c = cp.create_config("m", "x")
    c = cp.add_device(c, cp.create_processor("P", "DSP"))
    c = cp.add_device(c, cp.create_microphone_array("A", "Array"))
    v1 = json.loads(cp.serialize(c))
    v1["version"] = 1
    for d in v1["devices"]:
        d.pop("profileId", None)
        d.pop("dspBlocks", None)
    restored = cp.deserialize(json.dumps(v1))
    assert restored.version == cp.CONFIG_VERSION
    assert cp.find_device(restored, "P").profile_id == "generic-hardware-dsp"
    assert cp.find_device(restored, "A").dsp_blocks == []


def test_round_trip_with_blocks():
    c = cp.create_config("m", "x")
    c = cp.add_device(c, cp.create_processor("P", "DSP"))
    c = cp.add_dsp_block(c, "P", cp.create_dsp_block("peq4", "q1"))
    c = cp.update_dsp_block(c, "P", "q1", {"target_bus_id": "P-out-dante-1"})
    assert cp.serialize(cp.deserialize(cp.serialize(c))) == cp.serialize(c)
    assert json.loads(cp.serialize(c))["version"] == cp.CONFIG_VERSION
