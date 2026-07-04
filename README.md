# hue-agent-status

Philips Hue lights as a status indicator for AI coding agents.

While **Claude Code** or **OpenAI Codex** is working, your selected Hue lamps
slowly **breathe** — like a laptop's sleep light. The moment an agent needs
your input, approval, or attention, the lamps turn **red**. When every watched
session has ended, the lamps are **restored** to exactly the state they were
in before (including "off").

Works on Linux, macOS, and Windows. Python 3.11+.

## How it works

```
Claude Code / Codex hooks ──▶ hue-agent hook ──▶ local daemon (127.0.0.1 only)
                                                     │  breathe / red / restore
                                                     ▼
                                               Hue Bridge (LAN, API v2)
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
hue-agent setup               # discover bridge, press link button, pick lights
hue-agent install-hooks --all # wire up Claude Code and Codex
hue-agent doctor              # verify the whole chain
```

That's it. Start a Claude Code or Codex session and watch the lamp breathe.

`setup` walks you through:

1. **Bridge discovery** (or manual IP entry) — no Hue cloud account needed.
2. **Pairing** — you must physically press the bridge's link button; nothing
   is stored until the bridge issues an app key.
3. **Choosing targets** — individual lights, a room, or a zone.
4. **A live preview** — 10 s breathing, 3 s red, then restore.

## Commands

| Command | What it does |
| --- | --- |
| `hue-agent setup` | Discover + pair bridge, choose lights, preview |
| `hue-agent install-hooks --claude\|--codex\|--all` | Non-destructively add hooks (backs up configs first) |
| `hue-agent uninstall-hooks --claude\|--codex\|--all` | Remove only our hooks |
| `hue-agent daemon [--detach]` | Run the light daemon (foreground or background) |
| `hue-agent status` | Daemon state and tracked sessions |
| `hue-agent preview` | Breathe 10 s, red 3 s, restore |
| `hue-agent restore` | Manually restore lights from the snapshot |
| `hue-agent doctor` | Verify Python, config, keychain, bridge, key, targets, daemon, hooks |
| `hue-agent config show` / `config set <key> <value>` | Inspect / change configuration |
| `hue-agent autostart install\|uninstall\|status` | Optional: start daemon at login (systemd user service / LaunchAgent / Scheduled Task) |

## Configuration

Stored via `platformdirs` (e.g. `~/.config/hue-agent-status/config.toml` on
Linux, `~/Library/Application Support/hue-agent-status/` on macOS,
`%APPDATA%\hue-agent-status\` on Windows).

```toml
[bridge]
host = "192.168.1.50"
bridge_id = "001788fffe123456"
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
wait_color = "red"
wait_pulse_fallback = true   # double pulse for lamps that can't show red
restore = "smart"         # smart | always | never
idle_restore_transition_ms = 1500
wait_transition_ms = 800

[privacy]
debug_log_payloads = false
```

Example: `hue-agent config set animation.breath_period_seconds 7`

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
- **Codex** — `~/.codex/hooks.json`: command hooks (with `commandWindows`
  variants for Windows), plus `notify = ["<hue-agent>", "codex-notify"]` in
  `~/.codex/config.toml` so `agent-turn-complete` turns the lamps red. If you
  already have a different `notify` program configured, we refuse to replace
  it (chain `hue-agent codex-notify "$1"` from your script instead). The
  event table in `events.py` is data-driven, so future Codex events (e.g.
  `TuiQuestionOpened`) map to waiting with a one-line change.

Hook outputs never influence agent behavior — this is purely a side effect.

## Security & privacy

- Pairing requires the **physical link button** on the bridge.
- The Hue app key and daemon token are stored in your **OS keychain** when
  available; otherwise in a user-only (`0600`) file with a clear warning.
- The daemon binds **only to localhost** and refuses any other address.
- Every daemon request requires a **random bearer token**; payloads are
  capped at 64 KB.
- **No prompts, tool inputs, commands, file paths, or transcripts** are
  stored or logged. `--debug` prints only whitelisted metadata fields
  (event name, tool name, notification type).
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
