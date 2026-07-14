"""`hue-agent lights` / `role` against a fake inventory."""

import json

import pytest

from hue_agent_status import lights_cmd
from hue_agent_status.cli import main
from hue_agent_status.config import Config, WizBulbConfig, load_config, save_config
from hue_agent_status.roles import LightInfo


@pytest.fixture
def fake_inventory(monkeypatch):
    """Bypass bridge/UDP: a fixed two-backend inventory with live role math."""

    async def fake_list_lights(config, *, redact_errors=False, require_complete=False):
        infos = [
            LightInfo(
                ref="hue:1",
                backend="hue",
                id="1",
                name="Desk Lamp",
                supports_color=True,
            ),
            LightInfo(
                ref="hue:2",
                backend="hue",
                id="2",
                name="Bookshelf",
                supports_color=False,
            ),
            LightInfo(
                ref="wiz:aabbccddeeff",
                backend="wiz",
                id="aabbccddeeff",
                name="Desk Strip",
                supports_color=True,
                reachable=True,
            ),
        ]
        for info in infos:
            for role in ("thinking", "waiting"):
                configured = getattr(config.roles, role)
                if (
                    (not configured and info.ref)
                    or info.ref in configured
                    or info.id in configured
                ):
                    info.roles.append(role)
        return infos

    monkeypatch.setattr(lights_cmd, "list_lights", fake_list_lights)
    return fake_list_lights


def _seed_config():
    config = Config()
    config.bridge.host = "192.0.2.50"
    config.target.ids = ["1", "2"]
    save_config(config)


def test_hue_name_fallback_does_not_expose_device_id():
    class Lights:
        @staticmethod
        def get_device(light_id):
            return None

    class Bridge:
        lights = Lights()

    device_id = "private-stable-device-id"
    assert lights_cmd.hue_light_name(Bridge(), device_id) == "Unnamed Hue light"


class TestLightsCommand:
    async def test_wiz_role_membership_normalizes_mac(self, monkeypatch):
        from hue_agent_status.backends import wiz as wiz_backend
        from hue_agent_status.backends import wiz_protocol

        class NoNetworkTransport:
            async def send_command(self, *args, **kwargs):
                raise AssertionError("an addressless bulb should not be probed")

            def close(self):
                pass

        monkeypatch.setattr(wiz_backend, "_load_ip_cache", lambda: {})
        monkeypatch.setattr(wiz_protocol, "WizTransport", NoNetworkTransport)
        config = Config()
        config.wiz.bulbs = [WizBulbConfig(mac="aabbccddeeff", name="Desk strip")]
        config.roles.thinking = ["wiz:AA-BB-CC-DD-EE-FF"]

        (info,) = await lights_cmd._wiz_lights(config)
        assert "thinking" in info.roles

    def test_json_shape(self, fake_inventory, capsys):
        _seed_config()
        assert main(["lights", "--json"]) == 0
        payload = json.loads(capsys.readouterr().out)
        names = {light["name"] for light in payload["lights"]}
        assert names == {"Desk Lamp", "Bookshelf", "Desk Strip"}
        assert payload["wait_color"] == "red"
        assert payload["roles"]["thinking"]["configured"] == []
        assert len(payload["roles"]["thinking"]["effective"]) == 3

    def test_human_output_lists_roles(self, fake_inventory, capsys):
        _seed_config()
        assert main(["lights"]) == 0
        out = capsys.readouterr().out
        assert "Desk Lamp" in out and "thinking,waiting" in out

    def test_agent_output_omits_stable_device_identifiers(self, fake_inventory, capsys):
        _seed_config()
        assert main(["lights", "--agent"]) == 0
        out = capsys.readouterr().out
        payload = json.loads(out)
        assert {light["name"] for light in payload["lights"]} == {
            "Desk Lamp",
            "Bookshelf",
            "Desk Strip",
        }
        assert "aabbccddeeff" not in out
        assert "hue:1" not in out
        assert all(
            not {"ref", "id", "backend", "reachable"}.intersection(light)
            for light in payload["lights"]
        )

    def test_agent_output_redacts_backend_error_details(self, monkeypatch, capsys):
        _seed_config()

        async def broken_hue(config):
            raise RuntimeError("bridge failed at 192.0.2.123")

        async def no_wiz(config):
            return []

        monkeypatch.setattr(lights_cmd, "_hue_lights", broken_hue)
        monkeypatch.setattr(lights_cmd, "_wiz_lights", no_wiz)
        assert main(["lights", "--agent"]) == 0
        captured = capsys.readouterr()
        assert "192.0.2.123" not in captured.err
        assert "some configured lights could not be listed" in captured.err
        assert "hue" not in captured.err


class TestRoleCommand:
    def test_set_by_name_persists_refs(self, fake_inventory, capsys):
        _seed_config()
        assert main(["role", "set", "thinking", "desk lamp"]) == 0
        assert load_config().roles.thinking == ["hue:1"]

    def test_set_accepts_unique_substring(self, fake_inventory):
        _seed_config()
        assert main(["role", "set", "waiting", "book"]) == 0
        assert load_config().roles.waiting == ["hue:2"]

    def test_ambiguous_name_exits_2_with_candidates(self, fake_inventory, capsys):
        _seed_config()
        assert main(["role", "set", "thinking", "desk"]) == 2
        err = capsys.readouterr().err
        assert "Desk Lamp" in err and "Desk Strip" in err
        assert load_config().roles.thinking == []

    def test_add_makes_default_explicit_first(self, fake_inventory):
        _seed_config()
        # default = all three lights; adding must not silently shrink to one
        assert main(["role", "add", "waiting", "strip"]) == 0
        assert set(load_config().roles.waiting) == {
            "hue:1",
            "hue:2",
            "wiz:aabbccddeeff",
        }

    def test_remove_from_default_set(self, fake_inventory):
        _seed_config()
        assert main(["role", "remove", "thinking", "bookshelf"]) == 0
        assert set(load_config().roles.thinking) == {"hue:1", "wiz:aabbccddeeff"}

    def test_add_canonicalizes_legacy_bare_hue_ref(self, fake_inventory):
        _seed_config()
        config = load_config()
        config.roles.thinking = ["1"]
        save_config(config)

        assert main(["role", "add", "thinking", "desk lamp"]) == 0
        assert load_config().roles.thinking == ["hue:1"]

    def test_remove_matches_legacy_bare_hue_ref(self, fake_inventory):
        _seed_config()
        config = load_config()
        config.roles.thinking = ["1", "hue:2"]
        save_config(config)

        assert main(["role", "remove", "thinking", "desk lamp"]) == 0
        assert load_config().roles.thinking == ["hue:2"]

    def test_default_role_edit_fails_when_inventory_is_partial(
        self, monkeypatch, capsys
    ):
        config = Config()
        config.bridge.host = "192.0.2.50"
        config.target.ids = ["1"]
        config.wiz.bulbs = [WizBulbConfig(mac="aabbccddeeff", name="Desk strip")]
        save_config(config)

        async def broken_hue(config):
            raise RuntimeError("bridge unavailable")

        async def available_wiz(config):
            return [
                LightInfo(
                    ref="wiz:aabbccddeeff",
                    backend="wiz",
                    id="aabbccddeeff",
                    name="Desk Strip",
                    roles=["thinking", "waiting"],
                )
            ]

        monkeypatch.setattr(lights_cmd, "_hue_lights", broken_hue)
        monkeypatch.setattr(lights_cmd, "_wiz_lights", available_wiz)

        assert main(["role", "remove", "waiting", "desk strip"]) == 1
        assert load_config().roles.waiting == []
        assert "cannot edit a default role" in capsys.readouterr().err

    def test_explicit_role_edit_preserves_unavailable_backend_refs(self, monkeypatch):
        config = Config()
        config.bridge.host = "192.0.2.50"
        config.target.ids = ["1"]
        config.wiz.bulbs = [WizBulbConfig(mac="aabbccddeeff", name="Desk strip")]
        config.roles.waiting = ["hue:1", "wiz:aabbccddeeff"]
        save_config(config)

        async def broken_hue(config):
            raise RuntimeError("bridge unavailable")

        async def available_wiz(config):
            return [
                LightInfo(
                    ref="wiz:aabbccddeeff",
                    backend="wiz",
                    id="aabbccddeeff",
                    name="Desk Strip",
                    roles=["waiting"],
                )
            ]

        monkeypatch.setattr(lights_cmd, "_hue_lights", broken_hue)
        monkeypatch.setattr(lights_cmd, "_wiz_lights", available_wiz)

        assert main(["role", "remove", "waiting", "desk strip"]) == 0
        assert load_config().roles.waiting == ["hue:1"]

    def test_remove_only_default_light_is_rejected(self, monkeypatch, capsys):
        config = Config()
        config.bridge.host = "192.0.2.50"
        config.target.ids = ["1"]
        save_config(config)

        async def one_light(config, *, redact_errors=False, require_complete=False):
            return [
                LightInfo(
                    ref="hue:1",
                    backend="hue",
                    id="1",
                    name="Desk Lamp",
                    roles=["thinking", "waiting"],
                )
            ]

        monkeypatch.setattr(lights_cmd, "list_lights", one_light)

        assert main(["role", "remove", "thinking", "desk lamp"]) == 2
        assert load_config().roles.thinking == []
        assert "cannot remove every light" in capsys.readouterr().err

    def test_clear_resets_to_default(self, fake_inventory):
        _seed_config()
        main(["role", "set", "thinking", "desk lamp"])
        assert main(["role", "clear", "thinking"]) == 0
        assert load_config().roles.thinking == []

    def test_show_reports_defaults(self, fake_inventory, capsys):
        _seed_config()
        assert main(["role", "show"]) == 0
        out = capsys.readouterr().out
        assert "default: all configured lights" in out
        assert "wait color: red" in out

    def test_show_recognizes_legacy_bare_hue_ref(self, fake_inventory, capsys):
        _seed_config()
        config = load_config()
        config.roles.thinking = ["1"]
        save_config(config)

        assert main(["role", "show"]) == 0
        out = capsys.readouterr().out
        assert "Desk Lamp" in out
        assert "configured light is unavailable" not in out

    def test_show_does_not_print_unknown_stable_refs(self, fake_inventory, capsys):
        _seed_config()
        config = load_config()
        config.roles.waiting = ["wiz:private-stable-device-id"]
        save_config(config)

        assert main(["role", "show"]) == 0
        out = capsys.readouterr().out
        assert "private-stable-device-id" not in out
        assert "1 configured light is unavailable" in out

    def test_show_redacts_backend_error_details(self, monkeypatch, capsys):
        _seed_config()

        async def broken_hue(config):
            raise RuntimeError("bridge failed at 192.0.2.123")

        async def no_wiz(config):
            return []

        monkeypatch.setattr(lights_cmd, "_hue_lights", broken_hue)
        monkeypatch.setattr(lights_cmd, "_wiz_lights", no_wiz)
        assert main(["role", "show"]) == 0
        captured = capsys.readouterr()
        assert "192.0.2.123" not in captured.err
        assert "some configured lights could not be listed" in captured.err
