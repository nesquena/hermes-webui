import importlib
import json
import os
import time

import pytest

from api.runtime_contract import (
    RuntimeEvent,
    RuntimeStatus,
    make_event,
    make_status,
)

# Lazily import the journal module to test import isolation.
_JOURNAL = None


def _journal():
    global _JOURNAL
    if _JOURNAL is None:
        _JOURNAL = importlib.import_module("api.runtime_journal")
    return _JOURNAL


def _make_journal(tmp_path):
    jrn = _journal()
    return jrn.RuntimeJournal(base_dir=tmp_path / "runs")


def test_create_run_writes_run_status(tmp_path):
    journal = _make_journal(tmp_path)
    status = journal.create_run("session_1")
    assert isinstance(status, RuntimeStatus)
    assert status.run_id.startswith("run_")
    assert status.session_id == "session_1"
    assert status.status == "queued"
    assert status.terminal is False
    assert "cancel" in status.controls


def test_create_run_creates_active_session_mapping(tmp_path):
    journal = _make_journal(tmp_path)
    journal.create_run("session_1")
    active = journal.get_active_run_for_session("session_1")
    assert active is not None
    assert active.session_id == "session_1"
    assert active.status == "queued"


def test_append_event_records_event(tmp_path):
    journal = _make_journal(tmp_path)
    status = journal.create_run("session_1")
    event = make_event(run_id=status.run_id, session_id="session_1", seq=1, type="token.delta")
    journal.append_event(event)

    events = journal.read_events(status.run_id)
    assert events is not None
    assert len(events) == 1
    assert events[0].event_id == status.run_id + ":1"
    assert events[0].seq == 1
    assert events[0].type == "token.delta"


def test_append_event_maintains_monotonic_seq(tmp_path):
    journal = _make_journal(tmp_path)
    status = journal.create_run("session_1")
    e1 = make_event(run_id=status.run_id, session_id="session_1", seq=1, type="token.delta")
    e2 = make_event(run_id=status.run_id, session_id="session_1", seq=2, type="token.delta")
    e3 = make_event(run_id=status.run_id, session_id="session_1", seq=3, type="token.delta")
    journal.append_event(e1)
    journal.append_event(e2)
    journal.append_event(e3)

    events = journal.read_events(status.run_id)
    seqs = [e.seq for e in events]
    assert seqs == [1, 2, 3]


def test_read_events_returns_all_events_in_order(tmp_path):
    journal = _make_journal(tmp_path)
    status = journal.create_run("session_1")
    for seq, typ in enumerate(
        ["token.delta", "tool.started", "tool.done", "token.delta"], start=1
    ):
        journal.append_event(
            make_event(
                run_id=status.run_id, session_id="session_1", seq=seq, type=typ
            )
        )
    events = journal.read_events(status.run_id)
    assert len(events) == 4
    assert [e.type for e in events] == [
        "token.delta",
        "tool.started",
        "tool.done",
        "token.delta",
    ]


def test_read_events_after_seq_returns_only_later_events(tmp_path):
    journal = _make_journal(tmp_path)
    status = journal.create_run("session_1")
    for seq in range(1, 6):
        journal.append_event(
            make_event(
                run_id=status.run_id, session_id="session_1", seq=seq, type="token.delta"
            )
        )
    events = journal.read_events(status.run_id, after_seq=2)
    assert [e.seq for e in events] == [3, 4, 5]


def test_read_events_respects_limit(tmp_path):
    journal = _make_journal(tmp_path)
    status = journal.create_run("session_1")
    for seq in range(1, 6):
        journal.append_event(
            make_event(
                run_id=status.run_id, session_id="session_1", seq=seq, type="token.delta"
            )
        )
    events = journal.read_events(status.run_id, limit=2)
    assert len(events) == 2
    assert [e.seq for e in events] == [1, 2]


def test_get_status_returns_last_event_id_and_last_seq(tmp_path):
    journal = _make_journal(tmp_path)
    status = journal.create_run("session_1")
    e1 = make_event(run_id=status.run_id, session_id="session_1", seq=1, type="token.delta")
    e2 = make_event(run_id=status.run_id, session_id="session_1", seq=2, type="tool.started")
    journal.append_event(e1)
    journal.append_event(e2)

    fetched = journal.get_status(status.run_id)
    assert fetched is not None
    assert fetched.last_event_id == status.run_id + ":2"
    assert fetched.last_seq == 2


def test_terminal_status_survives_fresh_journal_object(tmp_path):
    journal = _make_journal(tmp_path)
    status = journal.create_run("session_1")
    run_id = status.run_id
    journal.mark_terminal(run_id, "completed", result={"ok": True})

    jrn = _journal()
    journal2 = jrn.RuntimeJournal(base_dir=tmp_path / "runs")
    fetched = journal2.get_status(run_id)
    assert fetched is not None
    assert fetched.status == "completed"
    assert fetched.terminal is True
    assert fetched.result == {"ok": True}


def test_terminal_run_no_longer_appears_as_active(tmp_path):
    journal = _make_journal(tmp_path)
    status = journal.create_run("session_1")
    run_id = status.run_id
    journal.mark_terminal(run_id, "completed")
    active = journal.get_active_run_for_session("session_1")
    assert active is None


def test_terminal_event_clears_active_session_mapping(tmp_path):
    journal = _make_journal(tmp_path)
    status = journal.create_run("session_1")
    run_id = status.run_id
    terminal_event = make_event(
        run_id=run_id,
        session_id="session_1",
        seq=1,
        type="done",
        terminal=True,
    )
    journal.append_event(terminal_event)
    active = journal.get_active_run_for_session("session_1")
    assert active is None


def test_active_session_mapping_points_to_latest_non_terminal_run(tmp_path):
    journal = _make_journal(tmp_path)
    s1 = journal.create_run("session_1")
    s2 = journal.create_run("session_1")
    journal.mark_terminal(s1.run_id, "completed")
    active = journal.get_active_run_for_session("session_1")
    assert active is not None
    assert active.run_id == s2.run_id


def test_secret_like_payload_fields_redacted_on_disk(tmp_path):
    journal = _make_journal(tmp_path)
    status = journal.create_run("session_1")
    event = make_event(
        run_id=status.run_id,
        session_id="session_1",
        seq=1,
        type="run.started",
        payload={
            "model": "sonnet",
            "api_key": "sk-secret",
            "token": "bearer-hush",
            "password": "p@ss",
            "nested": {"oauth_token": "xyz", "keep": "val"},
            "items": [{"secret": "shh"}, {"name": "public"}],
        },
    )
    journal.append_event(event)

    event_path = tmp_path / "runs" / f"{status.run_id}.jsonl"
    raw = event_path.read_text(encoding="utf-8")
    parsed = json.loads(raw.splitlines()[0])
    payload = parsed["payload"]
    assert payload["api_key"] == "[REDACTED]"
    assert payload["token"] == "[REDACTED]"
    assert payload["password"] == "[REDACTED]"
    assert payload["nested"]["oauth_token"] == "[REDACTED]"
    assert payload["items"][0]["secret"] == "[REDACTED]"


def test_non_secret_payload_fields_preserved_on_disk(tmp_path):
    journal = _make_journal(tmp_path)
    status = journal.create_run("session_1")
    event = make_event(
        run_id=status.run_id,
        session_id="session_1",
        seq=1,
        type="run.started",
        payload={
            "model": "sonnet",
            "workspace": "/home/user/proj",
            "nested": {"name": "keep-me", "count": 42},
        },
    )
    journal.append_event(event)

    events = journal.read_events(status.run_id)
    p = events[0].payload
    assert p["model"] == "sonnet"
    assert p["workspace"] == "/home/user/proj"
    assert p["nested"]["name"] == "keep-me"
    assert p["nested"]["count"] == 42


def test_unknown_run_get_status_returns_none(tmp_path):
    journal = _make_journal(tmp_path)
    assert journal.get_status("run_nonexistent") is None


def test_unknown_run_read_events_returns_none(tmp_path):
    journal = _make_journal(tmp_path)
    assert journal.read_events("run_nonexistent") is None


def test_unknown_run_append_event_raises_valueerror(tmp_path):
    journal = _make_journal(tmp_path)
    event = make_event(
        run_id="run_ghost", session_id="session_1", seq=1, type="token.delta"
    )
    with pytest.raises(ValueError, match="unknown run_id"):
        journal.append_event(event)


def test_unknown_run_mark_terminal_returns_none(tmp_path):
    journal = _make_journal(tmp_path)
    assert journal.mark_terminal("run_nonexistent", "completed") is None


def test_unknown_session_get_active_run_returns_none(tmp_path):
    journal = _make_journal(tmp_path)
    assert journal.get_active_run_for_session("no_such_session") is None


def test_mark_terminal_with_invalid_status_raises(tmp_path):
    journal = _make_journal(tmp_path)
    status = journal.create_run("session_1")
    with pytest.raises(ValueError, match="invalid terminal status"):
        journal.mark_terminal(status.run_id, "not_a_real_status")


def test_mark_terminal_sets_status_error_result(tmp_path):
    journal = _make_journal(tmp_path)
    status = journal.create_run("session_1")
    result = journal.mark_terminal(
        status.run_id,
        "failed",
        error={"message": "connection refused"},
        result={"partial": True},
    )
    assert result is not None
    assert result.status == "failed"
    assert result.terminal is True
    assert result.error == "connection refused"
    assert result.result == {"partial": True}


def test_runtime_journal_imports_cleanly_without_api_streaming(tmp_path):
    jrn = _journal()
    assert not hasattr(jrn, "streaming")
    mod_names = {name for name in dir(jrn) if not name.startswith("_")}
    assert "streaming" not in mod_names


def test_event_types_round_trip_correctly(tmp_path):
    journal = _make_journal(tmp_path)
    status = journal.create_run("session_1")
    contract_types = [
        "run.started",
        "run.status",
        "token.delta",
        "reasoning.delta",
        "reasoning.done",
        "progress",
        "tool.started",
        "tool.updated",
        "tool.done",
        "approval.requested",
        "approval.resolved",
        "clarify.requested",
        "clarify.resolved",
        "title.updated",
        "usage.updated",
        "usage.final",
    ]
    for seq, typ in enumerate(contract_types, start=1):
        journal.append_event(
            make_event(
                run_id=status.run_id,
                session_id="session_1",
                seq=seq,
                type=typ,
                payload={"key": "val"},
            )
        )
    events = journal.read_events(status.run_id)
    assert len(events) == len(contract_types)
    for i, typ in enumerate(contract_types):
        assert events[i].type == typ
        assert events[i].payload == {"key": "val"}


def test_mark_terminal_all_terminal_statuses(tmp_path):
    journal = _make_journal(tmp_path)
    for idx, ts in enumerate(["completed", "failed", "cancelled", "expired"]):
        sid = f"session_{idx}"
        s = journal.create_run(sid)
        result = journal.mark_terminal(s.run_id, ts)
        assert result is not None
        assert result.status == ts
        assert result.terminal is True


def test_create_run_multiple_runs_different_ids(tmp_path):
    journal = _make_journal(tmp_path)
    s1 = journal.create_run("session_1")
    s2 = journal.create_run("session_1")
    assert s1.run_id != s2.run_id
    assert s1.session_id == "session_1"
    assert s2.session_id == "session_1"


def test_index_survives_process_reopen(tmp_path):
    jrn = _journal()
    journal1 = jrn.RuntimeJournal(base_dir=tmp_path / "runs")
    s = journal1.create_run("session_1")
    journal1.append_event(
        make_event(run_id=s.run_id, session_id="session_1", seq=1, type="token.delta")
    )

    journal2 = jrn.RuntimeJournal(base_dir=tmp_path / "runs")
    events = journal2.read_events(s.run_id)
    assert events is not None
    assert len(events) == 1
    assert events[0].seq == 1

    fetched = journal2.get_status(s.run_id)
    assert fetched is not None
    assert fetched.last_seq == 1
