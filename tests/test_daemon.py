"""Daemon HTTP API: auth, validation, payload cap, and state orchestration."""

import asyncio
import json
import time

import pytest
from aiohttp.test_utils import TestClient, TestServer

from hue_agent_status.config import Config
from hue_agent_status.daemon import Daemon
from hue_agent_status.events import NormalizedEvent

TOKEN = "test-token-123"


class FakeController:
    """Records apply_state calls; no bridge required."""

    def __init__(self):
        self.states = []
        self.blinks = []
        self.restored = 0
        self.mode = "idle"
        self.configs = []

    async def apply_state(self, aggregate):
        self.mode = aggregate
        self.states.append(aggregate)

    async def blink_green(self, times=5):
        self.blinks.append(times)

    async def restore(self, transition_ms=None, policy=None):
        self.restored += 1
        self.mode = "idle"
        return 2

    async def update_config(self, config):
        self.configs.append(config)

    async def close(self):
        pass

    def runtime_status(self):
        return {"fake": {"mode": self.mode}}


@pytest.fixture
def config():
    cfg = Config()
    cfg.daemon.idle_grace_seconds = 0.0
    return cfg


@pytest.fixture
async def client(config):
    daemon = Daemon(config, controller=FakeController(), token=TOKEN)
    test_client = TestClient(TestServer(daemon.make_app()))
    await test_client.start_server()
    test_client.daemon_obj = daemon
    yield test_client
    await test_client.close()


def _auth(token=TOKEN):
    return {"Authorization": f"Bearer {token}"}


def _event(
    source="claude",
    session="s1",
    state="active",
    event="UserPromptSubmit",
    **extra,
):
    return {
        "source": source,
        "session_id": session,
        "state": state,
        "event": event,
        **extra,
    }


class TestAuth:
    async def test_missing_token_rejected(self, client):
        resp = await client.post("/event", json=_event())
        assert resp.status == 401

    async def test_wrong_token_rejected(self, client):
        resp = await client.post("/event", json=_event(), headers=_auth("nope"))
        assert resp.status == 401

    async def test_health_requires_token_too(self, client):
        resp = await client.get("/health")
        assert resp.status == 401

    async def test_valid_token_accepted(self, client):
        resp = await client.post("/event", json=_event(), headers=_auth())
        assert resp.status == 200


class TestEventEndpoint:
    async def test_event_updates_aggregate(self, client):
        resp = await client.post(
            "/event", json=_event(state="waiting"), headers=_auth()
        )
        body = await resp.json()
        assert body == {"ok": True, "aggregate": "waiting"}

    async def test_rejects_bad_source(self, client):
        resp = await client.post(
            "/event", json=_event(source="skynet"), headers=_auth()
        )
        assert resp.status == 400

    async def test_rejects_bad_state(self, client):
        resp = await client.post(
            "/event", json=_event(state="confused"), headers=_auth()
        )
        assert resp.status == 400

    async def test_rejects_subsession_without_identifier(self, client):
        resp = await client.post(
            "/event", json=_event(scope="subsession"), headers=_auth()
        )
        assert resp.status == 400

    async def test_rejects_missing_session(self, client):
        payload = {"source": "claude", "state": "active"}
        resp = await client.post("/event", json=payload, headers=_auth())
        assert resp.status == 400

    async def test_rejects_non_json(self, client):
        resp = await client.post(
            "/event",
            data=b"\x00\x01not json",
            headers={**_auth(), "Content-Type": "application/json"},
        )
        assert resp.status == 400

    async def test_rejects_oversized_payload(self, client):
        payload = _event()
        payload["padding"] = "x" * (70 * 1024)
        resp = await client.post(
            "/event",
            data=json.dumps(payload).encode(),
            headers={**_auth(), "Content-Type": "application/json"},
        )
        assert resp.status == 413

    async def test_ended_clears_session(self, client):
        await client.post("/event", json=_event(state="active"), headers=_auth())
        resp = await client.post("/event", json=_event(state="ended"), headers=_auth())
        body = await resp.json()
        assert body["aggregate"] == "idle"


class TestHealth:
    async def test_health_shape(self, client):
        await client.post("/event", json=_event(state="active"), headers=_auth())
        resp = await client.get("/health", headers=_auth())
        body = await resp.json()
        assert body["ok"] is True
        assert body["aggregate"] == "active"
        assert body["sessions"][0]["source"] == "claude"
        assert body["backends"]["fake"]["mode"] == "idle"
        assert "version" in body and "pid" in body


class TestRestoreAndShutdown:
    async def test_restore_clears_sessions_and_restores(self, client):
        await client.post("/event", json=_event(state="waiting"), headers=_auth())
        resp = await client.post("/restore", json={}, headers=_auth())
        body = await resp.json()
        assert body["ok"] is True
        assert client.daemon_obj.registry.aggregate() == "idle"
        assert client.daemon_obj.controller.restored == 1

    async def test_shutdown_sets_stop_flag(self, client):
        resp = await client.post("/shutdown", headers=_auth())
        assert (await resp.json())["ok"] is True
        assert client.daemon_obj._stopping.is_set()


class TestReload:
    async def test_reload_reads_config_and_updates_controller(self, client):
        from hue_agent_status.config import Config, save_config

        cfg = Config()
        cfg.daemon.completion_hold_seconds = 42
        save_config(cfg)
        resp = await client.post("/reload", headers=_auth())
        body = await resp.json()
        assert body["ok"] is True
        daemon = client.daemon_obj
        assert daemon.config.daemon.completion_hold_seconds == 42
        assert daemon.registry.completion_ttl == 42
        assert len(daemon.controller.configs) == 1

    async def test_reload_rejects_invalid_config_and_keeps_old(self, client):
        from hue_agent_status.config import config_path

        path = config_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("[daemon]\nhost = '0.0.0.0'\n")
        resp = await client.post("/reload", headers=_auth())
        assert resp.status == 400
        assert client.daemon_obj.config.daemon.host == "127.0.0.1"
        assert client.daemon_obj.controller.configs == []

    async def test_reload_notes_daemon_port_change(self, client):
        from hue_agent_status.config import Config, save_config

        cfg = Config()
        cfg.daemon.port = 9999
        save_config(cfg)
        resp = await client.post("/reload", headers=_auth())
        body = await resp.json()
        assert "restart" in body.get("note", "")

    async def test_reload_note_redacts_controller_error_details(self, client):
        from hue_agent_status.config import Config, save_config

        private_detail = "bridge failed at 192.0.2.123"

        async def broken_update(config):
            raise RuntimeError(private_detail)

        client.daemon_obj.controller.update_config = broken_update
        save_config(Config())
        resp = await client.post("/reload", headers=_auth())
        body = await resp.json()
        assert body["ok"] is True
        assert private_detail not in body.get("note", "")
        assert "private daemon log" in body.get("note", "")

    async def test_health_reports_config_mtime(self, client):
        from hue_agent_status.config import Config, save_config

        save_config(Config())
        await client.post("/reload", headers=_auth())
        resp = await client.get("/health", headers=_auth())
        body = await resp.json()
        assert body["config_mtime"] is not None


class TestOrchestration:
    async def test_same_steady_state_is_refreshed_for_backend_recovery(self, config):
        daemon = Daemon(config, controller=FakeController(), token=TOKEN)
        daemon.registry.apply_event(
            NormalizedEvent(
                source="claude",
                session_id="s1",
                state="waiting",
                event="PermissionRequest",
            )
        )

        await daemon._apply_aggregate()
        await daemon._apply_aggregate()

        assert daemon.controller.states == ["waiting", "waiting"]

    async def test_aggregate_transitions_drive_controller(self, config):
        daemon = Daemon(config, controller=FakeController(), token=TOKEN)
        orchestrator = asyncio.create_task(daemon._orchestrate())
        client = TestClient(TestServer(daemon.make_app()))
        await client.start_server()
        try:
            await client.post("/event", json=_event(state="active"), headers=_auth())
            await asyncio.sleep(0.05)
            assert daemon.controller.states == ["active"]

            await client.post("/event", json=_event(state="waiting"), headers=_auth())
            await asyncio.sleep(0.05)
            assert daemon.controller.states == ["active", "waiting"]

            await client.post("/event", json=_event(state="ended"), headers=_auth())
            await asyncio.sleep(0.05)
            assert daemon.controller.states == ["active", "waiting", "idle"]
        finally:
            daemon._stopping.set()
            daemon._wake.set()
            orchestrator.cancel()
            await client.close()

    async def test_waiting_wins_across_sessions(self, config):
        daemon = Daemon(config, controller=FakeController(), token=TOKEN)
        orchestrator = asyncio.create_task(daemon._orchestrate())
        client = TestClient(TestServer(daemon.make_app()))
        await client.start_server()
        try:
            await client.post("/event", json=_event(session="a"), headers=_auth())
            await client.post(
                "/event",
                json=_event(source="codex", session="b", state="waiting"),
                headers=_auth(),
            )
            await asyncio.sleep(0.05)
            assert daemon.controller.mode == "waiting"
            # ending the waiting session drops us back to active
            await client.post(
                "/event",
                json=_event(source="codex", session="b", state="ended"),
                headers=_auth(),
            )
            await asyncio.sleep(0.05)
            assert daemon.controller.mode == "active"
        finally:
            daemon._stopping.set()
            daemon._wake.set()
            orchestrator.cancel()
            await client.close()

    async def test_idle_grace_defers_restore(self, config):
        config.daemon.idle_grace_seconds = 0.2
        daemon = Daemon(config, controller=FakeController(), token=TOKEN)
        orchestrator = asyncio.create_task(daemon._orchestrate())
        client = TestClient(TestServer(daemon.make_app()))
        await client.start_server()
        try:
            await client.post("/event", json=_event(state="active"), headers=_auth())
            await asyncio.sleep(0.05)
            await client.post("/event", json=_event(state="ended"), headers=_auth())
            # a new prompt arrives within the grace window
            await asyncio.sleep(0.05)
            await client.post("/event", json=_event(state="active"), headers=_auth())
            await asyncio.sleep(0.3)
            assert "idle" not in daemon.controller.states
        finally:
            daemon._stopping.set()
            daemon._wake.set()
            orchestrator.cancel()
            await client.close()

    async def test_parent_completion_waits_for_child_then_blinks_and_goes_green(
        self, config
    ):
        daemon = Daemon(config, controller=FakeController(), token=TOKEN)
        orchestrator = asyncio.create_task(daemon._orchestrate())
        client = TestClient(TestServer(daemon.make_app()))
        await client.start_server()
        child = {"scope": "subsession", "subsession_id": "child-1"}
        try:
            await client.post("/event", json=_event(), headers=_auth())
            await client.post(
                "/event",
                json=_event(event="SubagentStart", **child),
                headers=_auth(),
            )
            response = await client.post(
                "/event",
                json=_event(state="complete", event="Stop", pending_work=[]),
                headers=_auth(),
            )
            assert (await response.json())["aggregate"] == "active"

            response = await client.post(
                "/event",
                json=_event(state="complete", event="SubagentStop", **child),
                headers=_auth(),
            )
            assert (await response.json())["aggregate"] == "complete"
            await asyncio.sleep(0.05)
            assert daemon.controller.mode == "complete"
            assert daemon.controller.blinks == [5]

            # A duplicate stop must not play the transient twice.
            await client.post(
                "/event",
                json=_event(state="complete", event="SubagentStop", **child),
                headers=_auth(),
            )
            await asyncio.sleep(0.05)
            assert daemon.controller.blinks == [5]
        finally:
            daemon._stopping.set()
            daemon._wake.set()
            orchestrator.cancel()
            await client.close()

    async def test_master_stop_waits_for_all_five_child_flashes_then_starts_hold(
        self, config
    ):
        controller = FakeController()
        blink_started = asyncio.Event()
        finish_blink = asyncio.Event()
        blink_cancelled = asyncio.Event()

        async def blocking_blink(times=5):
            controller.blinks.append(times)
            blink_started.set()
            try:
                await finish_blink.wait()
            except asyncio.CancelledError:
                blink_cancelled.set()
                raise

        controller.blink_green = blocking_blink
        daemon = Daemon(config, controller=controller, token=TOKEN)
        orchestrator = asyncio.create_task(daemon._orchestrate())
        client = TestClient(TestServer(daemon.make_app()))
        await client.start_server()
        child = {"scope": "subsession", "subsession_id": "child-1"}
        try:
            await client.post("/event", json=_event(), headers=_auth())
            await client.post(
                "/event", json=_event(event="SubagentStart", **child), headers=_auth()
            )
            await client.post(
                "/event",
                json=_event(state="complete", event="SubagentStop", **child),
                headers=_auth(),
            )
            await asyncio.wait_for(blink_started.wait(), timeout=1)

            response = await client.post(
                "/event",
                json=_event(state="complete", event="Stop", pending_work=[]),
                headers=_auth(),
            )
            assert (await response.json())["aggregate"] == "complete"
            await asyncio.sleep(0.02)
            assert not blink_cancelled.is_set()
            assert controller.mode == "active"

            release_time = time.time()
            finish_blink.set()
            async with asyncio.timeout(1):
                while controller.mode != "complete" or daemon._effect_task is not None:
                    await asyncio.sleep(0)

            session = daemon.registry._sessions[("claude", "s1")]
            assert controller.blinks == [5]
            assert not blink_cancelled.is_set()
            assert session.completion_started_at >= release_time
        finally:
            daemon._stopping.set()
            daemon._wake.set()
            orchestrator.cancel()
            await client.close()

    async def test_lone_child_completion_still_plays_one_signal(self, config):
        daemon = Daemon(config, controller=FakeController(), token=TOKEN)
        orchestrator = asyncio.create_task(daemon._orchestrate())
        client = TestClient(TestServer(daemon.make_app()))
        await client.start_server()
        try:
            response = await client.post(
                "/event",
                json=_event(
                    state="complete",
                    event="SubagentStop",
                    scope="subsession",
                    subsession_id="orphan-child",
                ),
                headers=_auth(),
            )
            assert (await response.json())["aggregate"] == "idle"
            await asyncio.sleep(0.05)
            assert daemon.controller.blinks == [5]
        finally:
            daemon._stopping.set()
            daemon._wake.set()
            orchestrator.cancel()
            await client.close()

    async def test_waiting_preempts_an_in_progress_green_blink(self, config):
        controller = FakeController()
        blink_started = asyncio.Event()
        blink_cancelled = asyncio.Event()

        async def blocking_blink(times=5):
            controller.blinks.append(times)
            blink_started.set()
            try:
                await asyncio.Future()
            finally:
                blink_cancelled.set()

        controller.blink_green = blocking_blink
        daemon = Daemon(config, controller=controller, token=TOKEN)
        orchestrator = asyncio.create_task(daemon._orchestrate())
        client = TestClient(TestServer(daemon.make_app()))
        await client.start_server()
        child = {"scope": "subsession", "subsession_id": "child-1"}
        try:
            await client.post("/event", json=_event(), headers=_auth())
            await client.post(
                "/event",
                json=_event(event="SubagentStart", **child),
                headers=_auth(),
            )
            await client.post(
                "/event",
                json=_event(state="complete", event="SubagentStop", **child),
                headers=_auth(),
            )
            await asyncio.wait_for(blink_started.wait(), timeout=1)

            await client.post(
                "/event",
                json=_event(state="waiting", event="PermissionRequest"),
                headers=_auth(),
            )
            await asyncio.wait_for(blink_cancelled.wait(), timeout=1)
            await asyncio.sleep(0.05)
            assert controller.mode == "waiting"
            assert controller.states[-1] == "waiting"
            assert daemon._effect_task is None
            assert daemon._pending_completion_effects == 0
        finally:
            daemon._stopping.set()
            daemon._wake.set()
            orchestrator.cancel()
            await client.close()

    async def test_new_prompt_cancels_blink_without_replaying_it(self, config):
        controller = FakeController()
        blink_started = asyncio.Event()

        async def blocking_blink(times=5):
            controller.blinks.append(times)
            blink_started.set()
            await asyncio.Future()

        controller.blink_green = blocking_blink
        daemon = Daemon(config, controller=controller, token=TOKEN)
        orchestrator = asyncio.create_task(daemon._orchestrate())
        client = TestClient(TestServer(daemon.make_app()))
        await client.start_server()
        child = {"scope": "subsession", "subsession_id": "child-1"}
        try:
            await client.post("/event", json=_event(), headers=_auth())
            await client.post(
                "/event",
                json=_event(event="SubagentStart", **child),
                headers=_auth(),
            )
            await client.post(
                "/event",
                json=_event(state="complete", event="SubagentStop", **child),
                headers=_auth(),
            )
            await asyncio.wait_for(blink_started.wait(), timeout=1)

            await client.post(
                "/event", json=_event(event="UserPromptSubmit"), headers=_auth()
            )
            await asyncio.sleep(0.05)

            assert controller.blinks == [5]
            assert daemon._pending_completion_effects == 0
            assert daemon._effect_task is None
            assert controller.mode == "active"
        finally:
            daemon._stopping.set()
            daemon._wake.set()
            orchestrator.cancel()
            await client.close()

    async def test_completion_hold_expires_without_idle_grace(self, config):
        config.daemon.completion_hold_seconds = 0.05
        config.daemon.idle_grace_seconds = 1
        daemon = Daemon(config, controller=FakeController(), token=TOKEN)
        orchestrator = asyncio.create_task(daemon._orchestrate())
        client = TestClient(TestServer(daemon.make_app()))
        await client.start_server()
        try:
            await client.post(
                "/event",
                json=_event(state="complete", event="Stop", pending_work=[]),
                headers=_auth(),
            )
            await asyncio.sleep(0.02)
            assert daemon.controller.mode == "complete"
            await asyncio.sleep(0.08)
            assert daemon.controller.mode == "idle"
        finally:
            daemon._stopping.set()
            daemon._wake.set()
            orchestrator.cancel()
            await client.close()
