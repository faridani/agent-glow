"""Codex hooks.json and config.toml notify install/uninstall."""

import json
import os
import stat
import tomllib

import pytest

from hue_agent_status import hooks_codex
from hue_agent_status.hooks_codex import CODEX_HOOK_EVENTS


def _read_hooks():
    return json.loads(hooks_codex.codex_hooks_path().read_text())


def _legacy_entry() -> dict:
    """The flat argv-list shape this tool wrote before Codex's schema settled."""
    return {
        "command": ["/old/venv/bin/hue-agent", "hook", "--source", "codex"],
        "commandWindows": ["py", "-m", "hue_agent_status", "hook", "--source", "codex"],
    }


class TestHooksJson:
    def test_install_creates_all_events(self):
        changed, backup = hooks_codex.install_hooks()
        assert changed and backup is None
        if os.name != "nt":
            assert stat.S_IMODE(hooks_codex.codex_hooks_path().stat().st_mode) == 0o600
        data = _read_hooks()
        for event in CODEX_HOOK_EVENTS:
            (group,) = data["hooks"][event]
            (handler,) = group["hooks"]
            assert handler["type"] == "command"
            assert isinstance(handler["command"], str)
            assert handler["command"].endswith("hook --source codex")
            assert isinstance(handler["commandWindows"], str)
            assert handler["commandWindows"].endswith("hook --source codex")
            assert handler["timeout"] == hooks_codex.CODEX_HOOK_TIMEOUT_SECONDS
            assert "async" not in handler  # Codex skips async command hooks
        assert hooks_codex.hooks_installed()

    def test_install_idempotent(self):
        hooks_codex.install_hooks()
        changed, _ = hooks_codex.install_hooks()
        assert not changed

    def test_merge_preserves_existing(self):
        path = hooks_codex.codex_hooks_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        foreign = {"hooks": [{"type": "command", "command": "/bin/other"}]}
        path.write_text(json.dumps({"hooks": {"Stop": [foreign]}}))
        changed, backup = hooks_codex.install_hooks()
        assert changed and backup is not None
        data = _read_hooks()
        assert data["hooks"]["Stop"][0] == foreign
        assert len(data["hooks"]["Stop"]) == 2

    def test_similarly_named_foreign_hook_is_preserved(self):
        path = hooks_codex.codex_hooks_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        foreign = {
            "hooks": [
                {
                    "type": "command",
                    "command": "/opt/not-hue-agent hook --source codex",
                }
            ]
        }
        path.write_text(json.dumps({"hooks": {"Stop": [foreign]}}))

        hooks_codex.install_hooks()
        assert _read_hooks()["hooks"]["Stop"][0] == foreign
        hooks_codex.uninstall_hooks()
        assert _read_hooks()["hooks"]["Stop"] == [foreign]

    def test_install_migrates_legacy_format(self):
        path = hooks_codex.codex_hooks_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"hooks": {"Stop": [_legacy_entry()]}}))
        assert not hooks_codex.hooks_installed()  # legacy = never fires = not installed
        changed, _ = hooks_codex.install_hooks()
        assert changed
        data = _read_hooks()
        (group,) = data["hooks"]["Stop"]  # legacy entry replaced, not duplicated
        assert group["hooks"][0]["type"] == "command"
        assert hooks_codex.hooks_installed()

    def test_partial_install_is_not_reported_as_installed(self):
        path = hooks_codex.codex_hooks_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"hooks": {"Stop": [hooks_codex.build_hook_entry()]}})
        )
        assert not hooks_codex.hooks_installed()

    def test_async_handler_is_not_reported_as_installed(self):
        hooks_codex.install_hooks()
        data = _read_hooks()
        data["hooks"]["Stop"][0]["hooks"][0]["async"] = True
        hooks_codex.codex_hooks_path().write_text(json.dumps(data))
        assert not hooks_codex.hooks_installed()

    def test_incomplete_handler_is_not_reported_as_installed(self):
        hooks_codex.install_hooks()
        data = _read_hooks()
        del data["hooks"]["PostToolUse"][0]["hooks"][0]["commandWindows"]
        hooks_codex.codex_hooks_path().write_text(json.dumps(data))
        assert not hooks_codex.hooks_installed()

    def test_install_refreshes_stale_path(self):
        hooks_codex.install_hooks()
        data = _read_hooks()
        data["hooks"]["Stop"][0]["hooks"][0]["command"] = (
            "/old/venv/bin/hue-agent hook --source codex"
        )
        hooks_codex.codex_hooks_path().write_text(json.dumps(data))
        assert not hooks_codex.hooks_installed()
        changed, _ = hooks_codex.install_hooks()
        assert changed
        (group,) = _read_hooks()["hooks"]["Stop"]
        assert "/old/venv" not in group["hooks"][0]["command"]

    def test_uninstall_removes_only_ours(self):
        path = hooks_codex.codex_hooks_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        foreign = {"hooks": [{"type": "command", "command": "/bin/other"}]}
        path.write_text(
            json.dumps({"hooks": {"Stop": [foreign], "PreCompact": [_legacy_entry()]}})
        )
        hooks_codex.install_hooks()
        assert hooks_codex.hooks_installed()
        changed, _ = hooks_codex.uninstall_hooks()
        assert changed
        assert not hooks_codex.hooks_installed()
        data = _read_hooks()
        assert data["hooks"]["Stop"] == [foreign]
        assert "PreCompact" not in data["hooks"]  # legacy entry cleaned up too

    def test_uninstall_keeps_foreign_handler_in_shared_group(self):
        path = hooks_codex.codex_hooks_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        ours = hooks_codex.build_hook_entry()["hooks"][0]
        mixed = {"hooks": [{"type": "command", "command": "/bin/other"}, ours]}
        path.write_text(json.dumps({"hooks": {"Stop": [mixed]}}))
        changed, _ = hooks_codex.uninstall_hooks()
        assert changed
        data = _read_hooks()
        assert data["hooks"]["Stop"] == [
            {"hooks": [{"type": "command", "command": "/bin/other"}]}
        ]

    @pytest.mark.skipif(os.name == "nt", reason="POSIX file modes")
    def test_install_and_uninstall_restrict_existing_mode(self):
        path = hooks_codex.codex_hooks_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"hooks": {}}))
        os.chmod(path, 0o640)

        hooks_codex.install_hooks()
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        hooks_codex.uninstall_hooks()
        assert stat.S_IMODE(path.stat().st_mode) == 0o600


class TestNotifyConfig:
    def test_install_into_missing_config(self):
        changed, backup = hooks_codex.install_notify()
        assert changed and backup is None
        if os.name != "nt":
            assert stat.S_IMODE(hooks_codex.codex_config_path().stat().st_mode) == 0o600
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
            '# my codex config\nmodel = "o4"\n\n[profiles.fast]\nmodel = "o4-mini"\n'
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

    def test_refuses_similarly_named_foreign_notify(self):
        path = hooks_codex.codex_config_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        foreign = ["/opt/not-hue-agent", "codex-notify"]
        path.write_text(f"notify = {json.dumps(foreign)}\n")

        assert not hooks_codex.notify_installed()
        with pytest.raises(ValueError, match="already sets notify"):
            hooks_codex.install_notify()
        assert tomllib.loads(path.read_text())["notify"] == foreign

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

    @pytest.mark.skipif(os.name == "nt", reason="POSIX file modes")
    def test_install_and_uninstall_restrict_existing_mode(self):
        path = hooks_codex.codex_config_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('model = "o4"\n')
        os.chmod(path, 0o640)

        hooks_codex.install_notify()
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        hooks_codex.uninstall_notify()
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
