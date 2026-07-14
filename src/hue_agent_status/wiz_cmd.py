"""`hue-agent wiz`: discover, add, list, and remove WiZ bulbs.

WiZ bulbs are paired to your network with the WiZ phone app; from there this
tool talks to them directly over the LAN (UDP port 38899) — no cloud.
Identity is the bulb's MAC; the IP is only a cached hint refreshed by
discovery, so DHCP changes don't break anything (a DHCP reservation still
makes startup snappier).
"""

from __future__ import annotations

import asyncio
import sys

from .config import WizBulbConfig, load_config, save_config
from .backends.wiz_protocol import (
    WizTransport,
    build_get_system_config,
    normalize_mac,
    parse_capabilities,
)
from .roles import parse_light_ref


def _mac_identity(value: str) -> tuple[str, str]:
    """Comparable WiZ identity, including malformed values being repaired."""
    try:
        return "mac", normalize_mac(value)
    except ValueError:
        return "raw", value.strip().casefold()


def _describe_caps(module_name: str) -> str:
    caps = parse_capabilities(module_name)
    if caps.supports_color:
        return "color"
    if caps.supports_ct:
        return "tunable white"
    return "dimmable"


async def _discover_with_details(broadcast: str, wait: float = 2.0) -> list[dict]:
    transport = WizTransport()
    try:
        found = await transport.discover(broadcast, wait=wait)
        bulbs = []
        for mac, ip in found:
            module = ""
            try:
                result = await transport.send_command(ip, build_get_system_config())
                module = str(result.get("moduleName", ""))
            except Exception:
                pass
            bulbs.append({"mac": mac, "ip": ip, "module": module})
        return bulbs
    finally:
        transport.close()


def cmd_discover(args) -> int:
    config = load_config()
    print("Searching for WiZ bulbs on your network (2s)...")
    bulbs = asyncio.run(_discover_with_details(config.wiz.broadcast))
    if not bulbs:
        print(
            "no WiZ bulbs answered — check they are powered on, set up in the "
            "WiZ app, and on this network"
        )
        return 1
    configured = {normalize_mac(b.mac) for b in config.wiz.bulbs if b.mac}
    for bulb in bulbs:
        tag = "configured" if bulb["mac"] in configured else "new"
        kind = _describe_caps(bulb["module"]) if bulb["module"] else "unknown"
        print(f"  {bulb['mac']}  {bulb['ip']:<15}  {kind:<13}  [{tag}]")
    new = [b for b in bulbs if b["mac"] not in configured]
    if new:
        print(f'\nadd one with: hue-agent wiz add {new[0]["mac"]} --name "My lamp"')
    return 0


def cmd_add(args) -> int:
    config = load_config()
    try:
        mac = normalize_mac(args.mac)
    except ValueError as err:
        print(f"error: {err}", file=sys.stderr)
        return 2
    if any(b.mac and normalize_mac(b.mac) == mac for b in config.wiz.bulbs):
        print(f"bulb {mac} is already configured")
        return 0

    ip = args.ip or ""
    module = ""
    if not ip:
        print("Locating the bulb on your network...")
        bulbs = asyncio.run(_discover_with_details(config.wiz.broadcast))
        match = next((b for b in bulbs if b["mac"] == mac), None)
        if match is None:
            print(
                f"error: bulb {mac} did not answer discovery; check power/network "
                "or pass --ip",
                file=sys.stderr,
            )
            return 1
        ip = match["ip"]
        module = match["module"]

    name = args.name or "WiZ bulb"
    config.wiz.bulbs.append(WizBulbConfig(mac=mac, ip=ip, name=name))
    save_config(config)
    kind = _describe_caps(module) if module else "capabilities unknown"
    print(f"added {name} ({mac} at {ip}, {kind})")

    from .cli import _reload_daemon_if_running

    _reload_daemon_if_running(config)
    return 0


def cmd_list(args) -> int:
    config = load_config()
    if not config.wiz.bulbs:
        print("no WiZ bulbs configured — run `hue-agent wiz discover`")
        return 0
    for bulb in config.wiz.bulbs:
        ip = bulb.ip or "(ip via discovery)"
        print(f"  {bulb.name or '(unnamed)'}  wiz:{bulb.mac}  {ip}")
    return 0


def cmd_remove(args) -> int:
    config = load_config()
    query = args.bulb.strip()
    query_name = query.casefold()
    query_mac = _mac_identity(query)
    kept, removed = [], []
    for bulb in config.wiz.bulbs:
        if query_mac == _mac_identity(bulb.mac) or query_name == bulb.name.casefold():
            removed.append(bulb)
        else:
            kept.append(bulb)
    if not removed:
        known = ", ".join(b.name or b.mac for b in config.wiz.bulbs) or "(none)"
        print(
            f"no configured bulb matches {args.bulb!r}; known: {known}", file=sys.stderr
        )
        return 2
    # Drop dangling role references so validation stays clean.
    gone = {_mac_identity(b.mac) for b in removed}
    role_updates = {}
    for role in ("thinking", "waiting"):
        refs = getattr(config.roles, role)
        kept_refs = []
        for ref in refs:
            backend, light_id = parse_light_ref(ref)
            if backend != "wiz" or _mac_identity(light_id) not in gone:
                kept_refs.append(ref)
        role_updates[role] = kept_refs
    remaining_lights = bool(kept or config.bridge.host or config.target.ids)
    emptied_roles = [
        role
        for role, kept_refs in role_updates.items()
        if getattr(config.roles, role) and not kept_refs
    ]
    if remaining_lights and emptied_roles:
        roles = ", ".join(emptied_roles)
        print(
            f"error: removal would empty the {roles} role, which means all "
            "configured lights; reassign or clear that role first",
            file=sys.stderr,
        )
        return 2
    config.wiz.bulbs = kept
    for role, kept_refs in role_updates.items():
        setattr(config.roles, role, kept_refs)
    save_config(config)
    for bulb in removed:
        print(f"removed {bulb.name or bulb.mac}")

    from .cli import _reload_daemon_if_running

    _reload_daemon_if_running(config)
    return 0


def run_wiz(args) -> int:
    handlers = {
        "discover": cmd_discover,
        "add": cmd_add,
        "list": cmd_list,
        "remove": cmd_remove,
    }
    handler = handlers.get(getattr(args, "wiz_command", None) or "list")
    return handler(args)
