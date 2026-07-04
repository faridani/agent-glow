"""Client side of the local daemon API, used by hooks and CLI commands.

Everything here is deliberately forgiving: hooks must never block or break a
Claude Code / Codex session, so every call has a short timeout and failures
degrade to "do nothing".
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import sysconfig
import time
from pathlib import Path

import httpx

from .config import Config, daemon_log_path, state_dir
from .events import NormalizedEvent

DEFAULT_TIMEOUT_SECONDS = 0.5
_AUTOSTART_WAIT_SECONDS = 2.5
_AUTOSTART_COOLDOWN_SECONDS = 30.0


def _timeout() -> float:
    raw = os.environ.get("HUE_AGENT_HOOK_TIMEOUT_MS")
    if raw:
        try:
            return max(0.05, int(raw) / 1000)
        except ValueError:
            pass
    return DEFAULT_TIMEOUT_SECONDS


def _base_url(config: Config) -> str:
    # config validation guarantees this is a loopback host; bracket IPv6.
    host = config.daemon.host or "127.0.0.1"
    if ":" in host:
        host = f"[{host}]"
    return f"http://{host}:{config.daemon.port}"


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def resolve_cli_command() -> list[str]:
    """Absolute command to invoke this CLI, for hooks/daemon/autostart.

    Resolution is deterministic (interpreter-adjacent script dirs first) and
    never trusts a binary that PATH lookup found in the current working
    directory — on Windows that would let a repo ship a fake hue-agent.exe.
    """
    exe_name = "hue-agent.exe" if sys.platform == "win32" else "hue-agent"
    candidates = [Path(sys.executable).parent / exe_name]
    try:
        candidates.append(Path(sysconfig.get_path("scripts")) / exe_name)
    except (KeyError, TypeError):
        pass
    for candidate in candidates:
        if candidate.is_file():
            return [str(candidate)]
    found = shutil.which(exe_name)
    if found:
        path = Path(found).resolve()
        try:
            in_cwd = path.parent == Path.cwd().resolve()
        except OSError:
            in_cwd = True
        if not in_cwd:
            return [str(path)]
    return [sys.executable, "-m", "hue_agent_status"]


def _autostart_allowed() -> bool:
    """At most one daemon spawn attempt per cooldown window across processes."""
    stamp = state_dir() / "autostart.stamp"
    try:
        if time.time() - stamp.stat().st_mtime < _AUTOSTART_COOLDOWN_SECONDS:
            return False
    except OSError:
        pass
    try:
        state_dir().mkdir(parents=True, exist_ok=True)
        stamp.touch()
    except OSError:
        pass
    return True


def spawn_daemon_detached(config: Config) -> bool:
    """Start `hue-agent daemon` fully detached from the current process."""
    cmd = resolve_cli_command() + ["daemon"]
    try:
        state_dir().mkdir(parents=True, exist_ok=True)
        log = open(daemon_log_path(), "ab")
    except OSError:
        log = subprocess.DEVNULL
    kwargs: dict = {
        "stdin": subprocess.DEVNULL,
        "stdout": log,
        "stderr": log,
        "close_fds": True,
    }
    if sys.platform == "win32":
        DETACHED_PROCESS = 0x00000008
        CREATE_NEW_PROCESS_GROUP = 0x00000200
        CREATE_NO_WINDOW = 0x08000000
        kwargs["creationflags"] = (
            DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW
        )
    else:
        kwargs["start_new_session"] = True
    try:
        subprocess.Popen(cmd, **kwargs)
        return True
    except OSError:
        return False
    finally:
        if log is not subprocess.DEVNULL:
            log.close()


def post_event(
    config: Config,
    token: str,
    event: NormalizedEvent,
    timeout: float | None = None,
    autostart: bool = True,
) -> bool:
    timeout = timeout if timeout is not None else _timeout()
    url = f"{_base_url(config)}/event"
    payload = event.to_payload()
    headers = _headers(token)
    try:
        response = httpx.post(url, json=payload, headers=headers, timeout=timeout)
        return response.status_code == 200
    except httpx.HTTPError:
        pass
    if not autostart or not _autostart_allowed():
        return False
    if not spawn_daemon_detached(config):
        return False
    deadline = time.monotonic() + _AUTOSTART_WAIT_SECONDS
    while time.monotonic() < deadline:
        time.sleep(0.15)
        try:
            response = httpx.post(url, json=payload, headers=headers, timeout=timeout)
            return response.status_code == 200
        except httpx.HTTPError:
            continue
    return False


def get_health(config: Config, token: str, timeout: float = 1.0) -> dict | None:
    try:
        response = httpx.get(
            f"{_base_url(config)}/health", headers=_headers(token), timeout=timeout
        )
        if response.status_code == 200:
            data = response.json()
            return data if isinstance(data, dict) else None
    except (httpx.HTTPError, ValueError):
        pass
    return None


def post_restore(
    config: Config, token: str, policy: str | None = None, timeout: float = 10.0
) -> dict | None:
    body = {"policy": policy} if policy else {}
    try:
        response = httpx.post(
            f"{_base_url(config)}/restore",
            json=body,
            headers=_headers(token),
            timeout=timeout,
        )
        if response.status_code == 200:
            return response.json()
    except (httpx.HTTPError, ValueError):
        pass
    return None


def post_shutdown(config: Config, token: str, timeout: float = 5.0) -> bool:
    try:
        response = httpx.post(
            f"{_base_url(config)}/shutdown", headers=_headers(token), timeout=timeout
        )
        return response.status_code == 200
    except httpx.HTTPError:
        return False
