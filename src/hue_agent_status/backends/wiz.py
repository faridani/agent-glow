"""WiZ bulb control: snapshot, breathing, waiting color, and smart restore.

Mirrors ``HueController``'s state machine, adapted to WiZ realities:

* bulbs are addressed directly over UDP (no hub) — identity is the MAC,
  the IP is only a cache refreshed by discovery when a probe mismatches;
* ``setPilot`` has no transition parameter, so breathing sends denser
  keyframes (~2.5 fps) and relies on the bulb's short internal fade;
* reading state costs a network round-trip, so override detection polls at
  most once per breath period instead of every frame.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import time
from dataclasses import asdict, dataclass

from ..animation import breathing_keyframes
from ..colors import parse_color
from ..config import (
    Config,
    ensure_private_dir,
    ensure_private_file,
    wiz_ip_cache_path,
    wiz_snapshot_path,
)
from ..roles import effective_role_ids
from .base import (
    OVERRIDE_BRIGHTNESS_TOLERANCE,
    BackendUnavailableError,
    atomic_write_json,
    clear_snapshot_data,
    load_snapshot_data,
    save_snapshot_data,
)
from .wiz_protocol import (
    COOL_KELVIN,
    WARMEST_KELVIN,
    WizCapabilities,
    WizTimeoutError,
    WizTransport,
    build_get_pilot,
    build_get_system_config,
    build_set_pilot,
    normalize_mac,
    parse_capabilities,
)

LOGGER = logging.getLogger(__name__)

_CONNECT_RETRY_SECONDS = 10.0
#: Frames per second the breathing loop aims for (no bulb-side interpolation).
_BREATH_FPS_TARGET = 2.5
#: Soft warm white for lamps that were off when breathing starts.
_SOFT_WARM_KELVIN = 2700


class WizUnavailableError(BackendUnavailableError):
    """No configured WiZ bulb is reachable right now."""


@dataclass
class WizLightSnapshot:
    id: str  # normalized mac
    on: bool
    dimming: float | None
    temp_k: int | None
    rgb: tuple[int, int, int] | None
    cold_white: int | None
    warm_white: int | None
    scene_id: int | None
    speed: int | None
    supports_color: bool
    supports_ct: bool
    reachable: bool | None

    def to_dict(self) -> dict:
        data = asdict(self)
        if self.rgb is not None:
            data["rgb"] = list(self.rgb)
        return data

    @classmethod
    def from_dict(cls, data: dict) -> "WizLightSnapshot":
        rgb = data.get("rgb")
        return cls(
            id=str(data["id"]),
            on=bool(data.get("on", False)),
            dimming=data.get("dimming"),
            temp_k=data.get("temp_k"),
            rgb=(int(rgb[0]), int(rgb[1]), int(rgb[2])) if rgb else None,
            cold_white=data.get("cold_white"),
            warm_white=data.get("warm_white"),
            scene_id=data.get("scene_id"),
            speed=data.get("speed"),
            supports_color=bool(data.get("supports_color", False)),
            supports_ct=bool(data.get("supports_ct", False)),
            reachable=data.get("reachable"),
        )


def snapshot_from_pilot(
    mac: str, pilot: dict, caps: WizCapabilities
) -> WizLightSnapshot:
    """Build a snapshot from a ``getPilot`` result."""
    rgb = None
    if all(k in pilot for k in ("r", "g", "b")):
        rgb = (int(pilot["r"]), int(pilot["g"]), int(pilot["b"]))
    scene_id = int(pilot.get("sceneId") or 0) or None
    return WizLightSnapshot(
        id=mac,
        on=bool(pilot.get("state", False)),
        dimming=pilot.get("dimming"),
        temp_k=pilot.get("temp"),
        rgb=rgb,
        cold_white=pilot.get("c"),
        warm_white=pilot.get("w"),
        scene_id=scene_id,
        speed=pilot.get("speed") if scene_id else None,
        supports_color=caps.supports_color,
        supports_ct=caps.supports_ct,
        reachable=True,
    )


def save_snapshot_file(
    snapshot: dict[str, WizLightSnapshot], controlled: set[str]
) -> None:
    save_snapshot_data(wiz_snapshot_path(), snapshot, controlled)


def load_snapshot_file() -> tuple[dict[str, WizLightSnapshot], set[str]] | None:
    return load_snapshot_data(wiz_snapshot_path(), WizLightSnapshot.from_dict)


def clear_snapshot_file() -> None:
    clear_snapshot_data(wiz_snapshot_path())


def _load_ip_cache() -> dict[str, str]:
    path = wiz_ip_cache_path()
    if not path.exists():
        return {}
    try:
        ensure_private_dir(path.parent)
        ensure_private_file(path)
        data = json.loads(path.read_text(encoding="utf-8"))
        return (
            {str(k): str(v) for k, v in data.items()} if isinstance(data, dict) else {}
        )
    except (OSError, ValueError):
        return {}


def _save_ip_cache(ips: dict[str, str]) -> None:
    try:
        atomic_write_json(wiz_ip_cache_path(), dict(sorted(ips.items())))
    except OSError as err:
        LOGGER.debug("cannot persist wiz ip cache: %s", err)


class WizController:
    """Owns the WiZ light state machine: idle -> active (breathe) / waiting."""

    name = "wiz"

    def __init__(self, config: Config, transport: WizTransport | None = None):
        self.config = config
        self.transport = transport or WizTransport()
        self.mode = "idle"  # idle | active | waiting
        self._breath_task: asyncio.Task | None = None
        self._snapshot: dict[str, WizLightSnapshot] = {}
        self._controlled: set[str] = set()
        self._lock = asyncio.Lock()
        self._mode_entered_at = 0.0
        self._last_connect_failure = 0.0
        self._connected = False
        self._ips: dict[str, str] = {}  # mac -> ip
        self._caps: dict[str, WizCapabilities] = {}
        self._thinking_ids: list[str] = []
        self._waiting_ids: list[str] = []
        self._all_ids: list[str] = []
        self._last_override_check = 0.0

    def _configured_macs(self) -> list[str]:
        macs = []
        for bulb in self.config.wiz.bulbs:
            try:
                mac = normalize_mac(bulb.mac)
            except ValueError:
                LOGGER.warning("ignoring wiz bulb with invalid mac %r", bulb.mac)
                continue
            if mac not in macs:
                macs.append(mac)
        return macs

    # -- connection ---------------------------------------------------------

    async def connect(self) -> None:
        if self._connected:
            return
        if time.monotonic() - self._last_connect_failure < _CONNECT_RETRY_SECONDS:
            raise WizUnavailableError("wiz bulbs recently unreachable; backing off")
        macs = self._configured_macs()
        if not macs:
            raise WizUnavailableError(
                "no WiZ bulbs configured; run `hue-agent wiz add`"
            )

        ips = _load_ip_cache()
        for bulb in self.config.wiz.bulbs:
            try:
                mac = normalize_mac(bulb.mac)
            except ValueError:
                continue
            if bulb.ip and mac not in ips:
                ips[mac] = bulb.ip

        missing = await self._probe(macs, ips)
        if missing:
            await self._rediscover(missing, ips)
        if not self._caps:
            self._last_connect_failure = time.monotonic()
            raise WizUnavailableError(
                "no configured WiZ bulb answered; check power and network"
            )
        self._ips = ips
        _save_ip_cache(ips)
        self._connected = True
        self._resolve_targets()

    async def _probe(self, macs: list[str], ips: dict[str, str]) -> list[str]:
        """Verify each cached IP still belongs to its mac; returns the misses."""

        async def check(mac: str) -> bool:
            ip = ips.get(mac)
            if not ip:
                return False
            try:
                result = await self.transport.send_command(
                    ip, build_get_system_config()
                )
            except Exception:
                return False
            try:
                found_mac = normalize_mac(str(result.get("mac", "")))
            except ValueError:
                return False
            if found_mac != mac:
                return False  # DHCP handed this IP to a different bulb
            self._caps[mac] = parse_capabilities(str(result.get("moduleName", "")))
            return True

        results = await asyncio.gather(*(check(mac) for mac in macs))
        return [mac for mac, ok in zip(macs, results) if not ok]

    async def _rediscover(self, missing: list[str], ips: dict[str, str]) -> None:
        try:
            found = await self.transport.discover(self.config.wiz.broadcast)
        except OSError as err:
            LOGGER.warning("wiz discovery failed: %s", err)
            return
        by_mac = dict(found)
        recovered = [mac for mac in missing if mac in by_mac]
        for mac in recovered:
            ips[mac] = by_mac[mac]
        if recovered:
            await self._probe(recovered, ips)
        for mac in missing:
            if mac not in by_mac:
                LOGGER.warning("wiz bulb %s not found on the network", mac)

    async def close(self) -> None:
        await self._cancel_breathing()
        self.transport.close()
        self._connected = False
        self._caps = {}

    # -- target resolution ------------------------------------------------------

    def _resolve_targets(self) -> None:
        pool = [mac for mac in self._configured_macs() if mac in self._caps]
        self._thinking_ids = self._validated_role_ids("thinking", pool)
        self._waiting_ids = self._validated_role_ids("waiting", pool)
        self._all_ids = []
        for mac in self._thinking_ids + self._waiting_ids:
            if mac not in self._all_ids:
                self._all_ids.append(mac)

    def _validated_role_ids(self, role: str, pool: list[str]) -> list[str]:
        ids = []
        for raw in effective_role_ids(self.config, role, pool, backend=self.name):
            try:
                mac = normalize_mac(raw)
            except ValueError:
                LOGGER.warning("roles.%s: invalid wiz mac %r", role, raw)
                continue
            if mac in pool:
                ids.append(mac)
            else:
                LOGGER.warning(
                    "roles.%s wiz bulb %s not configured/reachable", role, mac
                )
        return ids

    def target_summary(self) -> dict:
        return {
            "bulbs": self._configured_macs(),
            "thinking": list(self._thinking_ids),
            "waiting": list(self._waiting_ids),
        }

    # -- snapshot -----------------------------------------------------------------

    async def take_snapshot(self) -> None:
        snapshot: dict[str, WizLightSnapshot] = {}

        async def grab(mac: str) -> None:
            ip = self._ips.get(mac)
            if not ip:
                return
            try:
                pilot = await self.transport.send_command(ip, build_get_pilot())
            except Exception as err:
                LOGGER.warning("wiz %s unreachable at snapshot time: %s", mac, err)
                return
            caps = self._caps.get(mac, WizCapabilities(False, False))
            snapshot[mac] = snapshot_from_pilot(mac, pilot, caps)

        await asyncio.gather(*(grab(mac) for mac in self._all_ids))
        self._snapshot = snapshot
        self._controlled = set(snapshot)
        save_snapshot_file(snapshot, self._controlled)
        LOGGER.info("wiz snapshot taken for %d bulb(s)", len(snapshot))

    async def _snapshot_lights(self, macs: list[str]) -> None:
        """Merge fresh snapshots for specific bulbs (config reload adds)."""
        added = False
        for mac in macs:
            if mac in self._snapshot:
                continue
            ip = self._ips.get(mac)
            if not ip:
                continue
            try:
                pilot = await self.transport.send_command(ip, build_get_pilot())
            except Exception:
                continue
            caps = self._caps.get(mac, WizCapabilities(False, False))
            self._snapshot[mac] = snapshot_from_pilot(mac, pilot, caps)
            self._controlled.add(mac)
            added = True
        if added:
            save_snapshot_file(self._snapshot, self._controlled)

    # -- command helpers ----------------------------------------------------------

    async def _set_bulb(self, mac: str, **params) -> None:
        ip = self._ips.get(mac)
        if not ip:
            return
        try:
            await self.transport.send_command(ip, build_set_pilot(**params))
        except (WizTimeoutError, OSError) as err:
            LOGGER.debug("wiz %s command failed: %s", mac, err)

    def _role_light_ids(self, role: str) -> list[str]:
        ids = self._thinking_ids if role == "thinking" else self._waiting_ids
        return [mac for mac in ids if mac in self._controlled]

    def _driven_light_ids(self) -> list[str]:
        if self.mode == "active":
            return self._role_light_ids("thinking")
        if self.mode == "waiting":
            return self._role_light_ids("waiting")
        return []

    # -- override detection (smart restore) ----------------------------------------

    async def _check_overrides(self) -> None:
        """Poll driven bulbs (at most once per breath period) for takeover."""
        if self.config.animation.restore != "smart":
            return
        anim = self.config.animation
        now = time.monotonic()
        if now - self._last_override_check < anim.breath_period_seconds:
            return
        self._last_override_check = now
        settled = (now - self._mode_entered_at) > anim.breath_period_seconds
        if not settled:
            return
        controlled_before = set(self._controlled)

        async def check(mac: str) -> None:
            ip = self._ips.get(mac)
            if not ip:
                return
            try:
                pilot = await self.transport.send_command(ip, build_get_pilot())
            except Exception:
                return  # unreachable is not an override
            if not pilot.get("state", False):
                LOGGER.info("wiz %s turned off by user; leaving it alone", mac)
                self._controlled.discard(mac)
                return
            dimming = pilot.get("dimming")
            if dimming is None:
                return
            if self.mode == "active":
                low = anim.breath_min_brightness - OVERRIDE_BRIGHTNESS_TOLERANCE
                high = anim.breath_max_brightness + OVERRIDE_BRIGHTNESS_TOLERANCE
            elif self.mode == "waiting":
                low = anim.wait_brightness - OVERRIDE_BRIGHTNESS_TOLERANCE
                high = anim.wait_brightness + OVERRIDE_BRIGHTNESS_TOLERANCE
            else:
                return
            # dimming never goes below the firmware floor, so widen the band.
            low = max(low, 0)
            if not (low <= dimming <= high):
                LOGGER.info("wiz %s brightness changed by user; leaving it alone", mac)
                self._controlled.discard(mac)

        await asyncio.gather(*(check(mac) for mac in self._driven_light_ids()))
        if self._controlled != controlled_before:
            save_snapshot_file(self._snapshot, self._controlled)

    # -- state machine --------------------------------------------------------------

    async def apply_state(self, aggregate: str) -> None:
        async with self._lock:
            if aggregate == self.mode:
                return
            if aggregate in ("active", "waiting"):
                await self.connect()
                if not self._all_ids:
                    self._resolve_targets()
                if self.mode == "idle":
                    await self.take_snapshot()
            previous = self.mode
            self.mode = aggregate
            self._mode_entered_at = time.monotonic()
            if aggregate == "active":
                await self._enter_active(previous)
            elif aggregate == "waiting":
                await self._enter_waiting(previous)
            else:
                await self._enter_idle(previous)

    async def _cancel_breathing(self) -> None:
        task, self._breath_task = self._breath_task, None
        if task is not None:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass

    def _role_handoff_ids(self, leaving_role: str, entering_role: str) -> list[str]:
        leaving = self._role_light_ids(leaving_role)
        entering = set(self._role_light_ids(entering_role))
        return [mac for mac in leaving if mac not in entering]

    async def _enter_active(self, previous: str = "idle") -> None:
        await self._cancel_breathing()
        if previous == "waiting":
            await self._restore_subset(self._role_handoff_ids("waiting", "thinking"))
        self._breath_task = asyncio.create_task(self._breath_loop())

    async def _enter_waiting(self, previous: str = "idle") -> None:
        await self._cancel_breathing()
        if previous == "active":
            await self._restore_subset(self._role_handoff_ids("thinking", "waiting"))
        await self._apply_waiting_look()

    async def _enter_idle(self, previous: str) -> None:
        await self._cancel_breathing()
        if previous != "idle":
            await self._restore_locked()

    # -- breathing -------------------------------------------------------------------

    def _breath_color_params(self, snap: WizLightSnapshot) -> dict:
        """Color to apply when breathing starts, for one bulb."""
        choice = self.config.animation.breath_color
        if choice == "preserve":
            return {}
        if choice == "cool":
            if snap.supports_ct:
                return {"temp_k": COOL_KELVIN}
            return {}
        if choice == "auto" and snap.on:
            # Reapply the snapshot color explicitly: a waiting phase may have
            # painted the bulb red, and breathing must resume in its own color.
            if snap.scene_id:
                return {}  # scenes animate on their own; just dim them
            if snap.rgb is not None and snap.supports_color:
                return {
                    "rgb": snap.rgb,
                    "cold_white": snap.cold_white,
                    "warm_white": snap.warm_white,
                }
            if snap.temp_k is not None and snap.supports_ct:
                return {"temp_k": snap.temp_k}
            return {}
        # "warm" or "auto"-and-was-off: soft warm white
        if snap.supports_ct:
            return {"temp_k": _SOFT_WARM_KELVIN}
        return {}

    async def _prepare_breathing(self) -> None:
        anim = self.config.animation
        jobs = []
        for mac in self._role_light_ids("thinking"):
            snap = self._snapshot.get(mac)
            params = self._breath_color_params(snap) if snap else {}
            jobs.append(
                self._set_bulb(
                    mac, state=True, dimming=anim.breath_min_brightness, **params
                )
            )
        if jobs:
            await asyncio.gather(*jobs)

    async def _send_dimming(self, brightness: float) -> None:
        jobs = [
            self._set_bulb(mac, dimming=brightness)
            for mac in self._role_light_ids("thinking")
        ]
        if jobs:
            await asyncio.gather(*jobs)

    async def _breath_loop(self) -> None:
        anim = self.config.animation
        # No transition parameter in setPilot: send dense keyframes instead
        # and let the bulb's short internal fade smooth the steps.
        per_half = max(
            anim.breath_keyframes_per_half,
            math.ceil(anim.breath_period_seconds * _BREATH_FPS_TARGET / 2),
        )
        frames = breathing_keyframes(
            anim.breath_min_brightness,
            anim.breath_max_brightness,
            anim.breath_period_seconds,
            per_half,
            anim.easing,
        )
        try:
            await self._prepare_breathing()
            while True:
                for brightness, duration in frames:
                    await self._check_overrides()
                    await self._send_dimming(brightness)
                    await asyncio.sleep(duration)
        except asyncio.CancelledError:
            raise
        except Exception as err:
            LOGGER.warning("wiz breathing loop stopped: %s", err)

    # -- waiting ---------------------------------------------------------------------

    def _wait_rgb(self) -> tuple[int, int, int]:
        try:
            return parse_color(self.config.animation.wait_color).rgb
        except ValueError:
            return (255, 0, 0)

    async def _apply_waiting_look(self) -> None:
        anim = self.config.animation
        wait_rgb = self._wait_rgb()
        jobs = []
        pulse_targets: list[str] = []
        for mac in self._role_light_ids("waiting"):
            snap = self._snapshot.get(mac)
            if snap is None:
                continue
            if snap.supports_color:
                jobs.append(
                    self._set_bulb(
                        mac, state=True, dimming=anim.wait_brightness, rgb=wait_rgb
                    )
                )
            elif snap.supports_ct:
                jobs.append(
                    self._set_bulb(
                        mac,
                        state=True,
                        dimming=anim.wait_brightness,
                        temp_k=WARMEST_KELVIN,
                    )
                )
            else:
                jobs.append(
                    self._set_bulb(mac, state=True, dimming=anim.wait_brightness)
                )
                if anim.wait_pulse_fallback:
                    pulse_targets.append(mac)
        if jobs:
            await asyncio.gather(*jobs)
        if pulse_targets:
            await self._double_pulse(pulse_targets)

    async def _double_pulse(self, macs: list[str]) -> None:
        """Two gentle dips for bulbs that cannot show a color at all."""
        anim = self.config.animation
        low = max(10.0, anim.wait_brightness - 50.0)
        for _ in range(2):
            await asyncio.sleep(0.6)
            await asyncio.gather(*[self._set_bulb(mac, dimming=low) for mac in macs])
            await asyncio.sleep(0.45)
            await asyncio.gather(
                *[self._set_bulb(mac, dimming=anim.wait_brightness) for mac in macs]
            )

    # -- restore -----------------------------------------------------------------------

    def _restore_params(self, snap: WizLightSnapshot) -> dict:
        if not snap.on:
            return {"state": False}
        params: dict = {"state": True}
        if snap.dimming is not None:
            params["dimming"] = snap.dimming
        if snap.scene_id:
            params["scene_id"] = snap.scene_id
            params["speed"] = snap.speed
        elif snap.rgb is not None:
            params["rgb"] = snap.rgb
            params["cold_white"] = snap.cold_white
            params["warm_white"] = snap.warm_white
        elif snap.temp_k is not None:
            params["temp_k"] = snap.temp_k
        return params

    async def _restore_subset(
        self, macs: list[str], transition_ms: int | None = None
    ) -> None:
        jobs = []
        for mac in macs:
            snap = self._snapshot.get(mac)
            if snap is None or mac not in self._controlled:
                continue
            jobs.append(self._set_bulb(mac, **self._restore_params(snap)))
        if jobs:
            await asyncio.gather(*jobs)

    async def restore(
        self, transition_ms: int | None = None, policy: str | None = None
    ) -> int:
        async with self._lock:
            return await self._restore_locked(transition_ms, policy)

    async def _restore_locked(
        self, transition_ms: int | None = None, policy: str | None = None
    ) -> int:
        await self._cancel_breathing()
        if not self._snapshot:
            loaded = load_snapshot_file()
            if loaded:
                self._snapshot, self._controlled = loaded
        if not self._snapshot:
            self.mode = "idle"
            return 0
        if not self._connected:
            await self.connect()  # raises WizUnavailableError; snapshot kept
        policy = policy or self.config.animation.restore
        restored = 0
        if policy != "never":
            targets = [
                snap
                for mac, snap in self._snapshot.items()
                if policy == "always" or mac in self._controlled
            ]
            jobs = [
                self._set_bulb(snap.id, **self._restore_params(snap))
                for snap in targets
            ]
            if jobs:
                await asyncio.gather(*jobs)
            restored = len(jobs)
            LOGGER.info("restored %d wiz bulb(s)", restored)
        self._snapshot = {}
        self._controlled = set()
        self.mode = "idle"
        clear_snapshot_file()
        return restored

    def has_snapshot_file(self) -> bool:
        return load_snapshot_file() is not None

    async def restore_from_file(self) -> int:
        if load_snapshot_file() is None:
            return 0
        await self.connect()
        policy = self.config.animation.restore
        return await self.restore(policy="always" if policy != "never" else "never")

    # -- config reload --------------------------------------------------------------

    async def update_config(self, new_config: Config) -> None:
        """Adopt a new config at runtime; see HueController.update_config."""
        async with self._lock:
            old_union = list(self._all_ids)
            self.config = new_config
            if self.mode == "idle" or not self._connected:
                self._thinking_ids = []
                self._waiting_ids = []
                self._all_ids = []
                self._connected = False  # bulbs may have been added: re-probe
                self._caps = {}
                return
            await self._cancel_breathing()
            # Newly added bulbs need an IP and capabilities before use.
            self._connected = False
            self._caps = {}
            try:
                await self.connect()
            except WizUnavailableError as err:
                LOGGER.warning("wiz reload: %s", err)
                return
            new_union = set(self._all_ids)
            gone = [
                mac
                for mac in old_union
                if mac not in new_union and mac in self._controlled
            ]
            if gone:
                await self._restore_subset(gone)
                for mac in gone:
                    self._snapshot.pop(mac, None)
                    self._controlled.discard(mac)
                save_snapshot_file(self._snapshot, self._controlled)
            added = [mac for mac in self._all_ids if mac not in self._snapshot]
            if added:
                await self._snapshot_lights(added)
            driven_role = "thinking" if self.mode == "active" else "waiting"
            driven = set(self._role_light_ids(driven_role))
            stale = [mac for mac in self._controlled if mac not in driven]
            if stale:
                await self._restore_subset(stale)
            self._mode_entered_at = time.monotonic()
            if self.mode == "active":
                self._breath_task = asyncio.create_task(self._breath_loop())
            else:
                await self._apply_waiting_look()
