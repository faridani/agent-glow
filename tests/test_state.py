"""Hierarchical session aggregation, completion signals, and TTLs."""

import pytest

from hue_agent_status.events import NormalizedEvent
from hue_agent_status.state import SessionRegistry


def _ev(
    source="claude",
    session="s1",
    state="active",
    *,
    event="",
    scope="master",
    subsession=None,
    turn=None,
    pending=None,
):
    return NormalizedEvent(
        source=source,
        session_id=session,
        state=state,
        event=event,
        scope=scope,
        subsession_id=subsession,
        turn_id=turn,
        pending_work=pending,
    )


def test_empty_registry_is_idle():
    registry = SessionRegistry()
    assert registry.aggregate() == "idle"
    assert registry.next_expiry_delay(now=0) is None


def test_active_waiting_and_source_scoping():
    registry = SessionRegistry()
    registry.apply_event(_ev(session="a"), now=100)
    registry.apply_event(_ev(source="codex", session="a"), now=100)
    assert len(registry) == 2
    assert registry.aggregate() == "active"

    registry.apply_event(_ev(session="a", state="waiting"), now=101)
    assert registry.aggregate() == "waiting"


def test_subsession_wait_does_not_turn_master_red():
    registry = SessionRegistry()
    registry.apply_event(_ev(), now=100)
    registry.apply_event(
        _ev(state="waiting", scope="subsession", subsession="child", turn="t1"),
        now=101,
    )
    assert registry.aggregate() == "active"


def test_master_completion_holds_green_for_exact_ttl():
    registry = SessionRegistry(completion_hold_seconds=300)
    registry.apply_event(_ev(state="complete", turn="t1", pending=()), now=100)
    assert registry.aggregate() == "complete"
    assert registry.next_expiry_delay(now=250) == pytest.approx(150)
    assert registry.prune(now=399.999) == 0
    assert registry.aggregate() == "complete"
    assert registry.prune(now=400) == 1
    assert registry.aggregate() == "idle"


def test_duplicate_stop_and_notify_do_not_extend_completion_hold():
    registry = SessionRegistry(completion_hold_seconds=300)
    registry.apply_event(
        _ev(state="complete", event="Stop", turn="t1", pending=()), now=100
    )
    registry.apply_event(
        _ev(
            source="claude",
            state="complete",
            event="Notification",
            turn="t1",
            pending=(),
        ),
        now=200,
    )
    assert registry.next_expiry_delay(now=250) == pytest.approx(150)

    registry.apply_event(
        _ev(
            source="claude",
            state="complete",
            event="Notification",
            turn=None,
            pending=(),
        ),
        now=300,
    )
    assert registry.next_expiry_delay(now=350) == pytest.approx(50)


def test_pending_master_completion_remains_active_until_cleared():
    registry = SessionRegistry()
    registry.apply_event(_ev(event="UserPromptSubmit", turn="t1"), now=100)
    registry.apply_event(
        _ev(state="complete", event="Stop", turn="t1", pending=("job-1",)),
        now=110,
    )
    assert registry.aggregate() == "active"

    # A later authoritative snapshot for the same turn clears the pending job.
    registry.apply_event(
        _ev(state="complete", event="Stop", turn="t1", pending=()), now=150
    )
    assert registry.aggregate() == "complete"
    assert registry.next_expiry_delay(now=150) == pytest.approx(300)


def test_subsession_finish_blinks_once_then_master_keeps_working():
    registry = SessionRegistry()
    registry.apply_event(_ev(event="UserPromptSubmit", turn="t1"), now=100)
    start = _ev(scope="subsession", subsession="child-1", turn="t1", state="active")
    finish = _ev(scope="subsession", subsession="child-1", turn="t1", state="complete")
    assert registry.apply_event(start, now=101) is False
    assert registry.apply_event(finish, now=110) is True
    assert registry.apply_event(finish, now=111) is False
    assert registry.aggregate() == "active"


def test_restarted_subsession_can_finish_again():
    registry = SessionRegistry()
    registry.apply_event(_ev(turn="t1"), now=100)
    start = _ev(scope="subsession", subsession="child", turn="t1", state="active")
    finish = _ev(scope="subsession", subsession="child", turn="t1", state="complete")
    registry.apply_event(start, now=101)
    assert registry.apply_event(finish, now=102) is True
    registry.apply_event(start, now=103)
    assert registry.apply_event(finish, now=104) is True


def test_master_complete_waits_for_open_subsession_then_holds_green():
    registry = SessionRegistry(completion_hold_seconds=300)
    registry.apply_event(_ev(turn="t1"), now=100)
    registry.apply_event(
        _ev(scope="subsession", subsession="child", turn="t1"), now=101
    )
    registry.apply_event(
        _ev(state="complete", event="Stop", turn="t1", pending=()), now=102
    )
    assert registry.aggregate() == "active"

    did_finish = registry.apply_event(
        _ev(
            scope="subsession",
            subsession="child",
            turn="t1",
            state="complete",
            pending=(),
        ),
        now=110,
    )
    assert did_finish is True
    assert registry.aggregate() == "complete"
    assert registry.next_expiry_delay(now=110) == pytest.approx(300)


def test_child_finishes_before_master_completion():
    registry = SessionRegistry()
    registry.apply_event(_ev(turn="t1"), now=100)
    registry.apply_event(
        _ev(scope="subsession", subsession="child", turn="t1"), now=101
    )
    assert registry.apply_event(
        _ev(
            scope="subsession",
            subsession="child",
            turn="t1",
            state="complete",
        ),
        now=102,
    )
    assert registry.aggregate() == "active"
    registry.apply_event(
        _ev(state="complete", event="Stop", turn="t1", pending=()), now=110
    )
    assert registry.aggregate() == "complete"


def test_master_waiting_stays_red_when_subsession_finishes():
    registry = SessionRegistry()
    registry.apply_event(_ev(state="waiting", turn="t1"), now=100)
    registry.apply_event(
        _ev(scope="subsession", subsession="child", turn="t1"), now=101
    )
    assert registry.apply_event(
        _ev(
            scope="subsession",
            subsession="child",
            turn="t1",
            state="complete",
        ),
        now=102,
    )
    assert registry.aggregate() == "waiting"


def test_new_prompt_cancels_green_hold():
    registry = SessionRegistry()
    registry.apply_event(_ev(state="complete", turn="t1", pending=()), now=100)
    assert registry.aggregate() == "complete"
    registry.apply_event(
        _ev(state="active", event="UserPromptSubmit", turn="t2"), now=150
    )
    assert registry.aggregate() == "active"


def test_explicit_ended_removes_session_even_during_completion_hold():
    registry = SessionRegistry()
    registry.apply_event(_ev(state="complete", turn="t1", pending=()), now=100)
    registry.apply_event(_ev(state="ended", turn="t1"), now=101)
    assert registry.aggregate() == "idle"
    assert len(registry) == 0


def test_active_and_waiting_ttl_pruning():
    registry = SessionRegistry(active_ttl_seconds=10, waiting_ttl_seconds=20)
    registry.apply_event(_ev(session="active"), now=0)
    registry.apply_event(_ev(session="waiting", state="waiting"), now=0)
    assert registry.prune(now=10) == 1
    assert registry.aggregate() == "waiting"
    assert registry.prune(now=20) == 1
    assert registry.aggregate() == "idle"


def test_constructor_accepts_old_completion_ttl_alias():
    registry = SessionRegistry(turn_end_waiting_seconds=12)
    registry.apply_event(_ev(state="complete", pending=()), now=0)
    assert registry.next_expiry_delay(now=2) == pytest.approx(10)


def test_describe_reports_effective_state_without_payload_data():
    registry = SessionRegistry()
    registry.apply_event(_ev(state="complete", pending=("private-job",)), now=100)
    (info,) = registry.describe(now=105)
    assert set(info) == {"source", "session_id", "state", "age_seconds"}
    assert info == {
        "source": "claude",
        "session_id": "s1",
        "state": "active",
        "age_seconds": 5.0,
    }


def test_invalid_state_and_scope_are_ignored():
    registry = SessionRegistry()
    assert registry.apply_event(_ev(state="exploded"), now=100) is False
    assert registry.apply_event(_ev(scope="galaxy"), now=100) is False
    assert len(registry) == 0
