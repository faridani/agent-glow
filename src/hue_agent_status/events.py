"""Normalize Claude Code / Codex hook payloads into (source, session, state) events.

All mapping is table-driven so that future hook events (for example a Codex
``TuiQuestionOpened``) can be supported by adding one entry, without touching
any control flow.

States:

* ``active``  — the agent is working; lights breathe.
* ``waiting`` — the agent needs the user's input/approval; lights turn red.
* ``ended``   — the session is gone; it no longer contributes to the aggregate.
"""

from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass
from typing import Callable

SOURCES = ("claude", "codex")
STATES = ("active", "waiting", "ended")

MAX_SESSION_ID_LENGTH = 128


@dataclass(frozen=True)
class NormalizedEvent:
    source: str
    session_id: str
    state: str
    event: str = ""
    #: True when "waiting" means "the turn is over, it's the user's move"
    #: rather than "the agent is hard-blocked mid-task". Turn-end waiting
    #: stops demanding red after daemon.turn_end_waiting_seconds.
    turn_end: bool = False

    def to_payload(self) -> dict:
        return {
            "source": self.source,
            "session_id": self.session_id,
            "state": self.state,
            "event": self.event,
            "turn_end": self.turn_end,
        }


def _fallback_session_id(source: str, payload: dict) -> str:
    cwd = payload.get("cwd") if isinstance(payload.get("cwd"), str) else os.getcwd()
    digest = hashlib.sha256(f"{source}:{cwd}".encode()).hexdigest()
    return f"cwd-{digest[:16]}"


def session_id_from_payload(source: str, payload: dict) -> str:
    for key in ("session_id", "sessionId", "thread_id", "thread-id", "conversation_id"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()[:MAX_SESSION_ID_LENGTH]
    return _fallback_session_id(source, payload)


def event_name_from_payload(payload: dict) -> str:
    for key in ("hook_event_name", "hookEventName", "event_name", "event", "type"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


# --------------------------------------------------------------------------
# Claude Code
# --------------------------------------------------------------------------

#: Tools that mean Claude is asking the user something.
CLAUDE_WAITING_TOOLS = frozenset({"AskUserQuestion", "ExitPlanMode"})

#: Notification types that mean Claude is blocked on the user.
CLAUDE_WAITING_NOTIFICATION_TYPES = frozenset(
    {"permission_prompt", "idle_prompt", "elicitation_dialog"}
)

#: Heuristic for Notification payloads that carry only a human message.
_CLAUDE_WAITING_MESSAGE_RE = re.compile(
    r"needs your (permission|approval|input)|waiting for (your )?input|permission to use",
    re.IGNORECASE,
)

_BACKGROUND_TASK_KEYS = (
    "background_tasks",
    "backgroundTasks",
    "active_background_tasks",
    "running_background_tasks",
)


def _has_background_tasks(payload: dict) -> bool:
    for key in _BACKGROUND_TASK_KEYS:
        value = payload.get(key)
        if isinstance(value, bool):
            if value:
                return True
        elif isinstance(value, (int, float)):
            if value > 0:
                return True
        elif isinstance(value, (list, tuple, dict)):
            if len(value) > 0:
                return True
    return False


def _claude_pre_tool_use(payload: dict) -> str | None:
    tool = payload.get("tool_name") or payload.get("toolName")
    if isinstance(tool, str) and tool in CLAUDE_WAITING_TOOLS:
        return "waiting"
    return "active"


def _claude_notification(payload: dict) -> str | None:
    ntype = payload.get("notification_type") or payload.get("notificationType")
    if isinstance(ntype, str):
        return "waiting" if ntype in CLAUDE_WAITING_NOTIFICATION_TYPES else None
    message = payload.get("message")
    if isinstance(message, str) and _CLAUDE_WAITING_MESSAGE_RE.search(message):
        return "waiting"
    return None


def _claude_stop(payload: dict) -> str | None:
    return "active" if _has_background_tasks(payload) else "waiting"


#: event name -> state, None (no-op), or resolver(payload) -> state | None
CLAUDE_EVENT_MAP: dict[str, str | None | Callable[[dict], str | None]] = {
    "SessionStart": None,
    "UserPromptSubmit": "active",
    "PreToolUse": _claude_pre_tool_use,
    "PostToolUse": "active",
    "PostToolBatch": "active",
    "PermissionRequest": "waiting",
    "Notification": _claude_notification,
    "Stop": _claude_stop,
    "SubagentStop": "active",
    "StopFailure": "waiting",
    "SessionEnd": "ended",
    "PreCompact": "active",
}


# --------------------------------------------------------------------------
# Codex
# --------------------------------------------------------------------------


def _codex_subagent_stop(payload: dict) -> str | None:
    # "active if the parent turn remains active" — if the payload says the
    # turn ended, do nothing (a Stop will follow); otherwise assume active.
    for key in ("turn_active", "turnActive", "parent_turn_active"):
        value = payload.get(key)
        if value is False:
            return None
    return "active"


CODEX_EVENT_MAP: dict[str, str | None | Callable[[dict], str | None]] = {
    "SessionStart": None,  # active only once a prompt actually arrives
    "UserPromptSubmit": "active",
    "PreToolUse": "active",
    "PostToolUse": "active",
    "PermissionRequest": "waiting",
    "SubagentStart": "active",
    "SubagentStop": _codex_subagent_stop,
    "Stop": "waiting",
    "SessionEnd": "ended",
    # Defensive forward-compatibility: prompts the Codex TUI may one day
    # surface as hook events. Adding a row here is the whole change.
    "TuiQuestionOpened": "waiting",
    "user-input-requested": "waiting",
    "plan-questions-waiting": "waiting",
}

#: Codex `notify` program notification types (JSON in argv[1]).
CODEX_NOTIFY_MAP: dict[str, str] = {
    "agent-turn-complete": "waiting",
}

_EVENT_MAPS = {"claude": CLAUDE_EVENT_MAP, "codex": CODEX_EVENT_MAP}

#: Events whose "waiting" means the turn completed and the user is up next.
TURN_END_EVENTS = frozenset({"Stop", "StopFailure", "agent-turn-complete"})

#: Notification types that signal "still idle, user hasn't come back".
TURN_END_NOTIFICATION_TYPES = frozenset({"idle_prompt"})

_TURN_END_MESSAGE_RE = re.compile(r"waiting for (your )?input", re.IGNORECASE)


def _is_turn_end_waiting(event_name: str, payload: dict) -> bool:
    if event_name in TURN_END_EVENTS:
        return True
    if event_name == "Notification":
        ntype = payload.get("notification_type") or payload.get("notificationType")
        if isinstance(ntype, str):
            return ntype in TURN_END_NOTIFICATION_TYPES
        message = payload.get("message")
        if isinstance(message, str) and _TURN_END_MESSAGE_RE.search(message):
            return True
    return False


def normalize_hook_event(source: str, payload: dict) -> NormalizedEvent | None:
    """Map a raw hook payload to a NormalizedEvent, or None for no-ops."""
    if source not in _EVENT_MAPS or not isinstance(payload, dict):
        return None
    event_name = event_name_from_payload(payload)
    rule = _EVENT_MAPS[source].get(event_name)
    state = rule(payload) if callable(rule) else rule
    if state is None:
        return None
    return NormalizedEvent(
        source=source,
        session_id=session_id_from_payload(source, payload),
        state=state,
        event=event_name,
        turn_end=state == "waiting" and _is_turn_end_waiting(event_name, payload),
    )


def normalize_codex_notification(payload: dict) -> NormalizedEvent | None:
    """Map a Codex ``notify`` JSON payload (argv[1]) to a NormalizedEvent."""
    if not isinstance(payload, dict):
        return None
    ntype = payload.get("type")
    state = CODEX_NOTIFY_MAP.get(ntype) if isinstance(ntype, str) else None
    if state is None:
        return None
    return NormalizedEvent(
        source="codex",
        session_id=session_id_from_payload("codex", payload),
        state=state,
        event=ntype,
        turn_end=state == "waiting" and ntype in TURN_END_EVENTS,
    )
