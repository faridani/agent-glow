# hue-agent-status

Philips Hue and WiZ lights as a status indicator for AI coding agents.

While **Claude Code** or **OpenAI Codex** is working, your selected lamps
slowly **breathe** — like a laptop's sleep light. The moment an agent needs
your input, approval, or attention, the lamps turn **red** (or any color you
pick). When every watched session has ended, the lamps are **restored** to
exactly the state they were in before (including "off").

You choose *which* bulbs do what: the **thinking** role breathes while agents
work, the **waiting** role shows the wait color — and you can reassign them
mid-session from inside Claude Code or Codex with the **/glow** command.

Works on Linux, macOS, and Windows. Python 3.11+.

## How it works

```
Claude Code / Codex hooks ──▶ hue-agent hook ──▶ local daemon (127.0.0.1 only)
                                                     │  breathe / red / restore
                                                     ├─▶ Hue Bridge (LAN, API v2)
                                                     └─▶ WiZ bulbs (LAN, UDP 38899)
```

- A tiny **daemon** owns the light animation loop. It binds only to
  `127.0.0.1`, requires a random bearer token on every request, and accepts
  only small JSON events. It never sees prompts, commands, or file contents.
- **Hooks** installed into Claude Code / Codex send normalized events
  (`active`, `waiting`, `ended`) per session. If the daemon isn't running,
  the hook starts it in the background. Hooks always exit 0 and time out in
  500 ms, so they can never block or break your agent.
- The daemon tracks every `(source, session)` pair. If *any* session is
  waiting for you → red. Else if any is working → breathing. Else → restore.
- Two kinds of red: *blocked* (permission prompt, question — persists up to
  `waiting_ttl_seconds`) and *turn over* (`Stop` / `agent-turn-complete` —
  fades back to the lamps' original state after `turn_end_waiting_seconds`,
  default 5 minutes, so a finished conversation doesn't hold your lights
  red all evening).

## Install

```bash
pip install hue-agent-status
# or: pipx install hue-agent-status / uv tool install hue-agent-status
```

## Quick start

```bash
hue-agent setup                  # enter bridge IP, press link button, pick lights
hue-agent install-hooks --all    # wire up Claude Code and Codex
hue-agent install-commands --all # add the /glow command to both agents
hue-agent doctor                 # verify the whole chain
```

That's it. Start a Claude Code or Codex session and watch the lamp breathe.
(Codex asks you once, on its next start, to review and trust the new hooks.)

`setup` walks you through:

1. **Manual bridge IP entry** — no Hue cloud account or internet lookup.
   `--cloud-discovery` optionally asks Signify's online service for the IP.
2. **Pairing** — you must physically press the bridge's link button; nothing
   is stored until the bridge issues an app key.
3. **Choosing targets** — individual lights, a room, or a zone.
4. **A live preview** — 10 s breathing, 3 s red, then restore.

## Commands

| Command | What it does |
| --- | --- |
| `hue-agent setup [--cloud-discovery]` | Pair bridge, choose lights, preview; online discovery is opt-in |
| `hue-agent install-hooks --claude\|--codex\|--all` | Non-destructively add hooks (backs up configs first) |
| `hue-agent uninstall-hooks --claude\|--codex\|--all` | Remove only our hooks |
| `hue-agent install-commands --claude\|--codex\|--all` | Install the `/glow` command — Codex also gets the `$glow` skill and a sandbox approval rule (uninstall: `uninstall-commands`) |
| `hue-agent lights [--json]` | List every known light with name, ref, capabilities, roles |
| `hue-agent role show` | Current thinking/waiting assignments |
| `hue-agent role set\|add\|remove <thinking\|waiting> <name>...` | Assign bulbs to a role by name (case-insensitive, substrings ok) |
| `hue-agent role clear <thinking\|waiting>` | Reset a role to the default (all configured lights) |
| `hue-agent wiz discover` | Find WiZ bulbs on your network |
| `hue-agent wiz add <mac> [--name N] [--ip A]` / `wiz list` / `wiz remove <mac-or-name>` | Manage WiZ bulbs |
| `hue-agent daemon [--detach]` | Run the light daemon (foreground or background) |
| `hue-agent status` | Daemon state and tracked sessions |
| `hue-agent preview` | Breathe 10 s, red 3 s, restore |
| `hue-agent restore` | Manually restore lights from the snapshot |
| `hue-agent reload` | Make a running daemon re-read the config (config commands do this automatically) |
| `hue-agent doctor` | Verify Python, config, keychain, bridge, key, targets, WiZ bulbs, daemon, hooks, /glow, Codex skill + approval rule |
| `hue-agent config show` / `config set <key> <value>` | Inspect / change configuration |
| `hue-agent autostart install\|uninstall\|status` | Optional: start daemon at login (systemd user service / LaunchAgent / Scheduled Task) |

## The /glow command

After `hue-agent install-commands --all`, describe what you want in plain
language — type `/glow` in Claude Code, or `$glow` in Codex (installed as an
Agent Skill; the legacy custom prompt is also installed and shows up as
`/prompts:glow` in current Codex builds, `/glow` in older ones):

```
/glow                                             # show current assignments
/glow only the desk lamp breathes while you work
/glow make the strip turn purple when you need me
/glow add the bookshelf to the waiting lights
```

The agent runs the `hue-agent lights` / `role` / `config` commands for you;
changes apply immediately (the daemon reloads on every config change).
`lights --agent` output is sent to your selected AI provider and includes
friendly bulb names, color capability, role membership, and wait color. It
omits backend, reachability, stable Hue UUIDs, WiZ MAC addresses, and IP
addresses. Generated instructions abbreviate executables inside your home
directory as `$HOME/...`, so they do not send your absolute home path or OS
account name to the AI provider.

On Codex the installer also writes narrowly scoped execpolicy rules
(`~/.codex/rules/hue-agent-status.rules`) for `lights --agent`, role
show/set/add/remove/clear, and setting `animation.wait_color`. Other
`hue-agent` commands still require normal approval. The rules are skipped for
an editable install or when the command or source is inside the current
workspace; `$glow` still works there with normal per-command approval.

## Bulb roles

Two roles decide which bulb shows what:

- **thinking** — breathes while any agent is working.
- **waiting** — switches to `animation.wait_color` when an agent needs you.

By default both roles cover all configured lights (the original behavior). A
bulb in only one role is left completely untouched — and restored to its
previous state — while the other role is active. Roles live in config as
light references:

```toml
[roles]
thinking = ["hue:<resource-uuid>"]
waiting  = ["hue:<resource-uuid>", "wiz:aabbccddeeff"]
```

`wait_color` accepts a name (`red`, `orange`, `yellow`, `green`, `cyan`,
`blue`, `purple`, `magenta`, `pink`, `white`), a hex `#rrggbb`, or a CIE
`"x,y"` pair.

## WiZ bulbs

Bulbs set up in the WiZ app (Philips Smart Wi-Fi lighting, no hub) are driven
directly over the LAN — UDP port 38899, no cloud:

```bash
hue-agent wiz discover                       # broadcast: list bulbs with MAC + IP
hue-agent wiz add aabbccddeeff --name "Desk strip"
```

Bulbs are identified by MAC; the IP is only a cached hint that discovery
refreshes automatically when DHCP moves a bulb (a DHCP reservation still
makes startup snappier). RGB models show the wait color, tunable-white models
fall back to warm white, dimmable-only models to a brightness pulse. A WiZ
bulb that's offline never delays or blocks the Hue side (and vice versa).

## Configuration

Stored via `platformdirs` (e.g. `~/.config/hue-agent-status/config.toml` on
Linux, `~/Library/Application Support/hue-agent-status/` on macOS,
`%APPDATA%\hue-agent-status\` on Windows).

```toml
[bridge]
host = "192.0.2.10"       # example only; replace with your bridge's LAN IP
bridge_id = "<bridge-id>"
api_version = 2

[daemon]
host = "127.0.0.1"        # loopback only; anything else is rejected
port = 8765
active_ttl_seconds = 1800    # forget "active" sessions after 30 min silence
waiting_ttl_seconds = 14400  # blocked-waiting (permission prompts) after 4 h
turn_end_waiting_seconds = 300  # "turn over, your move" red fades after 5 min

[target]
mode = "lights"           # lights | room | zone | grouped_light
ids = ["<resource-uuid>"]

[animation]
breath_period_seconds = 6.0
breath_min_brightness = 25
breath_max_brightness = 65
easing = "sine"           # sine | linear
breath_color = "auto"     # auto | warm | cool | preserve
wait_brightness = 85
wait_color = "red"        # name | "#rrggbb" | "x,y"
wait_pulse_fallback = true   # double pulse for lamps that can't show color
restore = "smart"         # smart | always | never
idle_restore_transition_ms = 1500
wait_transition_ms = 800

[wiz]
broadcast = "255.255.255.255"

[[wiz.bulbs]]             # managed by `hue-agent wiz add/remove`
mac = "aabbccddeeff"
ip = "192.0.2.42"         # example only; discovery keeps a fresher runtime cache
name = "Desk strip"

[roles]                   # empty list = all configured lights
thinking = []
waiting = []

[privacy]
debug_log_payloads = false
```

Example: `hue-agent config set animation.breath_period_seconds 7` — config
commands tell a running daemon to reload automatically.

### Restore modes

- **smart** (default): restore only lights still under our control. If you
  manually switch or dim a controlled lamp mid-session, we stop touching it
  until the next idle cycle — your change wins.
- **always**: restore every snapshot light regardless.
- **never**: leave lights wherever the animation ended.

### Breathing

The daemon sends a few brightness keyframes per cycle sampled from a sine
curve and lets the bridge's own transitions glide between them (about one
command every 1.5 s at the default 6 s period) — no rapid command spam, and
grouped-light targets use a single command for the whole room.

Lamps that were already on keep their color while breathing; lamps that were
off breathe in a soft warm white. For "red" on lamps without color support:
color-temperature lamps use bright warm white, and dimmable-only lamps use
high brightness plus an optional gentle double pulse.

## What gets installed where

`hue-agent install-hooks` always backs up the target file first
(`*.hue-agent-backup-<timestamp>`) and merges — existing hooks are preserved.

- **Claude Code** — `~/.claude/settings.json`: hooks for `SessionStart`,
  `UserPromptSubmit`, `PreToolUse`, `PostToolUse`, `PostToolBatch`,
  `PermissionRequest`, `Notification`, `Stop`, `StopFailure`, `SessionEnd`,
  each invoking `hue-agent hook --source claude`. `AskUserQuestion` /
  `ExitPlanMode` tool calls and permission prompts map to **waiting** (red).
- **Codex** — `~/.codex/hooks.json`: command hooks in Codex's matcher-group
  format (`{"hooks": [{"type": "command", ...}]}`, with `commandWindows`
  variants for Windows, a 10 s timeout, and `async` so they never stall a
  turn); Codex asks you to review and trust new hooks on its next start.
  Installing also migrates the flat argv-list entries an earlier version of
  this tool wrote — current Codex parses those as empty groups and silently
  never runs them. Plus `notify = ["<hue-agent>", "codex-notify"]` in
  `~/.codex/config.toml` so `agent-turn-complete` turns the lamps red. If you
  already have a different `notify` program configured, we refuse to replace
  it (chain `hue-agent codex-notify "$1"` from your script instead). The
  event table in `events.py` is data-driven, so future Codex events (e.g.
  `TuiQuestionOpened`) map to waiting with a one-line change.
- **/glow command** — `hue-agent install-commands` writes
  `~/.claude/commands/glow.md`, and for Codex three pieces:
  `~/.codex/prompts/glow.md` (legacy custom prompt, `/prompts:glow`),
  `~/.agents/skills/glow/SKILL.md` (the `$glow` Agent Skill — Codex is
  deprecating custom prompts in favor of skills), and
  `~/.codex/rules/hue-agent-status.rules` (narrow execpolicy rules for the
  privacy-minimized inventory and role/wait-color changes). Files carry an
  ownership marker; a `glow.md` or `SKILL.md` you wrote yourself is never
  overwritten, and `uninstall-commands` removes only ours (with a backup).

Hook outputs never influence agent behavior — this is purely a side effect.

## Security & privacy

- Pairing requires the **physical link button** on the bridge.
- Setup uses manual bridge IP entry by default. `--cloud-discovery` contacts
  Signify's online discovery endpoint, which necessarily sees your public IP.
- The Hue app key and daemon token are stored in your **OS keychain** when
  available; otherwise in a user-only (`0600`) file with a clear warning.
- The daemon binds **only to localhost** and refuses any other address.
- Every daemon request requires a **random bearer token**; payloads are
  capped at 64 KB.
- **No prompts, tool inputs, commands, file paths, or transcripts** are
  stored or logged. `--debug` prints only whitelisted metadata fields
  (event name, tool name, notification type).
- Codex pre-approves only the trusted install's **absolute executable path**
  combined with the specific `$glow` inventory, role, and wait-color command
  prefixes. Discovery, setup, hook installation, autostart, and every other
  subcommand still require approval. Editable and workspace-local installs do
  not get approval rules. `uninstall-commands --codex` removes our rules.
- If this tool fails in any way, hooks still exit 0 — your agent session is
  never affected.

## Development

```bash
git clone https://github.com/faridani/agent-glow
cd agent-glow
python -m venv .venv && . .venv/bin/activate
pip install -e '.[dev]'
pytest
```

## License

MIT
