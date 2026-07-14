"""`hue-agent` command-line interface.

Heavy modules are imported lazily inside each subcommand so that the
latency-critical paths (`hook`, `codex-notify`) stay fast and, above all,
never raise: those two subcommands always exit 0.
"""

from __future__ import annotations

import argparse
import json
import sys

from . import MAX_HOOK_PAYLOAD_BYTES, __version__

#: Payload keys that are safe to echo in --debug output. Everything else
#: (prompts, tool inputs, commands, file paths, transcripts) is redacted.
_DEBUG_SAFE_KEYS = ("hook_event_name", "tool_name", "notification_type", "type")


def _debug_event(event) -> dict | None:
    """Debug-safe event fields; session identifiers are deliberately omitted."""
    if event is None:
        return None
    return {
        "source": event.source,
        "state": event.state,
        "event": event.event,
        "scope": event.scope,
    }


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="hue-agent",
        description="Philips Hue and WiZ lights as status indicators for Claude Code and OpenAI Codex.",
    )
    parser.add_argument(
        "--version", action="version", version=f"hue-agent-status {__version__}"
    )
    sub = parser.add_subparsers(dest="command")

    p = sub.add_parser("setup", help="pair a Hue Bridge and choose lights")
    p.add_argument(
        "--cloud-discovery",
        action="store_true",
        help="ask Signify's online service for the bridge IP (shares your public IP)",
    )

    for name, help_text in (
        ("install-hooks", "install Claude Code / Codex hook configuration"),
        ("uninstall-hooks", "remove our hooks from Claude Code / Codex configuration"),
        ("install-commands", "install the /glow slash command for Claude Code / Codex"),
        ("uninstall-commands", "remove our /glow slash command"),
    ):
        p = sub.add_parser(name, help=help_text)
        p.add_argument("--claude", action="store_true", help="Claude Code")
        p.add_argument("--codex", action="store_true", help="Codex")
        p.add_argument("--all", action="store_true", help="both Claude Code and Codex")

    p = sub.add_parser("daemon", help="run the light-control daemon")
    p.add_argument(
        "--detach", action="store_true", help="start in the background and return"
    )
    p.add_argument("--debug", action="store_true", help="verbose logging")

    p = sub.add_parser("hook", help="read a hook payload from stdin (used by agents)")
    p.add_argument("--source", choices=("claude", "codex"), required=True)
    p.add_argument("--debug", action="store_true")

    p = sub.add_parser("codex-notify", help="Codex notify program (JSON in argv)")
    p.add_argument("payload", nargs="?", default="")
    p.add_argument("--debug", action="store_true")

    sub.add_parser("status", help="show daemon and session status")

    p = sub.add_parser("lights", help="list all known lights with roles")
    output = p.add_mutually_exclusive_group()
    output.add_argument(
        "--json", action="store_true", help="full machine-readable output"
    )
    output.add_argument(
        "--agent",
        action="store_true",
        help="privacy-minimized JSON for AI-agent commands",
    )

    p = sub.add_parser("role", help="show or change which lights play each role")
    role_sub = p.add_subparsers(dest="role_command")
    role_sub.add_parser("show", help="print current role assignments")
    for verb, help_text in (
        ("set", "replace a role's light list"),
        ("add", "add lights to a role"),
        ("remove", "remove lights from a role"),
    ):
        rp = role_sub.add_parser(verb, help=help_text)
        rp.add_argument("role", choices=("thinking", "waiting"))
        rp.add_argument("lights", nargs="+", metavar="light", help="light name or ref")
    rp = role_sub.add_parser("clear", help="reset a role to the default (all lights)")
    rp.add_argument("role", choices=("thinking", "waiting"))

    p = sub.add_parser("preview", help="preview breathing + waiting-red, then restore")
    p.add_argument(
        "--force", action="store_true", help="run even if the daemon is busy"
    )

    sub.add_parser("restore", help="restore lights to their snapshot state")

    sub.add_parser("reload", help="make a running daemon re-read the config file")

    sub.add_parser("doctor", help="verify configuration, bridge, daemon, and hooks")

    p = sub.add_parser("config", help="show or change configuration")
    config_sub = p.add_subparsers(dest="config_command")
    config_sub.add_parser("show", help="print the effective configuration")
    setp = config_sub.add_parser(
        "set", help="set a value, e.g. animation.breath_period_seconds 7"
    )
    setp.add_argument("key")
    setp.add_argument("value")

    p = sub.add_parser("wiz", help="manage WiZ bulbs (discover, add, list, remove)")
    wiz_sub = p.add_subparsers(dest="wiz_command")
    wiz_sub.add_parser("discover", help="find WiZ bulbs on the local network")
    addp = wiz_sub.add_parser("add", help="add a discovered bulb by MAC")
    addp.add_argument("mac", help="bulb MAC address (12 hex digits, separators ok)")
    addp.add_argument("--name", default="", help="friendly name, e.g. 'Desk strip'")
    addp.add_argument("--ip", default="", help="skip discovery and use this IP")
    wiz_sub.add_parser("list", help="show configured WiZ bulbs")
    rmp = wiz_sub.add_parser("remove", help="remove a configured bulb")
    rmp.add_argument("bulb", help="bulb MAC or name")

    p = sub.add_parser("autostart", help="manage daemon autostart at login")
    p.add_argument("action", choices=("install", "uninstall", "status"))
    return parser


# -- latency-critical, never-fail subcommands ---------------------------------


def _cmd_hook(args) -> int:
    try:
        raw = sys.stdin.buffer.read(MAX_HOOK_PAYLOAD_BYTES + 1)
        if not raw or len(raw) > MAX_HOOK_PAYLOAD_BYTES:
            return 0
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            return 0

        from .events import normalize_hook_event

        event = normalize_hook_event(args.source, payload)
        if args.debug:
            safe = {k: payload.get(k) for k in _DEBUG_SAFE_KEYS if k in payload}
            print(
                f"hue-agent hook: fields={safe} -> {_debug_event(event)}",
                file=sys.stderr,
            )
        if event is None:
            return 0

        from . import secret_store
        from .client import post_event
        from .config import load_config

        secret_store.SILENT = True  # hooks must not print anything
        config = load_config()
        token = secret_store.ensure_daemon_token()
        sent = post_event(config, token, event)
        if args.debug:
            print(f"hue-agent hook: delivered={sent}", file=sys.stderr)
    except Exception as err:
        if getattr(args, "debug", False):
            print(f"hue-agent hook: error={type(err).__name__}", file=sys.stderr)
    return 0


def _cmd_codex_notify(args) -> int:
    try:
        if not args.payload:
            return 0
        payload = json.loads(args.payload[:MAX_HOOK_PAYLOAD_BYTES])
        if not isinstance(payload, dict):
            return 0

        from .events import normalize_codex_notification

        event = normalize_codex_notification(payload)
        if args.debug:
            print(
                "hue-agent codex-notify: "
                f"type={payload.get('type')!r} -> {_debug_event(event)}",
                file=sys.stderr,
            )
        if event is None:
            return 0

        from . import secret_store
        from .client import post_event
        from .config import load_config

        secret_store.SILENT = True  # notify programs must not print anything
        config = load_config()
        token = secret_store.ensure_daemon_token()
        post_event(config, token, event)
    except Exception as err:
        if getattr(args, "debug", False):
            print(
                f"hue-agent codex-notify: error={type(err).__name__}", file=sys.stderr
            )
    return 0


# -- normal subcommands ---------------------------------------------------------


def _cmd_daemon(args) -> int:
    from .config import load_config

    config = load_config()
    if args.detach:
        from . import secret_store
        from .client import get_health, spawn_daemon_detached

        token = secret_store.ensure_daemon_token()
        if get_health(config, token, timeout=1.0):
            print("daemon already running")
            return 0
        if not spawn_daemon_detached(config):
            print("failed to start daemon", file=sys.stderr)
            return 1
        import time

        for _ in range(20):
            time.sleep(0.15)
            health = get_health(config, token, timeout=0.5)
            if health:
                print(f"daemon started (pid {health.get('pid')})")
                return 0
        print("daemon start not confirmed; check `hue-agent status`", file=sys.stderr)
        return 1

    from .daemon import run_daemon

    return run_daemon(config, debug=args.debug)


def _cmd_install_hooks(args, install: bool) -> int:
    if not (args.claude or args.codex or getattr(args, "all", False)):
        print("choose --claude, --codex, or --all", file=sys.stderr)
        return 2
    do_claude = args.claude or args.all
    do_codex = args.codex or args.all
    verb = "installed" if install else "removed"
    status = 0
    if do_claude:
        from . import hooks_claude

        try:
            changed, backup = (
                hooks_claude.install() if install else hooks_claude.uninstall()
            )
            where = hooks_claude.claude_settings_path()
            if changed:
                print(f"claude: hooks {verb} in {where}")
                if backup:
                    print(f"claude: previous settings backed up to {backup}")
            else:
                print(f"claude: nothing to do ({where})")
        except (OSError, ValueError) as err:
            print(f"claude: failed: {err}", file=sys.stderr)
            status = 1
    if do_codex:
        from . import hooks_codex

        try:
            changed, backup = (
                hooks_codex.install_hooks()
                if install
                else hooks_codex.uninstall_hooks()
            )
            where = hooks_codex.codex_hooks_path()
            if changed:
                print(f"codex: hooks {verb} in {where}")
                if backup:
                    print(f"codex: previous hooks backed up to {backup}")
            else:
                print(f"codex: hooks — nothing to do ({where})")
            notify_changed, notify_backup = (
                hooks_codex.install_notify()
                if install
                else hooks_codex.uninstall_notify()
            )
            if notify_changed:
                print(f"codex: notify {verb} in {hooks_codex.codex_config_path()}")
                if notify_backup:
                    print(f"codex: previous config backed up to {notify_backup}")
            else:
                print("codex: notify — nothing to do")
            if install:
                print(
                    "codex: open /hooks in a new Codex session and trust the "
                    "current hook definitions"
                )
        except (OSError, ValueError) as err:
            print(f"codex: failed: {err}", file=sys.stderr)
            status = 1
    return status


def _cmd_install_commands(args, install: bool) -> int:
    if not (args.claude or args.codex or getattr(args, "all", False)):
        print("choose --claude, --codex, or --all", file=sys.stderr)
        return 2
    from . import commands_install

    verb = "installed" if install else "removed"
    status = 0
    for kind, wanted in (
        ("claude", args.claude or args.all),
        ("codex", args.codex or args.all),
    ):
        if not wanted:
            continue
        pieces = [
            (
                "/glow command",
                lambda k=kind: (
                    commands_install.install(k)
                    if install
                    else commands_install.uninstall(k)
                ),
                commands_install.command_path(kind),
            ),
        ]
        if kind == "codex":
            # Codex also gets a skill ($glow; custom prompts are deprecated)
            # and narrowly scoped execpolicy rules for trusted installs.
            pieces += [
                (
                    "$glow skill",
                    (
                        commands_install.install_codex_skill
                        if install
                        else commands_install.uninstall_codex_skill
                    ),
                    commands_install.codex_skill_path(),
                ),
                (
                    "approval rule",
                    (
                        commands_install.install_codex_rules
                        if install
                        else commands_install.uninstall_codex_rules
                    ),
                    commands_install.codex_rules_path(),
                ),
            ]
        for label, action, where in pieces:
            try:
                changed, backup = action()
                if changed:
                    print(f"{kind}: {label} {verb} at {where}")
                    if backup:
                        print(f"{kind}: previous file backed up to {backup}")
                else:
                    print(f"{kind}: {label} — nothing to do ({where})")
            except (OSError, ValueError) as err:
                print(f"{kind}: {label} failed: {err}", file=sys.stderr)
                status = 1
    return status


def _cmd_status(args) -> int:
    from . import secret_store
    from .client import get_health
    from .config import config_path, load_config

    config = load_config()
    print(f"config:  {config_path()}")
    print(f"bridge:  {config.bridge.host or '(not configured)'}")
    print(f"target:  {config.target.mode} {config.target.ids}")
    token = secret_store.get_daemon_token()
    health = get_health(config, token, timeout=1.5) if token else None
    if not health:
        print("daemon:  not running")
        return 0
    print(f"daemon:  running (pid {health.get('pid')}, v{health.get('version')})")
    print(f"state:   {health.get('aggregate')} (lights: {health.get('applied')})")
    print(
        f"green:   hold for {config.daemon.completion_hold_seconds}s after completion"
    )
    sessions = health.get("sessions") or []
    if sessions:
        print("sessions:")
        for s in sessions:
            print(
                f"  - {s.get('source')}:{s.get('session_id')}  "
                f"{s.get('state')}  (seen {s.get('age_seconds')}s ago)"
            )
    else:
        print("sessions: none")
    return 0


def _cmd_preview(args) -> int:
    import asyncio

    from . import secret_store
    from .backends import build_controller
    from .client import get_health
    from .config import load_config

    config = load_config()
    token = secret_store.get_daemon_token()
    if token and not args.force:
        health = get_health(config, token, timeout=1.0)
        if health and health.get("aggregate") != "idle":
            print(
                "daemon is currently animating lights; try later or use --force",
                file=sys.stderr,
            )
            return 1
    controller = build_controller(config)

    async def _run() -> None:
        try:
            print("previewing: breathing 10s, red 3s, then restore...")
            await controller.preview(breathe_seconds=10.0, red_seconds=3.0)
        finally:
            await controller.close()

    try:
        asyncio.run(_run())
    except Exception as err:
        print(f"preview failed: {err}", file=sys.stderr)
        return 1
    print("done — lights restored")
    return 0


def _cmd_restore(args) -> int:
    import asyncio

    from . import secret_store
    from .backends import build_controller
    from .client import post_restore
    from .config import load_config

    config = load_config()
    token = secret_store.get_daemon_token()
    if token:
        result = post_restore(config, token)
        if result and result.get("ok"):
            print(f"restored {result.get('restored', 0)} light(s) via daemon")
            return 0
    controller = build_controller(config)
    if not controller.has_snapshot_file():
        print("nothing to restore (no snapshot)")
        return 0

    async def _run() -> int:
        try:
            return await controller.restore_from_file()
        finally:
            await controller.close()

    try:
        restored = asyncio.run(_run())
    except Exception as err:
        print(f"restore failed: {err}", file=sys.stderr)
        return 1
    print(f"restored {restored} light(s) from snapshot file")
    return 0


def _reload_daemon_if_running(config=None) -> None:
    """Best-effort: tell a running daemon to pick up a config change."""
    try:
        from . import secret_store
        from .client import get_health, post_reload
        from .config import load_config

        config = config or load_config()
        token = secret_store.get_daemon_token()
        if not token or not get_health(config, token, timeout=1.0):
            return
        result = post_reload(config, token)
        if result and result.get("ok"):
            print("daemon reloaded")
            if result.get("note"):
                print(f"note: {result['note']}")
        else:
            print(
                "daemon running but reload failed; restart it to apply changes",
                file=sys.stderr,
            )
    except Exception:
        pass


def _cmd_reload(args) -> int:
    from . import secret_store
    from .client import get_health, post_reload
    from .config import load_config

    config = load_config()
    token = secret_store.get_daemon_token()
    if not token or not get_health(config, token, timeout=1.0):
        print("daemon not running — config applies on its next start")
        return 0
    result = post_reload(config, token)
    if result and result.get("ok"):
        print("daemon reloaded")
        if result.get("note"):
            print(f"note: {result['note']}")
        return 0
    error = (result or {}).get("error", "no response")
    print(f"reload failed: {error}", file=sys.stderr)
    return 1


def _cmd_config(args) -> int:
    import tomli_w

    from .config import (
        ConfigError,
        config_path,
        config_to_dict,
        load_config,
        save_config,
        set_config_value,
    )

    if args.config_command == "set":
        try:
            config = load_config()
            set_config_value(config, args.key, args.value)
            save_config(config)
        except (ConfigError, ValueError) as err:
            print(f"error: {err}", file=sys.stderr)
            return 2
        print(f"{args.key} = {args.value}")
        _reload_daemon_if_running(config)
        return 0
    # default: show
    try:
        config = load_config()
    except Exception as err:
        print(f"error: {err}", file=sys.stderr)
        return 2
    print(f"# {config_path()}")
    print(tomli_w.dumps(config_to_dict(config)), end="")
    return 0


def _cmd_doctor(args) -> int:
    from .config import Config, ConfigError, load_config
    from .doctor import run_doctor

    try:
        return run_doctor(load_config())
    except ConfigError as err:
        # A corrupt config file must not hide the rest of the checklist.
        return run_doctor(Config(), config_error=str(err))


def _cmd_autostart(args) -> int:
    from . import autostart

    if args.action == "install":
        print(autostart.install())
    elif args.action == "uninstall":
        print(autostart.uninstall())
    else:
        print(autostart.status())
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    # `hook` and `codex-notify` must never fail, not even on argparse errors.
    failsafe = bool(argv) and argv[0] in ("hook", "codex-notify")
    try:
        parser = _build_parser()
        args = parser.parse_args(argv)
        if args.command == "hook":
            return _cmd_hook(args)
        if args.command == "codex-notify":
            return _cmd_codex_notify(args)
        if args.command == "setup":
            from .setup_cmd import run_setup

            return run_setup(cloud_discovery=args.cloud_discovery)
        if args.command == "daemon":
            return _cmd_daemon(args)
        if args.command == "install-hooks":
            return _cmd_install_hooks(args, install=True)
        if args.command == "uninstall-hooks":
            return _cmd_install_hooks(args, install=False)
        if args.command == "install-commands":
            return _cmd_install_commands(args, install=True)
        if args.command == "uninstall-commands":
            return _cmd_install_commands(args, install=False)
        if args.command == "status":
            return _cmd_status(args)
        if args.command == "lights":
            from .lights_cmd import cmd_lights

            return cmd_lights(args)
        if args.command == "role":
            from .lights_cmd import cmd_role

            return cmd_role(args)
        if args.command == "preview":
            return _cmd_preview(args)
        if args.command == "restore":
            return _cmd_restore(args)
        if args.command == "reload":
            return _cmd_reload(args)
        if args.command == "doctor":
            return _cmd_doctor(args)
        if args.command == "config":
            return _cmd_config(args)
        if args.command == "wiz":
            from .wiz_cmd import run_wiz

            return run_wiz(args)
        if args.command == "autostart":
            return _cmd_autostart(args)
        parser.print_help()
        return 0
    except SystemExit as err:
        if failsafe:
            return 0
        code = err.code
        return code if isinstance(code, int) else 0 if code is None else 1
    except KeyboardInterrupt:
        return 130
    except Exception as err:
        if failsafe:
            return 0
        print(f"error: {err}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
