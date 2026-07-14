"""/glow command installer: fresh install, upgrade, foreign files, uninstall."""

import json
import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

from hue_agent_status import commands_install
from hue_agent_status.cli import main


@pytest.fixture
def trusted_rule_install(monkeypatch):
    monkeypatch.setattr(
        commands_install, "_approval_rule_refusal_reason", lambda command: None
    )


class TestInstall:
    def test_fresh_install_both(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HUE_AGENT_CLAUDE_COMMANDS_DIR", str(tmp_path / "commands"))
        changed, backup = commands_install.install("claude")
        assert changed and backup is None
        path = commands_install.command_path("claude")
        text = path.read_text()
        assert commands_install.GLOW_MARKER in text
        assert text.startswith("---")  # Claude frontmatter
        assert "$ARGUMENTS" in text
        assert "allowed-tools:" not in text
        if os.name != "nt":
            assert stat.S_IMODE(path.stat().st_mode) == 0o600

        changed, _ = commands_install.install("codex")
        codex_text = commands_install.command_path("codex").read_text()
        assert changed
        assert not codex_text.startswith("---")  # Codex: plain markdown
        assert commands_install.GLOW_MARKER in codex_text
        assert "sandbox" in codex_text  # Codex body warns about the sandbox
        assert "sandbox" not in text  # Claude body does not
        assert "lights --agent" in codex_text
        assert "lights --json" not in codex_text
        assert "never run discovery" in codex_text
        assert "selected AI provider" in codex_text
        assert "omits backend, reachability" in codex_text

    def test_reinstall_same_content_is_noop(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HUE_AGENT_CLAUDE_COMMANDS_DIR", str(tmp_path / "commands"))
        commands_install.install("claude")
        changed, backup = commands_install.install("claude")
        assert not changed and backup is None

    def test_generated_instructions_do_not_embed_home_path(self, monkeypatch):
        executable = Path.home() / "private-install" / "bin" / "hue-agent"
        monkeypatch.setattr(
            commands_install, "resolve_cli_command", lambda: [str(executable)]
        )

        text = commands_install.build_command_markdown("codex")
        assert str(Path.home()) not in text
        assert '"$HOME/private-install/bin/hue-agent"' in text

    def test_upgrade_backs_up_old_version(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HUE_AGENT_CLAUDE_COMMANDS_DIR", str(tmp_path / "commands"))
        path = commands_install.command_path("claude")
        path.parent.mkdir(parents=True)
        path.write_text(f"{commands_install.GLOW_MARKER}\nold version\n")
        if os.name != "nt":
            os.chmod(path, 0o640)
        changed, backup = commands_install.install("claude")
        assert changed and backup is not None
        assert "old version" in backup.read_text()
        if os.name != "nt":
            assert stat.S_IMODE(path.stat().st_mode) == 0o600
            assert stat.S_IMODE(backup.stat().st_mode) == 0o600

    def test_noop_reinstall_repairs_permissive_mode(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HUE_AGENT_CLAUDE_COMMANDS_DIR", str(tmp_path / "commands"))
        commands_install.install("claude")
        path = commands_install.command_path("claude")
        if os.name != "nt":
            os.chmod(path, 0o644)
        assert commands_install.install("claude") == (False, None)
        if os.name != "nt":
            assert stat.S_IMODE(path.stat().st_mode) == 0o600

    def test_refuses_foreign_glow_file(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HUE_AGENT_CLAUDE_COMMANDS_DIR", str(tmp_path / "commands"))
        path = commands_install.command_path("claude")
        path.parent.mkdir(parents=True)
        path.write_text("my own glow command\n")
        with pytest.raises(ValueError, match="not ours"):
            commands_install.install("claude")
        assert path.read_text() == "my own glow command\n"


class TestUninstall:
    def test_uninstall_removes_only_ours(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HUE_AGENT_CLAUDE_COMMANDS_DIR", str(tmp_path / "commands"))
        commands_install.install("claude")
        changed, backup = commands_install.uninstall("claude")
        assert changed and backup is not None
        assert not commands_install.command_path("claude").exists()

    def test_uninstall_leaves_foreign_files(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HUE_AGENT_CLAUDE_COMMANDS_DIR", str(tmp_path / "commands"))
        path = commands_install.command_path("claude")
        path.parent.mkdir(parents=True)
        path.write_text("someone else's\n")
        changed, _ = commands_install.uninstall("claude")
        assert not changed and path.exists()

    def test_uninstall_missing_is_noop(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HUE_AGENT_CLAUDE_COMMANDS_DIR", str(tmp_path / "commands"))
        assert commands_install.uninstall("claude") == (False, None)


class TestCodexSkill:
    def test_install_writes_skill_md(self):
        changed, backup = commands_install.install_codex_skill()
        assert changed and backup is None
        text = commands_install.codex_skill_path().read_text()
        assert text.startswith("---\nname: glow\n")
        assert "description: " in text
        assert commands_install.GLOW_MARKER in text
        assert "$ARGUMENTS" not in text  # prompt-only placeholder
        assert commands_install.codex_skill_installed()

    def test_reinstall_is_noop(self):
        commands_install.install_codex_skill()
        assert commands_install.install_codex_skill() == (False, None)

    def test_uninstall_removes_skill(self):
        commands_install.install_codex_skill()
        changed, backup = commands_install.uninstall_codex_skill()
        assert changed and backup is not None
        assert not commands_install.codex_skill_path().exists()

    def test_refuses_foreign_skill(self):
        path = commands_install.codex_skill_path()
        path.parent.mkdir(parents=True)
        path.write_text("---\nname: glow\n---\nmy own skill\n")
        with pytest.raises(ValueError, match="not ours"):
            commands_install.install_codex_skill()
        changed, _ = commands_install.uninstall_codex_skill()
        assert not changed and path.exists()


class TestCodexRules:
    @staticmethod
    def _matches(pattern, command):
        if len(command) < len(pattern):
            return False
        return all(
            actual in expected if isinstance(expected, list) else actual == expected
            for expected, actual in zip(pattern, command)
        )

    def test_rules_match_only_glow_commands(self):
        base = ["/opt/hue-agent"]
        patterns = [pattern for pattern, _ in commands_install._glow_rule_specs(base)]
        allowed = [
            ["lights", "--agent"],
            ["role", "show"],
            ["role", "set", "thinking", "Desk lamp"],
            ["role", "add", "waiting", "Strip"],
            ["role", "remove", "thinking", "Shelf"],
            ["role", "clear", "waiting"],
            ["config", "set", "animation.wait_color", "purple"],
        ]
        rejected = [
            ["lights"],
            ["lights", "--json"],
            ["config", "show"],
            ["config", "set", "daemon.port", "9999"],
            ["role", "set", "admin", "Desk lamp"],
            ["wiz", "discover"],
            ["setup"],
            ["install-hooks", "--all"],
            ["autostart", "install"],
            ["daemon", "--detach"],
        ]
        for suffix in allowed:
            assert any(self._matches(pattern, base + suffix) for pattern in patterns)
        for suffix in rejected:
            assert not any(
                self._matches(pattern, base + suffix) for pattern in patterns
            )

    @pytest.mark.skipif(shutil.which("codex") is None, reason="Codex CLI not installed")
    def test_generated_rules_validate_with_codex_execpolicy(self, tmp_path):
        base = ["/opt/hue-agent"]
        rules = tmp_path / "hue-agent-status.rules"
        rules.write_text(commands_install.build_rules_content(base))
        cases = [
            (["lights", "--agent"], "allow"),
            (["role", "show"], "allow"),
            (["role", "set", "thinking", "Desk lamp"], "allow"),
            (["role", "add", "waiting", "Strip"], "allow"),
            (["role", "remove", "waiting", "Strip"], "allow"),
            (["role", "clear", "thinking"], "allow"),
            (["config", "set", "animation.wait_color", "purple"], "allow"),
            (["lights", "--json"], None),
            (["config", "set", "daemon.port", "9999"], None),
            (["wiz", "discover"], None),
            (["install-hooks", "--all"], None),
            (["autostart", "install"], None),
        ]
        for suffix, expected in cases:
            result = subprocess.run(
                [
                    shutil.which("codex"),
                    "execpolicy",
                    "check",
                    "--rules",
                    str(rules),
                    "--",
                    *base,
                    *suffix,
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            assert json.loads(result.stdout).get("decision") == expected, suffix

    def test_install_writes_allow_rules(self, trusted_rule_install):
        changed, backup = commands_install.install_codex_rules()
        assert changed and backup is None
        text = commands_install.codex_rules_path().read_text()
        assert text.startswith(commands_install.RULES_MARKER)
        assert commands_install.RULES_SCOPE_MARKER in text
        assert text.count("prefix_rule(") == 5
        assert 'decision = "allow"' in text
        # the pattern is the resolved CLI command, element for element
        from hue_agent_status.client import resolve_cli_command

        for part in resolve_cli_command():
            assert f'"{part}"' in text
        assert commands_install.codex_rules_installed()
        if os.name != "nt":
            assert (
                stat.S_IMODE(commands_install.codex_rules_path().stat().st_mode)
                == 0o600
            )

    def test_reinstall_is_noop(self, trusted_rule_install):
        commands_install.install_codex_rules()
        assert commands_install.install_codex_rules() == (False, None)

    def test_uninstall_removes_rules(self, trusted_rule_install):
        commands_install.install_codex_rules()
        changed, _ = commands_install.uninstall_codex_rules()
        assert changed
        assert not commands_install.codex_rules_path().exists()

    def test_refuses_foreign_rules_file(self, trusted_rule_install):
        path = commands_install.codex_rules_path()
        path.parent.mkdir(parents=True)
        path.write_text('prefix_rule(pattern = ["mine"], decision = "allow")\n')
        with pytest.raises(ValueError, match="not ours"):
            commands_install.install_codex_rules()
        changed, _ = commands_install.uninstall_codex_rules()
        assert not changed and path.exists()

    def test_refusal_reasons_cover_editable_and_workspace_sources(self, tmp_path):
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        installed = tmp_path / "installed"
        installed.mkdir()
        executable = installed / "hue-agent"
        executable.write_text("launcher")
        installed_source = installed / "commands_install.py"
        installed_source.write_text("source")
        local_executable = workspace / "hue-agent"
        local_executable.write_text("launcher")
        local_source = workspace / "commands_install.py"
        local_source.write_text("source")

        assert "workspace-local" in commands_install._approval_rule_refusal_reason(
            [str(local_executable)],
            workspace=workspace,
            source_path=installed_source,
            editable=False,
        )
        assert "editable mode" in commands_install._approval_rule_refusal_reason(
            [str(executable)],
            workspace=workspace,
            source_path=installed_source,
            editable=True,
        )
        assert (
            "source is workspace-local"
            in commands_install._approval_rule_refusal_reason(
                [str(executable)],
                workspace=workspace,
                source_path=local_source,
                editable=False,
            )
        )
        assert "module invocation" in commands_install._approval_rule_refusal_reason(
            [str(executable), "-m", "hue_agent_status"],
            workspace=workspace,
            source_path=installed_source,
            editable=False,
        )
        assert (
            commands_install._approval_rule_refusal_reason(
                [str(executable)],
                workspace=workspace,
                source_path=installed_source,
                editable=False,
            )
            is None
        )

    def test_refusal_removes_previous_managed_blanket_rule(self, monkeypatch):
        path = commands_install.codex_rules_path()
        path.parent.mkdir(parents=True)
        path.write_text(
            f'{commands_install.RULES_MARKER}\nprefix_rule(pattern = ["hue-agent"])\n'
        )
        monkeypatch.setattr(
            commands_install,
            "_approval_rule_refusal_reason",
            lambda command: "the install is editable",
        )
        with pytest.raises(ValueError, match="normal per-command approval"):
            commands_install.install_codex_rules()
        assert not path.exists()


class TestCli:
    def test_requires_a_target_flag(self):
        assert main(["install-commands"]) == 2

    def test_install_and_uninstall_all(
        self, tmp_path, monkeypatch, capsys, trusted_rule_install
    ):
        monkeypatch.setenv("HUE_AGENT_CLAUDE_COMMANDS_DIR", str(tmp_path / "commands"))
        assert main(["install-commands", "--all"]) == 0
        out = capsys.readouterr().out
        assert "claude: /glow command installed" in out
        assert "codex: /glow command installed" in out
        assert "codex: $glow skill installed" in out
        assert "codex: approval rule installed" in out
        assert commands_install.is_installed("claude")
        assert commands_install.is_installed("codex")
        assert commands_install.codex_skill_installed()
        assert commands_install.codex_rules_installed()

        assert main(["uninstall-commands", "--all"]) == 0
        assert not commands_install.is_installed("claude")
        assert not commands_install.is_installed("codex")
        assert not commands_install.codex_skill_installed()
        assert not commands_install.codex_rules_installed()

    def test_untrusted_codex_install_keeps_command_and_skill(self, monkeypatch, capsys):
        monkeypatch.setattr(
            commands_install,
            "_approval_rule_refusal_reason",
            lambda command: "the install is editable",
        )
        assert main(["install-commands", "--codex"]) == 1
        captured = capsys.readouterr()
        assert "normal per-command approval" in captured.err
        assert commands_install.is_installed("codex")
        assert commands_install.codex_skill_installed()
        assert not commands_install.codex_rules_installed()

    def test_claude_only_does_not_touch_codex(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HUE_AGENT_CLAUDE_COMMANDS_DIR", str(tmp_path / "commands"))
        assert main(["install-commands", "--claude"]) == 0
        assert not commands_install.is_installed("codex")
        assert not commands_install.codex_skill_installed()
        assert not commands_install.codex_rules_installed()
