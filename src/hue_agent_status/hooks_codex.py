"""Non-destructive install/uninstall of Codex hooks and notify configuration.

* ``~/.codex/hooks.json`` — command hooks for session/tool events. The format
  mirrors codex-rs's ``HooksFile``: each event maps to a list of *matcher
  groups*, ``{"hooks": [{"type": "command", "command": "<shell string>",
  ...}]}``. (An earlier version of this tool wrote flat entries with argv
  lists; Codex parses those as empty matcher groups and silently never runs
  them, so install migrates them.) Codex asks the user to review and trust
  new hooks on its next start.
* ``~/.codex/config.toml`` — ``notify = [<hue-agent>, "codex-notify"]`` so that
  ``agent-turn-complete`` notifications mark the session as waiting.

The config.toml edit is line-based on purpose: rewriting the whole document
through a TOML serializer would destroy the user's comments and formatting.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import sys
import tomllib
from pathlib import Path

from .client import resolve_cli_command
from .hooks_claude import (
    atomic_write_text,
    backup_file,
    command_invokes,
    restrict_private_file,
)

CODEX_HOOK_EVENTS = [
    "SessionStart",
    "UserPromptSubmit",
    "PreToolUse",
    "PermissionRequest",
    "PostToolUse",
    "SubagentStart",
    "SubagentStop",
    "Stop",
]

#: Codex's own default is 600 s; a status-light hook must never stall a turn
#: that long (hue-agent exits within ~0.5 s on its own).
CODEX_HOOK_TIMEOUT_SECONDS = 10


def codex_dir() -> Path:
    override = os.environ.get("HUE_AGENT_CODEX_DIR")
    if override:
        return Path(override)
    return Path.home() / ".codex"


def codex_hooks_path() -> Path:
    return codex_dir() / "hooks.json"


def codex_config_path() -> Path:
    return codex_dir() / "config.toml"


def _windows_command(args: list[str]) -> list[str]:
    """Windows variant of the hook command for the ``commandWindows`` key."""
    if sys.platform == "win32":
        # resolve_cli_command is hardened against CWD binary planting.
        return resolve_cli_command() + args
    return ["py", "-m", "hue_agent_status"] + args


def build_hook_entry() -> dict:
    """One matcher group holding our command handler, per the HooksFile schema."""
    args = ["hook", "--source", "codex"]
    handler = {
        "type": "command",
        "command": shlex.join(resolve_cli_command() + args),
        "commandWindows": subprocess.list2cmdline(_windows_command(args)),
        "timeout": CODEX_HOOK_TIMEOUT_SECONDS,
        "async": True,
    }
    return {"hooks": [handler]}


def _is_our_command(command, subcommand: str) -> bool:
    return command_invokes(command, subcommand)


def _is_our_handler(handler) -> bool:
    return isinstance(handler, dict) and (
        _is_our_command(handler.get("command"), "hook")
        or _is_our_command(handler.get("commandWindows"), "hook")
    )


def _is_our_legacy_entry(entry) -> bool:
    """The old flat format we used to write: {"command": [argv...], ...}."""
    return (
        isinstance(entry, dict)
        and "hooks" not in entry
        and (
            _is_our_command(entry.get("command"), "hook")
            or _is_our_command(entry.get("commandWindows"), "hook")
        )
    )


def _without_our_handlers(entry):
    """Entry minus anything of ours: unchanged, trimmed, or None (drop it)."""
    if not isinstance(entry, dict):
        return entry
    if _is_our_legacy_entry(entry):
        return None
    handlers = entry.get("hooks")
    if not isinstance(handlers, list):
        return entry
    kept = [h for h in handlers if not _is_our_handler(h)]
    if len(kept) == len(handlers):
        return entry
    if not kept:
        return None
    return {**entry, "hooks": kept}


def _is_our_entry(entry) -> bool:
    if _is_our_legacy_entry(entry):
        return True
    if not isinstance(entry, dict):
        return False
    handlers = entry.get("hooks")
    return isinstance(handlers, list) and any(_is_our_handler(h) for h in handlers)


def _load_json(path: Path) -> dict:
    if not path.exists():
        return {}
    restrict_private_file(path)
    data = json.loads(path.read_text(encoding="utf-8") or "{}")
    if not isinstance(data, dict):
        raise ValueError(f"{path} does not contain a JSON object")
    return data


def _write_json(path: Path, data: dict) -> None:
    atomic_write_text(path, json.dumps(data, indent=2) + "\n", mode=0o600)


def install_hooks(hooks_path: Path | None = None) -> tuple[bool, Path | None]:
    """Add (or refresh) our matcher group per event; migrates the legacy format."""
    path = hooks_path or codex_hooks_path()
    data = _load_json(path)
    hooks = data.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        raise ValueError(f"'hooks' in {path} is not an object; refusing to modify")
    entry = build_hook_entry()
    changed = False
    for event in CODEX_HOOK_EVENTS:
        entries = hooks.setdefault(event, [])
        if not isinstance(entries, list):
            raise ValueError(
                f"hooks.{event} in {path} is not a list; refusing to modify"
            )
        foreign = [k for e in entries if (k := _without_our_handlers(e)) is not None]
        new_entries = foreign + [entry]
        if new_entries != entries:
            hooks[event] = new_entries
            changed = True
    backup = None
    if changed:
        backup = backup_file(path)
        _write_json(path, data)
    elif path.exists():
        restrict_private_file(path)
    return changed, backup


def uninstall_hooks(hooks_path: Path | None = None) -> tuple[bool, Path | None]:
    path = hooks_path or codex_hooks_path()
    if not path.exists():
        return False, None
    restrict_private_file(path)
    data = _load_json(path)
    hooks = data.get("hooks")
    if not isinstance(hooks, dict):
        return False, None
    changed = False
    for event in list(hooks.keys()):
        entries = hooks.get(event)
        if not isinstance(entries, list):
            continue
        kept = [k for e in entries if (k := _without_our_handlers(e)) is not None]
        if kept != entries:
            changed = True
            if kept:
                hooks[event] = kept
            else:
                del hooks[event]
    backup = None
    if changed:
        backup = backup_file(path)
        _write_json(path, data)
    return changed, backup


def hooks_installed(hooks_path: Path | None = None) -> bool:
    """True only for the current matcher-group format — a legacy-format
    install does nothing in today's Codex, so it counts as not installed."""
    path = hooks_path or codex_hooks_path()
    try:
        data = _load_json(path)
    except (OSError, ValueError):
        return False
    hooks = data.get("hooks")
    if not isinstance(hooks, dict):
        return False
    return any(
        isinstance(entries, list)
        and any(_is_our_entry(e) and not _is_our_legacy_entry(e) for e in entries)
        for entries in hooks.values()
    )


# -- notify = [...] in config.toml ------------------------------------------


def _notify_command() -> list[str]:
    return resolve_cli_command() + ["codex-notify"]


def _notify_line(command: list[str]) -> str:
    return "notify = [" + ", ".join(json.dumps(part) for part in command) + "]"


_NOTIFY_RE = re.compile(r"^\s*notify\s*=")


def _top_level_region(lines: list[str]) -> int:
    """Index of the first table header; top-level keys must be inserted before it."""
    for i, line in enumerate(lines):
        if re.match(r"^\s*\[", line):
            return i
    return len(lines)


def current_notify(config_path: Path | None = None) -> list[str] | None:
    path = config_path or codex_config_path()
    try:
        restrict_private_file(path)
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return None
    notify = data.get("notify")
    if isinstance(notify, list) and all(isinstance(x, str) for x in notify):
        return notify
    return None


def notify_installed(config_path: Path | None = None) -> bool:
    notify = current_notify(config_path)
    return bool(notify) and _is_our_command(notify, "codex-notify")


def install_notify(config_path: Path | None = None) -> tuple[bool, Path | None]:
    """Set top-level ``notify`` in config.toml, preserving the rest of the file."""
    path = config_path or codex_config_path()
    desired = _notify_command()
    current = current_notify(path)
    if current == desired:
        restrict_private_file(path)
        return False, None
    if current and not _is_our_command(current, "codex-notify"):
        # Codex supports a single notify program; never silently replace a
        # foreign one the user configured.
        raise ValueError(
            f"{path} already sets notify = {current!r}; remove it first, or chain "
            '`hue-agent codex-notify "$1"` from your existing notify script'
        )
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    # Sanity-check the document parses before we edit it.
    if text.strip():
        tomllib.loads(text)
    lines = text.splitlines()
    new_line = _notify_line(desired)
    top_end = _top_level_region(lines)
    replaced = False
    for i in range(top_end):
        if _NOTIFY_RE.match(lines[i]):
            lines[i] = new_line
            replaced = True
            break
    if not replaced:
        lines.insert(top_end, new_line)
        # keep a blank line between our key and the first table
        if top_end < len(lines) - 1 and lines[top_end + 1].strip():
            lines.insert(top_end + 1, "")
    new_text = "\n".join(lines).rstrip("\n") + "\n"
    tomllib.loads(new_text)  # never write invalid TOML
    backup = backup_file(path)
    atomic_write_text(path, new_text, mode=0o600)
    return True, backup


def uninstall_notify(config_path: Path | None = None) -> tuple[bool, Path | None]:
    """Remove our notify line; leaves foreign notify configurations alone."""
    path = config_path or codex_config_path()
    if not path.exists() or not notify_installed(path):
        return False, None
    lines = path.read_text(encoding="utf-8").splitlines()
    top_end = _top_level_region(lines)
    kept = []
    changed = False
    for i, line in enumerate(lines):
        if (
            i < top_end
            and _NOTIFY_RE.match(line)
            and ("hue-agent" in line or "hue_agent_status" in line)
        ):
            changed = True
            continue
        kept.append(line)
    if not changed:
        return False, None
    new_text = (
        "\n".join(kept).rstrip("\n") + "\n"
        if any(line.strip() for line in kept)
        else ""
    )
    tomllib.loads(new_text)
    backup = backup_file(path)
    atomic_write_text(path, new_text, mode=0o600)
    return True, backup
