"""Normalize Claude Code / Codex hooks into hierarchical lifecycle events.

The daemon distinguishes the master turn from its subsessions.  This matters
because a subagent finishing is a transient notification (five green blinks),
while only the master finishing with no outstanding work means the whole turn
is complete.

States:

* ``active``   -- the actor is working; lights breathe.
* ``waiting``  -- the *master* explicitly needs user input; lights turn red.
* ``complete`` -- an actor finished; subsessions blink and a fully finished
  master holds green.
* ``ended``    -- the session was closed without contributing further state.
"""

from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass
from typing import Callable

SOURCES = ("claude", "codex")
STATES = ("active", "waiting", "complete", "ended")
SCOPES = ("master", "subsession")

MAX_SESSION_ID_LENGTH = 128
MAX_TURN_ID_LENGTH = 128
MAX_SUBSESSION_ID_LENGTH = 128


@dataclass(frozen=True)
class NormalizedEvent:
    source: str
    session_id: str
    state: str
    event: str = ""
    scope: str = "master"
    subsession_id: str | None = None
    turn_id: str | None = None
    #: ``None`` means the source supplied no authoritative work snapshot;
    #: ``()`` means it explicitly reported that nothing remains.
    pending_work: tuple[str, ...] | None = None

    def __post_init__(self) -> None:
        if self.pending_work is not None and not isinstance(self.pending_work, tuple):
            object.__setattr__(self, "pending_work", tuple(self.pending_work))

    def to_payload(self) -> dict:
        return {
            "source": self.source,
            "session_id": self.session_id,
            "state": self.state,
            "event": self.event,
            "scope": self.scope,
            "subsession_id": self.subsession_id,
            "turn_id": self.turn_id,
            "pending_work": (
                list(self.pending_work) if self.pending_work is not None else None
            ),
        }


def _bounded_identifier(value: object, limit: int) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    return value.strip()[:limit]


def _fallback_session_id(source: str, payload: dict) -> str:
    cwd = payload.get("cwd") if isinstance(payload.get("cwd"), str) else os.getcwd()
    digest = hashlib.sha256(f"{source}:{cwd}".encode()).hexdigest()
    return f"cwd-{digest[:16]}"


def session_id_from_payload(source: str, payload: dict) -> str:
    for key in ("session_id", "sessionId", "thread_id", "thread-id", "conversation_id"):
        value = _bounded_identifier(payload.get(key), MAX_SESSION_ID_LENGTH)
        if value is not None:
            return value
    return _fallback_session_id(source, payload)


def turn_id_from_payload(payload: dict) -> str | None:
    # Claude calls a turn a prompt; Codex calls it a turn.  Keeping either ID
    # lets the registry deduplicate Codex's Stop + agent-turn-complete pair.
    for key in ("turn_id", "turn-id", "turnId", "prompt_id", "promptId"):
        value = _bounded_identifier(payload.get(key), MAX_TURN_ID_LENGTH)
        if value is not None:
            return value
    return None


def subsession_id_from_payload(payload: dict) -> str | None:
    for key in (
        "agent_id",
        "agentId",
        "subagent_id",
        "subagentId",
        "teammate_name",
        "teammateName",
    ):
        value = _bounded_identifier(payload.get(key), MAX_SUBSESSION_ID_LENGTH)
        if value is not None:
            return value
    return None


def event_name_from_payload(payload: dict) -> str:
    for key in ("hook_event_name", "hookEventName", "event_name", "event", "type"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _is_master(payload: dict) -> bool:
    return subsession_id_from_payload(payload) is None


def _work_ids(value: object, prefix: str) -> list[str]:
    """Extract stable IDs from a documented task/cron collection.

    A few older integrations supplied only a count.  Synthetic IDs retain the
    important distinction between "some work" and an explicitly empty list.
    """
    if isinstance(value, bool):
        return [f"{prefix}:unknown"] if value else []
    if isinstance(value, (int, float)):
        count = max(0, int(value))
        return [f"{prefix}:{index}" for index in range(count)]
    if isinstance(value, dict):
        value = list(value.values())
    if not isinstance(value, (list, tuple)):
        return []

    result: list[str] = []
    for index, item in enumerate(value):
        raw_id = item.get("id") if isinstance(item, dict) else item
        if isinstance(raw_id, (str, int, float)) and str(raw_id).strip():
            identifier = str(raw_id).strip()
        else:
            identifier = f"{prefix}:{index}"
        if identifier not in result:
            result.append(identifier)
    return result


def pending_work_from_payload(payload: dict) -> tuple[str, ...] | None:
    """Return the authoritative parent-session work snapshot, when supplied."""
    task_key = next(
        (key for key in ("background_tasks", "backgroundTasks") if key in payload),
        None,
    )
    cron_key = next(
        (key for key in ("session_crons", "sessionCrons") if key in payload),
        None,
    )
    if task_key is None and cron_key is None:
        return None

    result: list[str] = []
    if task_key is not None:
        result.extend(_work_ids(payload.get(task_key), "background"))
    if cron_key is not None:
        for identifier in _work_ids(payload.get(cron_key), "cron"):
            if identifier not in result:
                result.append(identifier)
    return tuple(result)


# --------------------------------------------------------------------------
# Claude Code
# --------------------------------------------------------------------------

# AskUserQuestion always blocks for an answer.  ExitPlanMode presents the
# master-session confirmation flow on supported Claude Code versions.
CLAUDE_WAITING_TOOLS = frozenset({"AskUserQuestion", "ExitPlanMode"})

# ``idle_prompt`` explicitly says Claude is done, so it is completion rather
# than a user-input blocker. ``agent_needs_input`` is only a background-session
# notification; the master can still be working, so it must not turn red.
CLAUDE_WAITING_NOTIFICATION_TYPES = frozenset(
    {"permission_prompt", "elicitation_dialog"}
)
CLAUDE_RESUMED_NOTIFICATION_TYPES = frozenset(
    {"elicitation_complete", "elicitation_response"}
)

_CLAUDE_WAITING_MESSAGE_RE = re.compile(
    r"needs your (permission|approval|input)|permission to use|"
    r"please (approve|answer|choose|confirm|provide)",
    re.IGNORECASE,
)


def _claude_pre_tool_use(payload: dict) -> str:
    tool = payload.get("tool_name") or payload.get("toolName")
    if _is_master(payload) and isinstance(tool, str) and tool in CLAUDE_WAITING_TOOLS:
        return "waiting"
    return "active"


def _claude_permission_request(payload: dict) -> str:
    # Permission requests inside a child must not overwrite the master state.
    # They remain active from the global light's perspective; only a prompt
    # surfaced to the master session turns the lamps red.
    return "waiting" if _is_master(payload) else "active"


def _claude_notification(payload: dict) -> str | None:
    ntype = payload.get("notification_type") or payload.get("notificationType")
    if isinstance(ntype, str):
        if ntype == "agent_needs_input":
            return "active"
        if ntype in CLAUDE_WAITING_NOTIFICATION_TYPES:
            return "waiting" if _is_master(payload) else "active"
        if ntype in CLAUDE_RESUMED_NOTIFICATION_TYPES:
            return "active"
        if ntype == "idle_prompt":
            return "complete" if _is_master(payload) else None
        if ntype == "agent_completed":
            return "complete"
        return None
    message = payload.get("message")
    if _is_master(payload) and isinstance(message, str):
        if _CLAUDE_WAITING_MESSAGE_RE.search(message):
            return "waiting"
    return None


CLAUDE_EVENT_MAP: dict[str, str | None | Callable[[dict], str | None]] = {
    "SessionStart": None,
    "UserPromptSubmit": "active",
    "PreToolUse": _claude_pre_tool_use,
    "PostToolUse": "active",
    "PostToolUseFailure": "active",
    "PostToolBatch": "active",
    "PermissionRequest": _claude_permission_request,
    "Notification": _claude_notification,
    "Elicitation": _claude_permission_request,
    "ElicitationResult": "active",
    "Stop": "complete",
    "SubagentStart": "active",
    "SubagentStop": "complete",
    "TeammateIdle": "complete",
    "StopFailure": "complete",
    # SessionEnd commonly follows a normal Stop; keep the completion hold.
    # An explicit normalized ``ended`` event remains available for abort/reset.
    "SessionEnd": "complete",
    "PreCompact": "active",
}


# --------------------------------------------------------------------------
# Codex
# --------------------------------------------------------------------------


def _codex_permission_request(payload: dict) -> str:
    return "waiting" if _is_master(payload) else "active"


_CODEX_WAITING_TOOLS = frozenset(
    {"request_user_input", "RequestUserInput", "AskUserQuestion"}
)


def _codex_pre_tool_use(payload: dict) -> str:
    tool = payload.get("tool_name") or payload.get("toolName")
    if _is_master(payload) and isinstance(tool, str):
        if (
            tool in _CODEX_WAITING_TOOLS
            or tool.rsplit("__", 1)[-1] in _CODEX_WAITING_TOOLS
        ):
            return "waiting"
    return "active"


CODEX_EVENT_MAP: dict[str, str | None | Callable[[dict], str | None]] = {
    "SessionStart": None,  # active only once a prompt actually arrives
    "UserPromptSubmit": "active",
    "PreToolUse": _codex_pre_tool_use,
    "PostToolUse": "active",
    "PermissionRequest": _codex_permission_request,
    "SubagentStart": "active",
    "SubagentStop": "complete",
    "Stop": "complete",
    "StopFailure": "complete",
    "SessionEnd": "complete",
    # Forward-compatible structured UI events.  Normal turn completion is not
    # treated as waiting: only an explicit input request is red.
    "TuiQuestionOpened": "waiting",
    "user-input-requested": "waiting",
    "plan-questions-waiting": "waiting",
}

CODEX_NOTIFY_MAP: dict[str, str] = {
    "agent-turn-complete": "complete",
}

_EVENT_MAPS = {"claude": CLAUDE_EVENT_MAP, "codex": CODEX_EVENT_MAP}
_SUBSESSION_EVENTS = frozenset({"SubagentStart", "SubagentStop", "TeammateIdle"})


def _synthetic_completion_id(payload: dict) -> str:
    """Stable opaque id for completion notifications that expose no agent id."""
    seed = "\0".join(
        str(payload.get(key, "")) for key in ("notification_type", "title", "message")
    )
    digest = hashlib.sha256(seed.encode("utf-8", "replace")).hexdigest()
    return f"notification-{digest[:16]}"


def _event_scope(event_name: str, payload: dict) -> tuple[str, str | None]:
    subsession_id = subsession_id_from_payload(payload)
    ntype = payload.get("notification_type") or payload.get("notificationType")
    if event_name == "Notification" and ntype == "agent_completed":
        return "subsession", subsession_id or _synthetic_completion_id(payload)
    if event_name in _SUBSESSION_EVENTS or subsession_id is not None:
        return "subsession", subsession_id or _synthetic_completion_id(payload)
    return "master", None


def _pending_work_for_event(
    source: str, event_name: str, state: str, payload: dict
) -> tuple[str, ...] | None:
    snapshot = pending_work_from_payload(payload)
    if snapshot is not None:
        return snapshot
    # Codex's completed-turn notification is authoritative for its turn.  Its
    # subagent set is tracked independently by agent_id in the registry.
    if (
        source == "codex"
        and state == "complete"
        and event_name
        in {
            "Stop",
            "StopFailure",
            "SessionEnd",
            "agent-turn-complete",
        }
    ):
        return ()
    if source == "claude" and event_name == "Notification":
        ntype = payload.get("notification_type") or payload.get("notificationType")
        if ntype == "idle_prompt":
            return ()
    if source == "claude" and event_name == "SessionEnd":
        return ()
    return None


def normalize_hook_event(source: str, payload: dict) -> NormalizedEvent | None:
    """Map a raw command-hook payload to a :class:`NormalizedEvent`."""
    if source not in _EVENT_MAPS or not isinstance(payload, dict):
        return None
    event_name = event_name_from_payload(payload)
    rule = _EVENT_MAPS[source].get(event_name)
    state = rule(payload) if callable(rule) else rule
    if state is None:
        return None
    scope, subsession_id = _event_scope(event_name, payload)
    return NormalizedEvent(
        source=source,
        session_id=session_id_from_payload(source, payload),
        state=state,
        event=event_name,
        scope=scope,
        subsession_id=subsession_id,
        turn_id=turn_id_from_payload(payload),
        pending_work=_pending_work_for_event(source, event_name, state, payload),
    )


def normalize_codex_notification(payload: dict) -> NormalizedEvent | None:
    """Map a Codex ``notify`` JSON payload (argv[1])."""
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
        scope="master",
        turn_id=turn_id_from_payload(payload),
        pending_work=(),
    )
