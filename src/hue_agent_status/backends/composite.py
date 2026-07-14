"""One controller surface over any number of light backends.

A failure in one backend (an unplugged WiZ bulb, an unreachable Hue Bridge)
must never keep another backend from showing agent status, so every fan-out
gathers with ``return_exceptions=True`` and raises only when *all* backends
failed.
"""

from __future__ import annotations

import asyncio
import logging

import contextlib

from .base import BackendUnavailableError

LOGGER = logging.getLogger(__name__)


def build_backends(config, app_key: str | None = None) -> list:
    """One backend instance per light system the config enables."""
    backends = []
    if config.bridge.host or config.target.ids:
        from .hue import HueController

        backends.append(HueController(config, app_key=app_key))
    if config.wiz.bulbs:
        from .wiz import WizController

        backends.append(WizController(config))
    return backends


class CompositeController:
    def __init__(self, backends):
        self.backends = list(backends)

    async def _fan_out(self, op: str, coros: list) -> list:
        """Run one coroutine per backend; raise only if every one failed."""
        if not coros:
            return []
        results = await asyncio.gather(*coros, return_exceptions=True)
        failures = [r for r in results if isinstance(r, BaseException)]
        if failures and len(failures) == len(results):
            raise failures[0]
        for failure in failures:
            LOGGER.warning("%s failed for one backend: %s", op, failure)
        return [r for r in results if not isinstance(r, BaseException)]

    async def apply_state(self, aggregate: str) -> None:
        if not self.backends:
            if aggregate == "idle":
                return
            raise BackendUnavailableError("no lights configured; run `hue-agent setup`")
        await self._fan_out(
            "apply_state", [b.apply_state(aggregate) for b in self.backends]
        )

    async def blink_green(self, times: int = 5) -> None:
        """Play one cancellable success blink across every available backend."""
        if not self.backends:
            return
        await self._fan_out(
            "blink_green", [b.blink_green(times=times) for b in self.backends]
        )

    async def restore(
        self, transition_ms: int | None = None, policy: str | None = None
    ) -> int:
        counts = await self._fan_out(
            "restore",
            [
                b.restore(transition_ms=transition_ms, policy=policy)
                for b in self.backends
            ],
        )
        return sum(counts)

    async def restore_from_file(self) -> int:
        counts = await self._fan_out(
            "restore_from_file", [b.restore_from_file() for b in self.backends]
        )
        return sum(counts)

    def has_snapshot_file(self) -> bool:
        return any(b.has_snapshot_file() for b in self.backends)

    async def close(self) -> None:
        await asyncio.gather(
            *(b.close() for b in self.backends), return_exceptions=True
        )

    def target_summary(self) -> dict:
        return {b.name: b.target_summary() for b in self.backends}

    def runtime_status(self) -> dict:
        """Small health view; backends without diagnostics degrade cleanly."""
        return {
            b.name: b.runtime_status()
            for b in self.backends
            if hasattr(b, "runtime_status")
        }

    async def update_config(self, config) -> None:
        """Adopt a new config: update surviving backends in place, restore and
        drop backends the config disabled, add newly enabled ones (idle until
        the daemon re-applies the aggregate)."""
        existing = {b.name: b for b in self.backends}
        updated = []
        for fresh in build_backends(config):
            current = existing.pop(fresh.name, None)
            if current is not None:
                await current.update_config(config)
                updated.append(current)
            else:
                updated.append(fresh)
        for removed in existing.values():
            with contextlib.suppress(Exception):
                await removed.restore()
            with contextlib.suppress(Exception):
                await removed.close()
        self.backends = updated

    async def preview(
        self, breathe_seconds: float = 10.0, red_seconds: float = 3.0
    ) -> None:
        """Breathe, go red, then restore — used by setup and `hue-agent preview`."""
        await self.apply_state("active")
        await asyncio.sleep(breathe_seconds)
        await self.apply_state("waiting")
        await asyncio.sleep(red_seconds)
        await self.restore(policy="always")
