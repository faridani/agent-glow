"""Session registry aggregation and TTL pruning."""

from hue_agent_status.events import NormalizedEvent
from hue_agent_status.state import SessionRegistry


def _ev(source="claude", session="s1", state="active", turn_end=False):
    return NormalizedEvent(
        source=source, session_id=session, state=state, turn_end=turn_end
    )


def test_empty_registry_is_idle():
    assert SessionRegistry().aggregate() == "idle"


def test_active_session_aggregates_active():
    reg = SessionRegistry()
    reg.apply_event(_ev(state="active"), now=100)
    assert reg.aggregate() == "active"


def test_waiting_beats_active():
    reg = SessionRegistry()
    reg.apply_event(_ev(session="a", state="active"), now=100)
    reg.apply_event(_ev(session="b", state="waiting"), now=100)
    reg.apply_event(_ev(source="codex", session="c", state="active"), now=100)
    assert reg.aggregate() == "waiting"


def test_ended_removes_session():
    reg = SessionRegistry()
    reg.apply_event(_ev(state="waiting"), now=100)
    reg.apply_event(_ev(state="ended"), now=101)
    assert reg.aggregate() == "idle"
    assert len(reg) == 0


def test_same_session_state_transitions():
    reg = SessionRegistry()
    reg.apply_event(_ev(state="active"), now=100)
    reg.apply_event(_ev(state="waiting"), now=101)
    assert reg.aggregate() == "waiting"
    reg.apply_event(_ev(state="active"), now=102)
    assert reg.aggregate() == "active"
    assert len(reg) == 1


def test_sessions_keyed_by_source_and_id():
    reg = SessionRegistry()
    reg.apply_event(_ev(source="claude", session="s1", state="active"), now=100)
    reg.apply_event(_ev(source="codex", session="s1", state="waiting"), now=100)
    assert len(reg) == 2


def test_active_ttl_pruning():
    reg = SessionRegistry(active_ttl_seconds=1800, waiting_ttl_seconds=14400)
    reg.apply_event(_ev(state="active"), now=0)
    assert reg.prune(now=1799) == 0
    assert reg.aggregate() == "active"
    assert reg.prune(now=1801) == 1
    assert reg.aggregate() == "idle"


def test_waiting_ttl_longer_than_active():
    reg = SessionRegistry(active_ttl_seconds=1800, waiting_ttl_seconds=14400)
    reg.apply_event(_ev(session="a", state="active"), now=0)
    reg.apply_event(_ev(session="w", state="waiting"), now=0)
    reg.prune(now=2000)
    assert reg.aggregate() == "waiting"  # active expired, waiting survives
    reg.prune(now=14401)
    assert reg.aggregate() == "idle"


def test_turn_end_waiting_expires_quickly():
    reg = SessionRegistry(
        active_ttl_seconds=1800, waiting_ttl_seconds=14400, turn_end_waiting_seconds=300
    )
    reg.apply_event(_ev(state="waiting", turn_end=True), now=0)  # Stop fired
    assert reg.aggregate() == "waiting"  # red right after the turn ends
    reg.prune(now=299)
    assert reg.aggregate() == "waiting"
    reg.prune(now=301)
    assert reg.aggregate() == "idle"  # lights restore a few minutes later


def test_blocked_waiting_uses_long_ttl():
    reg = SessionRegistry(
        active_ttl_seconds=1800, waiting_ttl_seconds=14400, turn_end_waiting_seconds=300
    )
    reg.apply_event(_ev(state="waiting", turn_end=False), now=0)  # PermissionRequest
    reg.prune(now=3600)
    assert reg.aggregate() == "waiting"  # still demanding attention
    reg.prune(now=14401)
    assert reg.aggregate() == "idle"


def test_new_prompt_clears_turn_end_flag():
    reg = SessionRegistry(turn_end_waiting_seconds=300)
    reg.apply_event(_ev(state="waiting", turn_end=True), now=0)
    reg.apply_event(_ev(state="active"), now=100)  # user came back
    reg.apply_event(_ev(state="waiting", turn_end=False), now=200)  # now blocked
    reg.prune(now=1000)
    assert reg.aggregate() == "waiting"  # long TTL applies again


def test_describe_contains_no_payload_fields():
    reg = SessionRegistry()
    reg.apply_event(_ev(state="active"), now=100)
    (info,) = reg.describe(now=105)
    assert set(info) == {"source", "session_id", "state", "age_seconds"}
    assert info["age_seconds"] == 5.0


def test_invalid_state_ignored():
    reg = SessionRegistry()
    reg.apply_event(_ev(state="exploded"), now=100)
    assert len(reg) == 0
