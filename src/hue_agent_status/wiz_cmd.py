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
    query = args.bulb.strip().lower()
    kept, removed = [], []
    for bulb in config.wiz.bulbs:
        normalized = ""
        try:
            normalized = normalize_mac(bulb.mac)
        except ValueError:
            pass
        if query in (normalized, bulb.name.lower()):
            removed.append(bulb)
        else:
            kept.append(bulb)
    if not removed:
        known = ", ".join(b.name or b.mac for b in config.wiz.bulbs) or "(none)"
        print(
            f"no configured bulb matches {args.bulb!r}; known: {known}", file=sys.stderr
        )
        return 2
    config.wiz.bulbs = kept
    # Drop dangling role references so validation stays clean.
    for role in ("thinking", "waiting"):
        refs = getattr(config.roles, role)
        gone = {f"wiz:{normalize_mac(b.mac)}" for b in removed}
        setattr(config.roles, role, [r for r in refs if r not in gone])
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
