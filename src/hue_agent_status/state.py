"""Session registry: tracks (source, session_id) -> state and the global aggregate."""

from __future__ import annotations

import time
from dataclasses import dataclass

from .events import STATES, NormalizedEvent


@dataclass
class SessionInfo:
    source: str
    session_id: str
    state: str
    last_seen: float
    turn_end: bool = False


class SessionRegistry:
    """State map keyed by (source, session_id) with TTL-based cleanup.

    Aggregate priority: any ``waiting`` session wins, then any ``active``
    session, otherwise ``idle``. Waiting that merely means "the turn is over"
    (Stop / agent-turn-complete) expires on the much shorter turn-end TTL, so
    a finished conversation doesn't hold the lights red for hours.
    """

    def __init__(
        self,
        active_ttl_seconds: float = 1800,
        waiting_ttl_seconds: float = 14400,
        turn_end_waiting_seconds: float = 300,
    ):
        self.active_ttl = active_ttl_seconds
        self.waiting_ttl = waiting_ttl_seconds
        self.turn_end_waiting_ttl = turn_end_waiting_seconds
        self._sessions: dict[tuple[str, str], SessionInfo] = {}

    def __len__(self) -> int:
        return len(self._sessions)

    def apply_event(self, event: NormalizedEvent, now: float | None = None) -> None:
        if event.state not in STATES:
            return
        now = time.time() if now is None else now
        key = (event.source, event.session_id)
        if event.state == "ended":
            self._sessions.pop(key, None)
            return
        self._sessions[key] = SessionInfo(
            source=event.source,
            session_id=event.session_id,
            state=event.state,
            last_seen=now,
            turn_end=bool(getattr(event, "turn_end", False)),
        )

    def prune(self, now: float | None = None) -> int:
        """Drop sessions whose TTL has expired; returns how many were dropped."""
        now = time.time() if now is None else now
        stale = []
        for key, info in self._sessions.items():
            if info.state == "active":
                ttl = self.active_ttl
            elif info.turn_end:
                ttl = min(self.turn_end_waiting_ttl, self.waiting_ttl)
            else:
                ttl = self.waiting_ttl
            if now - info.last_seen > ttl:
                stale.append(key)
        for key in stale:
            del self._sessions[key]
        return len(stale)

    def clear(self) -> None:
        self._sessions.clear()

    def aggregate(self) -> str:
        states = {info.state for info in self._sessions.values()}
        if "waiting" in states:
            return "waiting"
        if "active" in states:
            return "active"
        return "idle"

    def describe(self, now: float | None = None) -> list[dict]:
        """Safe summary for /health and `hue-agent status` (no payload data)."""
        now = time.time() if now is None else now
        return [
            {
                "source": info.source,
                "session_id": info.session_id,
                "state": info.state,
                "age_seconds": round(max(0.0, now - info.last_seen), 1),
            }
            for info in sorted(
                self._sessions.values(), key=lambda i: (i.source, i.session_id)
            )
        ]
