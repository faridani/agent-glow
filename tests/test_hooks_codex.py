"""Codex hooks.json and config.toml notify install/uninstall."""

import json
import tomllib

import pytest

from hue_agent_status import hooks_codex
from hue_agent_status.hooks_codex import CODEX_HOOK_EVENTS


def _read_hooks():
    return json.loads(hooks_codex.codex_hooks_path().read_text())


class TestHooksJson:
    def test_install_creates_all_events(self):
        changed, backup = hooks_codex.install_hooks()
        assert changed and backup is None
        data = _read_hooks()
        for event in CODEX_HOOK_EVENTS:
            (entry,) = data["hooks"][event]
            assert entry["command"][-3:] == ["hook", "--source", "codex"]
            assert "commandWindows" in entry
            assert entry["commandWindows"][-3:] == ["hook", "--source", "codex"]

    def test_install_idempotent(self):
        hooks_codex.install_hooks()
        changed, _ = hooks_codex.install_hooks()
        assert not changed

    def test_merge_preserves_existing(self):
        path = hooks_codex.codex_hooks_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"hooks": {"Stop": [{"command": ["/bin/other"]}]}}))
        changed, backup = hooks_codex.install_hooks()
        assert changed and backup is not None
        data = _read_hooks()
        assert data["hooks"]["Stop"][0] == {"command": ["/bin/other"]}
        assert len(data["hooks"]["Stop"]) == 2

    def test_uninstall_removes_only_ours(self):
        path = hooks_codex.codex_hooks_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"hooks": {"Stop": [{"command": ["/bin/other"]}]}}))
        hooks_codex.install_hooks()
        assert hooks_codex.hooks_installed()
        changed, _ = hooks_codex.uninstall_hooks()
        assert changed
        assert not hooks_codex.hooks_installed()
        data = _read_hooks()
        assert data["hooks"]["Stop"] == [{"command": ["/bin/other"]}]


class TestNotifyConfig:
    def test_install_into_missing_config(self):
        changed, backup = hooks_codex.install_notify()
        assert changed and backup is None
        notify = hooks_codex.current_notify()
        assert notify is not None
        assert notify[-1] == "codex-notify"
        assert hooks_codex.notify_installed()

    def test_install_is_idempotent(self):
        hooks_codex.install_notify()
        changed, _ = hooks_codex.install_notify()
        assert not changed

    def test_notify_inserted_before_first_table(self):
        path = hooks_codex.codex_config_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "# my codex config\n"
            'model = "o4"\n'
            "\n"
            "[profiles.fast]\n"
            'model = "o4-mini"\n'
        )
        changed, backup = hooks_codex.install_notify()
        assert changed and backup is not None
        text = path.read_text()
        data = tomllib.loads(text)
        assert data["model"] == "o4"
        assert data["profiles"]["fast"]["model"] == "o4-mini"
        assert data["notify"][-1] == "codex-notify"
        # comment preserved, notify at top level (before the table header)
        assert text.splitlines()[0] == "# my codex config"
        assert text.index("notify") < text.index("[profiles.fast]")

    def test_refuses_to_replace_foreign_notify(self):
        path = hooks_codex.codex_config_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('notify = ["some-other-tool"]\nmodel = "o4"\n')
        with pytest.raises(ValueError, match="already sets notify"):
            hooks_codex.install_notify()
        # file untouched
        data = tomllib.loads(path.read_text())
        assert data["notify"] == ["some-other-tool"]
        assert data["model"] == "o4"

    def test_updates_our_own_stale_notify_line(self):
        path = hooks_codex.codex_config_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('notify = ["/old/venv/bin/hue-agent", "codex-notify"]\n')
        changed, _ = hooks_codex.install_notify()
        assert changed
        data = tomllib.loads(path.read_text())
        assert data["notify"][-1] == "codex-notify"
        assert data["notify"] != ["/old/venv/bin/hue-agent", "codex-notify"]

    def test_uninstall_removes_our_line_only(self):
        path = hooks_codex.codex_config_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('model = "o4"\n')
        hooks_codex.install_notify()
        changed, _ = hooks_codex.uninstall_notify()
        assert changed
        data = tomllib.loads(path.read_text())
        assert "notify" not in data
        assert data["model"] == "o4"

    def test_uninstall_leaves_foreign_notify_alone(self):
        path = hooks_codex.codex_config_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('notify = ["some-other-tool"]\n')
        changed, _ = hooks_codex.uninstall_notify()
        assert not changed
        assert tomllib.loads(path.read_text())["notify"] == ["some-other-tool"]

    def test_never_writes_invalid_toml(self):
        hooks_codex.install_notify()
        tomllib.loads(hooks_codex.codex_config_path().read_text())
