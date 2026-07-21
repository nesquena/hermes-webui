import json
import os
from pathlib import Path

from api.run_journal import (
    RunJournalWriter,
    append_run_event,
    find_run_summary,
    latest_terminal_run_summary_for_session,
    latest_run_summary,
    read_run_events,
    stale_interrupted_event,
    terminal_run_summaries_for_session,
)


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

    # Terminal events also update the durable terminal-run index used by
    # unresolved Worklog reconciliation.
    assert len(fsync_calls) == 3


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
