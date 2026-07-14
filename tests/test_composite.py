"""CompositeController fan-out: error isolation, summing, preview sequence."""

import pytest

from hue_agent_status.backends import build_controller
from hue_agent_status.backends.base import BackendUnavailableError
from hue_agent_status.backends.composite import CompositeController
from hue_agent_status.config import Config


class RecordingBackend:
    def __init__(self, name="fake", fail=False, restored=1, has_file=False):
        self.name = name
        self.fail = fail
        self.calls = []
        self._restored = restored
        self._has_file = has_file

    async def update_config(self, config):
        self.calls.append(("update_config", config))

    def _maybe_fail(self):
        if self.fail:
            raise BackendUnavailableError(f"{self.name} is down")

    async def apply_state(self, aggregate):
        self.calls.append(("apply_state", aggregate))
        self._maybe_fail()

    async def blink_green(self, times=5):
        self.calls.append(("blink_green", times))
        self._maybe_fail()

    async def restore(self, transition_ms=None, policy=None):
        self.calls.append(("restore", policy))
        self._maybe_fail()
        return self._restored

    async def restore_from_file(self):
        self.calls.append(("restore_from_file", None))
        self._maybe_fail()
        return self._restored

    def has_snapshot_file(self):
        return self._has_file

    async def close(self):
        self.calls.append(("close", None))
        self._maybe_fail()

    def target_summary(self):
        return {"backend": self.name}

    def runtime_status(self):
        return {"mode": "idle", "breathing": False, "effect": None}


class TestFanOut:
    async def test_apply_state_reaches_every_backend(self):
        a, b = RecordingBackend("a"), RecordingBackend("b")
        composite = CompositeController([a, b])
        await composite.apply_state("active")
        assert ("apply_state", "active") in a.calls
        assert ("apply_state", "active") in b.calls

    async def test_blink_green_reaches_every_backend(self):
        a, b = RecordingBackend("a"), RecordingBackend("b")
        composite = CompositeController([a, b])
        await composite.blink_green(times=5)
        assert ("blink_green", 5) in a.calls
        assert ("blink_green", 5) in b.calls

    async def test_one_dead_backend_does_not_block_blink(self):
        a, b = RecordingBackend("a", fail=True), RecordingBackend("b")
        await CompositeController([a, b]).blink_green()
        assert ("blink_green", 5) in b.calls

    async def test_one_dead_backend_does_not_block_the_other(self):
        a, b = RecordingBackend("a", fail=True), RecordingBackend("b")
        composite = CompositeController([a, b])
        await composite.apply_state("waiting")  # must not raise
        assert ("apply_state", "waiting") in b.calls

    async def test_all_backends_failing_raises(self):
        a, b = RecordingBackend("a", fail=True), RecordingBackend("b", fail=True)
        composite = CompositeController([a, b])
        with pytest.raises(BackendUnavailableError):
            await composite.apply_state("waiting")

    async def test_restore_sums_counts_and_survives_partial_failure(self):
        a = RecordingBackend("a", restored=2)
        b = RecordingBackend("b", fail=True)
        c = RecordingBackend("c", restored=3)
        composite = CompositeController([a, b, c])
        assert await composite.restore(policy="always") == 5
        assert await composite.restore_from_file() == 5

    async def test_close_never_raises(self):
        composite = CompositeController([RecordingBackend("a", fail=True)])
        await composite.close()

    async def test_has_snapshot_file_any(self):
        a = RecordingBackend("a", has_file=False)
        b = RecordingBackend("b", has_file=True)
        assert CompositeController([a, b]).has_snapshot_file()
        assert not CompositeController([a]).has_snapshot_file()

    def test_target_summary_keyed_by_backend(self):
        composite = CompositeController([RecordingBackend("a"), RecordingBackend("b")])
        assert set(composite.target_summary()) == {"a", "b"}

    def test_runtime_status_keyed_by_backend(self):
        composite = CompositeController([RecordingBackend("a"), RecordingBackend("b")])
        assert set(composite.runtime_status()) == {"a", "b"}


class TestEmptyComposite:
    async def test_idle_is_a_noop(self):
        await CompositeController([]).apply_state("idle")

    async def test_non_idle_points_at_setup(self):
        with pytest.raises(BackendUnavailableError, match="hue-agent setup"):
            await CompositeController([]).apply_state("active")

    async def test_restore_is_zero(self):
        assert await CompositeController([]).restore() == 0


class TestPreview:
    async def test_preview_sequence(self, monkeypatch):
        import hue_agent_status.backends.composite as composite_module

        async def instant_sleep(_seconds):
            return None

        monkeypatch.setattr(composite_module.asyncio, "sleep", instant_sleep)
        backend = RecordingBackend("a")
        await CompositeController([backend]).preview()
        assert backend.calls == [
            ("apply_state", "active"),
            ("apply_state", "waiting"),
            ("restore", "always"),
        ]


class TestUpdateConfig:
    async def test_surviving_backend_updated_in_place(self):
        hue_like = RecordingBackend("hue")
        composite = CompositeController([hue_like])
        config = Config()
        config.bridge.host = "192.0.2.50"
        await composite.update_config(config)
        assert composite.backends == [hue_like]
        assert ("update_config", config) in hue_like.calls

    async def test_disabled_backend_restored_and_closed(self):
        orphan = RecordingBackend("wiz")
        composite = CompositeController([orphan])
        config = Config()
        config.bridge.host = "192.0.2.50"
        await composite.update_config(config)
        assert [b.name for b in composite.backends] == ["hue"]
        assert ("restore", None) in orphan.calls
        assert ("close", None) in orphan.calls


class TestFactory:
    def test_empty_config_builds_no_backends(self):
        assert build_controller(Config()).backends == []

    def test_bridge_config_builds_hue_backend(self):
        config = Config()
        config.bridge.host = "192.0.2.50"
        (backend,) = build_controller(config, app_key="k").backends
        assert backend.name == "hue"
