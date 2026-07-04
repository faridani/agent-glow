"""Non-destructive install/uninstall of Claude Code hooks in ~/.claude/settings.json."""

from __future__ import annotations

import json
import os
import shlex
import shutil
import sys
import time
from pathlib import Path

from .client import resolve_cli_command

CLAUDE_HOOK_EVENTS = [
    "SessionStart",
    "UserPromptSubmit",
    "PreToolUse",
    "PostToolUse",
    "PostToolBatch",
    "PermissionRequest",
    "Notification",
    "Stop",
    "StopFailure",
    "SessionEnd",
]

_HOOK_TIMEOUT_SECONDS = 10


def claude_settings_path() -> Path:
    override = os.environ.get("HUE_AGENT_CLAUDE_SETTINGS")
    if override:
        return Path(override)
    return Path.home() / ".claude" / "settings.json"


def _quote(part: str) -> str:
    """Quote one command part for the platform shell that runs hooks."""
    if sys.platform == "win32":
        return f'"{part}"' if (" " in part or "\t" in part) else part
    return shlex.quote(part)


def build_hook_command(source: str = "claude") -> str:
    parts = resolve_cli_command() + ["hook", "--source", source]
    return " ".join(_quote(p) for p in parts)


def is_our_command(command: str) -> bool:
    return ("hue-agent" in command or "hue_agent_status" in command) and " hook " in f" {command} "


def backup_file(path: Path) -> Path | None:
    if not path.exists():
        return None
    stamp = time.strftime("%Y%m%d-%H%M%S")
    backup = path.with_name(f"{path.name}.hue-agent-backup-{stamp}")
    shutil.copy2(path, backup)  # preserves restrictive permissions
    return backup


def _load_json(path: Path) -> dict:
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8") or "{}")
    if not isinstance(data, dict):
        raise ValueError(f"{path} does not contain a JSON object")
    return data


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def _event_has_our_hook(matcher_groups: list) -> bool:
    for group in matcher_groups:
        if not isinstance(group, dict):
            continue
        for hook in group.get("hooks", []):
            if isinstance(hook, dict) and is_our_command(str(hook.get("command", ""))):
                return True
    return False


def install(settings_path: Path | None = None) -> tuple[bool, Path | None]:
    """Add our hooks; returns (changed, backup path). Merges, never overwrites."""
    path = settings_path or claude_settings_path()
    data = _load_json(path)
    hooks = data.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        raise ValueError(f"'hooks' in {path} is not an object; refusing to modify")
    command = build_hook_command("claude")
    changed = False
    for event in CLAUDE_HOOK_EVENTS:
        groups = hooks.setdefault(event, [])
        if not isinstance(groups, list):
            raise ValueError(f"hooks.{event} in {path} is not a list; refusing to modify")
        if _event_has_our_hook(groups):
            continue
        groups.append(
            {
                "hooks": [
                    {
                        "type": "command",
                        "command": command,
                        "timeout": _HOOK_TIMEOUT_SECONDS,
                    }
                ]
            }
        )
        changed = True
    backup = None
    if changed:
        backup = backup_file(path)
        _write_json(path, data)
    return changed, backup


def uninstall(settings_path: Path | None = None) -> tuple[bool, Path | None]:
    path = settings_path or claude_settings_path()
    if not path.exists():
        return False, None
    data = _load_json(path)
    hooks = data.get("hooks")
    if not isinstance(hooks, dict):
        return False, None
    changed = False
    for event in list(hooks.keys()):
        groups = hooks.get(event)
        if not isinstance(groups, list):
            continue
        new_groups = []
        for group in groups:
            if not isinstance(group, dict):
                new_groups.append(group)
                continue
            kept = [
                hook
                for hook in group.get("hooks", [])
                if not (isinstance(hook, dict) and is_our_command(str(hook.get("command", ""))))
            ]
            if len(kept) != len(group.get("hooks", [])):
                changed = True
            if kept:
                group = dict(group)
                group["hooks"] = kept
                new_groups.append(group)
            elif not group.get("hooks"):
                # group had no hooks at all — keep as-is (not ours)
                new_groups.append(group)
        if new_groups != groups:
            if new_groups:
                hooks[event] = new_groups
            else:
                del hooks[event]
            changed = True
    backup = None
    if changed:
        backup = backup_file(path)
        _write_json(path, data)
    return changed, backup


def is_installed(settings_path: Path | None = None) -> bool:
    path = settings_path or claude_settings_path()
    try:
        data = _load_json(path)
    except (OSError, ValueError):
        return False
    hooks = data.get("hooks")
    if not isinstance(hooks, dict):
        return False
    return any(
        isinstance(groups, list) and _event_has_our_hook(groups)
        for groups in hooks.values()
    )
