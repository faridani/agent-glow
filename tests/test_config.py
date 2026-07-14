"""Config round-trips, validation, and `config set` coercion."""

import pytest

from hue_agent_status.config import (
    Config,
    ConfigError,
    config_path,
    load_config,
    save_config,
    set_config_value,
)


def test_defaults_without_file():
    config = load_config()
    assert config.daemon.host == "127.0.0.1"
    assert config.daemon.port == 8765
    assert config.daemon.completion_hold_seconds == 300
    assert config.animation.breath_period_seconds == 6.0
    assert config.animation.restore == "smart"
    assert config.target.mode == "lights"


def test_round_trip():
    config = Config()
    config.bridge.host = "192.0.2.50"
    config.bridge.bridge_id = "0011223344556677"
    config.target.mode = "room"
    config.target.ids = ["abc-123"]
    config.animation.breath_period_seconds = 7.5
    save_config(config)
    loaded = load_config()
    assert loaded.bridge.host == "192.0.2.50"
    assert loaded.target.mode == "room"
    assert loaded.target.ids == ["abc-123"]
    assert loaded.animation.breath_period_seconds == 7.5


def test_unknown_keys_ignored_for_forward_compat(tmp_path):
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "[bridge]\nhost = '198.51.100.9'\nfuture_flag = true\n\n[newsection]\nx = 1\n"
    )
    config = load_config()
    assert config.bridge.host == "198.51.100.9"


def test_set_value_coercion():
    config = Config()
    set_config_value(config, "daemon.port", "9999")
    assert config.daemon.port == 9999
    set_config_value(config, "animation.breath_period_seconds", "8.5")
    assert config.animation.breath_period_seconds == 8.5
    set_config_value(config, "privacy.debug_log_payloads", "true")
    assert config.privacy.debug_log_payloads is True
    set_config_value(config, "target.ids", "a, b, c")
    assert config.target.ids == ["a", "b", "c"]
    set_config_value(config, "target.ids", '["x", "y"]')
    assert config.target.ids == ["x", "y"]


def test_set_value_rejects_unknown_key():
    with pytest.raises(ConfigError):
        set_config_value(Config(), "daemon.bogus", "1")
    with pytest.raises(ConfigError):
        set_config_value(Config(), "nosection.port", "1")
    with pytest.raises(ConfigError):
        set_config_value(Config(), "toolong.a.b", "1")


def test_validation_rejects_non_loopback_daemon_host():
    config = Config()
    with pytest.raises(ConfigError):
        set_config_value(config, "daemon.host", "0.0.0.0")
    with pytest.raises(ConfigError):
        set_config_value(config, "daemon.host", "192.0.2.5")
    set_config_value(config, "daemon.host", "localhost")
    set_config_value(config, "daemon.host", "127.0.0.1")


def test_validation_rejects_negative_completion_hold():
    with pytest.raises(ConfigError, match="completion_hold_seconds"):
        set_config_value(Config(), "daemon.completion_hold_seconds", "-1")


def test_validation_rejects_bad_modes():
    config = Config()
    with pytest.raises(ConfigError):
        set_config_value(config, "target.mode", "disco")
    with pytest.raises(ConfigError):
        set_config_value(config, "animation.restore", "sometimes")


def test_validation_rejects_inverted_brightness():
    config = Config()
    config.animation.breath_min_brightness = 80
    config.animation.breath_max_brightness = 20
    with pytest.raises(ConfigError):
        save_config(config)


def test_roles_round_trip():
    config = Config()
    config.roles.thinking = ["hue:abc-123"]
    config.roles.waiting = ["hue:def-456", "wiz:aabbccddeeff"]
    save_config(config)
    loaded = load_config()
    assert loaded.roles.thinking == ["hue:abc-123"]
    assert loaded.roles.waiting == ["hue:def-456", "wiz:aabbccddeeff"]


def test_roles_default_empty_for_old_configs():
    config = Config()
    config.bridge.host = "192.0.2.50"
    save_config(config)
    loaded = load_config()
    assert loaded.roles.thinking == [] and loaded.roles.waiting == []


def test_roles_reject_unknown_backend():
    config = Config()
    private_ref = "private-backend:private-stable-id"
    config.roles.thinking = [private_ref]
    with pytest.raises(ConfigError, match="unknown backend") as exc_info:
        save_config(config)
    assert private_ref not in str(exc_info.value)


def test_roles_reject_empty_id():
    config = Config()
    config.roles.waiting = ["wiz:"]
    with pytest.raises(ConfigError, match="missing light id"):
        save_config(config)


@pytest.mark.parametrize("role", ["thinking", "waiting"])
def test_roles_reject_non_list(role):
    config = Config()
    setattr(config.roles, role, "legacy-id")
    with pytest.raises(ConfigError, match=rf"roles\.{role} must be a list"):
        save_config(config)


def test_roles_settable_via_config_set():
    config = Config()
    set_config_value(config, "roles.thinking", "hue:a, wiz:aabbccddeeff")
    assert config.roles.thinking == ["hue:a", "wiz:aabbccddeeff"]


def test_wiz_bulbs_round_trip():
    from hue_agent_status.config import WizBulbConfig

    config = Config()
    config.wiz.bulbs = [
        WizBulbConfig(mac="aabbccddeeff", ip="192.0.2.42", name="Desk strip"),
        WizBulbConfig(mac="AA:BB:CC:DD:EE:00"),
    ]
    save_config(config)
    loaded = load_config()
    assert loaded.wiz.bulbs[0].name == "Desk strip"
    assert loaded.wiz.bulbs[1].mac == "AA:BB:CC:DD:EE:00"
    assert loaded.wiz.broadcast == "255.255.255.255"


def test_wiz_validation():
    from hue_agent_status.config import WizBulbConfig

    config = Config()
    invalid_mac = "private-invalid-mac"
    config.wiz.bulbs = [WizBulbConfig(mac=invalid_mac)]
    with pytest.raises(ConfigError, match="12 hex") as exc_info:
        save_config(config)
    assert invalid_mac not in str(exc_info.value)
    config.wiz.bulbs = [
        WizBulbConfig(mac="aabbccddeeff"),
        WizBulbConfig(mac="AABBCCDDEEFF"),
    ]
    with pytest.raises(ConfigError, match="duplicate"):
        save_config(config)
    invalid_ip = "private-invalid-ip"
    config.wiz.bulbs = [WizBulbConfig(mac="aabbccddeeff", ip=invalid_ip)]
    with pytest.raises(ConfigError, match="invalid") as exc_info:
        save_config(config)
    assert invalid_ip not in str(exc_info.value)


def test_wiz_bulbs_not_settable_via_config_set():
    with pytest.raises(ConfigError, match="wiz add"):
        set_config_value(Config(), "wiz.bulbs", "x")


def test_wait_color_validated():
    config = Config()
    set_config_value(config, "animation.wait_color", "purple")
    set_config_value(config, "animation.wait_color", "#8000ff")
    set_config_value(config, "animation.wait_color", "0.675,0.322")
    with pytest.raises(ConfigError, match="wait_color"):
        set_config_value(config, "animation.wait_color", "plaid")
