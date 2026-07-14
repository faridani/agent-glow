"""Claude Code hook install/uninstall: merge, idempotency, backups."""

import json
import os
import stat

import pytest

from hue_agent_status import hooks_claude
from hue_agent_status.hooks_claude import CLAUDE_HOOK_EVENTS


def _read(path):
    return json.loads(path.read_text())


def test_install_into_missing_file():
    path = hooks_claude.claude_settings_path()
    changed, backup = hooks_claude.install()
    assert changed
    assert backup is None  # nothing existed to back up
    data = _read(path)
    if os.name != "nt":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    for event in CLAUDE_HOOK_EVENTS:
        assert event in data["hooks"]
        commands = [
            hook["command"] for group in data["hooks"][event] for hook in group["hooks"]
        ]
        assert any("hook --source claude" in c for c in commands)


def test_install_is_idempotent():
    hooks_claude.install()
    changed, _ = hooks_claude.install()
    assert not changed
    data = _read(hooks_claude.claude_settings_path())
    for event in CLAUDE_HOOK_EVENTS:
        assert len(data["hooks"][event]) == 1


def test_install_merges_with_existing_settings():
    path = hooks_claude.claude_settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = {
        "model": "opus",
        "hooks": {
            "PreToolUse": [
                {
                    "matcher": "Bash",
                    "hooks": [{"type": "command", "command": "/usr/bin/my-audit"}],
                }
            ]
        },
    }
    path.write_text(json.dumps(existing))
    changed, backup = hooks_claude.install()
    assert changed
    assert backup is not None and backup.exists()
    data = _read(path)
    assert data["model"] == "opus"  # untouched
    pre = data["hooks"]["PreToolUse"]
    assert len(pre) == 2
    assert pre[0]["hooks"][0]["command"] == "/usr/bin/my-audit"  # preserved
    # backup contains the original
    assert _read(backup) == existing


def test_similarly_named_foreign_hook_is_preserved():
    path = hooks_claude.claude_settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    command = "/opt/not-hue-agent hook --source claude"
    foreign = {"hooks": [{"type": "command", "command": command}]}
    path.write_text(json.dumps({"hooks": {"Stop": [foreign]}}))

    hooks_claude.install()
    assert _read(path)["hooks"]["Stop"][0] == foreign
    hooks_claude.uninstall()
    assert _read(path)["hooks"]["Stop"] == [foreign]


def test_uninstall_removes_only_ours():
    path = hooks_claude.claude_settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "hooks": {
                    "Stop": [
                        {"hooks": [{"type": "command", "command": "/usr/bin/other"}]}
                    ]
                }
            }
        )
    )
    hooks_claude.install()
    assert hooks_claude.is_installed()
    changed, backup = hooks_claude.uninstall()
    assert changed
    assert backup is not None
    data = _read(path)
    assert data["hooks"]["Stop"] == [
        {"hooks": [{"type": "command", "command": "/usr/bin/other"}]}
    ]
    for event in CLAUDE_HOOK_EVENTS:
        if event == "Stop":
            continue
        assert event not in data["hooks"]
    assert not hooks_claude.is_installed()


def test_uninstall_when_nothing_installed():
    changed, backup = hooks_claude.uninstall()
    assert not changed
    assert backup is None


def test_command_is_absolute_or_module_invocation():
    cmd = hooks_claude.build_hook_command()
    first = cmd.split()[0].strip('"')
    assert first.startswith(("/", "\\")) or ":" in first or "python" in first.lower()


@pytest.mark.skipif(os.name == "nt", reason="POSIX file modes")
def test_install_and_backup_are_private():
    path = hooks_claude.claude_settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"hooks": {}}))
    os.chmod(path, 0o640)
    _, backup = hooks_claude.install()
    assert backup is not None
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(backup.stat().st_mode) == 0o600


def test_refuses_to_touch_malformed_hooks_section():
    path = hooks_claude.claude_settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"hooks": "what"}))
    try:
        hooks_claude.install()
        assert False, "should have refused"
    except ValueError:
        pass
    assert _read(path) == {"hooks": "what"}  # untouched
