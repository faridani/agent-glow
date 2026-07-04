"""`hue-agent setup`: interactive bridge pairing and target selection.

Pairing requires physically pressing the Hue Bridge link button; nothing is
stored until the bridge has issued an app key. No Hue cloud account is used —
discovery talks to the bridge's LAN API only (plus Signify's unauthenticated
discovery endpoint for the initial IP lookup).
"""

from __future__ import annotations

import asyncio
import socket
import sys

from . import secret_store
from .config import Config, load_config, save_config
from .hue import HueController

MAX_PAIR_ATTEMPTS = 10


def _input(prompt: str) -> str:
    try:
        return input(prompt)
    except EOFError:
        raise SystemExit("\nsetup aborted") from None


def _choose(prompt: str, count: int, allow_quit: bool = True) -> int | None:
    while True:
        raw = _input(prompt).strip().lower()
        if allow_quit and raw in ("q", "quit", ""):
            return None
        try:
            index = int(raw)
        except ValueError:
            print("  please enter a number")
            continue
        if 1 <= index <= count:
            return index - 1
        print(f"  please enter a number between 1 and {count}")


async def _discover():
    from aiohue.discovery import discover_nupnp

    try:
        return await discover_nupnp()
    except Exception as err:
        print(f"  automatic discovery failed ({err})")
        return []


def _pick_bridge() -> tuple[str, str]:
    """Returns (host, bridge_id)."""
    print("Searching for Hue Bridges on your network...")
    bridges = asyncio.run(_discover())
    if bridges:
        print("\nDiscovered bridges:")
        for i, bridge in enumerate(bridges, start=1):
            print(f"  {i}. {bridge.host}  (id: {bridge.id})")
        print(f"  {len(bridges) + 1}. Enter an IP address manually")
        choice = _choose(f"Choose a bridge [1-{len(bridges) + 1}]: ", len(bridges) + 1)
        if choice is None:
            raise SystemExit("setup aborted")
        if choice < len(bridges):
            return bridges[choice].host, bridges[choice].id or ""
    else:
        print("No bridges discovered automatically.")
    host = _input("Enter your Hue Bridge IP address: ").strip()
    if not host:
        raise SystemExit("no bridge address given; setup aborted")
    return host, ""


def _pair(host: str) -> str:
    """Press-button pairing loop. Nothing is stored until a key is issued."""
    from aiohue.errors import LinkButtonNotPressed
    from aiohue.util import create_app_key

    device_type = f"hue-agent-status#{socket.gethostname()[:19]}"
    for attempt in range(1, MAX_PAIR_ATTEMPTS + 1):
        _input(
            "\nPress the round link button on your Hue Bridge, "
            "then press Enter here... "
        )
        try:
            return asyncio.run(create_app_key(host, device_type))
        except LinkButtonNotPressed:
            remaining = MAX_PAIR_ATTEMPTS - attempt
            if remaining:
                print(
                    "  The bridge says the link button was not pressed. "
                    "It stays armed for ~30 seconds after pressing — try again."
                )
            else:
                raise SystemExit(
                    "link button was never pressed; nothing was stored"
                ) from None
        except Exception as err:
            raise SystemExit(f"pairing failed: {err}; nothing was stored") from None
    raise SystemExit("pairing failed; nothing was stored")


async def _fetch_resources(config: Config, app_key: str):
    """Connect once and list rooms, zones, and lights with human names."""
    controller = HueController(config, app_key=app_key)
    await controller.connect()
    bridge = controller.bridge
    lights = []
    for light in bridge.lights:
        name = light.id
        try:
            device = bridge.lights.get_device(light.id)
            if device is not None and device.metadata is not None:
                name = device.metadata.name
        except Exception:
            pass
        lights.append((light.id, name, light.color is not None))
    rooms = [(room.id, room.metadata.name) for room in bridge.groups.room]
    zones = [(zone.id, zone.metadata.name) for zone in bridge.groups.zone]
    bridge_id = bridge.bridge_id or ""
    await controller.close()
    return lights, rooms, zones, bridge_id


def _pick_targets(lights, rooms, zones) -> tuple[str, list[str]]:
    print("\nWhat should indicate agent status?")
    options = [("lights", "Individual lights")]
    if rooms:
        options.append(("room", "A whole room"))
    if zones:
        options.append(("zone", "A zone"))
    for i, (_, label) in enumerate(options, start=1):
        print(f"  {i}. {label}")
    choice = _choose(f"Choose [1-{len(options)}]: ", len(options), allow_quit=False)
    mode = options[choice][0]

    if mode == "room" or mode == "zone":
        groups = rooms if mode == "room" else zones
        for i, (_, name) in enumerate(groups, start=1):
            print(f"  {i}. {name}")
        picked = _choose(f"Choose a {mode} [1-{len(groups)}]: ", len(groups), allow_quit=False)
        return mode, [groups[picked][0]]

    print("\nAvailable lights:")
    for i, (_, name, has_color) in enumerate(lights, start=1):
        tag = "color" if has_color else "no color"
        print(f"  {i}. {name}  ({tag})")
    while True:
        raw = _input("Choose lights (comma-separated numbers, e.g. 1,3): ").strip()
        try:
            indexes = [int(part) - 1 for part in raw.split(",") if part.strip()]
        except ValueError:
            print("  please enter numbers separated by commas")
            continue
        if indexes and all(0 <= i < len(lights) for i in indexes):
            return "lights", [lights[i][0] for i in sorted(set(indexes))]
        print("  invalid selection")


def _prompt_float(label: str, current: float, low: float, high: float) -> float:
    while True:
        raw = _input(f"  {label} [{current:g}]: ").strip()
        if not raw:
            return current
        try:
            value = float(raw)
        except ValueError:
            print("  please enter a number")
            continue
        if low <= value <= high:
            return value
        print(f"  must be between {low:g} and {high:g}")


def _tune_animation(config: Config) -> None:
    anim = config.animation
    print("\nAnimation preferences (press Enter to accept defaults):")
    anim.breath_period_seconds = _prompt_float(
        "Breathing period in seconds", anim.breath_period_seconds, 1.0, 60.0
    )
    anim.breath_min_brightness = _prompt_float(
        "Breathing min brightness %", anim.breath_min_brightness, 0.0, 100.0
    )
    anim.breath_max_brightness = _prompt_float(
        "Breathing max brightness %",
        max(anim.breath_max_brightness, anim.breath_min_brightness),
        anim.breath_min_brightness,
        100.0,
    )


def run_setup() -> int:
    config = load_config()

    host, bridge_id = _pick_bridge()
    print(f"\nPairing with bridge at {host} — this requires the physical link button.")
    app_key = _pair(host)

    # Persist the credential immediately: a hiccup later in setup must not
    # discard the app key the user just pressed a physical button for.
    config.bridge.host = host
    config.bridge.api_version = 2
    config.bridge.bridge_id = bridge_id
    backend = secret_store.set_app_key(app_key)
    secret_store.ensure_daemon_token()
    save_config(config)
    print("Paired successfully" + (" — app key stored in your OS keychain." if backend == "keyring" else "."))

    print("Loading lights and rooms from the bridge...")
    try:
        lights, rooms, zones, found_bridge_id = asyncio.run(
            _fetch_resources(config, app_key)
        )
    except Exception as err:
        raise SystemExit(
            f"could not read bridge resources: {err} "
            "(the app key was saved; re-run `hue-agent setup` to finish)"
        ) from None
    config.bridge.bridge_id = bridge_id or found_bridge_id

    if not lights:
        raise SystemExit("this bridge has no lights; nothing to control")

    mode, ids = _pick_targets(lights, rooms, zones)
    config.target.mode = mode
    config.target.ids = ids
    _tune_animation(config)
    save_config(config)
    print("Configuration saved.")

    raw = _input("\nRun a 15-second preview now (breathe, red, restore)? [Y/n]: ").strip().lower()
    if raw in ("", "y", "yes"):
        print("Previewing: breathing for 10s, red for 3s, then restoring...")
        controller = HueController(config, app_key=app_key)

        async def _preview():
            try:
                await controller.preview(breathe_seconds=10.0, red_seconds=3.0)
            finally:
                await controller.close()

        try:
            asyncio.run(_preview())
            print("Preview done — lights restored.")
        except Exception as err:
            print(f"preview failed: {err}", file=sys.stderr)

    print(
        "\nNext steps:\n"
        "  hue-agent install-hooks --all   # wire up Claude Code and Codex\n"
        "  hue-agent doctor                # verify everything\n"
        "  hue-agent autostart install     # optional: start daemon at login"
    )
    return 0
