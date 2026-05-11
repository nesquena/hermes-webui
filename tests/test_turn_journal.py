import json

from api.session_recovery import audit_session_recovery
from api.turn_journal import (
    append_turn_journal_event,
    derive_turn_journal_states,
    read_turn_journal,
)


def _write_session(session_dir, sid, messages=None):
    payload = {
        "session_id": sid,
        "title": "Turn journal test",
        "messages": messages or [],
    }
    (session_dir / f"{sid}.json").write_text(json.dumps(payload), encoding="utf-8")


def test_append_turn_journal_event_fsyncs_jsonl_and_preserves_payload(tmp_path):
    event = append_turn_journal_event(
        "sid-1",
        {
            "event": "submitted",
            "turn_id": "turn-1",
            "stream_id": "stream-1",
            "role": "user",
            "content": "hello",
            "attachments": [{"name": "a.png", "path": "/tmp/a.png"}],
        },
        session_dir=tmp_path,
    )

    assert event["version"] == 1
    assert event["session_id"] == "sid-1"
    assert event["created_at"] > 0
    journal_path = tmp_path / "_turn_journal" / "sid-1.jsonl"
    assert journal_path.exists()
    lines = journal_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["content"] == "hello"


def test_read_turn_journal_tolerates_malformed_lines(tmp_path):
    journal_dir = tmp_path / "_turn_journal"
    journal_dir.mkdir()
    (journal_dir / "sid-1.jsonl").write_text(
        '{"event":"submitted","turn_id":"turn-1","session_id":"sid-1"}\n'
        'not-json\n'
        '{"event":"completed","turn_id":"turn-1","session_id":"sid-1"}\n',
        encoding="utf-8",
    )

    result = read_turn_journal("sid-1", session_dir=tmp_path)

    assert [event["event"] for event in result["events"]] == ["submitted", "completed"]
    assert result["malformed"] == [{"line": 2, "raw": "not-json"}]


def test_derive_turn_journal_states_keeps_latest_event_per_turn():
    states = derive_turn_journal_states([
        {"event": "submitted", "turn_id": "turn-1", "created_at": 1},
        {"event": "worker_started", "turn_id": "turn-1", "created_at": 2},
        {"event": "submitted", "turn_id": "turn-2", "created_at": 3},
        {"event": "completed", "turn_id": "turn-1", "created_at": 4},
    ])

    assert states["turn-1"]["event"] == "completed"
    assert states["turn-2"]["event"] == "submitted"


def test_derive_turn_journal_states_uses_created_at_not_file_order():
    states = derive_turn_journal_states([
        {"event": "completed", "turn_id": "turn-1", "created_at": 20},
        {"event": "submitted", "turn_id": "turn-1", "created_at": 10},
    ])

    assert states["turn-1"]["event"] == "completed"


def test_audit_reports_pending_turn_journal_entry_when_user_message_absent(tmp_path):
    _write_session(tmp_path, "sid-1", messages=[])
    append_turn_journal_event(
        "sid-1",
        {
            "event": "submitted",
            "turn_id": "turn-1",
            "stream_id": "stream-1",
            "role": "user",
            "content": "recover me",
            "attachments": [],
        },
        session_dir=tmp_path,
    )

    report = audit_session_recovery(tmp_path)

    assert report["status"] == "warn"
    assert report["summary"]["repairable"] == 1
    assert report["items"] == [
        {
            "session_id": "sid-1",
            "kind": "turn_journal_pending_turn",
            "category": "repairable",
            "recommendation": "audit_only_pending_turn_journal",
            "live_messages": 0,
            "bak_messages": -1,
            "turn_id": "turn-1",
            "event": "submitted",
        }
    ]


def test_audit_ignores_completed_or_already_materialized_turn_journal_entry(tmp_path):
    _write_session(tmp_path, "sid-1", messages=[{"role": "user", "content": "already there"}])
    append_turn_journal_event(
        "sid-1",
        {
            "event": "submitted",
            "turn_id": "turn-1",
            "role": "user",
            "content": "already there",
        },
        session_dir=tmp_path,
    )
    append_turn_journal_event(
        "sid-1",
        {"event": "completed", "turn_id": "turn-1"},
        session_dir=tmp_path,
    )

    report = audit_session_recovery(tmp_path)

    assert report["status"] == "ok"
    assert report["items"] == []
