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
