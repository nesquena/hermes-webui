"""Sequence allocation and physical append must share one journal lock.

Only scheduling at the real append boundary is controlled. Writers, sequence
allocation, on-disk JSONL and the session replay reader are production code.
"""
from concurrent.futures import ThreadPoolExecutor
import threading

import pytest

from api import run_journal


@pytest.mark.parametrize("competitor", ["same_writer", "other_writer", "free_function"])
def test_writer_sequence_follows_physical_append_order(tmp_path, monkeypatch, competitor):
    sid, rid = "writer_order_session", "writer_order_run"
    writer = run_journal.RunJournalWriter(sid, rid, session_dir=tmp_path)
    other = run_journal.RunJournalWriter(sid, rid, session_dir=tmp_path)
    reached = threading.Event()
    release = threading.Event()
    real_append = run_journal.append_run_event

    def pause_first_append(session_id, run_id, event_name, payload=None, **kwargs):
        if payload == {"text": "paused"}:
            reached.set()
            assert release.wait(5), "test did not release the paused append"
        return real_append(session_id, run_id, event_name, payload, **kwargs)

    monkeypatch.setattr(run_journal, "append_run_event", pause_first_append)

    def append_competitor():
        if competitor == "free_function":
            return run_journal.append_run_event(
                sid, rid, "token", {"text": "overtaking"}, session_dir=tmp_path
            )
        target = writer if competitor == "same_writer" else other
        return target.append_sse_event("token", {"text": "overtaking"})

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(writer.append_sse_event, "token", {"text": "paused"})
        try:
            assert reached.wait(5), "writer did not reach the append boundary"
            second = pool.submit(append_competitor).result(timeout=5)
        finally:
            release.set()
        first_result = first.result(timeout=5)

    journal = run_journal.read_run_events(sid, rid, session_dir=tmp_path)
    # Deliberately do not sort: the reader requires physical contiguous order.
    assert [row["seq"] for row in journal["events"]] == [1, 2]
    assert [row["payload"]["text"] for row in journal["events"]] == ["overtaking", "paused"]
    assert second["event_id"] == f"{rid}:1"
    assert first_result["event_id"] == f"{rid}:2"
    replay = run_journal.read_session_run_events(
        sid, after_event_id=second["event_id"], session_dir=tmp_path
    )
    assert replay["status"] == "ok"
    assert [row["event_id"] for row in replay["events"]] == [first_result["event_id"]]


def test_writer_rejected_empty_name_does_not_reserve_a_sequence(tmp_path):
    writer = run_journal.RunJournalWriter("name_session", "name_run", session_dir=tmp_path)
    with pytest.raises(ValueError, match="event_name is required"):
        writer.append_sse_event("  ", {"text": "rejected"})
    event = writer.append_sse_event("token", {"text": "accepted"})
    assert event["seq"] == 1
    assert event["event_id"] == "name_run:1"


def test_metering_skip_still_does_not_reserve_or_write(tmp_path):
    writer = run_journal.RunJournalWriter("meter_session", "meter_run", session_dir=tmp_path)
    assert writer.append_sse_event("metering", {"tps": 10}) is None
    assert not (tmp_path / "_run_journal" / "meter_session" / "meter_run.jsonl").exists()
    assert writer.append_sse_event("token", {"text": "kept"})["seq"] == 1


def test_unused_writer_does_not_allocate_a_registry_lock(tmp_path):
    parent = str(tmp_path / run_journal.RUN_JOURNAL_DIR_NAME / "unused_session")
    writer = run_journal.RunJournalWriter("unused_session", "unused_run", session_dir=tmp_path)
    assert writer.append_sse_event("metering", {"tps": 10}) is None
    assert not any(key[0] == parent for key in run_journal._WRITER_LOCKS)
