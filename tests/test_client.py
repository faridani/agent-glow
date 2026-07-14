"""Privacy properties of the localhost daemon client."""

import os
import stat

import httpx

from hue_agent_status import client
from hue_agent_status.config import Config, daemon_log_path, state_dir
from hue_agent_status.events import NormalizedEvent


class _Response:
    status_code = 200

    @staticmethod
    def json():
        return {"ok": True}


def _event() -> NormalizedEvent:
    return NormalizedEvent(
        source="codex",
        session_id="session",
        state="active",
        event="UserPromptSubmit",
    )


def test_all_daemon_requests_ignore_proxy_environment(monkeypatch):
    calls = []

    def record(method):
        def request(*args, **kwargs):
            calls.append((method, args, kwargs))
            return _Response()

        return request

    monkeypatch.setattr(client.httpx, "get", record("get"))
    monkeypatch.setattr(client.httpx, "post", record("post"))

    config = Config()
    assert client.post_event(config, "token", _event(), autostart=False)
    assert client.get_health(config, "token") == {"ok": True}
    assert client.post_restore(config, "token") == {"ok": True}
    assert client.post_reload(config, "token") == {"ok": True}
    assert client.post_shutdown(config, "token")

    assert len(calls) == 5
    assert all(call[2]["trust_env"] is False for call in calls)


def test_post_event_retry_also_ignores_proxy_environment(monkeypatch):
    calls = []

    def post(*args, **kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            raise httpx.ConnectError("not running")
        return _Response()

    monkeypatch.setattr(client.httpx, "post", post)
    monkeypatch.setattr(client, "_autostart_allowed", lambda: True)
    monkeypatch.setattr(client, "spawn_daemon_detached", lambda config: True)
    monkeypatch.setattr(client.time, "sleep", lambda seconds: None)

    assert client.post_event(Config(), "token", _event())
    assert len(calls) == 2
    assert all(call["trust_env"] is False for call in calls)


def test_daemon_runtime_files_are_owner_only(monkeypatch):
    monkeypatch.setattr(client.subprocess, "Popen", lambda *args, **kwargs: object())

    assert client._autostart_allowed()
    assert client.spawn_daemon_detached(Config())

    if os.name != "nt":
        stamp = state_dir() / "autostart.stamp"
        assert stat.S_IMODE(state_dir().stat().st_mode) == 0o700
        assert stat.S_IMODE(stamp.stat().st_mode) == 0o600
        assert stat.S_IMODE(daemon_log_path().stat().st_mode) == 0o600
