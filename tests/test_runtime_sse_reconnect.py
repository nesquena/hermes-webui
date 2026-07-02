import json
import time

from api.runtime_contract import make_event, make_status
from api.runtime_journal import RuntimeJournal


def test_sse_event_stream_includes_id():
    journal = RuntimeJournal(base_dir="/tmp/hermes-test-runtime-sse")
    try:
        status = journal.create_run("session_sse")
        for seq in range(1, 4):
            journal.append_event(
                make_event(
                    run_id=status.run_id,
                    session_id="session_sse",
                    seq=seq,
                    type="token.delta",
                    payload={"text": f"token_{seq}"},
                )
            )
        events = journal.read_events(status.run_id)
        for i, ev in enumerate(events, start=1):
            assert ev.event_id == f"{status.run_id}:{i}"
            assert ev.seq == i
            assert isinstance(ev.to_dict(), dict)
            d = ev.to_dict()
            assert "event_id" in d
            assert "seq" in d
    finally:
        import shutil

        shutil.rmtree("/tmp/hermes-test-runtime-sse", ignore_errors=True)


def test_sse_reconnect_after_seq_resumes():
    journal = RuntimeJournal(base_dir="/tmp/hermes-test-runtime-sse-reconnect")
    try:
        status = journal.create_run("session_reconnect")
        for seq in range(1, 6):
            journal.append_event(
                make_event(
                    run_id=status.run_id,
                    session_id="session_reconnect",
                    seq=seq,
                    type="token.delta",
                    payload={"text": f"chunk_{seq}"},
                )
            )
        resumed = journal.read_events(status.run_id, after_seq=2)
        assert len(resumed) == 3
        assert resumed[0].seq == 3
        assert resumed[0].payload["text"] == "chunk_3"

        resumed2 = journal.read_events(status.run_id, after_seq=5)
        assert len(resumed2) == 0
    finally:
        import shutil

        shutil.rmtree("/tmp/hermes-test-runtime-sse-reconnect", ignore_errors=True)


def test_sse_reconnect_limit():
    journal = RuntimeJournal(base_dir="/tmp/hermes-test-runtime-sse-limit")
    try:
        status = journal.create_run("session_limit")
        for seq in range(1, 10):
            journal.append_event(
                make_event(
                    run_id=status.run_id,
                    session_id="session_limit",
                    seq=seq,
                    type="token.delta",
                    payload={"text": f"chunk_{seq}"},
                )
            )
        limited = journal.read_events(status.run_id, limit=3)
        assert len(limited) == 3
        assert limited[0].seq == 1
        assert limited[2].seq == 3
    finally:
        import shutil

        shutil.rmtree("/tmp/hermes-test-runtime-sse-limit", ignore_errors=True)


def test_sse_reconnect_terminal_run_no_linger():
    journal = RuntimeJournal(base_dir="/tmp/hermes-test-runtime-sse-terminal")
    try:
        status = journal.create_run("session_terminal")
        journal.mark_terminal(status.run_id, "completed")
        active = journal.get_active_run_for_session("session_terminal")
        assert active is None
    finally:
        import shutil

        shutil.rmtree("/tmp/hermes-test-runtime-sse-terminal", ignore_errors=True)


def test_event_id_stable_across_replay():
    journal = RuntimeJournal(base_dir="/tmp/hermes-test-runtime-sse-stable")
    try:
        status = journal.create_run("session_stable")
        journal.append_event(
            make_event(run_id=status.run_id, session_id="session_stable", seq=1, type="run.started")
        )
        journal.append_event(
            make_event(run_id=status.run_id, session_id="session_stable", seq=2, type="done", terminal=True)
        )
        events = journal.read_events(status.run_id)
        assert len(events) == 2
        assert events[0].event_id == f"{status.run_id}:1"
        assert events[1].event_id == f"{status.run_id}:2"
        assert events[1].terminal is True
    finally:
        import shutil

        shutil.rmtree("/tmp/hermes-test-runtime-sse-stable", ignore_errors=True)
