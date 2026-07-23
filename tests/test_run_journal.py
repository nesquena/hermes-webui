import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import api.run_journal as run_journal
from api.run_journal import (
    RunJournalWriter,
    append_run_event,
    compact_terminal_run_index_for_session,
    find_run_summary,
    latest_terminal_run_summary_for_session,
    latest_run_summary,
    read_run_events,
    stale_interrupted_event,
    terminal_run_summary_page_for_session,
    terminal_run_summaries_for_session,
)


def _terminal_page_cursor(page):
    return {
        "index_size": page["index_size"],
        "generation": page["index_generation"],
        "end_offset": page["next_index_end_offset"],
    }


def _message_ref(message):
    payload = {
        "role": str(message.get("role") or ""),
        "content": " ".join(str(message.get("content") or "").split()),
        "timestamp": message.get("_ts") or message.get("timestamp") or "",
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def test_run_journal_appends_monotonic_seq_and_reads_after_cursor(tmp_path):
    writer = RunJournalWriter("session_1", "run_1", session_dir=tmp_path)

    first = writer.append_sse_event("token", {"text": "hello"})
    second = writer.append_sse_event("done", {"session": {"session_id": "session_1"}})

    assert first["seq"] == 1
    assert first["event_id"] == "run_1:1"
    assert first["terminal"] is False
    assert second["seq"] == 2
    assert second["terminal"] is True
    assert second["terminal_state"] == "completed"

    journal = read_run_events("session_1", "run_1", after_seq=1, session_dir=tmp_path)
    assert [event["event"] for event in journal["events"]] == ["done"]


def test_run_journal_reads_bounded_replay_window(tmp_path):
    writer = RunJournalWriter("session_1", "run_1", session_dir=tmp_path)

    writer.append_sse_event("token", {"text": "one"})
    writer.append_sse_event("token", {"text": "two"})
    writer.append_sse_event("token", {"text": "three"})
    writer.append_sse_event("token", {"text": "four"})

    journal = read_run_events(
        "session_1",
        "run_1",
        after_seq=1,
        max_seq=3,
        session_dir=tmp_path,
    )

    assert [event["seq"] for event in journal["events"]] == [2, 3]
    assert [event["payload"]["text"] for event in journal["events"]] == ["two", "three"]


def test_run_journal_reads_suffix_after_default_row_cap(tmp_path):
    for seq in range(1, 2049):
        append_run_event("session_1", "run_long", "token", {"text": str(seq)}, session_dir=tmp_path, seq=seq)
    append_run_event("session_1", "run_long", "token", {"text": "suffix"}, session_dir=tmp_path, seq=2049)
    append_run_event("session_1", "run_long", "done", {"session": {}}, session_dir=tmp_path, seq=2050)

    journal = read_run_events("session_1", "run_long", after_seq=2048, session_dir=tmp_path)

    assert journal["complete"] is True
    assert journal["malformed"] == []
    assert [event["seq"] for event in journal["events"]] == [2049, 2050]
    assert [event["event"] for event in journal["events"]] == ["token", "done"]


def test_run_journal_compacts_oversized_terminal_session_payload(tmp_path):
    session_id = "session_huge_terminal"
    run_id = "run_huge_terminal"
    huge_context = "x" * (run_journal._RUN_EVENTS_MAX_BYTES + 10_000)
    assistant = {"role": "assistant", "content": "final answer", "_ts": 10.0}
    payload = {
        "terminal_session_persisted": True,
        "terminal_session_persisted_session_id": session_id,
        "session": {
            "session_id": session_id,
            "title": huge_context,
            "messages": [
                {"role": "user", "content": huge_context, "timestamp": 1.0},
                assistant,
            ],
            "message_count": 2,
            "tool_calls": [{"name": "terminal", "args": {"blob": huge_context}}],
            "runtime_journal_snapshot": {"blob": huge_context},
        },
        "details": huge_context,
        "usage": {"output_tokens": 3, "blob": huge_context},
    }

    append_run_event(session_id, run_id, "done", payload, session_dir=tmp_path)

    path = tmp_path / "_run_journal" / session_id / f"{run_id}.jsonl"
    assert path.stat().st_size < run_journal._RUN_EVENTS_MAX_BYTES
    assert "messages" in payload["session"]
    journal = read_run_events(session_id, run_id, session_dir=tmp_path)
    assert journal["complete"] is True
    assert journal["malformed"] == []
    event_payload = journal["events"][0]["payload"]
    compact_session = event_payload["session"]
    assert "messages" not in compact_session
    assert "tool_calls" not in compact_session
    assert compact_session["messages_omitted"]["version"] == "terminal_session_payload_omitted_v1"
    assert compact_session["tool_calls_omitted"]["version"] == "terminal_session_payload_omitted_v1"
    assert compact_session["runtime_journal_snapshot_omitted"]["version"] == "terminal_session_payload_omitted_v1"
    assert event_payload["terminal_message_target"] == {
        "version": "terminal_message_target_v1",
        "session_id": session_id,
        "run_id": run_id,
        "stream_id": run_id,
        "message_index": 1,
        "message_ref": _message_ref(assistant),
    }


def test_run_journal_compacts_continuation_terminal_without_rewriting_session_id(tmp_path):
    origin_session_id = "compression_origin"
    continuation_session_id = "compression_continuation"
    run_id = "run_compression"
    assistant = {"role": "assistant", "content": "continuation final", "_ts": 20.0}
    payload = {
        "terminal_session_persisted": True,
        "terminal_session_persisted_session_id": continuation_session_id,
        "session_id": continuation_session_id,
        "old_session_id": origin_session_id,
        "new_session_id": continuation_session_id,
        "continuation_session_id": continuation_session_id,
        "session": {
            "session_id": continuation_session_id,
            "parent_session_id": origin_session_id,
            "messages": [
                {"role": "user", "content": "continue", "timestamp": 19.0},
                assistant,
            ],
            "message_count": 2,
        },
    }

    append_run_event(origin_session_id, run_id, "done", payload, session_dir=tmp_path)

    journal = read_run_events(origin_session_id, run_id, session_dir=tmp_path)
    event = journal["events"][0]
    event_payload = event["payload"]
    assert event["session_id"] == origin_session_id
    assert event_payload["session_id"] == continuation_session_id
    assert event_payload["old_session_id"] == origin_session_id
    assert event_payload["new_session_id"] == continuation_session_id
    assert event_payload["continuation_session_id"] == continuation_session_id
    assert event_payload["session"]["session_id"] == continuation_session_id
    assert event_payload["session"]["parent_session_id"] == origin_session_id
    assert "messages" not in event_payload["session"]
    assert event_payload["terminal_message_target"] == {
        "version": "terminal_message_target_v1",
        "session_id": continuation_session_id,
        "run_id": run_id,
        "stream_id": run_id,
        "message_index": 1,
        "message_ref": _message_ref(assistant),
    }


def test_run_journal_bounds_unpersisted_terminal_session_payload(tmp_path):
    session_id = "session_save_failed"
    run_id = "run_save_failed"
    assistant = {"role": "assistant", "content": "only journal has me", "_ts": 5.0}
    payload = {
        "terminal_session_persisted": False,
        "session": {
            "session_id": session_id,
            "messages": [
                {"role": "user", "content": "please answer", "timestamp": 4.0},
                assistant,
            ],
            "message_count": 2,
        },
    }

    append_run_event(session_id, run_id, "apperror", payload, session_dir=tmp_path)

    journal = read_run_events(session_id, run_id, session_dir=tmp_path)
    event_payload = journal["events"][0]["payload"]
    assert event_payload["session"]["messages"][-1] == assistant
    assert "messages_omitted" not in event_payload["session"]
    assert event_payload["session"]["terminal_recovery_delta"]["version"] == "terminal_recovery_delta_v1"
    assert "terminal_message_target" not in event_payload


def test_run_journal_emits_recovery_control_when_unpersisted_terminal_payload_cannot_fit(tmp_path):
    session_id = "session_save_failed_huge"
    run_id = "run_save_failed_huge"
    payload = {
        "terminal_session_persisted": False,
        "session": {
            "session_id": session_id,
            "messages": [
                {"role": "user", "content": "please answer", "timestamp": 4.0},
                {"role": "assistant", "content": "x" * (run_journal._RUN_EVENTS_MAX_BYTES + 10_000), "_ts": 5.0},
            ],
            "message_count": 2,
        },
    }

    append_run_event(session_id, run_id, "apperror", payload, session_dir=tmp_path)

    journal = read_run_events(session_id, run_id, session_dir=tmp_path)
    assert journal["complete"] is True
    event_payload = journal["events"][0]["payload"]
    compact_session = event_payload["session"]
    assert "messages" not in compact_session
    assert compact_session["messages_omitted"]["reason"] == "terminal_session_save_failed_payload_too_large"
    assert event_payload["terminal_recovery_control"] == {
        "version": "terminal_recovery_control_v1",
        "reason": "terminal_session_save_failed_payload_too_large",
        "session_id": session_id,
        "run_id": run_id,
        "stream_id": run_id,
        "terminal_state": "errored",
    }
    assert event_payload["terminal_disposition"] == {
        "version": "terminal_disposition_v1",
        "kind": "consumed_non_materializable",
        "reason": "terminal_session_save_failed_payload_too_large",
        "session_id": session_id,
        "run_id": run_id,
        "stream_id": run_id,
    }


def test_run_journal_recovery_control_keeps_origin_authority_for_unpersisted_continuation(
    tmp_path,
):
    origin_session_id = "compression_origin_save_failed"
    continuation_session_id = "compression_continuation_save_failed"
    run_id = "run_save_failed_huge_continuation"
    payload = {
        "terminal_session_persisted": False,
        "session_id": continuation_session_id,
        "old_session_id": origin_session_id,
        "new_session_id": continuation_session_id,
        "continuation_session_id": continuation_session_id,
        "session": {
            "session_id": continuation_session_id,
            "parent_session_id": origin_session_id,
            "messages": [
                {"role": "user", "content": "please answer", "timestamp": 4.0},
                {
                    "role": "assistant",
                    "content": "x" * (run_journal._RUN_EVENTS_MAX_BYTES + 10_000),
                    "_ts": 5.0,
                },
            ],
            "message_count": 2,
        },
    }

    append_run_event(origin_session_id, run_id, "done", payload, session_dir=tmp_path)

    journal = read_run_events(origin_session_id, run_id, session_dir=tmp_path)
    assert journal["complete"] is True
    event = journal["events"][0]
    event_payload = event["payload"]
    assert event["session_id"] == origin_session_id
    assert event_payload["session_id"] == continuation_session_id
    assert event_payload["old_session_id"] == origin_session_id
    assert event_payload["new_session_id"] == continuation_session_id
    assert event_payload["continuation_session_id"] == continuation_session_id
    compact_session = event_payload["session"]
    assert compact_session["session_id"] == continuation_session_id
    assert compact_session["parent_session_id"] == origin_session_id
    assert "messages" not in compact_session
    assert compact_session["messages_omitted"]["reason"] == "terminal_session_save_failed_payload_too_large"
    assert event_payload["terminal_recovery_control"] == {
        "version": "terminal_recovery_control_v1",
        "reason": "terminal_session_save_failed_payload_too_large",
        "session_id": origin_session_id,
        "target_session_id": continuation_session_id,
        "continuation_session_id": continuation_session_id,
        "run_id": run_id,
        "stream_id": run_id,
        "terminal_state": "completed",
    }
    assert event_payload["terminal_disposition"] == {
        "version": "terminal_disposition_v1",
        "kind": "consumed_non_materializable",
        "reason": "terminal_session_save_failed_payload_too_large",
        "session_id": origin_session_id,
        "target_session_id": continuation_session_id,
        "continuation_session_id": continuation_session_id,
        "run_id": run_id,
        "stream_id": run_id,
    }


def test_run_journal_writer_bounds_unpersisted_utf8_terminal_events_with_origin_authority(
    tmp_path,
):
    origin_session_id = "compression_origin_writer_save_failed"
    continuation_session_id = "compression_continuation_writer_save_failed"
    cases = [
        ("done", {}, "completed"),
        ("apperror", {"type": "tool_limit_reached"}, "tool_limit_reached"),
        ("cancel", {"message": "Cancelled by user"}, "interrupted-by-user"),
    ]
    huge_answer = "终" * (run_journal._RUN_EVENTS_MAX_BYTES + 10_000)

    for event_name, extra_payload, expected_terminal_state in cases:
        run_id = f"run_writer_save_failed_{event_name}"
        payload = {
            "terminal_session_persisted": False,
            "session_id": continuation_session_id,
            "old_session_id": origin_session_id,
            "new_session_id": continuation_session_id,
            "continuation_session_id": continuation_session_id,
            "session": {
                "session_id": continuation_session_id,
                "parent_session_id": origin_session_id,
                "messages": [
                    {"role": "user", "content": "please answer", "timestamp": 4.0},
                    {
                        "role": "assistant",
                        "content": huge_answer,
                        "_ts": 5.0,
                    },
                ],
                "message_count": 2,
            },
        }
        payload.update(extra_payload)
        writer = RunJournalWriter(origin_session_id, run_id, session_dir=tmp_path)

        writer.append_sse_event(event_name, payload)

        path = tmp_path / "_run_journal" / origin_session_id / f"{run_id}.jsonl"
        assert path.stat().st_size < run_journal._RUN_EVENTS_MAX_BYTES
        journal = read_run_events(origin_session_id, run_id, session_dir=tmp_path)
        assert journal["complete"] is True
        event_payload = journal["events"][0]["payload"]
        compact_session = event_payload["session"]
        assert compact_session["session_id"] == continuation_session_id
        assert compact_session["parent_session_id"] == origin_session_id
        assert "messages" not in compact_session
        assert compact_session["messages_omitted"]["reason"] == "terminal_session_save_failed_payload_too_large"
        assert compact_session["terminal_recovery_delta"] == {
            "version": "terminal_recovery_delta_v1",
            "reason": "terminal_session_save_failed_payload_too_large",
            "message_count": 2,
            "messages_offset": 2,
        }
        assert event_payload["terminal_recovery_control"] == {
            "version": "terminal_recovery_control_v1",
            "reason": "terminal_session_save_failed_payload_too_large",
            "session_id": origin_session_id,
            "target_session_id": continuation_session_id,
            "continuation_session_id": continuation_session_id,
            "run_id": run_id,
            "stream_id": run_id,
            "terminal_state": expected_terminal_state,
        }
        assert event_payload["terminal_disposition"] == {
            "version": "terminal_disposition_v1",
            "kind": "consumed_non_materializable",
            "reason": "terminal_session_save_failed_payload_too_large",
            "session_id": origin_session_id,
            "target_session_id": continuation_session_id,
            "continuation_session_id": continuation_session_id,
            "run_id": run_id,
            "stream_id": run_id,
        }


def test_run_journal_fails_closed_on_physical_seq_reorder(tmp_path):
    append_run_event("session_1", "run_reorder", "token", {"text": "one"}, session_dir=tmp_path, seq=1)
    append_run_event("session_1", "run_reorder", "token", {"text": "three"}, session_dir=tmp_path, seq=3)
    append_run_event("session_1", "run_reorder", "token", {"text": "two"}, session_dir=tmp_path, seq=2)

    journal = read_run_events("session_1", "run_reorder", session_dir=tmp_path)

    assert journal["complete"] is False
    assert journal["limit_reason"] == "replay_noncontiguous"
    assert [event["seq"] for event in journal["events"]] == [1]
    assert journal["malformed"] == [{"line": 2, "reason": "replay_noncontiguous"}]


def test_run_journal_rejects_single_over_cap_row_before_json_parse(tmp_path):
    path = tmp_path / "_run_journal" / "session_1" / "run_giant.jsonl"
    path.parent.mkdir(parents=True)
    path.write_bytes(b'{"version":1,"payload":"' + (b"x" * 70000) + b'"}')

    journal = read_run_events(
        "session_1",
        "run_giant",
        session_dir=tmp_path,
        max_bytes=32,
        max_rows=64,
    )

    assert journal["events"] == []
    assert journal["malformed"] == [{"line": None, "reason": "replay_limit_bytes"}]


def test_run_journal_default_fsyncs_terminal_events_only(tmp_path, monkeypatch):
    path = tmp_path / "_run_journal" / "session_1" / "run_1.jsonl"
    path.parent.mkdir(parents=True)
    path.touch()
    fsync_calls = []
    monkeypatch.delenv("HERMES_WEBUI_RUN_JOURNAL_FSYNC", raising=False)
    monkeypatch.setattr("api.run_journal.os.fsync", lambda fd: fsync_calls.append(fd))

    append_run_event("session_1", "run_1", "token", {"text": "ok"}, session_dir=tmp_path)

    assert fsync_calls == []

    append_run_event("session_1", "run_1", "done", {"session": {}}, session_dir=tmp_path)

    # Terminal events also update the durable terminal-run index and its
    # writer-owned cursor authority used by unresolved Worklog reconciliation.
    assert len(fsync_calls) == 5


def test_run_journal_eager_fsync_mode_fsyncs_non_terminal_events(tmp_path, monkeypatch):
    path = tmp_path / "_run_journal" / "session_1" / "run_1.jsonl"
    path.parent.mkdir(parents=True)
    path.touch()
    fsync_calls = []
    monkeypatch.setenv("HERMES_WEBUI_RUN_JOURNAL_FSYNC", "eager")
    monkeypatch.setattr("api.run_journal.os.fsync", lambda fd: fsync_calls.append(fd))

    append_run_event("session_1", "run_1", "token", {"text": "ok"}, session_dir=tmp_path)

    assert len(fsync_calls) == 1


def test_run_journal_tolerates_malformed_lines(tmp_path):
    append_run_event("session_1", "run_1", "token", {"text": "ok"}, session_dir=tmp_path)
    path = tmp_path / "_run_journal" / "session_1" / "run_1.jsonl"
    with path.open("a", encoding="utf-8") as fh:
        fh.write("{not json}\n")
        fh.write(json.dumps(["wrong-shape"]) + "\n")

    journal = read_run_events("session_1", "run_1", session_dir=tmp_path)

    assert len(journal["events"]) == 1
    assert len(journal["malformed"]) == 2


def test_latest_summary_and_find_run_summary_classify_terminal_state(tmp_path):
    append_run_event("session_1", "run_1", "token", {"text": "ok"}, session_dir=tmp_path)
    append_run_event("session_1", "run_1", "cancel", {"message": "Cancelled by user"}, session_dir=tmp_path)

    summary = latest_run_summary("session_1", "run_1", session_dir=tmp_path)
    found = find_run_summary("run_1", session_dir=tmp_path)

    assert summary["terminal"] is True
    assert summary["terminal_state"] == "interrupted-by-user"
    assert summary["last_seq"] == 2
    assert found["session_id"] == "session_1"
    assert found["terminal_state"] == "interrupted-by-user"


def test_latest_terminal_run_summary_for_session_skips_running_runs(tmp_path):
    append_run_event("session_1", "run_1", "token", {"text": "old"}, session_dir=tmp_path)
    append_run_event("session_1", "run_1", "done", {"session": {}}, session_dir=tmp_path)
    append_run_event("session_1", "run_2", "token", {"text": "still running"}, session_dir=tmp_path)
    append_run_event("session_1", "run_3", "token", {"text": "latest"}, session_dir=tmp_path)
    append_run_event("session_1", "run_3", "cancel", {"message": "Cancelled"}, session_dir=tmp_path)

    summary = latest_terminal_run_summary_for_session("session_1", session_dir=tmp_path)

    assert summary["run_id"] == "run_3"
    assert summary["terminal"] is True
    assert summary["terminal_state"] == "interrupted-by-user"


def test_terminal_run_summaries_for_session_returns_bounded_newest_terminals(tmp_path):
    append_run_event("session_1", "run_1", "token", {"text": "old"}, session_dir=tmp_path)
    append_run_event("session_1", "run_1", "done", {"session": {}}, session_dir=tmp_path)
    append_run_event("session_1", "run_2", "token", {"text": "running"}, session_dir=tmp_path)
    append_run_event("session_1", "run_3", "token", {"text": "cancel"}, session_dir=tmp_path)
    append_run_event("session_1", "run_3", "cancel", {"message": "Cancelled"}, session_dir=tmp_path)

    summaries = terminal_run_summaries_for_session("session_1", session_dir=tmp_path, limit=2)

    assert [summary["run_id"] for summary in summaries] == ["run_3", "run_1"]
    assert [summary["terminal_state"] for summary in summaries] == [
        "interrupted-by-user",
        "completed",
    ]


def test_terminal_run_summaries_skips_resolved_before_candidate_budget(tmp_path):
    append_run_event("session_1", "run_old", "token", {"text": "old"}, session_dir=tmp_path)
    append_run_event("session_1", "run_old", "done", {"session": {}}, session_dir=tmp_path)
    old_path = tmp_path / "_run_journal" / "session_1" / "run_old.jsonl"
    os.utime(old_path, (1.0, 1.0))

    skipped_run_ids = set()
    for idx in range(70):
        run_id = f"run_{idx:03d}"
        skipped_run_ids.add(run_id)
        append_run_event("session_1", run_id, "token", {"text": "new"}, session_dir=tmp_path)
        append_run_event("session_1", run_id, "done", {"session": {}}, session_dir=tmp_path)
        path = tmp_path / "_run_journal" / "session_1" / f"{run_id}.jsonl"
        os.utime(path, (10.0 + idx, 10.0 + idx))

    bounded = terminal_run_summaries_for_session(
        "session_1",
        session_dir=tmp_path,
        limit=1,
        max_candidates=64,
        skip_run_ids=skipped_run_ids,
    )

    assert [summary["run_id"] for summary in bounded] == ["run_old"]


def test_terminal_run_summaries_bounds_unresolved_summary_parses(tmp_path, monkeypatch):
    for idx in range(80):
        run_id = f"run_{idx:03d}"
        append_run_event("session_1", run_id, "token", {"text": "new"}, session_dir=tmp_path)
        append_run_event("session_1", run_id, "done", {"session": {}}, session_dir=tmp_path)
        path = tmp_path / "_run_journal" / "session_1" / f"{run_id}.jsonl"
        os.utime(path, (10.0 + idx, 10.0 + idx))

    calls = []
    original_latest_run_summary = latest_run_summary

    def counted_latest_run_summary(session_id, run_id, *, session_dir=None):
        calls.append(run_id)
        return original_latest_run_summary(session_id, run_id, session_dir=session_dir)

    monkeypatch.setattr("api.run_journal.latest_run_summary", counted_latest_run_summary)

    summaries = terminal_run_summaries_for_session(
        "session_1",
        session_dir=tmp_path,
        limit=64,
        max_candidates=64,
    )

    assert len(summaries) == 64
    assert len(calls) == 0


def test_terminal_run_summaries_bounds_directory_stats(tmp_path, monkeypatch):
    for idx in range(300):
        run_id = f"run_{idx:03d}"
        append_run_event("session_1", run_id, "token", {"text": "new"}, session_dir=tmp_path)
        append_run_event("session_1", run_id, "done", {"session": {}}, session_dir=tmp_path)
        path = tmp_path / "_run_journal" / "session_1" / f"{run_id}.jsonl"
        os.utime(path, (10.0 + idx, 10.0 + idx))

    stat_calls = []
    original_stat = Path.stat
    journal_root = tmp_path / "_run_journal" / "session_1"

    def counted_stat(self, *args, **kwargs):
        if self.parent == journal_root and self.name.endswith(".jsonl"):
            stat_calls.append(self.name)
        return original_stat(self, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", counted_stat)

    summaries = terminal_run_summaries_for_session(
        "session_1",
        session_dir=tmp_path,
        limit=64,
        max_candidates=64,
    )

    assert len(summaries) == 64
    assert len(stat_calls) <= 64


def test_terminal_run_summaries_rejects_malformed_index_authority(tmp_path):
    append_run_event("session_1", "run_good", "token", {"text": "ok"}, session_dir=tmp_path)
    append_run_event("session_1", "run_good", "done", {"session": {}}, session_dir=tmp_path)
    index_path = tmp_path / "_run_journal" / "session_1" / "_terminal_runs.jsonl"
    with index_path.open("a", encoding="utf-8") as fh:
        fh.write(
            json.dumps(
                {
                    "version": 1,
                    "session_id": "foreign_session",
                    "run_id": "run_foreign",
                    "stream_id": "run_foreign",
                    "last_seq": 1,
                    "last_event_id": "run_foreign:1",
                    "terminal": True,
                    "terminal_state": "completed",
                }
            )
            + "\n"
        )
        fh.write(
            json.dumps(
                {
                    "version": 1,
                    "session_id": "session_1",
                    "run_id": "run_bad_stream",
                    "stream_id": "../run_bad_stream",
                    "last_seq": 1,
                    "last_event_id": "run_bad_stream:1",
                    "terminal": True,
                    "terminal_state": "completed",
                }
            )
            + "\n"
        )
        fh.write(
            json.dumps(
                {
                    "version": 1,
                    "session_id": "session_1",
                    "run_id": "run_bad_seq",
                    "stream_id": "run_bad_seq",
                    "last_seq": 2,
                    "last_event_id": "run_bad_seq:1",
                    "terminal": True,
                    "terminal_state": "completed",
                }
            )
            + "\n"
        )

    summaries = terminal_run_summaries_for_session("session_1", session_dir=tmp_path, limit=4)

    assert [summary["run_id"] for summary in summaries] == ["run_good"]


def test_terminal_run_summaries_advances_past_single_over_cap_index_tail(tmp_path):
    append_run_event("session_1", "run_good", "token", {"text": "ok"}, session_dir=tmp_path)
    append_run_event("session_1", "run_good", "done", {"session": {}}, session_dir=tmp_path)
    index_path = tmp_path / "_run_journal" / "session_1" / "_terminal_runs.jsonl"
    with index_path.open("ab") as fh:
        fh.write(b'{"version":1,"oversized":"' + (b"x" * (600 * 1024)) + b'"}')

    summaries = terminal_run_summaries_for_session("session_1", session_dir=tmp_path, limit=1)

    assert [summary["run_id"] for summary in summaries] == ["run_good"]


def test_terminal_run_index_append_reframes_after_malformed_tail(tmp_path):
    index_path = tmp_path / "_run_journal" / "session_1" / "_terminal_runs.jsonl"
    index_path.parent.mkdir(parents=True)
    index_path.write_bytes(b'{"version":1,"oversized":"' + (b"x" * (600 * 1024)) + b'"}')

    append_run_event("session_1", "run_good", "token", {"text": "ok"}, session_dir=tmp_path)
    append_run_event("session_1", "run_good", "done", {"session": {}}, session_dir=tmp_path)

    summaries = terminal_run_summaries_for_session("session_1", session_dir=tmp_path, limit=1)

    assert [summary["run_id"] for summary in summaries] == ["run_good"]


def test_terminal_run_summary_page_cursor_invalidates_same_size_replacement(tmp_path):
    append_run_event("session_1", "run_001", "token", {"text": "ok"}, session_dir=tmp_path)
    append_run_event("session_1", "run_001", "done", {"session": {}}, session_dir=tmp_path)
    first = terminal_run_summary_page_for_session(
        "session_1",
        session_dir=tmp_path,
        limit=1,
        max_candidates=1,
    )
    assert [summary["run_id"] for summary in first["summaries"]] == ["run_001"]
    assert "digest" in first["index_generation"]
    assert "authority" in first["index_generation"]
    cursor = _terminal_page_cursor(first)

    index_path = tmp_path / "_run_journal" / "session_1" / "_terminal_runs.jsonl"
    original = index_path.read_bytes()
    replacement = original.replace(b"run_001", b"run_002")
    assert len(replacement) == len(original)
    replacement_path = index_path.with_name("replacement-terminal-index.jsonl")
    replacement_path.write_bytes(replacement)
    os.replace(replacement_path, index_path)

    second = terminal_run_summary_page_for_session(
        "session_1",
        session_dir=tmp_path,
        limit=1,
        max_candidates=1,
        index_cursor=cursor,
    )

    assert [summary["run_id"] for summary in second["summaries"]] == ["run_002"]


def test_terminal_run_summary_page_cursor_rejects_same_metadata_truncate_regrow(
    tmp_path,
    monkeypatch,
):
    append_run_event("session_1", "run_001", "token", {"text": "ok"}, session_dir=tmp_path)
    append_run_event("session_1", "run_001", "done", {"session": {}}, session_dir=tmp_path)
    first = terminal_run_summary_page_for_session(
        "session_1",
        session_dir=tmp_path,
        limit=1,
        max_candidates=1,
    )
    assert [summary["run_id"] for summary in first["summaries"]] == ["run_001"]
    cursor = _terminal_page_cursor(first)

    index_path = tmp_path / "_run_journal" / "session_1" / "_terminal_runs.jsonl"
    original_stat = index_path.stat()
    original_bytes = index_path.read_bytes()
    replacement = original_bytes.replace(b"run_001", b"run_002")
    assert len(replacement) == len(original_bytes)
    with index_path.open("r+b") as fh:
        fh.truncate(0)
        fh.write(replacement)
        fh.flush()
        os.fsync(fh.fileno())
    os.utime(index_path, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))

    real_fstat = run_journal.os.fstat

    class AliasedIndexStat:
        st_dev = original_stat.st_dev
        st_ino = original_stat.st_ino
        st_size = original_stat.st_size
        st_mtime_ns = original_stat.st_mtime_ns
        st_ctime_ns = original_stat.st_ctime_ns

    def aliased_fstat(fd):
        stat_result = real_fstat(fd)
        try:
            fd_target = os.readlink(f"/proc/self/fd/{fd}")
        except OSError:
            try:
                fd_target = os.readlink(f"/dev/fd/{fd}")
            except OSError:
                fd_target = ""
        if Path(fd_target) == index_path:
            return AliasedIndexStat()
        return stat_result

    monkeypatch.setattr(run_journal.os, "fstat", aliased_fstat)

    second = terminal_run_summary_page_for_session(
        "session_1",
        session_dir=tmp_path,
        limit=1,
        max_candidates=1,
        index_cursor=cursor,
    )

    assert [summary["run_id"] for summary in second["summaries"]] == ["run_002"]


def test_terminal_run_summary_page_cursor_restarts_after_append_during_paging(tmp_path):
    for idx in range(4):
        run_id = f"run_{idx:03d}"
        append_run_event("session_1", run_id, "token", {"text": "ok"}, session_dir=tmp_path)
        append_run_event("session_1", run_id, "done", {"session": {}}, session_dir=tmp_path)

    first = terminal_run_summary_page_for_session(
        "session_1",
        session_dir=tmp_path,
        limit=1,
        max_candidates=1,
    )
    assert [summary["run_id"] for summary in first["summaries"]] == ["run_003"]
    cursor = _terminal_page_cursor(first)

    append_run_event("session_1", "run_004", "token", {"text": "new"}, session_dir=tmp_path)
    append_run_event("session_1", "run_004", "done", {"session": {}}, session_dir=tmp_path)

    second = terminal_run_summary_page_for_session(
        "session_1",
        session_dir=tmp_path,
        limit=1,
        max_candidates=2,
        skip_run_ids={"run_003"},
        index_cursor=cursor,
    )

    assert [summary["run_id"] for summary in second["summaries"]] == ["run_004"]


def test_terminal_run_index_compacts_under_bounded_policy(tmp_path, monkeypatch):
    monkeypatch.setattr(run_journal, "_TERMINAL_INDEX_COMPACT_TRIGGER_BYTES", 1)
    monkeypatch.setattr(run_journal, "_TERMINAL_INDEX_MAX_ROWS", 5)

    for idx in range(12):
        run_id = f"run_{idx:03d}"
        append_run_event("session_1", run_id, "token", {"text": "ok"}, session_dir=tmp_path)
        append_run_event("session_1", run_id, "done", {"session": {}}, session_dir=tmp_path)

    index_path = tmp_path / "_run_journal" / "session_1" / "_terminal_runs.jsonl"
    rows = index_path.read_text(encoding="utf-8").splitlines()
    summaries = terminal_run_summaries_for_session(
        "session_1",
        session_dir=tmp_path,
        limit=12,
        max_candidates=12,
    )

    assert [summary["run_id"] for summary in summaries] == [
        "run_011",
        "run_010",
        "run_009",
        "run_008",
        "run_007",
        "run_006",
        "run_005",
        "run_004",
        "run_003",
        "run_002",
        "run_001",
        "run_000",
    ]
    assert len(rows) == 12

    first = terminal_run_summary_page_for_session(
        "session_1",
        session_dir=tmp_path,
        limit=5,
        max_candidates=5,
    )
    assert [summary["run_id"] for summary in first["summaries"]] == [
        "run_011",
        "run_010",
        "run_009",
        "run_008",
        "run_007",
    ]
    assert compact_terminal_run_index_for_session(
        "session_1",
        session_dir=tmp_path,
        index_cursor=_terminal_page_cursor(first),
    )

    remaining = terminal_run_summaries_for_session(
        "session_1",
        session_dir=tmp_path,
        limit=12,
        max_candidates=12,
    )
    assert [summary["run_id"] for summary in remaining] == [
        "run_006",
        "run_005",
        "run_004",
        "run_003",
        "run_002",
        "run_001",
        "run_000",
    ]


def test_terminal_run_index_compaction_writes_marker_after_full_ack(tmp_path, monkeypatch):
    monkeypatch.setattr(run_journal, "_TERMINAL_INDEX_COMPACT_TRIGGER_BYTES", 1)

    for idx in range(3):
        run_id = f"run_{idx:03d}"
        append_run_event("session_1", run_id, "token", {"text": "ok"}, session_dir=tmp_path)
        append_run_event("session_1", run_id, "done", {"session": {}}, session_dir=tmp_path)

    page = terminal_run_summary_page_for_session(
        "session_1",
        session_dir=tmp_path,
        limit=3,
        max_candidates=3,
    )
    assert page["next_index_end_offset"] == 0
    assert compact_terminal_run_index_for_session(
        "session_1",
        session_dir=tmp_path,
        index_cursor=_terminal_page_cursor(page),
    )

    index_path = tmp_path / "_run_journal" / "session_1" / "_terminal_runs.jsonl"
    rows = [json.loads(row) for row in index_path.read_text(encoding="utf-8").splitlines()]
    assert rows == [
        {
            "version": run_journal.TERMINAL_RUN_INDEX_COMPACTED_MARKER_VERSION,
            "compacted": True,
        }
    ]
    assert terminal_run_summaries_for_session("session_1", session_dir=tmp_path, limit=3) == []

    append_run_event("session_1", "run_new", "token", {"text": "ok"}, session_dir=tmp_path)
    append_run_event("session_1", "run_new", "done", {"session": {}}, session_dir=tmp_path)

    summaries = terminal_run_summaries_for_session("session_1", session_dir=tmp_path, limit=1)
    assert [summary["run_id"] for summary in summaries] == ["run_new"]


def test_terminal_run_index_compaction_holds_process_barrier_against_append(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setattr(run_journal, "_TERMINAL_INDEX_COMPACT_TRIGGER_BYTES", 1)

    append_run_event("session_1", "run_old", "token", {"text": "ok"}, session_dir=tmp_path)
    append_run_event("session_1", "run_old", "done", {"session": {}}, session_dir=tmp_path)
    page = terminal_run_summary_page_for_session(
        "session_1",
        session_dir=tmp_path,
        limit=1,
        max_candidates=1,
    )
    assert page["next_index_end_offset"] == 0

    repo_root = Path(__file__).resolve().parents[1]
    ready_path = tmp_path / "child-ready"
    done_path = tmp_path / "child-done"
    child_code = (
        "import pathlib, sys\n"
        "from api.run_journal import append_run_event\n"
        "session_dir = pathlib.Path(sys.argv[1])\n"
        "pathlib.Path(sys.argv[2]).write_text('ready', encoding='utf-8')\n"
        "append_run_event('session_1', 'run_child', 'token', {'text': 'ok'}, session_dir=session_dir)\n"
        "append_run_event('session_1', 'run_child', 'done', {'session': {}}, session_dir=session_dir)\n"
        "pathlib.Path(sys.argv[3]).write_text('done', encoding='utf-8')\n"
    )
    processes = []
    original_replace = os.replace
    index_path = tmp_path / "_run_journal" / "session_1" / "_terminal_runs.jsonl"

    def replace_after_child_blocks(src, dst):
        if Path(dst) != index_path:
            return original_replace(src, dst)
        process = subprocess.Popen(
            [sys.executable, "-c", child_code, str(tmp_path), str(ready_path), str(done_path)],
            cwd=str(repo_root),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        processes.append(process)
        deadline = time.monotonic() + 5
        while not ready_path.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert ready_path.exists(), "child append process did not reach the append barrier"
        blocked_until = time.monotonic() + 0.25
        while time.monotonic() < blocked_until:
            assert not done_path.exists(), "child append completed inside the contested process lock"
            assert process.poll() is None, "child append process exited before the parent released the lock"
            time.sleep(0.01)
        return original_replace(src, dst)

    monkeypatch.setattr(run_journal.os, "replace", replace_after_child_blocks)

    assert compact_terminal_run_index_for_session(
        "session_1",
        session_dir=tmp_path,
        index_cursor=_terminal_page_cursor(page),
    )
    for process in processes:
        stdout, stderr = process.communicate(timeout=5)
        assert process.returncode == 0, stdout + stderr

    summaries = terminal_run_summaries_for_session("session_1", session_dir=tmp_path, limit=1)
    assert [summary["run_id"] for summary in summaries] == ["run_child"]


def test_terminal_run_index_stale_compaction_cursor_preserves_new_append(tmp_path, monkeypatch):
    monkeypatch.setattr(run_journal, "_TERMINAL_INDEX_COMPACT_TRIGGER_BYTES", 1)

    append_run_event("session_1", "run_old", "token", {"text": "ok"}, session_dir=tmp_path)
    append_run_event("session_1", "run_old", "done", {"session": {}}, session_dir=tmp_path)
    page = terminal_run_summary_page_for_session(
        "session_1",
        session_dir=tmp_path,
        limit=1,
        max_candidates=1,
    )
    cursor = _terminal_page_cursor(page)

    append_run_event("session_1", "run_child", "token", {"text": "ok"}, session_dir=tmp_path)
    append_run_event("session_1", "run_child", "done", {"session": {}}, session_dir=tmp_path)

    assert not compact_terminal_run_index_for_session(
        "session_1",
        session_dir=tmp_path,
        index_cursor=cursor,
    )
    summaries = terminal_run_summaries_for_session("session_1", session_dir=tmp_path, limit=2)
    assert [summary["run_id"] for summary in summaries] == ["run_child", "run_old"]


def test_terminal_index_process_lock_uses_windows_locking_when_fcntl_unavailable(
    tmp_path,
    monkeypatch,
):
    class FakeMsvcrt:
        LK_LOCK = 1
        LK_UNLCK = 2

        def __init__(self):
            self.calls = []

        def locking(self, fd, mode, size):
            self.calls.append((mode, size))

    fake = FakeMsvcrt()
    monkeypatch.setattr(run_journal, "_fcntl", None)
    monkeypatch.setattr(run_journal, "_msvcrt", fake)

    index_path = tmp_path / "_run_journal" / "session_1" / "_terminal_runs.jsonl"
    with run_journal._terminal_index_process_lock(index_path):
        assert (tmp_path / "_run_journal" / "session_1" / "._terminal_runs.jsonl.lock").exists()

    assert fake.calls == [(fake.LK_LOCK, 1), (fake.LK_UNLCK, 1)]


def test_terminal_run_summaries_uses_complete_legacy_summary_fallback(tmp_path, monkeypatch):
    path = tmp_path / "_run_journal" / "session_1" / "legacy_run.jsonl"
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "event": "done",
                "type": "done",
                "seq": 1,
                "event_id": "legacy_run:1",
                "run_id": "legacy_run",
                "session_id": "session_1",
                "terminal": True,
                "terminal_state": "completed",
                "payload": {},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    calls = []
    original_latest_run_summary = latest_run_summary

    def counted_latest_run_summary(
        session_id,
        run_id,
        *,
        session_dir=None,
        max_bytes=None,
        max_rows=None,
    ):
        calls.append({"max_bytes": max_bytes, "max_rows": max_rows})
        return original_latest_run_summary(
            session_id,
            run_id,
            session_dir=session_dir,
            max_bytes=max_bytes,
            max_rows=max_rows,
        )

    monkeypatch.setattr("api.run_journal.latest_run_summary", counted_latest_run_summary)

    summaries = terminal_run_summaries_for_session("session_1", session_dir=tmp_path, limit=1)

    assert [summary["run_id"] for summary in summaries] == ["legacy_run"]
    assert calls == [{"max_bytes": None, "max_rows": None}]


def test_terminal_run_summary_pages_advance_with_compact_index_cursor(tmp_path):
    for idx in range(150):
        run_id = f"run_{idx:03d}"
        append_run_event("session_1", run_id, "token", {"text": "ok"}, session_dir=tmp_path)
        append_run_event("session_1", run_id, "done", {"session": {}}, session_dir=tmp_path)

    first = terminal_run_summary_page_for_session(
        "session_1",
        session_dir=tmp_path,
        limit=64,
        max_candidates=64,
    )
    second = terminal_run_summary_page_for_session(
        "session_1",
        session_dir=tmp_path,
        limit=64,
        max_candidates=64,
        index_cursor=_terminal_page_cursor(first),
    )

    first_ids = [summary["run_id"] for summary in first["summaries"]]
    second_ids = [summary["run_id"] for summary in second["summaries"]]
    assert first_ids[0] == "run_149"
    assert len(first_ids) == 64
    assert len(second_ids) == 64
    assert set(first_ids).isdisjoint(second_ids)
    assert second["next_index_end_offset"] < first["next_index_end_offset"]


def test_latest_summary_reuses_unchanged_journal_summary_without_reparsing(tmp_path, monkeypatch):
    append_run_event("session_1", "run_1", "token", {"text": "ok"}, session_dir=tmp_path)
    append_run_event("session_1", "run_1", "done", {"session": {}}, session_dir=tmp_path)

    first = latest_run_summary("session_1", "run_1", session_dir=tmp_path)

    monkeypatch.setattr(
        "api.run_journal._read_jsonl",
        lambda _path: (_ for _ in ()).throw(AssertionError("unchanged journal was reparsed")),
    )
    repeated = latest_run_summary("session_1", "run_1", session_dir=tmp_path)

    assert repeated == first


def test_summary_cache_invalidates_on_same_size_rewrite_with_restored_mtime(tmp_path, monkeypatch):
    # A same-inode, same-size rewrite that restores the original mtime_ns (e.g. an
    # atomic replace, or a tool that preserves mtime) must still invalidate the
    # cached summary. The signature includes st_ctime_ns — which advances on any
    # content/metadata change and cannot be forged back — so device/inode/size/
    # mtime collisions alone can never serve a stale summary. Proven at the
    # signature level (the enforced TOCTOU precondition for the cache) with a
    # deterministic stat where ONLY ctime differs.
    import api.run_journal as run_journal

    append_run_event("session_1", "run_1", "token", {"text": "ok"}, session_dir=tmp_path)
    path = run_journal._run_path("session_1", "run_1", session_dir=tmp_path)
    real = path.stat()

    class _Stat:
        st_dev = real.st_dev
        st_ino = real.st_ino
        st_size = real.st_size
        st_mtime_ns = real.st_mtime_ns
        st_ctime_ns = real.st_ctime_ns  # overwritten per-call below

    seq = {"ctime": real.st_ctime_ns}

    def fake_stat(self, *a, **k):
        s = _Stat()
        s.st_ctime_ns = seq["ctime"]
        return s

    monkeypatch.setattr(Path, "stat", fake_stat)
    sig_before = run_journal._summary_cache_signature(path)
    # Same dev/inode/size/mtime, but a same-size in-place rewrite advanced ctime.
    seq["ctime"] = real.st_ctime_ns + 1
    sig_after = run_journal._summary_cache_signature(path)

    assert sig_after is not None and sig_before is not None
    assert sig_after != sig_before, "signature must change when only ctime advances"
    assert sig_before[:4] == sig_after[:4], "dev/inode/size/mtime_ns unexpectedly changed"


def test_summary_cache_does_not_store_result_when_journal_changes_during_read(tmp_path, monkeypatch):
    append_run_event("session_1", "run_1", "token", {"text": "ok"}, session_dir=tmp_path)
    append_run_event("session_1", "run_1", "done", {"session": {}}, session_dir=tmp_path)

    import api.run_journal as run_journal

    original_read = run_journal._read_jsonl

    def append_after_read(path):
        events, malformed = original_read(path)
        append_run_event(
            "session_1",
            "run_1",
            "cancel",
            {"message": "Cancelled by user"},
            session_dir=tmp_path,
        )
        return events, malformed

    monkeypatch.setattr(run_journal, "_read_jsonl", append_after_read)

    first = latest_run_summary("session_1", "run_1", session_dir=tmp_path)
    second = latest_run_summary("session_1", "run_1", session_dir=tmp_path)

    assert first["terminal_state"] == "completed"
    assert second["terminal_state"] == "interrupted-by-user"



def test_summary_cache_rejects_first_append_that_races_missing_journal_read(tmp_path, monkeypatch):
    import api.run_journal as run_journal

    original_read = run_journal._read_jsonl
    appended = False

    def append_after_missing_read(path):
        nonlocal appended
        events, malformed = original_read(path)
        if not appended:
            appended = True
            append_run_event(
                "session_1",
                "run_first_append",
                "done",
                {"session": {}},
                session_dir=tmp_path,
            )
        return events, malformed

    monkeypatch.setattr(run_journal, "_read_jsonl", append_after_missing_read)

    raced = latest_run_summary("session_1", "run_first_append", session_dir=tmp_path)
    refreshed = latest_run_summary("session_1", "run_first_append", session_dir=tmp_path)

    assert raced["terminal_state"] == "unknown"
    assert refreshed["terminal_state"] == "completed"
    assert refreshed["last_seq"] == 1
    assert refreshed["last_event_id"] == "run_first_append:1"


def test_terminal_state_classification_distinguishes_crash_from_user_cancel(tmp_path):
    append_run_event("session_1", "run_cancelled", "cancel", {"message": "Cancelled by user"}, session_dir=tmp_path)
    append_run_event("session_1", "run_crashed", "apperror", {"type": "interrupted"}, session_dir=tmp_path)
    append_run_event("session_1", "run_failed", "apperror", {"type": "auth_mismatch"}, session_dir=tmp_path)
    append_run_event("session_1", "run_tool_limit", "apperror", {"type": "tool_limit_reached"}, session_dir=tmp_path)
    append_run_event("session_1", "run_tool_limit_done", "done", {"terminal_state": "tool_limit_reached"}, session_dir=tmp_path)
    append_run_event("session_1", "run_unknown_done", "done", {"terminal_state": "future_unknown_state"}, session_dir=tmp_path)
    append_run_event("session_1", "run_done", "done", {"session": {}}, session_dir=tmp_path)

    assert latest_run_summary("session_1", "run_cancelled", session_dir=tmp_path)["terminal_state"] == "interrupted-by-user"
    assert latest_run_summary("session_1", "run_crashed", session_dir=tmp_path)["terminal_state"] == "interrupted-by-crash"
    assert latest_run_summary("session_1", "run_failed", session_dir=tmp_path)["terminal_state"] == "errored"
    assert latest_run_summary("session_1", "run_tool_limit", session_dir=tmp_path)["terminal_state"] == "tool_limit_reached"
    assert latest_run_summary("session_1", "run_tool_limit_done", session_dir=tmp_path)["terminal_state"] == "tool_limit_reached"
    assert latest_run_summary("session_1", "run_unknown_done", session_dir=tmp_path)["terminal_state"] == "completed"
    assert latest_run_summary("session_1", "run_done", session_dir=tmp_path)["terminal_state"] == "completed"


def test_summary_keeps_logical_terminal_state_when_stream_end_follows(tmp_path):
    append_run_event("session_1", "run_1", "apperror", {"type": "auth_mismatch"}, session_dir=tmp_path)
    append_run_event("session_1", "run_1", "stream_end", {"session_id": "session_1"}, session_dir=tmp_path)

    summary = latest_run_summary("session_1", "run_1", session_dir=tmp_path)

    assert summary["terminal"] is True
    assert summary["last_event"] == "stream_end"
    assert summary["terminal_state"] == "errored"


def test_stale_interrupted_event_reports_non_terminal_journal(tmp_path, monkeypatch):
    append_run_event("session_1", "run_1", "token", {"text": "partial"}, session_dir=tmp_path)

    monkeypatch.setattr("api.run_journal._default_session_dir", lambda: tmp_path)
    event = stale_interrupted_event("session_1", "run_1")
    assert event is not None

    assert event["event"] == "apperror"
    assert event["seq"] == 2
    assert event["terminal_state"] == "lost-worker-bookkeeping"
    assert event["payload"]["type"] == "interrupted"
    assert "last journaled event" in event["payload"]["hint"]
    assert "process restarted" not in event["payload"]["message"]
    assert "lost the live worker" not in event["payload"]["message"]
    assert "live worker stopped" in event["payload"]["message"]


def test_stale_interrupted_event_skips_terminal_journal(tmp_path, monkeypatch):
    append_run_event("session_1", "run_1", "done", {"session": {}}, session_dir=tmp_path)

    monkeypatch.setattr("api.run_journal._default_session_dir", lambda: tmp_path)

    assert stale_interrupted_event("session_1", "run_1") is None
