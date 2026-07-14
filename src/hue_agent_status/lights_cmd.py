"""`hue-agent lights` and `hue-agent role`: the /glow command's CLI surface.

``lights`` prints every known light across backends with friendly names,
capabilities, and role membership (``--json`` for machine consumption).
``role`` edits the per-state light lists by NAME — agents and humans both say
"Desk lamp", not a UUID.
"""

from __future__ import annotations

import asyncio
import json
import sys
from dataclasses import asdict

from .config import Config, load_config, save_config
from .roles import (
    LightInfo,
    MatchError,
    effective_role_ids,
    format_light_ref,
    match_light,
    parse_light_ref,
)

ROLE_HELP = {
    "thinking": "breathes while an agent is working",
    "waiting": "shows the wait color when an agent needs you",
}


class InventoryIncompleteError(RuntimeError):
    """At least one configured light backend could not be inventoried."""


def _canonical_light_ref(ref: str) -> str:
    """Use the same fully qualified references emitted by the inventory."""
    backend, light_id = parse_light_ref(ref)
    if backend == "wiz":
        from .backends.wiz_protocol import normalize_mac

        try:
            light_id = normalize_mac(light_id)
        except ValueError:
            pass
    return format_light_ref(backend, light_id)


def hue_light_name(bridge, light_id: str) -> str:
    """Friendly device name without falling back to a stable device id."""
    try:
        device = bridge.lights.get_device(light_id)
        if device is not None and device.metadata is not None:
            return device.metadata.name
    except Exception:
        pass
    return "Unnamed Hue light"


async def _hue_lights(config: Config) -> list[LightInfo]:
    if not (config.bridge.host or config.target.ids):
        return []
    from .backends.hue import HueController

    controller = HueController(config)
    try:
        await controller.connect()
        summary = controller.target_summary()
        thinking, waiting = set(summary["thinking"]), set(summary["waiting"])
        infos = []
        for light in controller.bridge.lights:
            roles = [
                role
                for role, members in (("thinking", thinking), ("waiting", waiting))
                if light.id in members
            ]
            infos.append(
                LightInfo(
                    ref=f"hue:{light.id}",
                    backend="hue",
                    id=light.id,
                    name=hue_light_name(controller.bridge, light.id),
                    supports_color=light.color is not None,
                    reachable=None,
                    roles=roles,
                )
            )
        return infos
    finally:
        await controller.close()


async def _wiz_lights(config: Config) -> list[LightInfo]:
    if not config.wiz.bulbs:
        return []
    from .backends.wiz import _load_ip_cache
    from .backends.wiz_protocol import (
        WizTransport,
        build_get_system_config,
        normalize_mac,
        parse_capabilities,
    )

    bulbs = []
    for bulb in config.wiz.bulbs:
        try:
            mac = normalize_mac(bulb.mac)
        except ValueError:
            continue
        bulbs.append((mac, bulb))
    pool = [mac for mac, _ in bulbs]

    def role_ids(role: str) -> set[str]:
        normalized = set()
        for raw in effective_role_ids(config, role, pool, backend="wiz"):
            try:
                normalized.add(normalize_mac(raw))
            except ValueError:
                continue
        return normalized

    thinking = role_ids("thinking")
    waiting = role_ids("waiting")

    cache = _load_ip_cache()
    transport = WizTransport()

    async def probe(mac: str, ip: str) -> tuple[bool | None, bool]:
        """(reachable, supports_color) via one getSystemConfig."""
        if not ip:
            return None, False
        try:
            result = await transport.send_command(
                ip, build_get_system_config(), retries=2
            )
            caps = parse_capabilities(str(result.get("moduleName", "")))
            return True, caps.supports_color
        except Exception:
            return False, False

    try:
        probes = await asyncio.gather(
            *(probe(mac, cache.get(mac) or bulb.ip) for mac, bulb in bulbs)
        )
    finally:
        transport.close()

    infos = []
    for (mac, bulb), (reachable, has_color) in zip(bulbs, probes):
        roles = [
            role
            for role, members in (("thinking", thinking), ("waiting", waiting))
            if mac in members
        ]
        infos.append(
            LightInfo(
                ref=f"wiz:{mac}",
                backend="wiz",
                id=mac,
                name=bulb.name or "Unnamed WiZ light",
                supports_color=has_color,
                reachable=reachable,
                roles=roles,
            )
        )
    return infos


async def list_lights(
    config: Config, *, redact_errors: bool = False, require_complete: bool = False
) -> list[LightInfo]:
    """Inventory across all configured backends; a dead backend just warns."""
    infos: list[LightInfo] = []
    incomplete = False
    for label, fetch in (("hue", _hue_lights), ("wiz", _wiz_lights)):
        try:
            infos.extend(await fetch(config))
        except Exception as err:
            incomplete = True
            if require_complete:
                continue
            if redact_errors:
                print(
                    "warning: some configured lights could not be listed",
                    file=sys.stderr,
                )
            else:
                print(f"warning: cannot list {label} lights: {err}", file=sys.stderr)
    if incomplete and require_complete:
        raise InventoryIncompleteError
    return infos


def _roles_payload(config: Config, infos: list[LightInfo]) -> dict:
    payload = {}
    for role in ("thinking", "waiting"):
        payload[role] = {
            "configured": list(getattr(config.roles, role)),
            "effective": [i.ref for i in infos if role in i.roles],
        }
    return payload


def _agent_payload(config: Config, infos: list[LightInfo]) -> dict:
    """Inventory for an AI agent, without stable device identifiers or IPs."""
    return {
        "lights": [
            {
                "name": info.name,
                "supports_color": info.supports_color,
                "roles": list(info.roles),
            }
            for info in infos
        ],
        "roles": {
            role: {
                "default_all": not bool(getattr(config.roles, role)),
                "effective_names": [info.name for info in infos if role in info.roles],
            }
            for role in ("thinking", "waiting")
        },
        "wait_color": config.animation.wait_color,
    }


def cmd_lights(args) -> int:
    config = load_config()
    agent_output = getattr(args, "agent", False)
    infos = asyncio.run(list_lights(config, redact_errors=agent_output))
    if agent_output:
        print(json.dumps(_agent_payload(config, infos), indent=2))
        return 0
    if getattr(args, "json", False):
        payload = {
            "lights": [asdict(info) for info in infos],
            "roles": _roles_payload(config, infos),
            "wait_color": config.animation.wait_color,
        }
        print(json.dumps(payload, indent=2))
        return 0
    if not infos:
        print("no lights known — run `hue-agent setup` and/or `hue-agent wiz discover`")
        return 1
    width = max(len(i.name) for i in infos)
    for info in infos:
        color = "color" if info.supports_color else "no color"
        roles = ",".join(info.roles) or "-"
        reach = {True: "", False: "  [unreachable]", None: ""}[info.reachable]
        print(f"  {info.name.ljust(width)}  {info.ref}  {color:<8}  {roles}{reach}")
    print(f"\nwait color: {config.animation.wait_color}")
    return 0


def _print_role_assignments(config: Config, infos: list[LightInfo]) -> None:
    by_ref = {i.ref: i for i in infos}
    for role in ("thinking", "waiting"):
        configured = getattr(config.roles, role)
        effective = [i for i in infos if role in i.roles]
        names = ", ".join(i.name for i in effective) or "(none)"
        default_note = "" if configured else "  (default: all configured lights)"
        print(f"  {role:<9} {names}{default_note}")
        unknown = sum(_canonical_light_ref(ref) not in by_ref for ref in configured)
        if unknown:
            noun = "light is" if unknown == 1 else "lights are"
            print(f"            ! {unknown} configured {noun} unavailable right now")
    print(f"  wait color: {config.animation.wait_color}")


def cmd_role(args) -> int:
    config = load_config()
    command = getattr(args, "role_command", None) or "show"
    if command == "show":
        infos = asyncio.run(list_lights(config, redact_errors=True))
        _print_role_assignments(config, infos)
        return 0

    role = args.role
    if command == "clear":
        setattr(config.roles, role, [])
        save_config(config)
        print(f"{role}: reset to default (all configured lights)")
    else:
        current = list(getattr(config.roles, role))
        require_complete = not current and command in ("add", "remove")
        try:
            inventory = asyncio.run(
                list_lights(
                    config,
                    redact_errors=True,
                    require_complete=require_complete,
                )
            )
        except InventoryIncompleteError:
            print(
                "error: cannot edit a default role while some configured lights "
                "could not be listed; retry when all light backends are available",
                file=sys.stderr,
            )
            return 1
        refs = []
        for query in args.lights:
            try:
                refs.append(match_light(query, inventory).ref)
            except MatchError as err:
                print(f"error: {err}", file=sys.stderr)
                return 2
        if command in ("add", "remove"):
            current = list(dict.fromkeys(_canonical_light_ref(ref) for ref in current))
            if not current:
                # The implicit "all lights" default must become explicit before
                # incremental edits mean anything.
                current = [i.ref for i in inventory if role in i.roles]
        if command == "set":
            current = list(dict.fromkeys(refs))
        elif command == "add":
            current.extend(ref for ref in refs if ref not in current)
        elif command == "remove":
            current = [ref for ref in current if ref not in refs]
            if not current:
                print(
                    "error: cannot remove every light from a role; an empty role "
                    "means all configured lights",
                    file=sys.stderr,
                )
                return 2
        setattr(config.roles, role, current)
        save_config(config)

    from .cli import _reload_daemon_if_running

    _reload_daemon_if_running(config)
    infos = asyncio.run(list_lights(config, redact_errors=True))
    _print_role_assignments(config, infos)
    return 0
