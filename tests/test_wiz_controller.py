"""WizController against a fake UDP transport: snapshot, looks, restore."""

import asyncio
import copy
import time

from hue_agent_status.backends.wiz import WizController, load_snapshot_file
from hue_agent_status.backends.wiz_protocol import WizTimeoutError
from hue_agent_status.config import Config, WizBulbConfig

RGB_MAC = "aa0000000001"
TW_MAC = "aa0000000002"
DW_MAC = "aa0000000003"


def rgb_bulb(ip="192.0.2.41"):
    return ip, {
        "mac": RGB_MAC,
        "module": "ESP01_SHRGB1C_31",
        "pilot": {"state": True, "dimming": 80, "r": 255, "g": 200, "b": 100},
    }


def tw_bulb(ip="192.0.2.42"):
    return ip, {
        "mac": TW_MAC,
        "module": "ESP56_SHTW3_01",
        "pilot": {"state": True, "dimming": 50, "temp": 2700},
    }


def dw_bulb(ip="192.0.2.43"):
    return ip, {
        "mac": DW_MAC,
        "module": "ESP01_SHDW1_31",
        "pilot": {"state": False, "dimming": 90},
    }


class FakeWizTransport:
    """In-memory bulbs keyed by IP; records every message like the Hue fake."""

    def __init__(self, bulbs):
        self.bulbs = dict(bulbs)
        self.commands = []
        self.offline = set()
        self.closed = False

    async def send_command(self, ip, message, timeout=1.0, retries=3):
        self.commands.append({"ip": ip, **message})
        if ip in self.offline or ip not in self.bulbs:
            raise WizTimeoutError(f"no reply from {ip}")
        bulb = self.bulbs[ip]
        method = message.get("method")
        if method == "getSystemConfig":
            return {"mac": bulb["mac"], "moduleName": bulb["module"]}
        if method == "getPilot":
            return dict(bulb["pilot"])
        if method == "setPilot":
            bulb["pilot"].update(message.get("params", {}))
            return {"success": True}
        return {}

    async def discover(self, broadcast="255.255.255.255", wait=2.0):
        return sorted(
            (bulb["mac"], ip)
            for ip, bulb in self.bulbs.items()
            if ip not in self.offline
        )

    def close(self):
        self.closed = True


def make_controller(bulb_specs, thinking=None, waiting=None, restore="smart"):
    config = Config()
    config.animation.restore = restore
    transport = FakeWizTransport(dict(bulb_specs))
    for ip, bulb in bulb_specs:
        config.wiz.bulbs.append(
            WizBulbConfig(mac=bulb["mac"], ip=ip, name=f"bulb-{bulb['mac'][-1]}")
        )
    if thinking is not None:
        config.roles.thinking = thinking
    if waiting is not None:
        config.roles.waiting = waiting
    return WizController(config, transport=transport)


def set_pilots(controller):
    return [c for c in controller.transport.commands if c.get("method") == "setPilot"]


def pause_next_get_pilot(monkeypatch, transport):
    """Return events that expose a captured getPilot result after a state change."""
    original_send = transport.send_command
    started = asyncio.Event()
    release = asyncio.Event()
    pending = True

    async def delayed_send(ip, message, timeout=1.0, retries=3):
        nonlocal pending
        if pending and message.get("method") == "getPilot":
            pending = False
            result = await original_send(ip, message, timeout=timeout, retries=retries)
            started.set()
            await release.wait()
            return result
        return await original_send(ip, message, timeout=timeout, retries=retries)

    monkeypatch.setattr(transport, "send_command", delayed_send)
    return started, release


class TestConnect:
    async def test_connect_resolves_ips_and_capabilities(self):
        controller = make_controller([rgb_bulb(), tw_bulb()])
        await controller.connect()
        assert controller._ips[RGB_MAC] == "192.0.2.41"
        assert controller._caps[RGB_MAC].supports_color
        assert not controller._caps[TW_MAC].supports_color

    async def test_all_offline_raises(self):
        controller = make_controller([rgb_bulb()])
        controller.transport.offline.add("192.0.2.41")
        import pytest

        from hue_agent_status.backends.wiz import WizUnavailableError

        with pytest.raises(WizUnavailableError):
            await controller.connect()

    async def test_stale_ip_recovered_via_discovery(self):
        # Config says .41, but the bulb now lives at .99 (DHCP moved it).
        ip, bulb = rgb_bulb("192.0.2.99")
        controller = make_controller([(ip, bulb)])
        controller.config.wiz.bulbs[0].ip = "192.0.2.41"
        await controller.connect()
        assert controller._ips[RGB_MAC] == "192.0.2.99"

    async def test_stale_cache_falls_back_to_configured_ip(self, monkeypatch):
        controller = make_controller([rgb_bulb("192.0.2.99")])
        monkeypatch.setattr(
            "hue_agent_status.backends.wiz._load_ip_cache",
            lambda: {RGB_MAC: "192.0.2.41"},
        )

        async def blocked_discovery(*args, **kwargs):
            raise OSError("broadcast unavailable")

        monkeypatch.setattr(controller.transport, "discover", blocked_discovery)
        await controller.connect()
        assert controller._ips[RGB_MAC] == "192.0.2.99"

    async def test_one_offline_bulb_does_not_block_connect(self):
        controller = make_controller([rgb_bulb(), tw_bulb()])
        controller.transport.offline.add("192.0.2.42")
        await controller.connect()
        assert RGB_MAC in controller._caps
        assert controller._thinking_ids == [RGB_MAC]


class TestSnapshotAndRestore:
    async def test_snapshot_persists_and_restore_puts_pilots_back(self):
        controller = make_controller([rgb_bulb(), dw_bulb()])
        await controller.connect()
        await controller.take_snapshot()
        assert set(controller._snapshot) == {RGB_MAC, DW_MAC}
        loaded = load_snapshot_file()
        assert loaded is not None and set(loaded[0]) == {RGB_MAC, DW_MAC}

        controller.transport.commands.clear()
        restored = await controller.restore()
        assert restored == 2
        commands = {c["ip"]: c for c in set_pilots(controller)}
        rgb_params = commands["192.0.2.41"]["params"]
        assert rgb_params["state"] is True
        assert (rgb_params["r"], rgb_params["g"], rgb_params["b"]) == (255, 200, 100)
        assert commands["192.0.2.43"]["params"] == {"state": False}
        assert load_snapshot_file() is None

    async def test_restore_never_policy_sends_nothing(self):
        controller = make_controller([rgb_bulb()], restore="never")
        await controller.connect()
        await controller.take_snapshot()
        controller.transport.commands.clear()
        assert await controller.restore() == 0
        assert set_pilots(controller) == []


class TestWaitingLook:
    async def test_capability_aware_waiting(self):
        controller = make_controller([rgb_bulb(), tw_bulb(), dw_bulb()])
        controller.config.animation.wait_pulse_fallback = False
        await controller.apply_state("waiting")
        commands = {c["ip"]: c["params"] for c in set_pilots(controller)}
        wait_b = controller.config.animation.wait_brightness
        rgb_params = commands["192.0.2.41"]
        assert (rgb_params["r"], rgb_params["g"], rgb_params["b"]) == (255, 0, 0)
        assert rgb_params["dimming"] == wait_b
        assert commands["192.0.2.42"]["temp"] == 2200
        assert "r" not in commands["192.0.2.42"]
        assert "temp" not in commands["192.0.2.43"]
        assert commands["192.0.2.43"]["dimming"] == wait_b
        await controller.apply_state("idle")

    async def test_wait_color_applies_to_wiz(self):
        controller = make_controller([rgb_bulb()])
        controller.config.animation.wait_color = "blue"
        await controller.apply_state("waiting")
        (command,) = [c for c in set_pilots(controller) if c["ip"] == "192.0.2.41"]
        assert command["params"]["b"] == 255 and command["params"]["r"] == 0
        await controller.apply_state("idle")

    async def test_offline_bulb_does_not_block_the_others(self):
        controller = make_controller([rgb_bulb(), tw_bulb()])
        await controller.connect()
        controller.transport.offline.add("192.0.2.42")
        await controller.apply_state("waiting")
        ips = {c["ip"] for c in set_pilots(controller)}
        assert "192.0.2.41" in ips
        await controller.apply_state("idle")


class TestBreathing:
    async def test_active_dims_only_thinking_bulbs(self):
        controller = make_controller(
            [rgb_bulb(), tw_bulb()],
            thinking=[f"wiz:{RGB_MAC}"],
            waiting=[f"wiz:{TW_MAC}"],
        )
        await controller.apply_state("active")
        for _ in range(50):
            await asyncio.sleep(0.02)
            if set_pilots(controller):
                break
        ips = {c["ip"] for c in set_pilots(controller)}
        assert ips == {"192.0.2.41"}
        await controller.apply_state("idle")

    async def test_dimming_respects_firmware_floor(self):
        controller = make_controller([rgb_bulb()])
        controller.config.animation.breath_min_brightness = 2.0
        controller.config.animation.breath_max_brightness = 20.0
        await controller.apply_state("active")
        for _ in range(50):
            await asyncio.sleep(0.02)
            if set_pilots(controller):
                break
        assert all(c["params"]["dimming"] >= 10 for c in set_pilots(controller))
        await controller.apply_state("idle")


class TestRoleHandoff:
    async def test_waiting_restores_thinking_only_bulb(self):
        controller = make_controller(
            [rgb_bulb(), tw_bulb()],
            thinking=[f"wiz:{RGB_MAC}"],
            waiting=[f"wiz:{TW_MAC}"],
        )
        await controller.apply_state("active")
        controller.transport.commands.clear()
        await controller.apply_state("waiting")
        rgb_commands = [c for c in set_pilots(controller) if c["ip"] == "192.0.2.41"]
        # the rgb bulb went back to its snapshot color, not to red
        assert any(
            c["params"].get("r") == 255 and c["params"].get("g") == 200
            for c in rgb_commands
        )
        assert not any(c["params"].get("g") == 0 for c in rgb_commands)
        await controller.apply_state("idle")


class TestSmartOverride:
    async def test_user_turning_bulb_off_stops_control(self):
        controller = make_controller([rgb_bulb(), tw_bulb()])
        await controller.apply_state("waiting")
        controller.transport.bulbs["192.0.2.41"]["pilot"]["state"] = False
        controller._mode_entered_at = time.monotonic() - 60
        controller._last_override_check = 0.0
        await controller._check_overrides()
        assert RGB_MAC not in controller._controlled
        assert TW_MAC in controller._controlled
        assert load_snapshot_file()[1] == {TW_MAC}
        await controller.apply_state("idle")

    async def test_waiting_override_is_checked_before_idle_transition(self):
        controller = make_controller([rgb_bulb()])
        await controller.apply_state("waiting")
        controller.transport.bulbs["192.0.2.41"]["pilot"]["state"] = False
        controller._mode_entered_at = time.monotonic() - 60
        controller._last_override_check = 0.0
        controller.transport.commands.clear()

        await asyncio.wait_for(controller.apply_state("idle"), timeout=1)

        assert set_pilots(controller) == []
        assert not controller.transport.bulbs["192.0.2.41"]["pilot"]["state"]

    async def test_waiting_override_is_checked_before_direct_smart_restore(self):
        controller = make_controller([rgb_bulb()], restore="always")
        await controller.apply_state("waiting")
        controller.transport.bulbs["192.0.2.41"]["pilot"]["state"] = False
        controller._mode_entered_at = time.monotonic() - 60
        controller._last_override_check = 0.0
        controller.transport.commands.clear()

        restored = await asyncio.wait_for(controller.restore(policy="smart"), timeout=1)

        assert restored == 0
        assert set_pilots(controller) == []
        assert not controller.transport.bulbs["192.0.2.41"]["pilot"]["state"]

    async def test_stale_override_poll_cannot_recreate_restored_snapshot(
        self, monkeypatch
    ):
        controller = make_controller([rgb_bulb()])
        await controller.apply_state("waiting")
        controller.transport.bulbs["192.0.2.41"]["pilot"]["state"] = False
        controller._mode_entered_at = time.monotonic() - 60
        controller._last_override_check = 0.0
        started, release = pause_next_get_pilot(monkeypatch, controller.transport)

        check_task = asyncio.create_task(controller._check_overrides())
        await asyncio.wait_for(started.wait(), timeout=1)
        await controller.restore()
        assert load_snapshot_file() is None

        release.set()
        await check_task
        assert controller.mode == "idle"
        assert controller._snapshot == {}
        assert controller._controlled == set()
        assert load_snapshot_file() is None

    async def test_stale_override_poll_is_ignored_after_same_mode_reload(
        self, monkeypatch
    ):
        controller = make_controller([rgb_bulb()])
        await controller.apply_state("waiting")
        controller.transport.bulbs["192.0.2.41"]["pilot"]["state"] = False
        controller._mode_entered_at = time.monotonic() - 60
        controller._last_override_check = 0.0
        started, release = pause_next_get_pilot(monkeypatch, controller.transport)

        check_task = asyncio.create_task(controller._check_overrides())
        await asyncio.wait_for(started.wait(), timeout=1)
        await controller.update_config(copy.deepcopy(controller.config))
        release.set()
        await check_task

        assert controller.mode == "waiting"
        assert RGB_MAC in controller._controlled
        assert load_snapshot_file()[1] == {RGB_MAC}
