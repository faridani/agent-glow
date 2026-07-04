"""Hue Bridge control: snapshot, breathing, waiting-red, and smart restore.

Uses the Hue API v2 via ``aiohue``. Commands prefer grouped-light resources
(room / zone / grouped_light target modes) so one request drives many lamps;
in ``lights`` mode each selected lamp is driven concurrently behind a small
rate limiter that respects the bridge guidance of ~10 light commands/s and
~1 group command/s.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from .animation import breathing_keyframes
from .config import Config, snapshot_path

LOGGER = logging.getLogger(__name__)

#: xy chromaticity used for the "waiting" red (inside all Hue gamuts).
RED_XY = (0.675, 0.322)
#: Soft warm white (~2700K) for lamps that were off when breathing starts.
SOFT_WARM_MIREK = 370
#: Warmest mirek most lamps accept — red-impossible fallback look.
WARMEST_MIREK = 454
#: Soft warm-white xy for color-capable lamps without a CT channel.
SOFT_WHITE_XY = (0.4573, 0.41)

#: Brightness drift (percent) beyond which we assume the user took over.
OVERRIDE_BRIGHTNESS_TOLERANCE = 12.0

_CONNECT_RETRY_SECONDS = 10.0


class HueUnavailableError(Exception):
    """Bridge not configured or not reachable right now."""


@dataclass
class LightSnapshot:
    id: str
    on: bool
    brightness: float | None
    color_xy: tuple[float, float] | None
    color_temp_mirek: int | None
    supports_color: bool
    supports_ct: bool
    supports_dimming: bool
    reachable: bool | None

    def to_dict(self) -> dict:
        data = asdict(self)
        if self.color_xy is not None:
            data["color_xy"] = list(self.color_xy)
        return data

    @classmethod
    def from_dict(cls, data: dict) -> "LightSnapshot":
        xy = data.get("color_xy")
        return cls(
            id=str(data["id"]),
            on=bool(data.get("on", False)),
            brightness=data.get("brightness"),
            color_xy=(float(xy[0]), float(xy[1])) if xy else None,
            color_temp_mirek=data.get("color_temp_mirek"),
            supports_color=bool(data.get("supports_color", False)),
            supports_ct=bool(data.get("supports_ct", False)),
            supports_dimming=bool(data.get("supports_dimming", True)),
            reachable=data.get("reachable"),
        )


class RateLimiter:
    """Global spacing between bridge commands (lights ~10/s, groups ~1/s)."""

    INTERVALS = {"light": 0.1, "grouped": 1.0}

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._last: dict[str, float] = {}

    async def wait(self, kind: str) -> None:
        async with self._lock:
            now = time.monotonic()
            wait_until = self._last.get(kind, 0.0) + self.INTERVALS.get(kind, 0.1)
            if wait_until > now:
                await asyncio.sleep(wait_until - now)
            self._last[kind] = time.monotonic()


def _atomic_write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    tmp.replace(path)


def save_snapshot_file(snapshot: dict[str, LightSnapshot], controlled: set[str]) -> None:
    _atomic_write_json(
        snapshot_path(),
        {
            "saved_at": time.time(),
            "controlled": sorted(controlled),
            "lights": {lid: snap.to_dict() for lid, snap in snapshot.items()},
        },
    )


def load_snapshot_file() -> tuple[dict[str, LightSnapshot], set[str]] | None:
    try:
        data = json.loads(snapshot_path().read_text(encoding="utf-8"))
        lights = {
            lid: LightSnapshot.from_dict(item)
            for lid, item in data.get("lights", {}).items()
        }
        return lights, set(data.get("controlled", []))
    except (OSError, ValueError, KeyError):
        return None


def clear_snapshot_file() -> None:
    try:
        os.unlink(snapshot_path())
    except OSError:
        pass


class HueController:
    """Owns the light state machine: idle -> active (breathe) / waiting (red)."""

    def __init__(self, config: Config, app_key: str | None = None):
        self.config = config
        self._app_key = app_key
        self.bridge = None
        self.mode = "idle"  # idle | active | waiting
        self._breath_task: asyncio.Task | None = None
        self._snapshot: dict[str, LightSnapshot] = {}
        self._controlled: set[str] = set()
        self._grouped_id: str | None = None
        self._light_ids: list[str] = []
        self._rate = RateLimiter()
        self._lock = asyncio.Lock()
        self._mode_entered_at = 0.0
        self._last_connect_failure = 0.0

    # -- connection ---------------------------------------------------------

    async def connect(self) -> None:
        if self.bridge is not None:
            return
        if time.monotonic() - self._last_connect_failure < _CONNECT_RETRY_SECONDS:
            raise HueUnavailableError("bridge recently unreachable; backing off")
        host = self.config.bridge.host
        if not host:
            raise HueUnavailableError("no bridge configured; run `hue-agent setup`")
        if self._app_key is None:
            from . import secret_store

            self._app_key = secret_store.get_app_key()
        if not self._app_key:
            raise HueUnavailableError("no Hue app key stored; run `hue-agent setup`")

        from aiohue.v2 import HueBridgeV2

        bridge = HueBridgeV2(host, self._app_key)
        try:
            async with asyncio.timeout(10):
                await bridge.initialize()
        except Exception as err:
            self._last_connect_failure = time.monotonic()
            try:
                await bridge.close()
            except Exception:
                pass
            raise HueUnavailableError(f"cannot reach Hue Bridge at {host}: {err}") from err
        self.bridge = bridge
        self._resolve_targets()

    async def close(self) -> None:
        await self._cancel_breathing()
        if self.bridge is not None:
            try:
                await self.bridge.close()
            except Exception:
                pass
            self.bridge = None

    # -- target resolution ----------------------------------------------------

    def _resolve_targets(self) -> None:
        mode = self.config.target.mode
        ids = list(self.config.target.ids)
        grouped: str | None = None
        lights: list[str] = []
        if mode == "lights":
            for lid in ids:
                if self.bridge.lights.get(lid):
                    lights.append(lid)
                else:
                    LOGGER.warning("configured light %s not found on bridge", lid)
        elif mode in ("room", "zone"):
            ctrl = self.bridge.groups.room if mode == "room" else self.bridge.groups.zone
            group = ctrl.get(ids[0]) if ids else None
            if group is None:
                LOGGER.warning("configured %s %s not found on bridge", mode, ids)
            else:
                grouped = group.grouped_light
                lights = [light.id for light in ctrl.get_lights(group.id)]
        elif mode == "grouped_light":
            gid = ids[0] if ids else None
            if gid and self.bridge.groups.grouped_light.get(gid):
                grouped = gid
                lights = [
                    light.id for light in self.bridge.groups.grouped_light.get_lights(gid)
                ]
            else:
                LOGGER.warning("configured grouped_light %s not found on bridge", ids)
        self._grouped_id = grouped
        self._light_ids = lights
        if not lights and not grouped:
            LOGGER.warning("no valid target lights resolved from config")

    def target_summary(self) -> dict:
        return {
            "mode": self.config.target.mode,
            "grouped_light": self._grouped_id,
            "lights": list(self._light_ids),
        }

    # -- snapshot -------------------------------------------------------------

    def _light_reachable(self, light) -> bool | None:
        try:
            device = self.bridge.lights.get_device(light.id)
            if device is None:
                return None
            zigbee = getattr(self.bridge.sensors, "zigbee_connectivity", None)
            if zigbee is None:
                return None
            for conn in zigbee:
                if conn.owner and conn.owner.rid == device.id:
                    return str(getattr(conn.status, "value", conn.status)) == "connected"
        except Exception:
            return None
        return None

    def _snapshot_light(self, light) -> LightSnapshot:
        xy = None
        if light.color is not None and light.color.xy is not None:
            xy = (light.color.xy.x, light.color.xy.y)
        mirek = None
        if light.color_temperature is not None:
            if light.color_temperature.mirek_valid is not False:
                mirek = light.color_temperature.mirek
        return LightSnapshot(
            id=light.id,
            on=bool(light.on.on) if light.on is not None else False,
            brightness=light.dimming.brightness if light.dimming is not None else None,
            color_xy=xy,
            color_temp_mirek=mirek,
            supports_color=light.color is not None,
            supports_ct=light.color_temperature is not None,
            supports_dimming=light.dimming is not None,
            reachable=self._light_reachable(light),
        )

    def take_snapshot(self) -> None:
        snapshot: dict[str, LightSnapshot] = {}
        for lid in self._light_ids:
            light = self.bridge.lights.get(lid)
            if light is not None:
                snapshot[lid] = self._snapshot_light(light)
        self._snapshot = snapshot
        self._controlled = set(snapshot)
        save_snapshot_file(snapshot, self._controlled)
        LOGGER.info("snapshot taken for %d light(s)", len(snapshot))

    # -- command helpers --------------------------------------------------------

    async def _set_light(self, light_id: str, **kwargs) -> None:
        await self._rate.wait("light")
        try:
            await self.bridge.lights.set_state(light_id, **kwargs)
        except Exception as err:
            LOGGER.debug("light %s command failed: %s", light_id, err)

    async def _set_grouped(self, **kwargs) -> None:
        await self._rate.wait("grouped")
        try:
            await self.bridge.groups.grouped_light.set_state(self._grouped_id, **kwargs)
        except Exception as err:
            LOGGER.debug("grouped_light %s command failed: %s", self._grouped_id, err)

    def _active_light_ids(self) -> list[str]:
        return [lid for lid in self._light_ids if lid in self._controlled]

    # -- override detection (smart restore) --------------------------------------

    def _check_overrides(self) -> None:
        """Drop lights the user has visibly taken over from the controlled set."""
        if self.config.animation.restore != "smart" or self.bridge is None:
            return
        anim = self.config.animation
        grace = anim.breath_period_seconds
        settled = (time.monotonic() - self._mode_entered_at) > grace
        controlled_before = set(self._controlled)
        for lid in list(self._controlled):
            light = self.bridge.lights.get(lid)
            if light is None:
                continue
            # Breathing/waiting always turns controlled lights on, so an
            # off light after the grace period means the user switched it off.
            is_on = bool(light.on.on) if light.on is not None else False
            if not is_on and settled:
                LOGGER.info("light %s turned off by user; leaving it alone", lid)
                self._controlled.discard(lid)
                continue
            # Brightness drift is only meaningful when we address lights
            # individually — a grouped command clobbers per-light levels anyway.
            if self._grouped_id is None and settled and is_on and light.dimming:
                brightness = light.dimming.brightness
                if brightness is None:
                    continue
                if self.mode == "active":
                    low = anim.breath_min_brightness - OVERRIDE_BRIGHTNESS_TOLERANCE
                    high = anim.breath_max_brightness + OVERRIDE_BRIGHTNESS_TOLERANCE
                elif self.mode == "waiting":
                    low = anim.wait_brightness - OVERRIDE_BRIGHTNESS_TOLERANCE
                    high = anim.wait_brightness + OVERRIDE_BRIGHTNESS_TOLERANCE
                else:
                    continue
                if not (low <= brightness <= high):
                    LOGGER.info(
                        "light %s brightness changed by user; leaving it alone", lid
                    )
                    self._controlled.discard(lid)
        if self._controlled != controlled_before:
            save_snapshot_file(self._snapshot, self._controlled)

    # -- state machine ----------------------------------------------------------

    async def apply_state(self, aggregate: str) -> None:
        """Drive lights to match the aggregate session state."""
        async with self._lock:
            if aggregate == self.mode:
                return
            if aggregate in ("active", "waiting"):
                await self.connect()
                if not self._light_ids and not self._grouped_id:
                    self._resolve_targets()
                if self.mode == "idle":
                    self.take_snapshot()
            previous = self.mode
            self.mode = aggregate
            self._mode_entered_at = time.monotonic()
            if aggregate == "active":
                await self._enter_active()
            elif aggregate == "waiting":
                await self._enter_waiting()
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

    async def _enter_active(self) -> None:
        await self._cancel_breathing()
        self._breath_task = asyncio.create_task(self._breath_loop())

    async def _enter_waiting(self) -> None:
        await self._cancel_breathing()
        self._check_overrides()
        await self._apply_waiting_look()

    async def _enter_idle(self, previous: str) -> None:
        await self._cancel_breathing()
        if previous != "idle":
            await self._restore_locked()  # apply_state already holds the lock

    # -- breathing -----------------------------------------------------------

    def _breath_color_kwargs(self, snap: LightSnapshot) -> dict:
        """Color to apply when breathing starts, for one light."""
        choice = self.config.animation.breath_color
        if choice == "preserve":
            return {}
        if choice == "cool":
            if snap.supports_ct:
                return {"color_temp": 233}  # ~4300K
            if snap.supports_color:
                return {"color_xy": (0.31, 0.33)}
            return {}
        if choice == "auto" and snap.on:
            # Reapply the snapshot color explicitly: a waiting phase may have
            # painted the lamp red, and breathing must resume in its own color.
            if snap.color_temp_mirek is not None and snap.supports_ct:
                return {"color_temp": snap.color_temp_mirek}
            if snap.color_xy is not None and snap.supports_color:
                return {"color_xy": snap.color_xy}
            return {}
        # "warm" or "auto"-and-was-off: soft warm white
        if snap.supports_ct:
            return {"color_temp": SOFT_WARM_MIREK}
        if snap.supports_color:
            return {"color_xy": SOFT_WHITE_XY}
        return {}

    async def _prepare_breathing(self) -> None:
        """Turn targets on at minimum brightness with a gentle fade."""
        anim = self.config.animation
        ramp_ms = max(400, int(anim.breath_period_seconds * 1000 / 4))
        if self._grouped_id is not None:
            await self._set_grouped(
                on=True, brightness=anim.breath_min_brightness, transition_time=ramp_ms
            )
            # Lamps that were off get a soft color so the group looks coherent.
            color_jobs = []
            for lid in self._active_light_ids():
                snap = self._snapshot.get(lid)
                if snap is None:
                    continue
                kwargs = self._breath_color_kwargs(snap)
                if kwargs:
                    color_jobs.append(self._set_light(lid, transition_time=ramp_ms, **kwargs))
            if color_jobs:
                await asyncio.gather(*color_jobs)
        else:
            jobs = []
            for lid in self._active_light_ids():
                snap = self._snapshot.get(lid)
                kwargs = self._breath_color_kwargs(snap) if snap else {}
                jobs.append(
                    self._set_light(
                        lid,
                        on=True,
                        brightness=anim.breath_min_brightness,
                        transition_time=ramp_ms,
                        **kwargs,
                    )
                )
            await asyncio.gather(*jobs)
        await asyncio.sleep(ramp_ms / 1000)

    async def _send_brightness(self, brightness: float, transition_ms: int) -> None:
        if self._grouped_id is not None:
            await self._set_grouped(brightness=brightness, transition_time=transition_ms)
            return
        jobs = [
            self._set_light(lid, brightness=brightness, transition_time=transition_ms)
            for lid in self._active_light_ids()
        ]
        if jobs:
            await asyncio.gather(*jobs)

    async def _breath_loop(self) -> None:
        anim = self.config.animation
        frames = breathing_keyframes(
            anim.breath_min_brightness,
            anim.breath_max_brightness,
            anim.breath_period_seconds,
            anim.breath_keyframes_per_half,
            anim.easing,
        )
        try:
            await self._prepare_breathing()
            while True:
                for brightness, duration in frames:
                    self._check_overrides()
                    await self._send_brightness(brightness, int(duration * 1000))
                    await asyncio.sleep(duration)
        except asyncio.CancelledError:
            raise
        except Exception as err:
            LOGGER.warning("breathing loop stopped: %s", err)

    # -- waiting (red) ---------------------------------------------------------

    async def _apply_waiting_look(self) -> None:
        anim = self.config.animation
        transition = anim.wait_transition_ms
        pulse_targets: list[str] = []
        if self._grouped_id is not None:
            await self._set_grouped(
                on=True,
                brightness=anim.wait_brightness,
                color_xy=RED_XY,
                transition_time=transition,
            )
            # Color-incapable members: approximate red with warm high brightness.
            jobs = []
            for lid in self._active_light_ids():
                snap = self._snapshot.get(lid)
                if snap is None or snap.supports_color:
                    continue
                if snap.supports_ct:
                    jobs.append(
                        self._set_light(lid, color_temp=WARMEST_MIREK, transition_time=transition)
                    )
                elif anim.wait_pulse_fallback:
                    pulse_targets.append(lid)
            if jobs:
                await asyncio.gather(*jobs)
        else:
            jobs = []
            for lid in self._active_light_ids():
                snap = self._snapshot.get(lid)
                if snap is None:
                    continue
                if snap.supports_color:
                    jobs.append(
                        self._set_light(
                            lid,
                            on=True,
                            brightness=anim.wait_brightness,
                            color_xy=RED_XY,
                            transition_time=transition,
                        )
                    )
                elif snap.supports_ct:
                    jobs.append(
                        self._set_light(
                            lid,
                            on=True,
                            brightness=anim.wait_brightness,
                            color_temp=WARMEST_MIREK,
                            transition_time=transition,
                        )
                    )
                else:
                    jobs.append(
                        self._set_light(
                            lid,
                            on=True,
                            brightness=anim.wait_brightness,
                            transition_time=transition,
                        )
                    )
                    if anim.wait_pulse_fallback:
                        pulse_targets.append(lid)
            if jobs:
                await asyncio.gather(*jobs)
        if pulse_targets:
            await self._double_pulse(pulse_targets)

    async def _double_pulse(self, light_ids: list[str]) -> None:
        """Two gentle dips for lamps that cannot show red at all."""
        anim = self.config.animation
        low = max(5.0, anim.wait_brightness - 50.0)
        for _ in range(2):
            await asyncio.sleep(0.6)
            await asyncio.gather(
                *[self._set_light(lid, brightness=low, transition_time=300) for lid in light_ids]
            )
            await asyncio.sleep(0.45)
            await asyncio.gather(
                *[
                    self._set_light(lid, brightness=anim.wait_brightness, transition_time=300)
                    for lid in light_ids
                ]
            )

    # -- restore -----------------------------------------------------------------

    def _restore_kwargs(self, snap: LightSnapshot, transition_ms: int) -> dict:
        if not snap.on:
            return {"on": False, "transition_time": transition_ms}
        kwargs: dict = {"on": True, "transition_time": transition_ms}
        if snap.brightness is not None:
            kwargs["brightness"] = snap.brightness
        # A light reports a valid mirek only while in color-temperature mode,
        # so prefer it; otherwise fall back to the xy color.
        if snap.color_temp_mirek is not None and snap.supports_ct:
            kwargs["color_temp"] = snap.color_temp_mirek
        elif snap.color_xy is not None and snap.supports_color:
            kwargs["color_xy"] = snap.color_xy
        return kwargs

    async def restore(
        self, transition_ms: int | None = None, policy: str | None = None
    ) -> int:
        """Put lights back to their snapshot. Returns the number restored."""
        async with self._lock:
            return await self._restore_locked(transition_ms, policy)

    async def _restore_locked(
        self, transition_ms: int | None = None, policy: str | None = None
    ) -> int:
        await self._cancel_breathing()  # never race the breath loop
        if not self._snapshot:
            # A fresh daemon has no in-memory snapshot, but a crashed
            # predecessor may have left the persisted one — recover it
            # rather than deleting the only copy.
            loaded = load_snapshot_file()
            if loaded:
                self._snapshot, self._controlled = loaded
        if not self._snapshot:
            self.mode = "idle"
            return 0
        if self.bridge is None:
            await self.connect()  # raises HueUnavailableError; snapshot kept
        policy = policy or self.config.animation.restore
        transition = (
            transition_ms
            if transition_ms is not None
            else self.config.animation.idle_restore_transition_ms
        )
        restored = 0
        if policy != "never":
            targets = [
                snap
                for lid, snap in self._snapshot.items()
                if policy == "always" or lid in self._controlled
            ]
            jobs = [
                self._set_light(snap.id, **self._restore_kwargs(snap, transition))
                for snap in targets
            ]
            if jobs:
                await asyncio.gather(*jobs)
            restored = len(jobs)
            LOGGER.info("restored %d light(s)", restored)
        self._snapshot = {}
        self._controlled = set()
        self.mode = "idle"
        clear_snapshot_file()
        return restored

    async def restore_from_file(self) -> int:
        """Restore from the persisted snapshot (works without a running daemon)."""
        if load_snapshot_file() is None:
            return 0
        await self.connect()
        policy = self.config.animation.restore
        return await self.restore(policy="always" if policy != "never" else "never")

    # -- preview -------------------------------------------------------------------

    async def preview(self, breathe_seconds: float = 10.0, red_seconds: float = 3.0) -> None:
        """Breathe, go red, then restore — used by setup and `hue-agent preview`."""
        await self.connect()
        self.take_snapshot()
        self.mode = "active"
        self._mode_entered_at = time.monotonic()
        self._breath_task = asyncio.create_task(self._breath_loop())
        await asyncio.sleep(breathe_seconds)
        await self._cancel_breathing()
        self.mode = "waiting"
        self._mode_entered_at = time.monotonic()
        await self._apply_waiting_look()
        await asyncio.sleep(red_seconds)
        await self.restore(policy="always")
