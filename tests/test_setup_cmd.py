"""Setup must not disclose the local machine hostname while pairing."""

from types import SimpleNamespace

from hue_agent_status import setup_cmd


def test_pair_uses_static_non_identifying_device_type(monkeypatch):
    captured = {}

    async def create_app_key(host, device_type):
        captured["host"] = host
        captured["device_type"] = device_type
        return "app-key"

    def hostname_must_not_be_read():
        raise AssertionError("pairing read the local hostname")

    monkeypatch.setattr("aiohue.util.create_app_key", create_app_key)
    monkeypatch.setattr("socket.gethostname", hostname_must_not_be_read)
    monkeypatch.setattr(setup_cmd, "_input", lambda prompt: "")

    assert setup_cmd._pair("bridge.local") == "app-key"
    assert captured == {
        "host": "bridge.local",
        "device_type": setup_cmd.HUE_APP_DEVICE_TYPE,
    }


def test_bridge_lookup_is_manual_by_default(monkeypatch, capsys):
    def cloud_discovery_must_not_run():
        raise AssertionError("default setup contacted cloud discovery")

    monkeypatch.setattr(setup_cmd, "_discover", cloud_discovery_must_not_run)
    monkeypatch.setattr(setup_cmd, "_input", lambda prompt: "192.0.2.10")

    assert setup_cmd._pick_bridge() == ("192.0.2.10", "")
    assert "public IP will not be sent" in capsys.readouterr().out


def test_cloud_discovery_requires_explicit_opt_in(monkeypatch):
    bridge = SimpleNamespace(host="192.0.2.10", id="example-bridge")

    async def discover():
        return [bridge]

    monkeypatch.setattr(setup_cmd, "_discover", discover)
    monkeypatch.setattr(setup_cmd, "_choose", lambda prompt, count: 0)

    assert setup_cmd._pick_bridge(cloud_discovery=True) == (
        "192.0.2.10",
        "example-bridge",
    )
