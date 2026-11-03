"""Failed live appends must not poison the next restart repair.

Faults are injected only at the real filesystem boundary. Session pending
state, RunJournalWriter, Stop, and repeated cold-load repair are production code.
"""
import errno
import json
import os
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from api import models, run_journal
from api.helpers import public_session_projection
from api.run_journal import RunJournalWriter
from tests.test_cancel_restart_journal_recovery import (
    _assert_boundary_output_recovered,
    _isolated_state,  # noqa: F401 -- shared isolated fixture, no live state
    _persist_recovery_boundary_turn,
)


def _fail_append(monkeypatch, path, fault):
    """Inject the same fault for old buffered and candidate binary writers."""
    real_open, real_write, real_fdopen = os.open, os.write, os.fdopen
    real_fsync = os.fsync
    counts = {"write": 0}

    def journal_fd(fd):
        return os.fstat(fd).st_ino == path.stat().st_ino

    def fail_open(name, flags, *args, **kwargs):
        if os.fspath(name) == str(path) and fault in {"enospc", "emfile"}:
            code = errno.ENOSPC if fault == "enospc" else errno.EMFILE
            raise OSError(code, "injected journal open failure")
        return real_open(name, flags, *args, **kwargs)

    def fail_write(fd, data):
        if not journal_fd(fd):
            return real_write(fd, data)
        counts["write"] += 1
        if fault == "zero":
            return 0
        if fault == "partial-error":
            if counts["write"] == 1:
                return real_write(fd, data[:max(1, len(data) // 2)])
            raise OSError(errno.ENOSPC, "injected failure after partial write")
        if fault == "short":
            return real_write(fd, data[:max(1, len(data) // 3)])
        return real_write(fd, data)

    class OldTextFault:
        def __init__(self, fh):
            self.fh = fh

        def __enter__(self):
            self.fh.__enter__()
            return self

        def __exit__(self, *args):
            return self.fh.__exit__(*args)

        def __getattr__(self, name):
            return getattr(self.fh, name)

        def write(self, text):
            data = text.encode("utf-8")
            written = fail_write(self.fh.fileno(), data)
            if fault == "partial-error":
                fail_write(self.fh.fileno(), data[written:])
            return written

    def fail_fdopen(fd, *args, **kwargs):
        fh = real_fdopen(fd, *args, **kwargs)
        if kwargs.get("encoding") == "utf-8" and fault in {"partial-error", "zero", "short"}:
            return OldTextFault(fh)
        return fh

    def fail_fsync(fd):
        if journal_fd(fd) and fault == "fsync":
            raise OSError(errno.EIO, "injected journal fsync failure")
        return real_fsync(fd)

    monkeypatch.setattr(os, "open", fail_open)
    monkeypatch.setattr(os, "write", fail_write)
    monkeypatch.setattr(os, "fdopen", fail_fdopen)
    monkeypatch.setattr(os, "fsync", fail_fsync)
    return counts


def _forget_writer(path):
    # Model a new interpreter's empty sequence cache, without restarting any
    # process or service. The independent review also uses a real child process.
    with run_journal._SEQ_CACHE_LOCK:
        run_journal._SEQ_CACHE.pop(str(path), None)


@pytest.mark.parametrize("lifecycle", ["crash", "stop"])
@pytest.mark.parametrize("completed", [False, True])
@pytest.mark.parametrize("fresh_writer", [False, True])
@pytest.mark.parametrize("fault", ["enospc", "emfile", "partial-error", "zero", "fsync", "serialize"])
def test_failed_append_preserves_answer_through_real_repair(
    lifecycle, completed, fresh_writer, fault, monkeypatch,
):
    sid = f"append-fault-{lifecycle}-{completed}-{fresh_writer}-{fault}"
    stream = sid + "-run"
    session = _persist_recovery_boundary_turn(sid, stream, lifecycle)
    writer = RunJournalWriter(sid, stream)
    writer.append_sse_event("token", {"text": "PREFIX"})
    path = run_journal._run_path(sid, stream)
    before = path.read_bytes()
    # Terminal-mode flush failures are meaningful only for fsynced events.
    event = "done" if fault == "fsync" else "token"
    payload = {"text": object()} if fault == "serialize" else {"text": "LOST"}
    failure = None
    with monkeypatch.context() as patch:
        _fail_append(patch, path, fault)
        # The live adapters catch these exceptions and continue streaming.
        try:
            writer.append_sse_event(event, payload)
        except (OSError, TypeError) as exc:
            failure = exc
    after_failure = path.read_bytes()
    if fresh_writer:
        _forget_writer(path)
        writer = RunJournalWriter(sid, stream)
    continuation = writer.append_sse_event("token", {"text": "AFTER"})
    if completed:
        writer.append_sse_event("done", {"session": public_session_projection(session.__dict__)})
    _assert_boundary_output_recovered(
        sid, stream, "PREFIXAFTER", completed=completed, lifecycle=lifecycle,
    )
    assert failure is not None
    assert after_failure == before
    assert continuation["seq"] == 2


@pytest.mark.parametrize("lifecycle", ["crash", "stop"])
@pytest.mark.parametrize("completed", [False, True])
@pytest.mark.parametrize("fresh_writer", [False, True])
def test_lone_surrogate_roundtrips_journal_and_real_cold_repair(lifecycle, completed, fresh_writer):
    sid = f"append-surrogate-{lifecycle}-{completed}-{fresh_writer}"
    stream = sid + "-run"
    session = _persist_recovery_boundary_turn(sid, stream, lifecycle)
    writer = RunJournalWriter(sid, stream)
    writer.append_sse_event("token", {"text": "PREFIX"})
    writer.append_sse_event("token", {"text": "\ud83d"})
    path = run_journal._run_path(sid, stream)
    assert json.loads(path.read_bytes().splitlines()[1])["payload"]["text"] == "\ud83d"
    if fresh_writer:
        _forget_writer(path)
    assert writer.append_sse_event("token", {"text": "AFTER"})["seq"] == 3
    if completed:
        writer.append_sse_event("done", {"session": public_session_projection(session.__dict__)})
    _assert_boundary_output_recovered(
        sid, stream, "PREFIX\ud83dAFTER", completed=completed, lifecycle=lifecycle,
    )
    assert any(row.get("content") == "PREFIX\ud83dAFTER" for row in models.Session.load(sid).messages)


@pytest.mark.parametrize("lifecycle", ["crash", "stop"])
@pytest.mark.parametrize("completed", [False, True])
@pytest.mark.parametrize("tail", ["json", "utf8", "valid-token", "valid-done"])
def test_fresh_append_repairs_tail_then_real_repair_recovers(lifecycle, completed, tail):
    sid = f"append-tail-{lifecycle}-{completed}-{tail}"
    stream = sid + "-run"
    session = _persist_recovery_boundary_turn(sid, stream, lifecycle)
    writer = RunJournalWriter(sid, stream)
    writer.append_sse_event("token", {"text": "PREFIX"})
    path = run_journal._run_path(sid, stream)
    if tail in {"valid-token", "valid-done"}:
        writer.append_sse_event("done" if tail == "valid-done" else "token", {"text": "MIDDLE"})
        path.write_bytes(path.read_bytes().rstrip(b"\n"))
        expected_seq = 3
        answer = "PREFIXMIDDLEAFTER" if tail == "valid-token" else "PREFIX"
    else:
        with path.open("ab") as fh:
            fh.write(b'{"seq":' if tail == "json" else b'{"payload":{"text":"\xe2\x82')
        expected_seq, answer = 2, "PREFIXAFTER"
    _forget_writer(path)
    # A terminal prefix may receive stream_end after reattach. Do not invent
    # another token segment after done and demand that recovery merge it.
    event = "stream_end" if tail == "valid-done" else "token"
    continuation = RunJournalWriter(sid, stream).append_sse_event(event, {"text": "AFTER"})
    if completed:
        writer.append_sse_event("done", {"session": public_session_projection(session.__dict__)})
    _assert_boundary_output_recovered(
        sid, stream, answer, completed=completed or tail == "valid-done", lifecycle=lifecycle,
    )
    assert continuation["seq"] == expected_seq


def test_positive_short_writes_finish_one_complete_row(tmp_path, monkeypatch):
    writer = RunJournalWriter("short-session", "short-run", session_dir=tmp_path)
    writer.append_sse_event("token", {"text": "PREFIX"})
    path = run_journal._run_path("short-session", "short-run", session_dir=tmp_path)
    with monkeypatch.context() as patch:
        counts = _fail_append(patch, path, "short")
        assert writer.append_sse_event("token", {"text": "AFTER"})["seq"] == 2
    rows = run_journal.read_run_events("short-session", "short-run", session_dir=tmp_path, validated_recovery=True)
    assert [row["payload"]["text"] for row in rows["events"]] == ["PREFIX", "AFTER"]
    assert counts["write"] > 1


@pytest.mark.parametrize("tail", ["foreign", "duplicate", "terminal", "malformed-newline", "invalid-utf8"])
def test_first_append_does_not_erase_invalid_committed_evidence(tmp_path, tail):
    sid, stream = "invalid-append-session", "invalid-append-run"
    writer = RunJournalWriter(sid, stream, session_dir=tmp_path)
    row = writer.append_sse_event("token", {"text": "PREFIX"})
    path = run_journal._run_path(sid, stream, session_dir=tmp_path)
    if tail == "foreign":
        row["session_id"] = "foreign"
    elif tail == "duplicate":
        row.update(seq=1, event_id=f"{stream}:1")
    elif tail == "terminal":
        row["terminal"] = True
    fragment = json.dumps(row).encode()
    if tail == "malformed-newline":
        fragment = b'{broken\n'
    elif tail == "invalid-utf8":
        fragment = b'{"text":"\xff"}'
    with path.open("ab") as fh:
        fh.write(fragment)
    before = path.read_bytes()
    _forget_writer(path)
    with pytest.raises(ValueError):
        writer.append_sse_event("token", {"text": "AFTER"})
    assert path.read_bytes() == before
    assert not run_journal.read_run_events(sid, stream, session_dir=tmp_path, validated_recovery=True)["events"]


def test_fresh_writer_keeps_explicit_sequence_contract_without_recovery_authority(tmp_path):
    sid, stream = "explicit-seed-session", "explicit-seed-run"
    run_journal.append_run_event(sid, stream, "token", {"text": "PREFIX"}, session_dir=tmp_path, seq=5)
    path = run_journal._run_path(sid, stream, session_dir=tmp_path)
    before = path.read_bytes()
    _forget_writer(path)
    assert RunJournalWriter(sid, stream, session_dir=tmp_path).append_sse_event("cancel", {})["seq"] == 6
    assert path.read_bytes().startswith(before)
    assert [row["seq"] for row in run_journal.read_run_events(sid, stream, session_dir=tmp_path)["events"]] == [5, 6]
    strict = run_journal.read_run_events(sid, stream, session_dir=tmp_path, validated_recovery=True)
    assert strict["events"] == []
    assert run_journal.select_authoritative_terminal_event(strict["events"]) is None


def test_rollback_failure_evicts_sequence_then_revalidates_actual_tail(tmp_path, monkeypatch):
    sid, stream = "rollback-session", "rollback-run"
    writer = RunJournalWriter(sid, stream, session_dir=tmp_path)
    writer.append_sse_event("token", {"text": "PREFIX"})
    path = run_journal._run_path(sid, stream, session_dir=tmp_path)
    before = path.read_bytes()
    with monkeypatch.context() as patch:
        _fail_append(patch, path, "partial-error")

        def failed_rollback(fd, size):
            raise OSError(errno.EIO, "injected rollback failure")

        patch.setattr(os, "ftruncate", failed_rollback)
        with pytest.raises(OSError, match="rollback failure"):
            writer.append_sse_event("token", {"text": "LOST"})
    assert str(path) not in run_journal._SEQ_CACHE
    assert path.read_bytes().startswith(before)
    assert path.stat().st_size > len(before)
    assert writer.append_sse_event("token", {"text": "AFTER"})["seq"] == 2
    assert path.read_bytes().splitlines(keepends=True)[0] == before
    strict = run_journal.read_run_events(sid, stream, session_dir=tmp_path, validated_recovery=True)
    assert [row["payload"]["text"] for row in strict["events"]] == ["PREFIX", "AFTER"]


@pytest.mark.parametrize("tail", [b'{"seq":', b'{"text":"\xe2\x82'])
def test_real_gateway_reattach_repairs_tail_before_journal_is_needed_again(tail, monkeypatch):
    import api.gateway_chat as gateway_chat
    from tests.test_gateway_run_reattach_after_restart import (
        _orphaned_gateway_turn,
        _wait_for_reattach_threads,
    )

    monkeypatch.setenv("HERMES_WEBUI_CHAT_BACKEND", "gateway")
    monkeypatch.setenv("HERMES_WEBUI_GATEWAY_USE_RUNS_API", "1")
    monkeypatch.setenv("HERMES_WEBUI_GATEWAY_BASE_URL", "http://gateway.local")
    monkeypatch.setattr(gateway_chat, "GATEWAY_REATTACH_POLL_INTERVAL", 0.01)
    sid, stream = _orphaned_gateway_turn()
    session = models.Session.load(sid)
    writer = RunJournalWriter(sid, stream)
    writer.append_sse_event("token", {"text": "PREFIX"})
    path = run_journal._run_path(sid, stream)
    with path.open("ab") as fh:
        fh.write(tail)
    _forget_writer(path)
    monkeypatch.setattr(gateway_chat, "_get_gateway_run_status", lambda *a, **kw: {
        "run_id": "run_survivor", "status": "completed", "output": "PREFIX",
    })
    assert gateway_chat.resume_gateway_runs_after_restart() == [sid]
    _wait_for_reattach_threads()
    rows = run_journal.read_run_events(sid, stream, validated_recovery=True)
    assert rows["malformed"] == []
    assert run_journal.select_authoritative_terminal_event(rows["events"])["terminal_state"] == "completed"
    assert rows["events"][0]["payload"]["text"] == "PREFIX"
    # Model a stale pending checkpoint that requires the journal on a later
    # cold load. This is a controlled persistence fixture, not a power-loss test.
    session.gateway_run = None
    session.save()
    _assert_boundary_output_recovered(sid, stream, "PREFIX", completed=True, lifecycle="crash")


def test_cancel_repair_cannot_observe_terminal_before_failed_fsync_rolls_back(monkeypatch):
    from api import config
    from tests.test_cancel_restart_journal_recovery import _pending_stream_hook, _stream_output

    sid, stream = "failed-terminal-admission", "failed-terminal-admission-run"
    _persist_recovery_boundary_turn(sid, stream, "stop")
    config.ACTIVE_RUNS.clear()
    writer = RunJournalWriter(sid, stream)
    writer.append_sse_event("token", {"text": "PREFIX"})
    path = run_journal._run_path(sid, stream)
    fsync_entered, release = threading.Event(), threading.Event()
    reader_entered, reader_finished = threading.Event(), threading.Event()
    real_fsync = os.fsync
    real_read = run_journal._read_validated_recovery_events

    def pause_then_fail(fd):
        if os.fstat(fd).st_ino == path.stat().st_ino:
            fsync_entered.set()
            assert release.wait(5)
            raise OSError(errno.EIO, "injected delayed terminal fsync failure")
        return real_fsync(fd)

    def note_read(*args, **kwargs):
        reader_entered.set()
        return real_read(*args, **kwargs)

    def read_session():
        try:
            models.SESSIONS.clear()
            return models.get_session(sid)
        finally:
            reader_finished.set()

    with monkeypatch.context() as patch, ThreadPoolExecutor(max_workers=2) as pool:
        patch.setattr(os, "fsync", pause_then_fail)
        patch.setattr(run_journal, "_read_validated_recovery_events", note_read)
        writing = pool.submit(writer.append_sse_event, "done", {})
        try:
            assert fsync_entered.wait(5)
            reading = pool.submit(read_session)
            assert reader_entered.wait(5)
            assert not reader_finished.wait(0.25), "recovery observed a terminal row before append settlement"
        finally:
            release.set()
        with pytest.raises(OSError, match="terminal fsync failure"):
            writing.result(timeout=5)
        recovered = reading.result(timeout=5)
    assert _stream_output(recovered, stream) == []
    assert _pending_stream_hook(recovered, stream) is not None
    assert models._run_journal_terminal_state(recovered, stream) is None
    assert writer.append_sse_event("done", {})["seq"] == 2
    _assert_boundary_output_recovered(sid, stream, "PREFIX", completed=True, lifecycle="stop")


def test_missing_recovery_journal_does_not_allocate_a_writer_lock(tmp_path):
    sid, stream = "missing-reader-session", "missing-reader-run"
    parent = str(tmp_path / run_journal.RUN_JOURNAL_DIR_NAME / sid)
    result = run_journal.read_run_events(sid, stream, session_dir=tmp_path, validated_recovery=True)
    assert result["events"] == []
    assert run_journal.delete_run_journal(sid, session_dir=tmp_path) is False
    assert not any(key[0] == parent for key in run_journal._WRITER_LOCKS)
