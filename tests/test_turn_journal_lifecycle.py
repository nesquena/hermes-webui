import os

from api import turn_journal
from api.turn_journal import (
    append_turn_journal_event,
    append_turn_journal_event_for_stream,
    derive_turn_journal_states,
)


def test_append_turn_journal_event_for_stream_reuses_submitted_turn_id(tmp_path):
    submitted = append_turn_journal_event(
        "sid-1",
        {"event": "submitted", "turn_id": "turn-1", "stream_id": "stream-1", "content": "hello"},
        session_dir=tmp_path,
    )

    worker = append_turn_journal_event_for_stream(
        "sid-1",
        "stream-1",
        {"event": "worker_started"},
        session_dir=tmp_path,
    )

    assert submitted["turn_id"] == "turn-1"
    assert worker["turn_id"] == "turn-1"
    states, _ = derive_turn_journal_states([submitted, worker])
    assert states["turn-1"]["event"] == "worker_started"


def test_append_turn_journal_event_for_stream_falls_back_to_new_turn_for_missing_stream(tmp_path):
    event = append_turn_journal_event_for_stream(
        "sid-1",
        "stream-missing",
        {"event": "interrupted", "reason": "no submitted event found"},
        session_dir=tmp_path,
    )

    assert event["stream_id"] == "stream-missing"
    assert event["turn_id"]
    assert event["event"] == "interrupted"


def test_append_turn_journal_event_skips_directory_fsync_without_o_directory(tmp_path, monkeypatch):
    monkeypatch.delattr(os, "O_DIRECTORY", raising=False)

    event = append_turn_journal_event(
        "sid-windows",
        {"event": "submitted", "content": "hello"},
        session_dir=tmp_path,
    )

    assert event["event"] == "submitted"
    journal_dir = tmp_path / "_turn_journal"
    shards = list(journal_dir.glob(f"sid-windows~{os.getpid()}.jsonl"))
    assert len(shards) == 1, f"expected one pid-scoped shard, found: {list(journal_dir.iterdir())}"


def test_find_active_idempotent_turn_ignores_interrupted_attempt(tmp_path):
    append_turn_journal_event(
        "sid-1",
        {
            "event": "submitted",
            "turn_id": "turn-1",
            "stream_id": "stream-1",
            "idempotency_key": "kanban:default:t_1:4",
        },
        session_dir=tmp_path,
    )
    append_turn_journal_event_for_stream(
        "sid-1",
        "stream-1",
        {"event": "interrupted", "reason": "worker_start_failed"},
        session_dir=tmp_path,
    )

    assert (
        turn_journal.find_active_idempotent_turn(
            "sid-1", "kanban:default:t_1:4", session_dir=tmp_path
        )
        is None
    )


def test_find_active_idempotent_turn_returns_started_attempt(tmp_path):
    append_turn_journal_event(
        "sid-1",
        {
            "event": "submitted",
            "turn_id": "turn-1",
            "stream_id": "stream-1",
            "idempotency_key": "kanban:default:t_1:4",
        },
        session_dir=tmp_path,
    )
    append_turn_journal_event_for_stream(
        "sid-1",
        "stream-1",
        {"event": "worker_started"},
        session_dir=tmp_path,
    )

    event = turn_journal.find_active_idempotent_turn(
        "sid-1",
        "kanban:default:t_1:4",
        active_stream_id="stream-1",
        session_dir=tmp_path,
    )

    assert event is not None
    assert event["stream_id"] == "stream-1"
    assert event["event"] == "worker_started"


def test_find_active_idempotent_turn_retries_stale_started_attempt(tmp_path):
    append_turn_journal_event(
        "sid-1",
        {
            "event": "submitted",
            "turn_id": "turn-1",
            "stream_id": "stream-1",
            "idempotency_key": "kanban:default:t_1:4",
        },
        session_dir=tmp_path,
    )
    append_turn_journal_event_for_stream(
        "sid-1",
        "stream-1",
        {"event": "worker_started"},
        session_dir=tmp_path,
    )

    assert (
        turn_journal.find_active_idempotent_turn(
            "sid-1",
            "kanban:default:t_1:4",
            active_stream_id=None,
            session_dir=tmp_path,
        )
        is None
    )


def test_find_active_idempotent_turn_keeps_prior_worker_acceptance(tmp_path):
    append_turn_journal_event(
        "sid-1",
        {
            "event": "submitted",
            "turn_id": "turn-1",
            "stream_id": "stream-1",
            "idempotency_key": "kanban:default:t_1:4",
        },
        session_dir=tmp_path,
    )
    append_turn_journal_event_for_stream(
        "sid-1",
        "stream-1",
        {"event": "worker_started"},
        session_dir=tmp_path,
    )
    append_turn_journal_event_for_stream(
        "sid-1",
        "stream-1",
        {"event": "assistant_started"},
        session_dir=tmp_path,
    )

    event = turn_journal.find_active_idempotent_turn(
        "sid-1",
        "kanban:default:t_1:4",
        active_stream_id="stream-1",
        session_dir=tmp_path,
    )

    assert event is not None
    assert event["event"] == "assistant_started"


def test_find_active_idempotent_turn_retries_accepted_attempt_after_interruption(
    tmp_path,
):
    append_turn_journal_event(
        "sid-1",
        {
            "event": "submitted",
            "turn_id": "turn-1",
            "stream_id": "stream-1",
            "idempotency_key": "kanban:default:t_1:4",
        },
        session_dir=tmp_path,
    )
    append_turn_journal_event_for_stream(
        "sid-1",
        "stream-1",
        {"event": "worker_started"},
        session_dir=tmp_path,
    )
    append_turn_journal_event_for_stream(
        "sid-1",
        "stream-1",
        {"event": "interrupted", "reason": "provider_error"},
        session_dir=tmp_path,
    )

    assert (
        turn_journal.find_active_idempotent_turn(
            "sid-1",
            "kanban:default:t_1:4",
            active_stream_id=None,
            session_dir=tmp_path,
        )
        is None
    )


def test_find_active_idempotent_turn_returns_submitted_attempt_only_while_live(tmp_path):
    append_turn_journal_event(
        "sid-1",
        {
            "event": "submitted",
            "turn_id": "turn-1",
            "stream_id": "stream-1",
            "idempotency_key": "kanban:default:t_1:4",
        },
        session_dir=tmp_path,
    )

    event = turn_journal.find_active_idempotent_turn(
        "sid-1",
        "kanban:default:t_1:4",
        active_stream_id="stream-1",
        session_dir=tmp_path,
    )

    assert event is not None
    assert event["event"] == "submitted"


def test_find_active_idempotent_turn_ignores_stale_submitted_attempt(tmp_path):
    append_turn_journal_event(
        "sid-1",
        {
            "event": "submitted",
            "turn_id": "turn-1",
            "stream_id": "stream-dead",
            "idempotency_key": "kanban:default:t_1:4",
        },
        session_dir=tmp_path,
    )

    assert (
        turn_journal.find_active_idempotent_turn(
            "sid-1",
            "kanban:default:t_1:4",
            active_stream_id=None,
            session_dir=tmp_path,
        )
        is None
    )


def test_find_active_idempotent_turn_returns_completed_attempt_when_idle(tmp_path):
    append_turn_journal_event(
        "sid-1",
        {
            "event": "submitted",
            "turn_id": "turn-1",
            "stream_id": "stream-1",
            "idempotency_key": "kanban:default:t_1:4",
        },
        session_dir=tmp_path,
    )
    append_turn_journal_event_for_stream(
        "sid-1",
        "stream-1",
        {"event": "completed"},
        session_dir=tmp_path,
    )

    event = turn_journal.find_active_idempotent_turn(
        "sid-1",
        "kanban:default:t_1:4",
        active_stream_id=None,
        session_dir=tmp_path,
    )

    assert event is not None
    assert event["event"] == "completed"


def test_find_active_idempotent_turn_keeps_completed_attempt_after_late_interruption(
    tmp_path,
):
    append_turn_journal_event(
        "sid-1",
        {
            "event": "submitted",
            "turn_id": "turn-1",
            "stream_id": "stream-1",
            "idempotency_key": "kanban:default:t_1:4",
        },
        session_dir=tmp_path,
    )
    append_turn_journal_event_for_stream(
        "sid-1",
        "stream-1",
        {"event": "completed"},
        session_dir=tmp_path,
    )
    append_turn_journal_event_for_stream(
        "sid-1",
        "stream-1",
        {"event": "interrupted", "reason": "late_cancel"},
        session_dir=tmp_path,
    )

    event = turn_journal.find_active_idempotent_turn(
        "sid-1",
        "kanban:default:t_1:4",
        active_stream_id=None,
        session_dir=tmp_path,
    )

    assert event is not None
    assert event["event"] == "completed"
