"""WiZ protocol builders/parsers — pure functions, no sockets."""

import pytest

from hue_agent_status.backends.wiz import WizLightSnapshot, snapshot_from_pilot
from hue_agent_status.backends.wiz_protocol import (
    build_get_pilot,
    build_registration,
    build_set_pilot,
    clamp_dimming,
    normalize_mac,
    parse_capabilities,
)


class TestNormalizeMac:
    @pytest.mark.parametrize(
        "raw",
        ["aabbccddeeff", "AA:BB:CC:DD:EE:FF", "aa-bb-cc-dd-ee-ff", "AABB.CCDD.EEFF"],
    )
    def test_accepted_forms(self, raw):
        assert normalize_mac(raw) == "aabbccddeeff"

    @pytest.mark.parametrize("raw", ["", "aabbcc", "aabbccddeeffgg", "not-a-mac", None])
    def test_rejects_garbage(self, raw):
        with pytest.raises(ValueError):
            normalize_mac(raw)


class TestClampDimming:
    def test_firmware_floor(self):
        assert clamp_dimming(9) == 10
        assert clamp_dimming(0) == 10

    def test_ceiling_and_rounding(self):
        assert clamp_dimming(150) == 100
        assert clamp_dimming(54.6) == 55


class TestBuildSetPilot:
    def test_never_mixes_temp_and_rgb(self):
        params = build_set_pilot(rgb=(255, 0, 0), temp_k=2700)["params"]
        assert "temp" not in params
        assert (params["r"], params["g"], params["b"]) == (255, 0, 0)

    def test_scene_wins_over_rgb(self):
        params = build_set_pilot(scene_id=4, speed=50, rgb=(1, 2, 3))["params"]
        assert params["sceneId"] == 4 and params["speed"] == 50
        assert "r" not in params

    def test_dimming_clamped(self):
        assert build_set_pilot(dimming=3)["params"]["dimming"] == 10

    def test_rgb_channels_clamped(self):
        params = build_set_pilot(rgb=(300, -5, 128))["params"]
        assert (params["r"], params["g"], params["b"]) == (255, 0, 128)

    def test_state_only(self):
        assert build_set_pilot(state=False) == {
            "method": "setPilot",
            "params": {"state": False},
        }


class TestCapabilities:
    def test_rgb_module(self):
        caps = parse_capabilities("ESP01_SHRGB1C_31")
        assert caps.supports_color and caps.supports_ct

    def test_tunable_white_module(self):
        caps = parse_capabilities("ESP56_SHTW3_01")
        assert not caps.supports_color and caps.supports_ct

    def test_dimmable_and_unknown_are_the_safe_floor(self):
        for module in ("ESP01_SHDW1_31", "", "MYSTERY_9000"):
            caps = parse_capabilities(module)
            assert not caps.supports_color and not caps.supports_ct


class TestMessages:
    def test_get_pilot_shape(self):
        assert build_get_pilot() == {"method": "getPilot", "params": {}}

    def test_registration_does_not_register(self):
        assert build_registration()["params"]["register"] is False


class TestSnapshotFromPilot:
    def test_rgb_pilot(self):
        caps = parse_capabilities("ESP01_SHRGB1C_31")
        pilot = {
            "state": True,
            "dimming": 60,
            "r": 255,
            "g": 100,
            "b": 0,
            "c": 0,
            "w": 20,
        }
        snap = snapshot_from_pilot("aabbccddeeff", pilot, caps)
        assert snap.on and snap.dimming == 60
        assert snap.rgb == (255, 100, 0) and snap.warm_white == 20
        assert snap.scene_id is None

    def test_scene_pilot_prefers_scene(self):
        caps = parse_capabilities("ESP01_SHRGB1C_31")
        pilot = {"state": True, "dimming": 40, "sceneId": 12, "speed": 80}
        snap = snapshot_from_pilot("aabbccddeeff", pilot, caps)
        assert snap.scene_id == 12 and snap.speed == 80

    def test_round_trip_via_dict(self):
        caps = parse_capabilities("ESP56_SHTW3_01")
        pilot = {"state": False, "dimming": 30, "temp": 2700}
        snap = snapshot_from_pilot("aabbccddeeff", pilot, caps)
        again = WizLightSnapshot.from_dict(snap.to_dict())
        assert again == snap
