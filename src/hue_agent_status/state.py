"""Hierarchical session registry and global light-state aggregation."""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from .events import SCOPES, STATES, NormalizedEvent


@dataclass
class SubsessionInfo:
    subsession_id: str
    state: str
    last_seen: float
    turn_id: str | None = None


@dataclass
class SessionInfo:
    source: str
    session_id: str
    state: str
    last_seen: float
    turn_id: str | None = None
    pending_work: tuple[str, ...] | None = None
    subsessions: dict[str, SubsessionInfo] = field(default_factory=dict)
    completed_subsessions: set[tuple[str | None, str]] = field(default_factory=set)
    completion_started_at: float | None = None


class SessionRegistry:
    """Track master turns and their subsessions.

    Aggregate priority is explicit master ``waiting``, then any effective
    ``active`` work, then a fully ``complete`` master, otherwise ``idle``.
    A master completion with pending work or open subsessions remains
    effectively active.  ``apply_event`` returns ``True`` exactly once for
    each newly completed subsession so the daemon can enqueue its green blink.
    """

    def __init__(
        self,
        active_ttl_seconds: float = 1800,
        waiting_ttl_seconds: float = 14400,
        completion_hold_seconds: float = 300,
        *,
        turn_end_waiting_seconds: float | None = None,
    ):
        # ``turn_end_waiting_seconds`` is accepted only as a constructor
        # compatibility alias for callers upgrading from the old red-on-Stop
        # model.  It now controls the green completion hold.
        if turn_end_waiting_seconds is not None:
            completion_hold_seconds = turn_end_waiting_seconds
        self.active_ttl = active_ttl_seconds
        self.waiting_ttl = waiting_ttl_seconds
        self.completion_ttl = completion_hold_seconds
        self._sessions: dict[tuple[str, str], SessionInfo] = {}

    def __len__(self) -> int:
        return len(self._sessions)

    @staticmethod
    def _same_turn(current: str | None, incoming: str | None) -> bool:
        return current == incoming

    @staticmethod
    def _effective_state(info: SessionInfo) -> str:
        if info.state == "waiting":
            return "waiting"
        if info.state == "active" or info.subsessions or info.pending_work:
            return "active"
        if info.state == "complete" and info.completion_started_at is not None:
            return "complete"
        return "idle"

    @staticmethod
    def _completion_key(event: NormalizedEvent) -> tuple[str | None, str] | None:
        if not event.subsession_id:
            return None
        return (event.turn_id, event.subsession_id)

    def _refresh_completion(self, info: SessionInfo, now: float) -> None:
        eligible = (
            info.state == "complete" and not info.subsessions and not info.pending_work
        )
        if eligible:
            if info.completion_started_at is None:
                info.completion_started_at = now
        else:
            info.completion_started_at = None

    def _new_session(self, event: NormalizedEvent, now: float) -> SessionInfo:
        info = SessionInfo(
            source=event.source,
            session_id=event.session_id,
            state="active",
            last_seen=now,
            turn_id=event.turn_id,
        )
        self._sessions[(event.source, event.session_id)] = info
        return info

    def _apply_subsession(
        self, info: SessionInfo | None, event: NormalizedEvent, now: float
    ) -> bool:
        subsession_id = event.subsession_id
        if not subsession_id:
            if info is not None and event.pending_work is not None:
                info.pending_work = event.pending_work
                info.last_seen = now
                self._refresh_completion(info, now)
            return False

        if info is None:
            # A start/progress event is sufficient evidence that the parent
            # session is working.  A lone finish still produces its blink but
            # must not invent a completed master session.
            if event.state in ("active", "waiting"):
                info = self._new_session(event, now)
            else:
                return event.state == "complete"

        completion_key = self._completion_key(event)
        if event.state in ("active", "waiting"):
            if completion_key is not None:
                info.completed_subsessions.discard(completion_key)
            info.subsessions[subsession_id] = SubsessionInfo(
                subsession_id=subsession_id,
                state=event.state,
                last_seen=now,
                turn_id=event.turn_id,
            )
            if event.pending_work is not None:
                info.pending_work = event.pending_work
            info.last_seen = now
            info.completion_started_at = None
            return False

        if event.state == "ended":
            info.subsessions.pop(subsession_id, None)
            if event.pending_work is not None:
                info.pending_work = event.pending_work
            info.last_seen = now
            self._refresh_completion(info, now)
            return False

        if event.state != "complete":
            return False

        is_new = (
            completion_key is not None
            and completion_key not in info.completed_subsessions
        )
        if completion_key is not None:
            info.completed_subsessions.add(completion_key)
        info.subsessions.pop(subsession_id, None)

        if event.pending_work is not None:
            info.pending_work = event.pending_work
        elif info.pending_work and subsession_id in info.pending_work:
            info.pending_work = tuple(
                identifier
                for identifier in info.pending_work
                if identifier != subsession_id
            )

        if is_new:
            info.last_seen = now
        self._refresh_completion(info, now)
        return is_new

    def _apply_master(
        self,
        key: tuple[str, str],
        info: SessionInfo | None,
        event: NormalizedEvent,
        now: float,
    ) -> None:
        if event.state == "ended":
            # Hook-level SessionEnd normalizes to complete.  ``ended`` is
            # reserved for an explicit daemon/API reset and always removes.
            self._sessions.pop(key, None)
            return

        if info is None:
            info = self._new_session(event, now)

        if event.state in ("active", "waiting"):
            new_prompt = event.event == "UserPromptSubmit"
            if new_prompt or not self._same_turn(info.turn_id, event.turn_id):
                info.completed_subsessions.clear()
            info.state = event.state
            if event.turn_id is not None or info.turn_id is None:
                info.turn_id = event.turn_id
            info.pending_work = event.pending_work
            info.completion_started_at = None
            info.last_seen = now
            return

        if event.state != "complete":
            return

        incoming_turn = event.turn_id if event.turn_id is not None else info.turn_id
        duplicate = info.state == "complete" and self._same_turn(
            info.turn_id, incoming_turn
        )

        snapshot_changed = False
        if event.pending_work is not None and event.pending_work != info.pending_work:
            info.pending_work = event.pending_work
            snapshot_changed = True

        if not duplicate:
            info.state = "complete"
            info.turn_id = incoming_turn
            info.last_seen = now
        elif snapshot_changed and info.completion_started_at is None:
            # Refresh stale-work pruning while a duplicate completion supplies
            # a newer pending-work snapshot, but never extend an existing hold.
            info.last_seen = now

        self._refresh_completion(info, now)

    def apply_event(self, event: NormalizedEvent, now: float | None = None) -> bool:
        """Apply an event and report whether a subsession newly completed."""
        if event.state not in STATES or event.scope not in SCOPES:
            return False
        now = time.time() if now is None else now
        key = (event.source, event.session_id)
        info = self._sessions.get(key)
        if event.scope == "subsession":
            return self._apply_subsession(info, event, now)
        self._apply_master(key, info, event, now)
        return False

    def _ttl_for(self, info: SessionInfo) -> tuple[float, float]:
        state = self._effective_state(info)
        if state == "waiting":
            return self.waiting_ttl, info.last_seen
        if state == "complete" and info.completion_started_at is not None:
            return self.completion_ttl, info.completion_started_at
        return self.active_ttl, info.last_seen

    def prune(self, now: float | None = None) -> int:
        """Drop sessions whose effective-state TTL expired."""
        now = time.time() if now is None else now
        stale = []
        for key, info in self._sessions.items():
            ttl, since = self._ttl_for(info)
            if now - since >= ttl:
                stale.append(key)
        for key in stale:
            del self._sessions[key]
        return len(stale)

    def next_expiry_delay(self, now: float | None = None) -> float | None:
        """Seconds until the next TTL transition, or ``None`` if empty.

        The daemon uses this to wake at the exact end of a 300-second green
        hold instead of waiting for its next periodic prune tick.
        """
        if not self._sessions:
            return None
        now = time.time() if now is None else now
        return min(
            max(0.0, ttl - (now - since))
            for info in self._sessions.values()
            for ttl, since in (self._ttl_for(info),)
        )

    def clear(self) -> None:
        self._sessions.clear()

    def start_completion_hold(self, now: float | None = None) -> None:
        """Start one full global hold after the final green look is visible.

        Resetting every eligible completed parent is intentional: if several
        sessions finished at different times, the global light should remain
        green for the configured duration after the last work and any queued
        child-completion blinks have finished.
        """
        now = time.time() if now is None else now
        for info in self._sessions.values():
            if (
                info.state == "complete"
                and not info.subsessions
                and not info.pending_work
            ):
                info.completion_started_at = now

    def aggregate(self) -> str:
        states = {self._effective_state(info) for info in self._sessions.values()}
        if "waiting" in states:
            return "waiting"
        if "active" in states:
            return "active"
        if "complete" in states:
            return "complete"
        return "idle"

    def describe(self, now: float | None = None) -> list[dict]:
        """Safe summary for /health and ``hue-agent status``."""
        now = time.time() if now is None else now
        return [
            {
                "source": info.source,
                "session_id": info.session_id,
                "state": self._effective_state(info),
                "age_seconds": round(max(0.0, now - info.last_seen), 1),
            }
            for info in sorted(
                self._sessions.values(), key=lambda item: (item.source, item.session_id)
            )
        ]
