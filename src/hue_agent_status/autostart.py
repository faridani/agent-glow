"""Optional daemon autostart: systemd user service, LaunchAgent, or Scheduled Task.

Autostart is a convenience only — hooks always auto-start a detached daemon
when it is not running, so none of this is required for correct behavior.
"""

from __future__ import annotations

import os
import plistlib
import shutil
import subprocess
import sys
from pathlib import Path

from .client import resolve_cli_command

SERVICE_NAME = "hue-agent-status"
_MAC_LABEL = "io.github.faridani.hue-agent-status"


def _write_private_bytes(path: Path, content: bytes) -> None:
    """Write generated launch configuration without exposing embedded home paths."""
    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags, 0o600)
    try:
        if hasattr(os, "fchmod"):
            os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as fh:
            fd = -1
            fh.write(content)
    finally:
        if fd >= 0:
            os.close(fd)


def _run(cmd: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, check=False)


# -- Linux (systemd --user) ---------------------------------------------------


def _systemd_unit_path() -> Path:
    return Path.home() / ".config" / "systemd" / "user" / f"{SERVICE_NAME}.service"


def _systemd_quote(part: str) -> str:
    """Quote one ExecStart word for systemd (spaces, quotes, % specifiers)."""
    part = part.replace("%", "%%")
    if any(c in part for c in " \t'\""):
        return '"' + part.replace("\\", "\\\\").replace('"', '\\"') + '"'
    return part


def _install_systemd() -> str:
    if not shutil.which("systemctl"):
        return "systemctl not found; skipping (hooks will auto-start the daemon)"
    exec_start = " ".join(_systemd_quote(p) for p in resolve_cli_command() + ["daemon"])
    unit = _systemd_unit_path()
    content = (
        "[Unit]\n"
        "Description=hue-agent-status daemon (Hue lights for Claude Code / Codex)\n\n"
        "[Service]\n"
        f"ExecStart={exec_start}\n"
        "Restart=on-failure\n"
        "RestartSec=5\n\n"
        "[Install]\n"
        "WantedBy=default.target\n"
    )
    _write_private_bytes(unit, content.encode("utf-8"))
    _run(["systemctl", "--user", "daemon-reload"])
    result = _run(["systemctl", "--user", "enable", "--now", f"{SERVICE_NAME}.service"])
    if result.returncode != 0:
        return f"wrote {unit} but enabling failed: {result.stderr.strip()}"
    return f"installed and started systemd user service ({unit})"


def _uninstall_systemd() -> str:
    if shutil.which("systemctl"):
        _run(["systemctl", "--user", "disable", "--now", f"{SERVICE_NAME}.service"])
        _run(["systemctl", "--user", "daemon-reload"])
    unit = _systemd_unit_path()
    if unit.exists():
        unit.unlink()
        return f"removed {unit}"
    return "no systemd unit installed"


# -- macOS (LaunchAgent) -------------------------------------------------------


def _launchagent_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{_MAC_LABEL}.plist"


def _install_launchagent() -> str:
    plist = _launchagent_path()
    content = plistlib.dumps(
        {
            "Label": _MAC_LABEL,
            "ProgramArguments": resolve_cli_command() + ["daemon"],
            "RunAtLoad": True,
            "KeepAlive": False,
        }
    )
    _write_private_bytes(plist, content)
    result = _run(["launchctl", "load", str(plist)])
    if result.returncode != 0:
        return f"wrote {plist} but launchctl load failed: {result.stderr.strip()}"
    return f"installed LaunchAgent ({plist})"


def _uninstall_launchagent() -> str:
    plist = _launchagent_path()
    if plist.exists():
        _run(["launchctl", "unload", str(plist)])
        plist.unlink()
        return f"removed {plist}"
    return "no LaunchAgent installed"


# -- Windows (Scheduled Task) ----------------------------------------------------


def _windows_daemon_command() -> list[str]:
    # pythonw.exe keeps the logon task from flashing a console window.
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    if pythonw.exists():
        return [str(pythonw), "-m", "hue_agent_status", "daemon"]
    return resolve_cli_command() + ["daemon"]


def _install_schtask() -> str:
    command = " ".join(f'"{p}"' if " " in p else p for p in _windows_daemon_command())
    result = _run(
        [
            "schtasks",
            "/Create",
            "/F",
            "/SC",
            "ONLOGON",
            "/TN",
            SERVICE_NAME,
            "/TR",
            command,
        ]
    )
    if result.returncode != 0:
        return f"schtasks failed: {result.stderr.strip() or result.stdout.strip()}"
    return f"installed Scheduled Task '{SERVICE_NAME}' (runs at logon)"


def _uninstall_schtask() -> str:
    result = _run(["schtasks", "/Delete", "/F", "/TN", SERVICE_NAME])
    if result.returncode != 0:
        return "no Scheduled Task installed"
    return f"removed Scheduled Task '{SERVICE_NAME}'"


# -- public API --------------------------------------------------------------------


def install() -> str:
    if sys.platform == "darwin":
        return _install_launchagent()
    if sys.platform == "win32":
        return _install_schtask()
    return _install_systemd()


def uninstall() -> str:
    if sys.platform == "darwin":
        return _uninstall_launchagent()
    if sys.platform == "win32":
        return _uninstall_schtask()
    return _uninstall_systemd()


def status() -> str:
    if sys.platform == "darwin":
        return "installed" if _launchagent_path().exists() else "not installed"
    if sys.platform == "win32":
        result = _run(["schtasks", "/Query", "/TN", SERVICE_NAME])
        return "installed" if result.returncode == 0 else "not installed"
    return "installed" if _systemd_unit_path().exists() else "not installed"
