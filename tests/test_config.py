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
    assert config.animation.breath_period_seconds == 6.0
    assert config.animation.restore == "smart"
    assert config.target.mode == "lights"


def test_round_trip():
    config = Config()
    config.bridge.host = "192.168.1.50"
    config.bridge.bridge_id = "001788fffe123456"
    config.target.mode = "room"
    config.target.ids = ["abc-123"]
    config.animation.breath_period_seconds = 7.5
    save_config(config)
    loaded = load_config()
    assert loaded.bridge.host == "192.168.1.50"
    assert loaded.target.mode == "room"
    assert loaded.target.ids == ["abc-123"]
    assert loaded.animation.breath_period_seconds == 7.5


def test_unknown_keys_ignored_for_forward_compat(tmp_path):
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "[bridge]\nhost = '10.0.0.9'\nfuture_flag = true\n\n[newsection]\nx = 1\n"
    )
    config = load_config()
    assert config.bridge.host == "10.0.0.9"


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
        set_config_value(config, "daemon.host", "192.168.1.5")
    set_config_value(config, "daemon.host", "localhost")
    set_config_value(config, "daemon.host", "127.0.0.1")


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
