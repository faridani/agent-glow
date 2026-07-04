"""Normalization tables: hook payloads -> active/waiting/ended."""

from hue_agent_status.events import (
    normalize_codex_notification,
    normalize_hook_event,
)


def _claude(event_name, **extra):
    payload = {"hook_event_name": event_name, "session_id": "s1", **extra}
    return normalize_hook_event("claude", payload)


def _codex(event_name, **extra):
    payload = {"hook_event_name": event_name, "session_id": "s1", **extra}
    return normalize_hook_event("codex", payload)


class TestClaudeNormalization:
    def test_user_prompt_submit_is_active(self):
        assert _claude("UserPromptSubmit").state == "active"

    def test_pre_tool_use_is_active(self):
        assert _claude("PreToolUse", tool_name="Bash").state == "active"

    def test_ask_user_question_is_waiting(self):
        assert _claude("PreToolUse", tool_name="AskUserQuestion").state == "waiting"

    def test_exit_plan_mode_is_waiting(self):
        assert _claude("PreToolUse", tool_name="ExitPlanMode").state == "waiting"

    def test_post_tool_use_and_batch_are_active(self):
        assert _claude("PostToolUse").state == "active"
        assert _claude("PostToolBatch").state == "active"

    def test_permission_request_is_waiting(self):
        assert _claude("PermissionRequest").state == "waiting"

    def test_notification_waiting_types(self):
        for ntype in ("permission_prompt", "idle_prompt", "elicitation_dialog"):
            assert _claude("Notification", notification_type=ntype).state == "waiting"

    def test_notification_other_type_is_noop(self):
        assert _claude("Notification", notification_type="info") is None

    def test_notification_permission_message_heuristic(self):
        event = _claude("Notification", message="Claude needs your permission to use Bash")
        assert event.state == "waiting"

    def test_notification_plain_message_is_noop(self):
        assert _claude("Notification", message="Task finished") is None

    def test_stop_is_waiting(self):
        assert _claude("Stop").state == "waiting"

    def test_stop_with_background_tasks_is_active(self):
        assert _claude("Stop", background_tasks=[{"id": 1}]).state == "active"
        assert _claude("Stop", backgroundTasks=2).state == "active"
        assert _claude("Stop", background_tasks=[]).state == "waiting"

    def test_stop_failure_is_waiting(self):
        assert _claude("StopFailure").state == "waiting"

    def test_session_end_is_ended(self):
        assert _claude("SessionEnd").state == "ended"

    def test_session_start_is_noop(self):
        assert _claude("SessionStart") is None

    def test_unknown_event_is_noop(self):
        assert _claude("SomethingNew") is None


class TestCodexNormalization:
    def test_active_events(self):
        for name in ("UserPromptSubmit", "PreToolUse", "PostToolUse", "SubagentStart"):
            assert _codex(name).state == "active"

    def test_permission_request_is_waiting(self):
        assert _codex("PermissionRequest").state == "waiting"

    def test_stop_is_waiting(self):
        assert _codex("Stop").state == "waiting"

    def test_session_start_is_noop(self):
        assert _codex("SessionStart") is None

    def test_subagent_stop_active_when_turn_active(self):
        assert _codex("SubagentStop").state == "active"
        assert _codex("SubagentStop", turn_active=True).state == "active"

    def test_subagent_stop_noop_when_turn_ended(self):
        assert _codex("SubagentStop", turn_active=False) is None

    def test_future_waiting_events_table(self):
        for name in ("TuiQuestionOpened", "user-input-requested", "plan-questions-waiting"):
            assert _codex(name).state == "waiting"


class TestTurnEndClassification:
    """'Your turn' waiting decays quickly; 'agent blocked' waiting persists."""

    def test_stop_is_turn_end(self):
        assert _claude("Stop").turn_end is True
        assert _claude("StopFailure").turn_end is True

    def test_blocked_waiting_is_not_turn_end(self):
        assert _claude("PermissionRequest").turn_end is False
        assert _claude("PreToolUse", tool_name="AskUserQuestion").turn_end is False
        assert _claude("Notification", notification_type="permission_prompt").turn_end is False

    def test_idle_notification_is_turn_end(self):
        assert _claude("Notification", notification_type="idle_prompt").turn_end is True

    def test_waiting_message_heuristic_is_turn_end(self):
        event = _claude("Notification", message="Claude is waiting for your input")
        assert event.state == "waiting" and event.turn_end is True

    def test_codex_stop_and_notify_are_turn_end(self):
        assert _codex("Stop").turn_end is True
        event = normalize_codex_notification({"type": "agent-turn-complete"})
        assert event.turn_end is True

    def test_active_events_never_turn_end(self):
        assert _claude("UserPromptSubmit").turn_end is False
        assert _claude("Stop", background_tasks=[1]).turn_end is False  # still active

    def test_turn_end_travels_in_payload(self):
        assert _claude("Stop").to_payload()["turn_end"] is True
        assert _claude("PermissionRequest").to_payload()["turn_end"] is False


class TestSessionIds:
    def test_session_id_passthrough(self):
        event = _claude("UserPromptSubmit")
        assert event.session_id == "s1"

    def test_fallback_session_id_is_stable_per_cwd(self):
        p1 = {"hook_event_name": "UserPromptSubmit", "cwd": "/tmp/project"}
        p2 = {"hook_event_name": "UserPromptSubmit", "cwd": "/tmp/project"}
        p3 = {"hook_event_name": "UserPromptSubmit", "cwd": "/tmp/other"}
        e1 = normalize_hook_event("claude", p1)
        e2 = normalize_hook_event("claude", p2)
        e3 = normalize_hook_event("claude", p3)
        assert e1.session_id == e2.session_id
        assert e1.session_id != e3.session_id
        assert e1.session_id.startswith("cwd-")

    def test_long_session_id_truncated(self):
        event = normalize_hook_event(
            "claude", {"hook_event_name": "UserPromptSubmit", "session_id": "x" * 500}
        )
        assert len(event.session_id) == 128

    def test_unknown_source_rejected(self):
        assert normalize_hook_event("gemini", {"hook_event_name": "Stop"}) is None


class TestCodexNotify:
    def test_agent_turn_complete_is_waiting(self):
        event = normalize_codex_notification(
            {"type": "agent-turn-complete", "thread_id": "t9"}
        )
        assert event.state == "waiting"
        assert event.source == "codex"
        assert event.session_id == "t9"

    def test_unknown_type_is_noop(self):
        assert normalize_codex_notification({"type": "something-else"}) is None
        assert normalize_codex_notification({}) is None
        assert normalize_codex_notification("nope") is None
