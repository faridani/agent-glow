"""Local daemon: owns the light animation loop and a localhost-only event API.

Security posture:

* binds only to a loopback address (refuses anything else),
* requires ``Authorization: Bearer <token>`` on every request,
* accepts only small JSON payloads (64 KB cap),
* never logs hook payload contents.

The successful bind on the configured port doubles as the single-instance
lock: a second daemon cannot bind and exits after confirming a healthy peer.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import secrets as _secrets
import signal
import sys

from aiohttp import web

from . import MAX_HOOK_PAYLOAD_BYTES, __version__
from .backends import BackendUnavailableError, build_controller
from .config import (
    Config,
    ConfigError,
    ensure_private_dir,
    is_loopback_host,
    load_config,
    pidfile_path,
    state_dir,
    validate_config,
    write_private_text,
)
from .events import (
    MAX_SESSION_ID_LENGTH,
    MAX_SUBSESSION_ID_LENGTH,
    MAX_TURN_ID_LENGTH,
    SCOPES,
    SOURCES,
    STATES,
    NormalizedEvent,
)
from .state import SessionRegistry

LOGGER = logging.getLogger(__name__)

_PRUNE_TICK_SECONDS = 5.0
_MAX_QUEUED_COMPLETION_EFFECTS = 8


def _config_file_mtime() -> float | None:
    from .config import config_path

    try:
        return config_path().stat().st_mtime
    except OSError:
        return None


class Daemon:
    def __init__(self, config: Config, controller=None, token: str | None = None):
        self.config = config
        if token is not None:
            tokens = {token}
        else:
            from . import secret_store

            tokens = secret_store.all_daemon_tokens()
        self._tokens = {t.encode("utf-8", "surrogateescape") for t in tokens}
        self.token = sorted(tokens)[0]  # canonical token for outgoing calls
        self.controller = (
            controller if controller is not None else build_controller(config)
        )
        self.registry = SessionRegistry(
            active_ttl_seconds=config.daemon.active_ttl_seconds,
            waiting_ttl_seconds=config.daemon.waiting_ttl_seconds,
            completion_hold_seconds=config.daemon.completion_hold_seconds,
        )
        self._wake = asyncio.Event()
        self._stopping = asyncio.Event()
        self._applied = "idle"
        self._pending_completion_effects = 0
        self._effect_task: asyncio.Task | None = None
        self._config_mtime = _config_file_mtime()

    # -- HTTP layer -----------------------------------------------------------

    def make_app(self) -> web.Application:
        @web.middleware
        async def auth_middleware(request: web.Request, handler):
            header = request.headers.get("Authorization", "")
            provided = header[7:] if header.startswith("Bearer ") else ""
            provided_bytes = provided.encode("utf-8", "surrogateescape")
            authorized = provided and any(
                _secrets.compare_digest(provided_bytes, expected)
                for expected in self._tokens
            )
            if not authorized:
                return web.json_response(
                    {"ok": False, "error": "unauthorized"}, status=401
                )
            return await handler(request)

        app = web.Application(
            middlewares=[auth_middleware], client_max_size=MAX_HOOK_PAYLOAD_BYTES
        )
        app.router.add_post("/event", self.handle_event)
        app.router.add_get("/health", self.handle_health)
        app.router.add_post("/restore", self.handle_restore)
        app.router.add_post("/reload", self.handle_reload)
        app.router.add_post("/shutdown", self.handle_shutdown)
        return app

    async def handle_event(self, request: web.Request) -> web.Response:
        try:
            body = await request.json()
        except web.HTTPRequestEntityTooLarge:
            raise  # let aiohttp answer 413 for payloads over the 64 KB cap
        except Exception:
            return web.json_response({"ok": False, "error": "invalid json"}, status=400)
        if not isinstance(body, dict):
            return web.json_response(
                {"ok": False, "error": "invalid payload"}, status=400
            )
        source = body.get("source")
        state = body.get("state")
        session_id = body.get("session_id")
        event_name = body.get("event", "")
        scope = body.get("scope", "master")
        subsession_id = body.get("subsession_id")
        turn_id = body.get("turn_id")
        pending_work = body.get("pending_work")
        valid_subsession_id = subsession_id is None or (
            isinstance(subsession_id, str)
            and 0 < len(subsession_id) <= MAX_SUBSESSION_ID_LENGTH
        )
        valid_turn_id = turn_id is None or (
            isinstance(turn_id, str) and 0 < len(turn_id) <= MAX_TURN_ID_LENGTH
        )
        valid_pending_work = pending_work is None or (
            isinstance(pending_work, list)
            and len(pending_work) <= 256
            and all(
                isinstance(identifier, str)
                and 0 < len(identifier) <= MAX_SESSION_ID_LENGTH
                for identifier in pending_work
            )
        )
        if (
            source not in SOURCES
            or state not in STATES
            or scope not in SCOPES
            or not isinstance(session_id, str)
            or not (0 < len(session_id) <= MAX_SESSION_ID_LENGTH)
            or not isinstance(event_name, str)
            or not valid_subsession_id
            or not valid_turn_id
            or not valid_pending_work
            or (scope == "subsession" and subsession_id is None)
            or (scope == "master" and subsession_id is not None)
        ):
            return web.json_response(
                {"ok": False, "error": "invalid event"}, status=400
            )
        event = NormalizedEvent(
            source=source,
            session_id=session_id,
            state=state,
            event=event_name[:64],
            scope=scope,
            subsession_id=subsession_id,
            turn_id=turn_id,
            pending_work=tuple(pending_work) if pending_work is not None else None,
        )
        new_completion = self.registry.apply_event(event)
        aggregate = self.registry.aggregate()
        if scope == "master" and state == "active" and event_name == "UserPromptSubmit":
            # A new turn supersedes decorative signals from the previous turn.
            await self._cancel_effect(clear_pending=True)
        elif aggregate == "waiting":
            # Explicit user input always wins over decorative completion effects.
            self._pending_completion_effects = 0
        elif new_completion:
            self._pending_completion_effects = min(
                _MAX_QUEUED_COMPLETION_EFFECTS,
                self._pending_completion_effects + 1,
            )
        LOGGER.debug("event: %s/%s/%s -> %s", source, scope, event_name[:64], state)
        self._wake.set()
        return web.json_response({"ok": True, "aggregate": aggregate})

    async def handle_health(self, request: web.Request) -> web.Response:
        runtime_status = getattr(self.controller, "runtime_status", None)
        try:
            backends = runtime_status() if runtime_status is not None else {}
        except Exception:
            backends = {}
        return web.json_response(
            {
                "ok": True,
                "version": __version__,
                "pid": os.getpid(),
                "aggregate": self.registry.aggregate(),
                "applied": self._applied,
                "sessions": self.registry.describe(),
                "completion_effect": {
                    "running": self._effect_task is not None,
                    "queued": self._pending_completion_effects,
                },
                "completion_hold_seconds": self.registry.completion_ttl,
                "backends": backends,
                "config_mtime": self._config_mtime,
            }
        )

    async def handle_reload(self, request: web.Request) -> web.Response:
        """Re-read config.toml and apply it without restarting the daemon."""
        try:
            new_config = load_config()
            validate_config(new_config)
        except ConfigError as err:
            LOGGER.warning("reload rejected: %s", err)
            return web.json_response({"ok": False, "error": str(err)}, status=400)
        notes = []
        if (
            new_config.daemon.host != self.config.daemon.host
            or new_config.daemon.port != self.config.daemon.port
        ):
            notes.append("daemon host/port changes need a daemon restart")
        self.config = new_config
        self.registry.active_ttl = new_config.daemon.active_ttl_seconds
        self.registry.waiting_ttl = new_config.daemon.waiting_ttl_seconds
        self.registry.completion_ttl = new_config.daemon.completion_hold_seconds
        await self._cancel_effect()
        try:
            await self.controller.update_config(new_config)
        except Exception as err:
            LOGGER.warning("reload: controller update failed: %s", err)
            notes.append("lights not updated; check the private daemon log")
        self._config_mtime = _config_file_mtime()
        # Force the orchestrator to re-apply the aggregate so backends that
        # joined on this reload start animating immediately.
        self._applied = "stale"
        self._wake.set()
        LOGGER.info("config reloaded")
        payload: dict = {"ok": True}
        if notes:
            payload["note"] = "; ".join(notes)
        return web.json_response(payload)

    async def handle_restore(self, request: web.Request) -> web.Response:
        policy = None
        with contextlib.suppress(Exception):
            body = await request.json()
            if isinstance(body, dict) and body.get("policy") in ("smart", "always"):
                policy = body["policy"]
        self.registry.clear()
        await self._cancel_effect(clear_pending=True)
        try:
            restored = await self.controller.restore(policy=policy)
        except Exception as err:
            LOGGER.warning("restore failed: %s", err)
            return web.json_response(
                {"ok": False, "error": "restore failed"}, status=502
            )
        self._applied = "idle"
        self._wake.set()  # let the orchestrator re-evaluate immediately
        return web.json_response({"ok": True, "restored": restored})

    async def handle_shutdown(self, request: web.Request) -> web.Response:
        LOGGER.info("shutdown requested")
        self._stopping.set()
        return web.json_response({"ok": True})

    # -- orchestration ----------------------------------------------------------

    async def _cancel_effect(self, *, clear_pending: bool = False) -> None:
        if clear_pending:
            self._pending_completion_effects = 0
        task, self._effect_task = self._effect_task, None
        if task is None:
            return
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    async def _play_completion_effect(self) -> None:
        try:
            await self.controller.blink_green(times=5)
        except asyncio.CancelledError:
            raise
        except BackendUnavailableError as err:
            LOGGER.warning("cannot blink completion signal: %s", err)
        except Exception:
            LOGGER.exception("unexpected error playing completion signal")

    def _effect_finished(self, task: asyncio.Task) -> None:
        if self._effect_task is task:
            self._effect_task = None
            if (
                self.registry.aggregate() == "complete"
                and self._pending_completion_effects == 0
            ):
                # The steady success hold starts after the final five-flash
                # child signal, not while that signal is still playing.
                self.registry.start_completion_hold()
            self._wake.set()

    def _start_next_effect(self, aggregate: str) -> None:
        if (
            aggregate == "waiting"
            or self._effect_task is not None
            or self._pending_completion_effects <= 0
        ):
            return
        self._pending_completion_effects -= 1
        task = asyncio.create_task(self._play_completion_effect())
        self._effect_task = task
        task.add_done_callback(self._effect_finished)

    async def _apply_aggregate(self) -> None:
        aggregate = self.registry.aggregate()
        if aggregate == "waiting":
            await self._cancel_effect(clear_pending=True)
        if aggregate == self._applied:
            # A backend command or animation task can fail independently of
            # the registry. Re-entering a steady mode lets controllers restart
            # breathing or retry a bulb that missed the latest red/green look.
            if (
                aggregate in ("active", "waiting", "complete")
                and self._effect_task is None
            ):
                try:
                    await self.controller.apply_state(aggregate)
                except Exception as err:
                    LOGGER.warning("cannot refresh %s light state: %s", aggregate, err)
            self._start_next_effect(aggregate)
            return
        if aggregate == "complete" and self._effect_task is not None:
            # Preserve an in-progress child completion signal. Its callback
            # wakes us to apply steady green after all five flashes finish.
            return
        if aggregate == "active" and self._applied == "complete":
            self._pending_completion_effects = 0
        await self._cancel_effect()
        if aggregate == "idle":
            # Grace period so a Stop immediately followed by a new prompt
            # doesn't restore-then-reanimate the lights.
            grace = (
                0.0
                if self._applied == "complete"
                else self.config.daemon.idle_grace_seconds
            )
            if grace > 0:
                with contextlib.suppress(asyncio.TimeoutError, TimeoutError):
                    await asyncio.wait_for(self._wake.wait(), timeout=grace)
                self.registry.prune()
                aggregate = self.registry.aggregate()
                if aggregate == self._applied:
                    self._start_next_effect(aggregate)
                    return
        try:
            await self.controller.apply_state(aggregate)
            self._applied = aggregate
            if (
                aggregate == "complete"
                and self._effect_task is None
                and self._pending_completion_effects == 0
            ):
                self.registry.start_completion_hold()
            LOGGER.info("lights -> %s", aggregate)
        except BackendUnavailableError as err:
            LOGGER.warning("cannot control lights: %s", err)
        except Exception:
            LOGGER.exception("unexpected error applying light state")
        self._start_next_effect(aggregate)

    async def _orchestrate(self) -> None:
        while not self._stopping.is_set():
            expiry = self.registry.next_expiry_delay()
            timeout = (
                _PRUNE_TICK_SECONDS
                if expiry is None
                else min(_PRUNE_TICK_SECONDS, expiry)
            )
            with contextlib.suppress(asyncio.TimeoutError, TimeoutError):
                await asyncio.wait_for(self._wake.wait(), timeout=timeout)
            self._wake.clear()
            if self._stopping.is_set():
                break
            self.registry.prune()
            await self._apply_aggregate()

    # -- lifecycle ----------------------------------------------------------------

    async def run_async(self) -> int:
        host = self.config.daemon.host
        port = self.config.daemon.port
        if not is_loopback_host(host):
            LOGGER.error("refusing to bind non-loopback address %r", host)
            return 2

        runner = web.AppRunner(self.make_app())
        await runner.setup()
        site = web.TCPSite(runner, host, port)
        try:
            await site.start()
        except OSError as err:
            await runner.cleanup()
            from .client import get_health

            health = get_health(self.config, self.token, timeout=1.0)
            if health:
                LOGGER.info("daemon already running (pid %s)", health.get("pid"))
                return 0
            LOGGER.error("cannot bind %s:%s: %s", host, port, err)
            return 1

        ensure_private_dir(state_dir())
        write_private_text(pidfile_path(), str(os.getpid()))
        LOGGER.info("daemon listening on %s:%s (pid %d)", host, port, os.getpid())

        loop = asyncio.get_running_loop()
        if sys.platform != "win32":
            for sig in (signal.SIGINT, signal.SIGTERM):
                with contextlib.suppress(NotImplementedError, ValueError):
                    loop.add_signal_handler(sig, self._stopping.set)

        orchestrator = asyncio.create_task(self._orchestrate())
        try:
            await self._stopping.wait()
        except (KeyboardInterrupt, asyncio.CancelledError):
            pass
        finally:
            self._stopping.set()
            self._wake.set()
            orchestrator.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await orchestrator
            await self._cancel_effect(clear_pending=True)
            # idle transition stops any animation and restores the snapshot
            with contextlib.suppress(Exception):
                await self.controller.apply_state("idle")
            close = getattr(self.controller, "close", None)
            if close is not None:
                with contextlib.suppress(Exception):
                    await close()
            with contextlib.suppress(OSError):
                pidfile_path().unlink()
            await runner.cleanup()
            LOGGER.info("daemon stopped")
        return 0

    def run(self) -> int:
        try:
            return asyncio.run(self.run_async())
        except KeyboardInterrupt:
            return 0


def _setup_logging(debug: bool = False) -> None:
    # Detached daemons already have stderr redirected into daemon.log by
    # spawn_daemon_detached(), so a single stream handler covers both modes.
    ensure_private_dir(state_dir())
    logging.basicConfig(
        level=logging.DEBUG if debug else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[logging.StreamHandler()],
    )


def run_daemon(config: Config, debug: bool = False) -> int:
    _setup_logging(debug)
    return Daemon(config).run()
