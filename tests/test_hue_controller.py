"""HueController logic against a fake bridge: snapshot, waiting look, restore."""

import time

from hue_agent_status.backends.hue import (
    RED_XY,
    WARMEST_MIREK,
    HueController,
    load_snapshot_file,
)
from hue_agent_status.config import Config


class _On:
    def __init__(self, on):
        self.on = on


class _Dimming:
    def __init__(self, brightness):
        self.brightness = brightness


class _XY:
    def __init__(self, x, y):
        self.x = x
        self.y = y


class _Color:
    def __init__(self, x, y):
        self.xy = _XY(x, y)


class _CT:
    def __init__(self, mirek, valid=True):
        self.mirek = mirek
        self.mirek_valid = valid


class FakeLight:
    def __init__(self, id, on=True, brightness=80.0, color=None, ct=None):
        self.id = id
        self.on = _On(on)
        self.dimming = _Dimming(brightness) if brightness is not None else None
        self.color = color
        self.color_temperature = ct


class FakeLightsController:
    def __init__(self, lights):
        self._lights = {light.id: light for light in lights}
        self.commands = []

    def get(self, id):
        return self._lights.get(id)

    def get_device(self, id):
        return None

    async def set_state(
        self,
        id,
        on=None,
        brightness=None,
        color_xy=None,
        color_temp=None,
        transition_time=None,
    ):
        self.commands.append(
            {
                "id": id,
                "on": on,
                "brightness": brightness,
                "color_xy": color_xy,
                "color_temp": color_temp,
                "transition_time": transition_time,
            }
        )
        light = self._lights[id]
        if on is not None:
            light.on.on = on
        if brightness is not None and light.dimming:
            light.dimming.brightness = brightness


class FakeBridge:
    def __init__(self, lights):
        self.lights = FakeLightsController(lights)
        self.sensors = object()  # no zigbee_connectivity attribute

    async def close(self):
        pass


def make_controller(lights, restore="smart", thinking=None, waiting=None):
    config = Config()
    config.target.mode = "lights"
    config.target.ids = [light.id for light in lights]
    config.animation.restore = restore
    if thinking is not None:
        config.roles.thinking = thinking
    if waiting is not None:
        config.roles.waiting = waiting
    controller = HueController(config, app_key="k")
    controller.bridge = FakeBridge(lights)
    controller._resolve_targets()

    async def _noop_connect():
        return None

    controller.connect = _noop_connect
    return controller


def COLOR():
    return FakeLight("c1", on=True, brightness=70, color=_Color(0.4, 0.4), ct=_CT(300))


def CT_ONLY():
    return FakeLight("t1", on=False, brightness=50, ct=_CT(370))


def DIM_ONLY():
    return FakeLight("d1", on=True, brightness=90)


class TestSnapshot:
    def test_snapshot_captures_state(self):
        controller = make_controller([COLOR(), CT_ONLY(), DIM_ONLY()])
        controller.take_snapshot()
        snap = controller._snapshot
        assert snap["c1"].on and snap["c1"].brightness == 70
        assert snap["c1"].color_xy == (0.4, 0.4)
        assert snap["c1"].supports_color and snap["c1"].supports_ct
        assert not snap["t1"].on
        assert snap["t1"].color_temp_mirek == 370 and not snap["t1"].supports_color
        assert snap["d1"].supports_dimming and not snap["d1"].supports_ct
        # snapshot persisted for daemonless `hue-agent restore`
        loaded = load_snapshot_file()
        assert loaded is not None
        assert set(loaded[0]) == {"c1", "t1", "d1"}

    def test_invalid_mirek_not_snapshotted(self):
        light = FakeLight("x", ct=_CT(300, valid=False))
        controller = make_controller([light])
        controller.take_snapshot()
        assert controller._snapshot["x"].color_temp_mirek is None


class TestWaitingLook:
    async def test_capability_aware_red(self):
        controller = make_controller([COLOR(), CT_ONLY(), DIM_ONLY()])
        controller.config.animation.wait_pulse_fallback = False
        controller.take_snapshot()
        controller.mode = "waiting"
        await controller._apply_waiting_look()
        commands = {c["id"]: c for c in controller.bridge.lights.commands}
        assert commands["c1"]["color_xy"] == RED_XY
        assert commands["t1"]["color_temp"] == WARMEST_MIREK
        assert commands["t1"]["color_xy"] is None
        assert (
            commands["d1"]["color_xy"] is None and commands["d1"]["color_temp"] is None
        )
        for c in commands.values():
            assert c["on"] is True
            assert c["brightness"] == controller.config.animation.wait_brightness


class TestRestore:
    async def test_restore_puts_lights_back(self):
        controller = make_controller([COLOR(), CT_ONLY()])
        controller.take_snapshot()
        await controller.restore()
        commands = {c["id"]: c for c in controller.bridge.lights.commands}
        # was on: brightness + color restored (mirek preferred when valid)
        assert commands["c1"]["on"] is True
        assert commands["c1"]["brightness"] == 70
        assert commands["c1"]["color_temp"] == 300
        # was off: turned back off
        assert commands["t1"]["on"] is False
        assert controller._snapshot == {}
        assert load_snapshot_file() is None

    async def test_restore_prefers_xy_when_no_valid_mirek(self):
        light = FakeLight(
            "c2", on=True, brightness=40, color=_Color(0.2, 0.3), ct=_CT(None)
        )
        controller = make_controller([light])
        controller.take_snapshot()
        await controller.restore()
        (command,) = controller.bridge.lights.commands
        assert command["color_xy"] == (0.2, 0.3)
        assert command["color_temp"] is None

    async def test_restore_never_policy_sends_nothing(self):
        controller = make_controller([COLOR()], restore="never")
        controller.take_snapshot()
        restored = await controller.restore()
        assert restored == 0
        assert controller.bridge.lights.commands == []


class TestSmartOverride:
    async def test_user_switching_light_off_stops_control(self):
        lights = [COLOR(), DIM_ONLY()]
        controller = make_controller(lights)
        controller.take_snapshot()
        controller.mode = "active"
        controller._mode_entered_at = time.monotonic() - 60  # past grace period
        lights[1].dimming.brightness = 50.0  # breathing has d1 in band by now
        lights[0].on.on = False  # user turns c1 off mid-session
        controller._check_overrides()
        assert "c1" not in controller._controlled
        assert "d1" in controller._controlled
        await controller.restore()  # smart: only d1 touched
        touched = {c["id"] for c in controller.bridge.lights.commands}
        assert touched == {"d1"}

    async def test_brightness_drift_stops_control(self):
        lights = [DIM_ONLY()]
        controller = make_controller(lights)
        controller.take_snapshot()
        controller.mode = "active"
        controller._mode_entered_at = time.monotonic() - 60
        lights[0].dimming.brightness = 100.0  # way above the breathing band
        controller._check_overrides()
        assert controller._controlled == set()

    async def test_breathing_band_is_not_an_override(self):
        lights = [DIM_ONLY()]
        controller = make_controller(lights)
        controller.take_snapshot()
        controller.mode = "active"
        controller._mode_entered_at = time.monotonic() - 60
        lights[0].dimming.brightness = 45.0  # inside min..max band
        controller._check_overrides()
        assert controller._controlled == {"d1"}

    async def test_always_policy_ignores_overrides(self):
        lights = [COLOR()]
        controller = make_controller(lights, restore="always")
        controller.take_snapshot()
        controller.mode = "active"
        controller._mode_entered_at = time.monotonic() - 60
        lights[0].on.on = False
        controller._check_overrides()  # no-op for non-smart policies
        await controller.restore()
        assert {c["id"] for c in controller.bridge.lights.commands} == {"c1"}


class TestApplyState:
    async def test_active_then_idle_round_trip(self):
        controller = make_controller([COLOR()])
        await controller.apply_state("active")
        assert controller.mode == "active"
        assert controller._breath_task is not None
        await controller.apply_state("idle")
        assert controller.mode == "idle"
        assert controller._breath_task is None
        # last command restores the original brightness
        last = controller.bridge.lights.commands[-1]
        assert last["brightness"] == 70

    async def test_waiting_stops_breathing(self):
        controller = make_controller([COLOR()])
        await controller.apply_state("active")
        await controller.apply_state("waiting")
        assert controller._breath_task is None
        assert controller.mode == "waiting"
        red = [c for c in controller.bridge.lights.commands if c["color_xy"] == RED_XY]
        assert red
        await controller.apply_state("idle")

    async def test_breathing_after_waiting_reapplies_original_color(self):
        """waiting -> active must not keep breathing in red."""
        controller = make_controller([COLOR()])
        await controller.apply_state("active")
        await controller.apply_state("waiting")  # paints the lamp red
        controller.bridge.lights.commands.clear()
        await controller.apply_state("active")  # resume breathing
        import asyncio

        # _prepare_breathing sits behind the 100 ms command rate limiter
        for _ in range(50):
            await asyncio.sleep(0.02)
            if controller.bridge.lights.commands:
                break
        commands = controller.bridge.lights.commands
        # the snapshot color (CT mode, mirek 300) is reapplied, replacing red
        assert any(c["color_temp"] == 300 for c in commands)
        assert not any(c["color_xy"] == RED_XY for c in commands)
        await controller.apply_state("idle")


class TestRoles:
    """Disjoint thinking/waiting sets: handoffs restore the bulbs leaving."""

    def _controller(self):
        # c1 breathes while thinking; d1 goes bright when waiting.
        lights = [COLOR(), DIM_ONLY()]
        return make_controller(lights, thinking=["hue:c1"], waiting=["hue:d1"])

    def test_default_roles_cover_all_targets(self):
        controller = make_controller([COLOR(), DIM_ONLY()])
        assert controller._thinking_ids == ["c1", "d1"]
        assert controller._waiting_ids == ["c1", "d1"]

    def test_unknown_role_light_dropped_with_warning(self):
        controller = make_controller(
            [COLOR()], thinking=["hue:ghost"], waiting=["hue:c1"]
        )
        assert controller._thinking_ids == []
        assert controller._waiting_ids == ["c1"]

    async def test_snapshot_covers_union_of_roles(self):
        controller = self._controller()
        await controller.apply_state("active")
        assert set(controller._snapshot) == {"c1", "d1"}
        await controller.apply_state("idle")

    async def test_active_drives_only_thinking_lights(self):
        controller = self._controller()
        await controller.apply_state("active")
        import asyncio

        for _ in range(50):
            await asyncio.sleep(0.02)
            if controller.bridge.lights.commands:
                break
        touched = {c["id"] for c in controller.bridge.lights.commands}
        assert "c1" in touched and "d1" not in touched
        await controller.apply_state("idle")

    async def test_waiting_reds_waiting_set_and_restores_thinking_set(self):
        controller = self._controller()
        controller.config.animation.wait_pulse_fallback = False
        await controller.apply_state("active")
        controller.bridge.lights.commands.clear()
        await controller.apply_state("waiting")
        commands = controller.bridge.lights.commands
        by_id = {}
        for c in commands:
            by_id.setdefault(c["id"], []).append(c)
        # c1 (thinking-only) went back to its snapshot brightness, not red
        assert any(c["brightness"] == 70 for c in by_id["c1"])
        assert not any(c["color_xy"] == RED_XY for c in by_id["c1"])
        # d1 (waiting-only, dimmable) got the wait brightness
        wait_b = controller.config.animation.wait_brightness
        assert any(c["brightness"] == wait_b for c in by_id["d1"])
        await controller.apply_state("idle")

    async def test_back_to_active_restores_waiting_only_bulb(self):
        controller = self._controller()
        controller.config.animation.wait_pulse_fallback = False
        await controller.apply_state("active")
        await controller.apply_state("waiting")
        controller.bridge.lights.commands.clear()
        await controller.apply_state("active")
        commands = controller.bridge.lights.commands
        d1 = [c for c in commands if c["id"] == "d1"]
        # d1 left the waiting look and returned to its snapshot brightness
        assert any(c["brightness"] == 90 for c in d1)
        await controller.apply_state("idle")

    async def test_idle_restores_the_whole_union(self):
        controller = self._controller()
        controller.config.animation.wait_pulse_fallback = False
        await controller.apply_state("waiting")
        controller.bridge.lights.commands.clear()
        await controller.apply_state("idle")
        touched = {c["id"] for c in controller.bridge.lights.commands}
        assert touched == {"c1", "d1"}

    async def test_waiting_only_bulb_at_snapshot_is_not_an_override(self):
        controller = self._controller()
        controller.take_snapshot()
        controller.mode = "active"
        controller._mode_entered_at = time.monotonic() - 60
        # d1 sits at its snapshot brightness (90), far above the breath band —
        # but it is not driven in active mode, so it must stay controlled.
        controller._check_overrides()
        assert "d1" in controller._controlled


class TestWaitColor:
    async def test_wait_color_purple_reaches_lights(self):
        controller = make_controller([COLOR()])
        controller.config.animation.wait_color = "purple"
        controller.take_snapshot()
        controller.mode = "waiting"
        await controller._apply_waiting_look()
        (command,) = controller.bridge.lights.commands
        assert command["color_xy"] != RED_XY
        x, y = command["color_xy"]
        assert 0 < x < 1 and 0 < y < 1

    async def test_invalid_wait_color_falls_back_to_red(self):
        controller = make_controller([COLOR()])
        controller.config.animation.wait_color = "plaid"
        controller.take_snapshot()
        controller.mode = "waiting"
        await controller._apply_waiting_look()
        (command,) = controller.bridge.lights.commands
        assert command["color_xy"] == RED_XY


class TestUpdateConfig:
    async def test_idle_reload_just_swaps_config(self):
        controller = make_controller([COLOR()])
        new_config = Config()
        new_config.target.mode = "lights"
        new_config.target.ids = ["c1"]
        await controller.update_config(new_config)
        assert controller.config is new_config
        assert controller.bridge.lights.commands == []

    async def test_reload_while_active_adds_and_removes_lights(self):
        lights = [COLOR(), CT_ONLY(), DIM_ONLY()]
        controller = make_controller(lights, thinking=["hue:c1"], waiting=["hue:c1"])
        await controller.apply_state("active")
        assert set(controller._snapshot) == {"c1"}
        controller.bridge.lights.commands.clear()

        new_config = Config()
        new_config.target.mode = "lights"
        new_config.target.ids = ["c1", "t1", "d1"]
        new_config.roles.thinking = ["hue:d1"]  # c1 out, d1 in
        new_config.roles.waiting = ["hue:d1"]
        await controller.update_config(new_config)
        # c1 left the union: restored to its snapshot and forgotten
        assert "c1" not in controller._snapshot
        c1_cmds = [c for c in controller.bridge.lights.commands if c["id"] == "c1"]
        assert any(c["brightness"] == 70 for c in c1_cmds)
        # d1 joined: snapshotted before being driven
        assert "d1" in controller._snapshot
        assert controller._snapshot["d1"].brightness == 90
        await controller.apply_state("idle")

    async def test_reload_while_waiting_reapplies_wait_color(self):
        controller = make_controller([COLOR()])
        await controller.apply_state("waiting")
        controller.bridge.lights.commands.clear()
        new_config = Config()
        new_config.target.mode = "lights"
        new_config.target.ids = ["c1"]
        new_config.animation.wait_color = "0.2,0.3"
        await controller.update_config(new_config)
        assert any(
            c["color_xy"] == (0.2, 0.3) for c in controller.bridge.lights.commands
        )
        await controller.apply_state("idle")


class TestSnapshotRecovery:
    async def test_fresh_controller_restores_from_persisted_snapshot(self):
        """A daemon restart must not delete the only copy of the snapshot."""
        lights = [COLOR(), CT_ONLY()]
        first = make_controller(lights)
        first.take_snapshot()  # persists snapshot.json

        second = make_controller(lights)  # fresh: empty in-memory snapshot
        restored = await second.restore(policy="always")
        assert restored == 2
        commands = {c["id"]: c for c in second.bridge.lights.commands}
        assert commands["c1"]["brightness"] == 70
        assert commands["t1"]["on"] is False
        assert load_snapshot_file() is None  # cleared only after restoring
