"""CLI behavior, especially the never-fail guarantee of hook paths."""

import io
import json

import pytest

from hue_agent_status import client as client_module
from hue_agent_status.cli import main


@pytest.fixture
def sent_events(monkeypatch):
    """Capture events instead of talking to a daemon (or spawning one)."""
    captured = []

    def fake_post_event(config, token, event, timeout=None, autostart=True):
        captured.append(event)
        return True

    monkeypatch.setattr(client_module, "post_event", fake_post_event)
    return captured


def _stdin(monkeypatch, data: bytes):
    fake = io.TextIOWrapper(io.BytesIO(data), encoding="utf-8")
    monkeypatch.setattr("sys.stdin", fake)


class TestHookCommand:
    def test_valid_event_is_sent(self, monkeypatch, sent_events):
        payload = {"hook_event_name": "UserPromptSubmit", "session_id": "abc"}
        _stdin(monkeypatch, json.dumps(payload).encode())
        assert main(["hook", "--source", "claude"]) == 0
        (event,) = sent_events
        assert event.state == "active"
        assert event.session_id == "abc"

    def test_noop_event_sends_nothing(self, monkeypatch, sent_events):
        payload = {"hook_event_name": "SessionStart", "session_id": "abc"}
        _stdin(monkeypatch, json.dumps(payload).encode())
        assert main(["hook", "--source", "claude"]) == 0
        assert sent_events == []

    def test_garbage_stdin_exits_zero(self, monkeypatch, sent_events):
        _stdin(monkeypatch, b"\x00\xffnot json at all")
        assert main(["hook", "--source", "claude"]) == 0
        assert sent_events == []

    def test_empty_stdin_exits_zero(self, monkeypatch, sent_events):
        _stdin(monkeypatch, b"")
        assert main(["hook", "--source", "claude"]) == 0

    def test_oversized_payload_dropped(self, monkeypatch, sent_events):
        big = json.dumps({"hook_event_name": "Stop", "x": "a" * (70 * 1024)})
        _stdin(monkeypatch, big.encode())
        assert main(["hook", "--source", "claude"]) == 0
        assert sent_events == []

    def test_bad_arguments_still_exit_zero(self, monkeypatch):
        _stdin(monkeypatch, b"{}")
        assert main(["hook", "--source", "aliens"]) == 0
        assert main(["hook"]) == 0

    def test_delivery_failure_exits_zero(self, monkeypatch):
        def boom(*args, **kwargs):
            raise RuntimeError("daemon exploded")

        monkeypatch.setattr(client_module, "post_event", boom)
        payload = {"hook_event_name": "Stop", "session_id": "abc"}
        _stdin(monkeypatch, json.dumps(payload).encode())
        assert main(["hook", "--source", "claude"]) == 0

    def test_debug_output_redacts_session_id(self, monkeypatch, sent_events, capsys):
        payload = {
            "hook_event_name": "UserPromptSubmit",
            "session_id": "private-session-identifier",
        }
        _stdin(monkeypatch, json.dumps(payload).encode())
        assert main(["hook", "--source", "claude", "--debug"]) == 0
        err = capsys.readouterr().err
        assert "private-session-identifier" not in err
        assert "UserPromptSubmit" in err

    def test_debug_error_redacts_exception_message(self, monkeypatch, capsys):
        def boom(*args, **kwargs):
            raise RuntimeError("private path: USER_HOME/private/project")

        monkeypatch.setattr(client_module, "post_event", boom)
        payload = {"hook_event_name": "Stop", "session_id": "abc"}
        _stdin(monkeypatch, json.dumps(payload).encode())
        assert main(["hook", "--source", "claude", "--debug"]) == 0
        err = capsys.readouterr().err
        assert "USER_HOME/private" not in err
        assert "error=RuntimeError" in err


class TestCodexNotify:
    def test_agent_turn_complete_sent_as_complete(self, sent_events):
        payload = json.dumps({"type": "agent-turn-complete", "thread_id": "t1"})
        assert main(["codex-notify", payload]) == 0
        (event,) = sent_events
        assert event.state == "complete"
        assert event.source == "codex"

    def test_unknown_type_exits_zero(self, sent_events):
        assert main(["codex-notify", json.dumps({"type": "mystery"})]) == 0
        assert sent_events == []

    def test_invalid_json_exits_zero(self, sent_events):
        assert main(["codex-notify", "{{{"]) == 0

    def test_missing_argument_exits_zero(self, sent_events):
        assert main(["codex-notify"]) == 0

    def test_debug_output_redacts_thread_id(self, sent_events, capsys):
        payload = json.dumps(
            {"type": "agent-turn-complete", "thread_id": "private-thread-identifier"}
        )
        assert main(["codex-notify", payload, "--debug"]) == 0
        err = capsys.readouterr().err
        assert "private-thread-identifier" not in err
        assert "agent-turn-complete" in err


class TestConfigCommands:
    def test_config_show_runs(self, capsys):
        assert main(["config", "show"]) == 0
        out = capsys.readouterr().out
        assert "[daemon]" in out and "port = 8765" in out

    def test_config_set_persists(self, capsys):
        assert main(["config", "set", "daemon.port", "9001"]) == 0
        assert main(["config", "show"]) == 0
        assert "port = 9001" in capsys.readouterr().out

    def test_config_set_rejects_bad_value(self, capsys):
        assert main(["config", "set", "daemon.host", "0.0.0.0"]) == 2

    def test_version_flag(self, capsys):
        assert main(["--version"]) == 0
        assert "hue-agent-status" in capsys.readouterr().out


class TestInstallHooksCommand:
    def test_requires_a_target_flag(self, capsys):
        assert main(["install-hooks"]) == 2

    def test_install_all(self, capsys):
        assert main(["install-hooks", "--all"]) == 0
        out = capsys.readouterr().out
        assert "claude: hooks installed" in out
        assert "codex: hooks installed" in out
        assert "codex: notify installed" in out
        assert "/hooks" in out and "trust" in out

    def test_uninstall_after_install(self, capsys):
        main(["install-hooks", "--all"])
        assert main(["uninstall-hooks", "--all"]) == 0
        out = capsys.readouterr().out
        assert "claude: hooks removed" in out
