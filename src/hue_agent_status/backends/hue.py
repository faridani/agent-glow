"""Hue Bridge control: breathing, waiting-red, completion-green, and restore.

Uses the Hue API v2 via ``aiohue``. Commands prefer grouped-light resources
(room / zone / grouped_light target modes) so one request drives many lamps;
in ``lights`` mode each selected lamp is driven concurrently behind a small
rate limiter that respects the bridge guidance of ~10 light commands/s and
~1 group command/s.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import asdict, dataclass

from ..animation import breathing_keyframes
from ..colors import parse_color
from ..config import Config, snapshot_path
from ..roles import effective_role_ids
from .base import (
    OVERRIDE_BRIGHTNESS_TOLERANCE,
    BackendUnavailableError,
    clear_snapshot_data,
    load_snapshot_data,
    save_snapshot_data,
)

LOGGER = logging.getLogger(__name__)

#: xy chromaticity used for the "waiting" red (inside all Hue gamuts).
RED_XY = (0.675, 0.322)
#: Soft warm white (~2700K) for lamps that were off when breathing starts.
SOFT_WARM_MIREK = 370
#: Warmest mirek most lamps accept — red-impossible fallback look.
WARMEST_MIREK = 454
#: Soft warm-white xy for color-capable lamps without a CT channel.
SOFT_WHITE_XY = (0.4573, 0.41)
#: Distinct success fallback for tunable-white lamps that cannot show green.
SUCCESS_COOL_MIREK = 233
#: Completion green; parsed once so Hue and CLI color semantics stay aligned.
GREEN_XY = parse_color("green").xy

_CONNECT_RETRY_SECONDS = 10.0
_BLINK_LOW_BRIGHTNESS = 5.0
_BLINK_HALF_SECONDS = 0.45
_BLINK_TRANSITION_MS = 150


class HueUnavailableError(BackendUnavailableError):
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


def save_snapshot_file(
    snapshot: dict[str, LightSnapshot], controlled: set[str]
) -> None:
    save_snapshot_data(snapshot_path(), snapshot, controlled)


def load_snapshot_file() -> tuple[dict[str, LightSnapshot], set[str]] | None:
    return load_snapshot_data(snapshot_path(), LightSnapshot.from_dict)


def clear_snapshot_file() -> None:
    clear_snapshot_data(snapshot_path())


class HueController:
    """Own the steady light mode plus cancellable transient green blinks."""

    name = "hue"

    def __init__(self, config: Config, app_key: str | None = None):
        self.config = config
        self._app_key = app_key
        self.bridge = None
        self.mode = "idle"  # idle | active | waiting | complete
        self._breath_task: asyncio.Task | None = None
        # Blink coroutines are owned by the daemon.  This generation lets a
        # state transition invalidate one without waiting through its sleeps.
        self._effect_generation = 0
        self._blink_active_token: int | None = None
        self._failed_ids: set[str] = set()
        self._grouped_failed = False
        self._snapshot: dict[str, LightSnapshot] = {}
        self._controlled: set[str] = set()
        self._grouped_id: str | None = None
        self._light_ids: list[str] = []
        self._thinking_ids: list[str] = []
        self._waiting_ids: list[str] = []
        self._all_ids: list[str] = []
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
            from .. import secret_store

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
            raise HueUnavailableError(
                f"cannot reach Hue Bridge at {host}: {err}"
            ) from err
        self.bridge = bridge
        self._resolve_targets()

    async def close(self) -> None:
        async with self._lock:
            self._effect_generation += 1
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
            ctrl = (
                self.bridge.groups.room if mode == "room" else self.bridge.groups.zone
            )
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
                    light.id
                    for light in self.bridge.groups.grouped_light.get_lights(gid)
                ]
            else:
                LOGGER.warning("configured grouped_light %s not found on bridge", ids)
        roles = self.config.roles
        if (roles.thinking or roles.waiting) and grouped is not None:
            # A grouped command cannot address a subset of the group, so
            # per-role lists force individual light commands.
            LOGGER.info(
                "per-role light lists configured; driving lights individually "
                "instead of the grouped-light fast path"
            )
            grouped = None
        self._grouped_id = grouped
        self._light_ids = lights
        self._thinking_ids = self._validated_role_ids("thinking", lights)
        self._waiting_ids = self._validated_role_ids("waiting", lights)
        self._all_ids = []
        for lid in self._thinking_ids + self._waiting_ids:
            if lid not in self._all_ids:
                self._all_ids.append(lid)
        if not self._all_ids and not grouped:
            LOGGER.warning("no valid target lights resolved from config")

    def _validated_role_ids(self, role: str, default_ids: list[str]) -> list[str]:
        """Role ids for this backend, dropping ids the bridge doesn't know."""
        ids = effective_role_ids(self.config, role, default_ids, backend=self.name)
        valid = []
        for lid in ids:
            if lid in default_ids or self.bridge.lights.get(lid):
                valid.append(lid)
            else:
                LOGGER.warning("roles.%s light %s not found on bridge", role, lid)
        return valid

    def target_summary(self) -> dict:
        return {
            "mode": self.config.target.mode,
            "grouped_light": self._grouped_id,
            "lights": list(self._light_ids),
            "thinking": list(self._thinking_ids),
            "waiting": list(self._waiting_ids),
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
                    return (
                        str(getattr(conn.status, "value", conn.status)) == "connected"
                    )
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
        # Snapshot the union of both roles: a waiting-only bulb needs its
        # snapshot even though the session usually starts in "active".
        snapshot: dict[str, LightSnapshot] = {}
        for lid in self._all_ids:
            light = self.bridge.lights.get(lid)
            if light is not None:
                snapshot[lid] = self._snapshot_light(light)
        self._snapshot = snapshot
        self._controlled = set(snapshot)
        save_snapshot_file(snapshot, self._controlled)
        LOGGER.info("snapshot taken for %d light(s)", len(snapshot))

    def _snapshot_lights(self, light_ids: list[str]) -> None:
        """Merge fresh snapshots for specific lights (config reload adds)."""
        added = False
        for lid in light_ids:
            if lid in self._snapshot:
                continue
            light = self.bridge.lights.get(lid)
            if light is None:
                continue
            self._snapshot[lid] = self._snapshot_light(light)
            self._controlled.add(lid)
            added = True
        if added:
            save_snapshot_file(self._snapshot, self._controlled)

    # -- command helpers --------------------------------------------------------

    async def _set_light(self, light_id: str, **kwargs) -> None:
        await self._rate.wait("light")
        try:
            await self.bridge.lights.set_state(light_id, **kwargs)
            self._failed_ids.discard(light_id)
        except Exception as err:
            self._failed_ids.add(light_id)
            LOGGER.debug("light %s command failed: %s", light_id, err)

    async def _set_grouped(self, **kwargs) -> None:
        await self._rate.wait("grouped")
        try:
            await self.bridge.groups.grouped_light.set_state(self._grouped_id, **kwargs)
            self._grouped_failed = False
        except Exception as err:
            self._grouped_failed = True
            LOGGER.debug("grouped_light %s command failed: %s", self._grouped_id, err)

    def _role_light_ids(self, role: str) -> list[str]:
        ids = self._thinking_ids if role == "thinking" else self._waiting_ids
        return [lid for lid in ids if lid in self._controlled]

    def _all_controlled_ids(self) -> list[str]:
        return [lid for lid in self._all_ids if lid in self._controlled]

    def _mode_light_ids(self, mode: str) -> list[str]:
        if mode == "active":
            return self._role_light_ids("thinking")
        if mode == "waiting":
            return self._role_light_ids("waiting")
        if mode == "complete":
            return self._all_controlled_ids()
        return []

    def _driven_light_ids(self) -> list[str]:
        """Lights the current mode is actively commanding."""
        return self._mode_light_ids(self.mode)

    def _mode_handoff_ids(self, leaving: str, entering: str) -> list[str]:
        entering_ids = set(self._mode_light_ids(entering))
        return [lid for lid in self._mode_light_ids(leaving) if lid not in entering_ids]

    # -- override detection (smart restore) --------------------------------------

    def _check_overrides(self) -> None:
        """Drop lights the user has visibly taken over from the controlled set."""
        if self.config.animation.restore != "smart" or self.bridge is None:
            return
        anim = self.config.animation
        grace = anim.breath_period_seconds
        settled = (time.monotonic() - self._mode_entered_at) > grace
        controlled_before = set(self._controlled)
        # Only lights the current mode drives can be "taken over": a
        # waiting-only bulb sitting at its restored snapshot brightness during
        # active is not a user override.
        for lid in self._driven_light_ids():
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
                elif self.mode in ("waiting", "complete"):
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
                if (
                    aggregate == "active"
                    and self._blink_active_token is None
                    and (self._breath_task is None or self._breath_task.done())
                ):
                    # A transient bridge/network failure must not leave an
                    # apparently-active controller permanently motionless.
                    await self._cancel_breathing()
                    self._mode_entered_at = time.monotonic()
                    self._breath_task = asyncio.create_task(self._breath_loop())
                elif (
                    aggregate in ("waiting", "complete")
                    and self._blink_active_token is None
                    and (
                        self._grouped_failed
                        or bool(self._failed_ids & set(self._driven_light_ids()))
                    )
                ):
                    if aggregate == "waiting":
                        await self._apply_waiting_look()
                    else:
                        await self._apply_complete_look()
                return
            if aggregate in ("active", "waiting", "complete"):
                await self.connect()
                if not self._all_ids and not self._grouped_id:
                    self._resolve_targets()
                if self.mode == "idle":
                    self.take_snapshot()
            previous = self.mode
            self._effect_generation += 1
            # Stop outstanding commands, then check the role we are leaving
            # while its mode and grace timestamp are still current.  Once
            # ``self.mode`` changes, waiting-only (or thinking-only) lights are
            # no longer considered driven and a handoff restore could overwrite
            # a last-moment user change.
            await self._cancel_breathing()
            self._check_overrides()
            self.mode = aggregate
            self._mode_entered_at = time.monotonic()
            if aggregate == "active":
                await self._enter_active(previous)
            elif aggregate == "waiting":
                await self._enter_waiting(previous)
            elif aggregate == "complete":
                await self._enter_complete(previous)
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

    async def _enter_active(self, previous: str = "idle") -> None:
        await self._restore_subset(self._mode_handoff_ids(previous, "active"))
        self._breath_task = asyncio.create_task(self._breath_loop())

    async def _enter_waiting(self, previous: str = "idle") -> None:
        await self._restore_subset(self._mode_handoff_ids(previous, "waiting"))
        await self._apply_waiting_look()

    async def _enter_complete(self, previous: str = "idle") -> None:
        await self._restore_subset(self._mode_handoff_ids(previous, "complete"))
        await self._apply_complete_look()

    async def _enter_idle(self, previous: str) -> None:
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
            for lid in self._role_light_ids("thinking"):
                snap = self._snapshot.get(lid)
                if snap is None:
                    continue
                kwargs = self._breath_color_kwargs(snap)
                if kwargs:
                    color_jobs.append(
                        self._set_light(lid, transition_time=ramp_ms, **kwargs)
                    )
            if color_jobs:
                await asyncio.gather(*color_jobs)
        else:
            jobs = []
            for lid in self._role_light_ids("thinking"):
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
            await self._set_grouped(
                brightness=brightness, transition_time=transition_ms
            )
            return
        jobs = [
            self._set_light(lid, brightness=brightness, transition_time=transition_ms)
            for lid in self._role_light_ids("thinking")
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

    def _wait_xy(self) -> tuple[float, float]:
        # The daemon may be running with a hand-edited (unvalidated) config
        # file, so a broken wait_color degrades to the classic red.
        try:
            return parse_color(self.config.animation.wait_color).xy
        except ValueError:
            LOGGER.warning(
                "invalid animation.wait_color %r; using red",
                self.config.animation.wait_color,
            )
            return RED_XY

    async def _apply_waiting_look(self) -> None:
        anim = self.config.animation
        transition = anim.wait_transition_ms
        wait_xy = self._wait_xy()
        pulse_targets: list[str] = []
        if self._grouped_id is not None:
            await self._set_grouped(
                on=True,
                brightness=anim.wait_brightness,
                color_xy=wait_xy,
                transition_time=transition,
            )
            # Color-incapable members: approximate red with warm high brightness.
            jobs = []
            for lid in self._role_light_ids("waiting"):
                snap = self._snapshot.get(lid)
                if snap is None or snap.supports_color:
                    continue
                if snap.supports_ct:
                    jobs.append(
                        self._set_light(
                            lid, color_temp=WARMEST_MIREK, transition_time=transition
                        )
                    )
                elif anim.wait_pulse_fallback:
                    pulse_targets.append(lid)
            if jobs:
                await asyncio.gather(*jobs)
        else:
            jobs = []
            for lid in self._role_light_ids("waiting"):
                snap = self._snapshot.get(lid)
                if snap is None:
                    continue
                if snap.supports_color:
                    jobs.append(
                        self._set_light(
                            lid,
                            on=True,
                            brightness=anim.wait_brightness,
                            color_xy=wait_xy,
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
                *[
                    self._set_light(lid, brightness=low, transition_time=300)
                    for lid in light_ids
                ]
            )
            await asyncio.sleep(0.45)
            await asyncio.gather(
                *[
                    self._set_light(
                        lid, brightness=anim.wait_brightness, transition_time=300
                    )
                    for lid in light_ids
                ]
            )

    # -- completion / transient success ----------------------------------------

    def _can_group_ids(self, light_ids: list[str]) -> bool:
        """A grouped command is safe only while every group member is controlled."""
        return bool(self._grouped_id) and set(light_ids) == set(self._all_ids)

    async def _apply_green_level(self, brightness: float) -> None:
        """Apply completion green, with a cool-white capability fallback."""
        light_ids = self._all_controlled_ids()
        if not light_ids:
            return
        transition = _BLINK_TRANSITION_MS
        if self._can_group_ids(light_ids):
            await self._set_grouped(
                on=True,
                brightness=brightness,
                color_xy=GREEN_XY,
                transition_time=transition,
            )
            # A group ignores color for non-color members.  Give CT-only
            # members a deliberate cool-white success look instead.
            jobs = []
            for lid in light_ids:
                snap = self._snapshot.get(lid)
                if snap is None:
                    continue
                kwargs: dict = {"transition_time": transition}
                if not snap.supports_color and snap.supports_ct:
                    kwargs["color_temp"] = SUCCESS_COOL_MIREK
                if not snap.supports_dimming:
                    kwargs["on"] = brightness > _BLINK_LOW_BRIGHTNESS
                if len(kwargs) > 1:
                    jobs.append(self._set_light(lid, **kwargs))
            if jobs:
                await asyncio.gather(*jobs)
            return

        jobs = []
        for lid in light_ids:
            snap = self._snapshot.get(lid)
            if snap is None:
                continue
            kwargs: dict = {
                "on": True,
                "transition_time": transition,
            }
            if snap.supports_dimming:
                kwargs["brightness"] = brightness
            if snap.supports_color:
                kwargs["color_xy"] = GREEN_XY
            elif snap.supports_ct:
                kwargs["color_temp"] = SUCCESS_COOL_MIREK
            elif not snap.supports_dimming:
                # On/off-only devices cannot express a low brightness, so the
                # low phase is a real off phase.
                kwargs["on"] = brightness > _BLINK_LOW_BRIGHTNESS
            jobs.append(self._set_light(lid, **kwargs))
        if jobs:
            await asyncio.gather(*jobs)

    async def _apply_complete_look(self) -> None:
        await self._apply_green_level(self.config.animation.wait_brightness)

    async def _blink_delay(self) -> None:
        """Small seam for focused tests without accelerating the breath loop."""
        await asyncio.sleep(_BLINK_HALF_SECONDS)

    async def blink_green(self, times: int = 5) -> None:
        """Blink all controlled lamps green, without ever covering waiting-red.

        The daemon owns and may cancel this coroutine.  State transitions use
        ``_effect_generation`` so a command already in flight always precedes
        (and can never overwrite) the newer steady look.
        """
        count = max(0, int(times))
        if count == 0:
            return
        token: int | None = None
        completed = False
        try:
            async with self._lock:
                if self.mode == "waiting":
                    return
                if self.mode == "idle":
                    await self.connect()
                    if not self._all_ids and not self._grouped_id:
                        self._resolve_targets()
                    self.take_snapshot()
                if not self._all_controlled_ids():
                    return
                self._effect_generation += 1
                token = self._effect_generation
                self._blink_active_token = token
                await self._cancel_breathing()
                self._check_overrides()
            for _ in range(count):
                async with self._lock:
                    if token != self._effect_generation or self.mode == "waiting":
                        return
                    await self._apply_green_level(_BLINK_LOW_BRIGHTNESS)
                await self._blink_delay()
                async with self._lock:
                    if token != self._effect_generation or self.mode == "waiting":
                        return
                    await self._apply_green_level(self.config.animation.wait_brightness)
                await self._blink_delay()
            completed = True
        finally:
            if token is not None:
                async with self._lock:
                    if token == self._effect_generation and self.mode == "active":
                        # The overlay touched the union, including waiting-only
                        # bulbs. Restore it all before breathing takes ownership of
                        # the thinking subset again.
                        await self._restore_subset(self._all_controlled_ids())
                        self._mode_entered_at = time.monotonic()
                        self._breath_task = asyncio.create_task(self._breath_loop())
                    elif (
                        token == self._effect_generation
                        and self.mode == "complete"
                        and not completed
                    ):
                        # Normal completion ends on the fifth high phase already.
                        # Cancellation may have stopped on low, so repaint solid.
                        await self._apply_complete_look()
                    elif token == self._effect_generation and self.mode == "idle":
                        await self._restore_locked()
                    if self._blink_active_token == token:
                        self._blink_active_token = None

    def runtime_status(self) -> dict:
        task = self._breath_task
        return {
            "mode": self.mode,
            "breathing": bool(task is not None and not task.done()),
            "effect": "blink_green" if self._blink_active_token is not None else None,
            "resolved": len(self._all_ids),
            "snapshotted": len(self._snapshot),
            "controlled": len(self._controlled),
            "failed": len(self._failed_ids) + int(self._grouped_failed),
        }

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

    async def _restore_subset(
        self, light_ids: list[str], transition_ms: int | None = None
    ) -> None:
        """Put specific lights back to snapshot without ending the session.

        Used for role handoffs (a bulb leaving the animated set) and config
        reloads; keeps the snapshot so the final idle restore stays complete.
        """
        transition = (
            transition_ms
            if transition_ms is not None
            else self.config.animation.idle_restore_transition_ms
        )
        jobs = []
        for lid in light_ids:
            snap = self._snapshot.get(lid)
            if snap is None or lid not in self._controlled:
                continue
            jobs.append(self._set_light(lid, **self._restore_kwargs(snap, transition)))
        if jobs:
            await asyncio.gather(*jobs)

    async def restore(
        self, transition_ms: int | None = None, policy: str | None = None
    ) -> int:
        """Put lights back to their snapshot. Returns the number restored."""
        async with self._lock:
            self._effect_generation += 1
            await self._cancel_breathing()
            self._check_overrides()
            return await self._restore_locked(transition_ms, policy)

    async def _restore_locked(
        self, transition_ms: int | None = None, policy: str | None = None
    ) -> int:
        self._effect_generation += 1
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

    async def update_config(self, new_config: Config) -> None:
        """Adopt a new config at runtime, keeping the snapshot correct.

        Lights leaving the target/role sets are restored and forgotten;
        lights joining get a fresh snapshot *before* being driven; the current
        mode is re-entered so new role sets and animation parameters apply
        immediately.
        """
        async with self._lock:
            self._effect_generation += 1
            old_union = list(self._all_ids)
            self.config = new_config
            if self.mode == "idle" or self.bridge is None:
                # Nothing is being driven: forget resolved sets and let the
                # next apply_state re-resolve lazily.
                self._grouped_id = None
                self._light_ids = []
                self._thinking_ids = []
                self._waiting_ids = []
                self._all_ids = []
                return
            await self._cancel_breathing()
            self._resolve_targets()
            new_union = set(self._all_ids)
            gone = [
                lid
                for lid in old_union
                if lid not in new_union and lid in self._controlled
            ]
            if gone:
                await self._restore_subset(gone)
                for lid in gone:
                    self._snapshot.pop(lid, None)
                    self._controlled.discard(lid)
                save_snapshot_file(self._snapshot, self._controlled)
            added = [lid for lid in self._all_ids if lid not in self._snapshot]
            if added:
                self._snapshot_lights(added)
            # Put every controlled light the current mode no longer drives
            # back to its snapshot, then re-enter the mode with the new sets.
            driven = set(self._mode_light_ids(self.mode))
            stale = [lid for lid in self._controlled if lid not in driven]
            if stale:
                await self._restore_subset(stale)
            self._mode_entered_at = time.monotonic()
            if self.mode == "active":
                self._breath_task = asyncio.create_task(self._breath_loop())
            elif self.mode == "waiting":
                await self._apply_waiting_look()
            elif self.mode == "complete":
                await self._apply_complete_look()

    def has_snapshot_file(self) -> bool:
        return load_snapshot_file() is not None

    async def restore_from_file(self) -> int:
        """Restore from the persisted snapshot (works without a running daemon)."""
        if load_snapshot_file() is None:
            return 0
        await self.connect()
        policy = self.config.animation.restore
        return await self.restore(policy="always" if policy != "never" else "never")
