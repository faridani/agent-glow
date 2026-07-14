"""Install the /glow command for Claude Code and Codex.

Claude Code custom commands are markdown files in ``~/.claude/commands/``.
Codex gets three pieces:

* ``~/.codex/prompts/glow.md`` — a custom prompt, invoked as ``/prompts:glow``
  in current Codex builds (older ones list it as ``/glow``). Codex has
  deprecated custom prompts in favor of skills, so we install both.
* ``~/.agents/skills/glow/SKILL.md`` — an Agent Skill, invoked as ``$glow``
  or picked implicitly when the user asks about their status lights.
* ``~/.codex/rules/hue-agent-status.rules`` — narrowly scoped execpolicy
  rules for the privacy-minimized inventory and role/color changes used by
  ``$glow``. Other commands still require normal approval. Rules are not
  installed for editable or workspace-local executables/source.

All bodies get the same instructions (Claude's adds YAML frontmatter). The
absolute CLI path is embedded at install time — the agent's shell may not
have ``hue-agent`` on PATH — so reinstalling after moving the venv refreshes
it.

Mirrors hooks_claude.py's manners: non-destructive (a marker proves a file
is ours; foreign files are never touched), timestamped backups on overwrite,
clean uninstall.
"""

from __future__ import annotations

import importlib.metadata
import json
import os
import shlex
from pathlib import Path

from .client import resolve_cli_command
from .hooks_claude import atomic_write_text, backup_file, restrict_private_file
from .hooks_codex import codex_dir

GLOW_MARKER = "<!-- hue-agent-status:glow -->"
RULES_MARKER = "# hue-agent-status:glow"
RULES_SCOPE_MARKER = "# scope: glow-v2"
COMMAND_KINDS = ("claude", "codex")


def claude_commands_dir() -> Path:
    override = os.environ.get("HUE_AGENT_CLAUDE_COMMANDS_DIR")
    if override:
        return Path(override)
    return Path.home() / ".claude" / "commands"


def codex_prompts_dir() -> Path:
    return codex_dir() / "prompts"


def agents_skills_dir() -> Path:
    override = os.environ.get("HUE_AGENT_AGENTS_SKILLS_DIR")
    if override:
        return Path(override)
    return Path.home() / ".agents" / "skills"


def command_path(kind: str) -> Path:
    if kind == "claude":
        return claude_commands_dir() / "glow.md"
    return codex_prompts_dir() / "glow.md"


def codex_skill_path() -> Path:
    return agents_skills_dir() / "glow" / "SKILL.md"


def codex_rules_path() -> Path:
    return codex_dir() / "rules" / "hue-agent-status.rules"


def _cli_path() -> str:
    """Shell command for AI-facing instructions without exposing the home path."""
    home = Path.home().absolute()
    parts = []
    for part in resolve_cli_command():
        path = Path(part).expanduser()
        try:
            relative = Path(os.path.abspath(path)).relative_to(home)
        except (OSError, ValueError):
            parts.append(shlex.quote(part))
            continue
        suffix = relative.as_posix()
        suffix = (
            suffix.replace("\\", "\\\\")
            .replace('"', '\\"')
            .replace("$", "\\$")
            .replace("`", "\\`")
        )
        parts.append(f'"$HOME/{suffix}"' if suffix else '"$HOME"')
    return " ".join(parts)


_CODEX_SANDBOX_NOTE = """
Note: `hue-agent` talks to the light daemon on 127.0.0.1 and to bulbs on the
LAN, and its config lives outside the workspace, so it cannot run inside the
sandbox. For trusted, non-editable installs, narrowly scoped approval rules
cover only the `$glow` inventory, role, and wait-color commands. If Codex asks
for approval, request normal per-command approval instead of concluding the
lights are broken.
"""


def _body(cli: str, request_line: str, codex_note: bool = False) -> str:
    note = _CODEX_SANDBOX_NOTE if codex_note else ""
    return f"""{GLOW_MARKER}

You control the user's status lights (hue-agent-status). Bulbs in the
**thinking** role breathe while an agent is working; bulbs in the **waiting**
role switch to the wait color when an agent needs the user. A bulb can be in
both roles, one, or neither. An empty role means "all configured lights".

The CLI is: `{cli}`
{note}
User request: {request_line}

Privacy: output from `lights --agent` is sent to the selected AI provider. It
includes friendly bulb names, color capability, role membership, and wait
color only. It omits backend, reachability, stable Hue UUIDs, WiZ MAC
addresses, and IP addresses.

If the request is empty, just report the current setup (steps 1 and 5).

1. Run `{cli} lights --agent` to see light names, color capability, current
   role membership, and wait color. This privacy-minimized view omits backend,
   reachability, stable Hue UUIDs, WiZ MAC addresses, and IP addresses.
2. Map the request onto commands (refer to lights by NAME; matching is
   case-insensitive, unique substrings are fine):
   - replace a role's bulbs: `{cli} role set thinking "Desk lamp" "Shelf"`
   - add / remove bulbs: `{cli} role add waiting "Strip"` / `{cli} role remove thinking "Shelf"`
   - back to default (all lights): `{cli} role clear thinking`
   - change the waiting color: `{cli} config set animation.wait_color purple`
     (names like red/orange/green/blue/purple, `#rrggbb`, or CIE `"x,y"`)
   - for a new WiZ bulb, ask the user to run `{cli} wiz discover` and
     `{cli} wiz add ...` themselves in a local terminal; never run discovery
     from the agent because its output contains local IP and MAC addresses
3. If a name is ambiguous the command exits 2 and lists the candidates —
   retry with a fuller name from that list.
4. Changes apply immediately (the daemon reloads automatically); never edit
   the config file directly and never restart the daemon.
5. Finish with `{cli} lights --agent` and summarize in one or two sentences
   which bulbs now do what, e.g. "Desk lamp breathes while agents work; the
   strip turns purple when they need you."
"""


def build_command_markdown(kind: str, cli: str | None = None) -> str:
    cli = cli or _cli_path()
    body = _body(cli, request_line="$ARGUMENTS", codex_note=kind == "codex")
    if kind == "claude":
        frontmatter = (
            "---\n"
            "description: Configure which lights show agent status (thinking/waiting roles, colors)\n"
            'argument-hint: e.g. "only the desk lamp breathes, the strip goes purple when waiting"\n'
            "---\n"
        )
        return frontmatter + body
    return body


def build_skill_markdown(cli: str | None = None) -> str:
    cli = cli or _cli_path()
    frontmatter = (
        "---\n"
        "name: glow\n"
        "description: Configure which status lights react to AI-agent activity — "
        "assign bulbs to the thinking (breathe while working) or waiting (turn red "
        "when input is needed) role, change the wait color, or guide WiZ bulb setup. Use "
        "when the user mentions glow, status lights, or which lamp should breathe "
        "or turn red.\n"
        "---\n"
    )
    return frontmatter + _body(
        cli,
        request_line="whatever accompanies the $glow mention (the user's message).",
        codex_note=True,
    )


def _glow_rule_specs(command: list[str]) -> list[tuple[list, str]]:
    """Execpolicy prefixes for only the commands the generated skill uses."""
    return [
        (
            command + ["lights", "--agent"],
            "Read the privacy-minimized status-light inventory for $glow",
        ),
        (
            command + ["role", "show"],
            "Read status-light role assignments for $glow",
        ),
        (
            command + ["role", ["set", "add", "remove"], ["thinking", "waiting"]],
            "Change status-light role assignments requested through $glow",
        ),
        (
            command + ["role", "clear", ["thinking", "waiting"]],
            "Reset a status-light role requested through $glow",
        ),
        (
            command + ["config", "set", "animation.wait_color"],
            "Change only the status-light wait color requested through $glow",
        ),
    ]


def build_rules_content(command: list[str] | None = None) -> str:
    command = command or resolve_cli_command()
    blocks = []
    for pattern, justification in _glow_rule_specs(command):
        blocks.append(
            "prefix_rule(\n"
            f"    pattern = {json.dumps(pattern)},\n"
            '    decision = "allow",\n'
            f"    justification = {json.dumps(justification)},\n"
            ")"
        )
    return (
        f"{RULES_MARKER}\n"
        f"{RULES_SCOPE_MARKER}\n"
        "# Managed by `hue-agent install-commands --codex`; do not edit.\n"
        "# Only the privacy-minimized inventory and $glow role/color edits are\n"
        "# pre-approved. Every other hue-agent command follows normal approval.\n\n"
        + "\n\n".join(blocks)
        + "\n"
    )


def _workspace_root(start: Path | None = None) -> Path:
    current = (start or Path.cwd()).resolve()
    for candidate in (current, *current.parents):
        if (candidate / ".git").exists():
            return candidate
    return current


def _is_within(path: Path, directory: Path) -> bool:
    try:
        path.relative_to(directory)
        return True
    except ValueError:
        return False


def _is_editable_install() -> bool:
    try:
        direct_url = importlib.metadata.distribution("hue-agent-status").read_text(
            "direct_url.json"
        )
        data = json.loads(direct_url or "{}")
    except (
        importlib.metadata.PackageNotFoundError,
        json.JSONDecodeError,
        OSError,
        TypeError,
    ):
        return False
    if not isinstance(data, dict) or not isinstance(data.get("dir_info"), dict):
        return False
    return data["dir_info"].get("editable") is True


def _approval_rule_refusal_reason(
    command: list[str],
    *,
    workspace: Path | None = None,
    source_path: Path | None = None,
    editable: bool | None = None,
) -> str | None:
    """Explain why auto-approving this command would cross a trust boundary."""
    if len(command) != 1:
        return "the CLI resolves through a Python module invocation that a workspace can shadow"

    executable = Path(command[0]).expanduser()
    if not executable.is_absolute():
        return "the CLI executable path is not absolute"
    try:
        resolved_executable = executable.resolve(strict=True)
    except OSError:
        return "the CLI executable cannot be resolved"
    if not resolved_executable.is_file():
        return "the CLI executable is not a regular file"

    root = _workspace_root(workspace)
    lexical_executable = Path(os.path.abspath(executable))
    if _is_within(lexical_executable, root) or _is_within(resolved_executable, root):
        return "the CLI executable is workspace-local and can be edited by the agent"

    is_editable = _is_editable_install() if editable is None else editable
    if is_editable:
        return "hue-agent-status is installed in editable mode"

    source = (source_path or Path(__file__)).resolve()
    if _is_within(source, root):
        return "the hue-agent-status source is workspace-local and can be edited by the agent"
    return None


def _install_marked_file(
    path: Path, content: str, marker: str
) -> tuple[bool, Path | None]:
    """Write a file we own; returns (changed, backup). Never clobbers foreign files."""
    if path.exists():
        restrict_private_file(path)
        existing = path.read_text(encoding="utf-8")
        if marker not in existing:
            raise ValueError(f"{path} exists and is not ours; refusing to overwrite")
        if existing == content:
            try:
                os.chmod(path, 0o600)
            except OSError:
                pass
            return False, None
        backup = backup_file(path)
    else:
        backup = None
    atomic_write_text(path, content, mode=0o600)
    return True, backup


def _uninstall_marked_file(path: Path, marker: str) -> tuple[bool, Path | None]:
    if not path.exists():
        return False, None
    restrict_private_file(path)
    if marker not in path.read_text(encoding="utf-8"):
        return False, None  # not ours; leave it alone
    backup = backup_file(path)
    path.unlink()
    return True, backup


def install(kind: str) -> tuple[bool, Path | None]:
    return _install_marked_file(
        command_path(kind), build_command_markdown(kind), GLOW_MARKER
    )


def uninstall(kind: str) -> tuple[bool, Path | None]:
    return _uninstall_marked_file(command_path(kind), GLOW_MARKER)


def is_installed(kind: str) -> bool:
    path = command_path(kind)
    try:
        restrict_private_file(path)
        return GLOW_MARKER in path.read_text(encoding="utf-8")
    except OSError:
        return False


def install_codex_skill() -> tuple[bool, Path | None]:
    return _install_marked_file(codex_skill_path(), build_skill_markdown(), GLOW_MARKER)


def uninstall_codex_skill() -> tuple[bool, Path | None]:
    changed, backup = _uninstall_marked_file(codex_skill_path(), GLOW_MARKER)
    if changed:
        try:
            codex_skill_path().parent.rmdir()  # remove skills/glow/ if now empty
        except OSError:
            pass
    return changed, backup


def codex_skill_installed() -> bool:
    try:
        path = codex_skill_path()
        restrict_private_file(path)
        return GLOW_MARKER in path.read_text(encoding="utf-8")
    except OSError:
        return False


def install_codex_rules() -> tuple[bool, Path | None]:
    command = resolve_cli_command()
    reason = _approval_rule_refusal_reason(command)
    if reason:
        removed = False
        path = codex_rules_path()
        try:
            restrict_private_file(path)
            owned = RULES_MARKER in path.read_text(encoding="utf-8")
        except OSError:
            owned = False
        if owned:
            path.unlink()
            removed = True
        cleanup = "; the previous managed rule was removed" if removed else ""
        raise ValueError(
            f"refusing to auto-install an approval rule because {reason}{cleanup}; "
            "$glow remains usable with normal per-command approval"
        )
    return _install_marked_file(
        codex_rules_path(), build_rules_content(command), RULES_MARKER
    )


def uninstall_codex_rules() -> tuple[bool, Path | None]:
    return _uninstall_marked_file(codex_rules_path(), RULES_MARKER)


def codex_rules_installed() -> bool:
    try:
        path = codex_rules_path()
        restrict_private_file(path)
        content = path.read_text(encoding="utf-8")
        return RULES_MARKER in content and RULES_SCOPE_MARKER in content
    except OSError:
        return False
