"""`hue-agent wiz` configuration repair and MAC normalization."""

from hue_agent_status.cli import main
from hue_agent_status.config import (
    Config,
    WizBulbConfig,
    config_path,
    load_config,
    save_config,
)


def test_remove_normalizes_configured_and_role_macs():
    config = Config()
    config.wiz.bulbs = [WizBulbConfig(mac="AA-BB-CC-DD-EE-FF", name="Desk strip")]
    config.roles.thinking = ["wiz:AA:BB:CC:DD:EE:FF"]
    save_config(config)

    assert main(["wiz", "remove", "aa.bb.cc.dd.ee.ff"]) == 0
    loaded = load_config()
    assert loaded.wiz.bulbs == []
    assert loaded.roles.thinking == []


def test_remove_repairs_malformed_mac_and_dangling_role_ref():
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "[[wiz.bulbs]]\n"
        'mac = "not-a-mac"\n'
        'name = "Broken bulb"\n'
        "\n[roles]\n"
        'thinking = ["wiz:NOT-A-MAC"]\n',
        encoding="utf-8",
    )

    assert main(["wiz", "remove", "not-a-mac"]) == 0
    loaded = load_config()
    assert loaded.wiz.bulbs == []
    assert loaded.roles.thinking == []


def test_remove_rejects_role_that_would_fall_back_to_all(capsys):
    config = Config()
    config.wiz.bulbs = [
        WizBulbConfig(mac="AA-BB-CC-DD-EE-01", name="Role bulb"),
        WizBulbConfig(mac="aabbccddee02", name="Other bulb"),
    ]
    config.roles.thinking = ["wiz:AA:BB:CC:DD:EE:01"]
    save_config(config)

    assert main(["wiz", "remove", "role bulb"]) == 2
    loaded = load_config()
    assert len(loaded.wiz.bulbs) == 2
    assert loaded.roles.thinking == ["wiz:AA:BB:CC:DD:EE:01"]
    assert "would empty the thinking role" in capsys.readouterr().err
