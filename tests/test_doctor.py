"""Checks that ``doctor`` validates only the configured light backends."""

from hue_agent_status import doctor
from hue_agent_status.config import Config, WizBulbConfig, save_config


def test_wiz_only_config_is_valid():
    config = Config()
    config.wiz.bulbs = [WizBulbConfig(mac="aabbccddeeff", name="Desk lamp")]
    save_config(config)

    result = doctor._check_config(config)

    assert result.status == doctor.OK


def test_wiz_only_doctor_skips_hue_checks(monkeypatch, capsys):
    config = Config()
    config.wiz.bulbs = [WizBulbConfig(mac="aabbccddeeff", name="Desk lamp")]
    save_config(config)

    def unexpected_app_key_check():
        raise AssertionError("WiZ-only doctor must not check a Hue app key")

    async def unexpected_bridge_checks(_config):
        raise AssertionError("WiZ-only doctor must not check a Hue bridge")

    async def available_wiz(_config):
        return [doctor.CheckResult("wiz", doctor.OK, "Desk lamp reachable")]

    monkeypatch.setattr(doctor, "_check_app_key", unexpected_app_key_check)
    monkeypatch.setattr(doctor, "_bridge_checks", unexpected_bridge_checks)
    monkeypatch.setattr(doctor, "_wiz_checks", available_wiz)

    assert doctor.run_doctor(config) == 0
    output = capsys.readouterr().out
    assert "app-key" not in output
    assert "bridge" not in output
    assert "wiz" in output


def test_daemon_check_warns_when_active_wiz_animation_is_missing(monkeypatch):
    from hue_agent_status import client, secret_store

    monkeypatch.setattr(secret_store, "get_daemon_token", lambda: "token")
    monkeypatch.setattr(
        client,
        "get_health",
        lambda *args, **kwargs: {
            "pid": 123,
            "aggregate": "active",
            "backends": {
                "wiz": {
                    "mode": "active",
                    "breathing": False,
                    "missing": 2,
                }
            },
        },
    )

    result = doctor._check_daemon(Config())

    assert result.status == doctor.WARN
    assert "animation stopped" in result.detail
    assert "missing 2" in result.detail


def test_daemon_check_warns_on_mode_mismatch_and_failed_targets(monkeypatch):
    from hue_agent_status import client, secret_store

    monkeypatch.setattr(secret_store, "get_daemon_token", lambda: "token")
    monkeypatch.setattr(
        client,
        "get_health",
        lambda *args, **kwargs: {
            "pid": 123,
            "aggregate": "waiting",
            "backends": {
                "wiz": {
                    "mode": "active",
                    "breathing": True,
                    "missing": 0,
                    "failed": 1,
                }
            },
        },
    )

    result = doctor._check_daemon(Config())

    assert result.status == doctor.WARN
    assert "mode is active" in result.detail
    assert "1 failed command" in result.detail
    assert "green hold: 300s" in result.detail
