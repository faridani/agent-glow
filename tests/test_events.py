"""Hook-payload normalization into hierarchical lifecycle events."""

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
    def test_work_events_are_active(self):
        for name in (
            "UserPromptSubmit",
            "PostToolUse",
            "PostToolUseFailure",
            "PostToolBatch",
            "PreCompact",
            "ElicitationResult",
        ):
            assert _claude(name).state == "active"
        assert _claude("PreToolUse", tool_name="Bash").state == "active"

    def test_explicit_master_question_and_permission_are_waiting(self):
        for tool in ("AskUserQuestion", "ExitPlanMode"):
            event = _claude("PreToolUse", tool_name=tool)
            assert event.state == "waiting" and event.scope == "master"
        assert _claude("PermissionRequest").state == "waiting"
        assert _claude("Elicitation").state == "waiting"

    def test_subsession_permission_does_not_turn_master_red(self):
        event = _claude("PermissionRequest", agent_id="child-1")
        assert event.state == "active"
        assert event.scope == "subsession"
        assert event.subsession_id == "child-1"

    def test_notification_waiting_types_are_explicit(self):
        for ntype in (
            "permission_prompt",
            "elicitation_dialog",
        ):
            event = _claude("Notification", notification_type=ntype)
            assert event.state == "waiting" and event.scope == "master"

    def test_background_agent_input_notification_keeps_working_state(self):
        event = _claude("Notification", notification_type="agent_needs_input")
        assert event.state == "active"
        assert event.scope == "master"

    def test_child_notification_never_sets_master_waiting_or_complete(self):
        event = _claude(
            "Notification", notification_type="permission_prompt", agent_id="child"
        )
        assert event.state == "active" and event.scope == "subsession"
        assert (
            _claude("Notification", notification_type="idle_prompt", agent_id="child")
            is None
        )

    def test_idle_prompt_is_completion_not_waiting(self):
        event = _claude("Notification", notification_type="idle_prompt")
        assert event.state == "complete"
        assert event.scope == "master"
        assert event.pending_work == ()

    def test_notification_resume_and_other_types(self):
        for ntype in ("elicitation_complete", "elicitation_response"):
            assert _claude("Notification", notification_type=ntype).state == "active"
        assert _claude("Notification", notification_type="auth_success") is None

    def test_background_agent_completion_is_a_subsession_signal(self):
        event = _claude(
            "Notification",
            notification_type="agent_completed",
            title="Background agent finished",
            message="research session completed",
        )
        duplicate = _claude(
            "Notification",
            notification_type="agent_completed",
            title="Background agent finished",
            message="research session completed",
        )
        assert event.state == "complete"
        assert event.scope == "subsession"
        assert event.subsession_id.startswith("notification-")
        assert event.subsession_id == duplicate.subsession_id

    def test_teammate_idle_is_a_named_subsession_completion(self):
        event = _claude("TeammateIdle", teammate_name="researcher")
        assert event.state == "complete"
        assert event.scope == "subsession"
        assert event.subsession_id == "researcher"

    def test_notification_permission_message_heuristic(self):
        event = _claude(
            "Notification", message="Claude needs your permission to use Bash"
        )
        assert event.state == "waiting"
        assert _claude("Notification", message="Task finished") is None

    def test_stop_is_complete_with_authoritative_pending_work(self):
        event = _claude(
            "Stop",
            prompt_id="prompt-1",
            background_tasks=[{"id": "agent-1"}, {"id": "shell-2"}],
            session_crons=[{"id": "cron-1"}],
        )
        assert event.state == "complete"
        assert event.turn_id == "prompt-1"
        assert event.pending_work == ("agent-1", "shell-2", "cron-1")

    def test_stop_without_snapshot_is_still_complete(self):
        event = _claude("Stop")
        assert event.state == "complete"
        assert event.pending_work is None

    def test_legacy_background_count_remains_active_evidence(self):
        event = _claude("Stop", backgroundTasks=2)
        assert event.state == "complete"
        assert len(event.pending_work) == 2

    def test_stop_failure_and_session_end_are_complete_never_waiting(self):
        assert _claude("StopFailure").state == "complete"
        session_end = _claude("SessionEnd")
        assert session_end.state == "complete"
        assert session_end.pending_work == ()

    def test_subagent_lifecycle_has_parent_and_child_ids(self):
        start = _claude("SubagentStart", agent_id="agent-7", prompt_id="prompt-2")
        assert start.state == "active"
        assert start.scope == "subsession"
        assert start.session_id == "s1"
        assert start.subsession_id == "agent-7"
        assert start.turn_id == "prompt-2"

        stop = _claude(
            "SubagentStop",
            agent_id="agent-7",
            prompt_id="prompt-2",
            background_tasks=[],
            session_crons=[],
        )
        assert stop.state == "complete"
        assert stop.scope == "subsession"
        assert stop.pending_work == ()

    def test_session_start_and_unknown_event_are_noops(self):
        assert _claude("SessionStart") is None
        assert _claude("SomethingNew") is None


class TestCodexNormalization:
    def test_work_events_are_active(self):
        for name in ("UserPromptSubmit", "PostToolUse"):
            assert _codex(name).state == "active"
        assert _codex("PreToolUse", tool_name="Bash").state == "active"

    def test_explicit_input_tools_are_waiting(self):
        for tool in (
            "request_user_input",
            "RequestUserInput",
            "AskUserQuestion",
            "functions__request_user_input",
        ):
            assert _codex("PreToolUse", tool_name=tool).state == "waiting"

    def test_subsession_input_tool_does_not_set_master_waiting(self):
        event = _codex("PreToolUse", tool_name="request_user_input", agent_id="child-1")
        assert event.state == "active"
        assert event.scope == "subsession"

    def test_permission_request_is_waiting_only_for_master(self):
        assert _codex("PermissionRequest").state == "waiting"
        assert _codex("PermissionRequest", agent_id="child").state == "active"

    def test_master_stop_and_failure_are_complete(self):
        for name in ("Stop", "StopFailure", "SessionEnd"):
            event = _codex(name, turn_id="turn-1")
            assert event.state == "complete"
            assert event.scope == "master"
            assert event.turn_id == "turn-1"
            assert event.pending_work == ()

    def test_subagent_start_and_stop_are_scoped(self):
        start = _codex("SubagentStart", agent_id="agent-1", turn_id="turn-2")
        stop = _codex("SubagentStop", agent_id="agent-1", turn_id="turn-2")
        assert start.state == "active"
        assert stop.state == "complete"
        assert start.scope == stop.scope == "subsession"
        assert start.subsession_id == stop.subsession_id == "agent-1"

    def test_structured_future_wait_events_are_waiting(self):
        for name in (
            "TuiQuestionOpened",
            "user-input-requested",
            "plan-questions-waiting",
        ):
            assert _codex(name).state == "waiting"

    def test_session_start_is_noop(self):
        assert _codex("SessionStart") is None


class TestIdentifiersAndPayload:
    def test_fallback_session_id_is_stable_per_cwd(self):
        first = normalize_hook_event(
            "claude", {"hook_event_name": "UserPromptSubmit", "cwd": "/tmp/one"}
        )
        again = normalize_hook_event(
            "claude", {"hook_event_name": "UserPromptSubmit", "cwd": "/tmp/one"}
        )
        other = normalize_hook_event(
            "claude", {"hook_event_name": "UserPromptSubmit", "cwd": "/tmp/two"}
        )
        assert first.session_id == again.session_id
        assert first.session_id != other.session_id
        assert first.session_id.startswith("cwd-")

    def test_long_identifiers_are_bounded(self):
        event = normalize_hook_event(
            "claude",
            {
                "hook_event_name": "SubagentStart",
                "session_id": "s" * 500,
                "prompt_id": "p" * 500,
                "agent_id": "a" * 500,
            },
        )
        assert len(event.session_id) == 128
        assert len(event.turn_id) == 128
        assert len(event.subsession_id) == 128

    def test_hierarchical_fields_travel_in_payload(self):
        event = _claude(
            "SubagentStop",
            agent_id="child",
            prompt_id="turn",
            background_tasks=[],
        )
        assert event.to_payload() == {
            "source": "claude",
            "session_id": "s1",
            "state": "complete",
            "event": "SubagentStop",
            "scope": "subsession",
            "subsession_id": "child",
            "turn_id": "turn",
            "pending_work": [],
        }

    def test_unknown_source_rejected(self):
        assert normalize_hook_event("gemini", {"hook_event_name": "Stop"}) is None


class TestCodexNotify:
    def test_agent_turn_complete_is_master_completion(self):
        event = normalize_codex_notification(
            {
                "type": "agent-turn-complete",
                "thread-id": "thread-9",
                "turn-id": "turn-9",
            }
        )
        assert event.state == "complete"
        assert event.scope == "master"
        assert event.source == "codex"
        assert event.session_id == "thread-9"
        assert event.turn_id == "turn-9"
        assert event.pending_work == ()

    def test_unknown_type_is_noop(self):
        assert normalize_codex_notification({"type": "something-else"}) is None
        assert normalize_codex_notification({}) is None
        assert normalize_codex_notification("nope") is None
