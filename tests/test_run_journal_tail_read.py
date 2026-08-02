"""Tests: run-journal summary readers use a bounded TAIL read.

Regression: ``_read_jsonl`` read the WHOLE journal file via ``read_text()`` and
parsed every line. ``read_session_run_events`` (the replay path) already had
``_SESSION_REPLAY_MAX_BYTES`` / ``_SESSION_REPLAY_MAX_ROWS`` caps + chunked
streaming, but the summary readers on the hot status/sidebar poll path
(``latest_run_summary`` / ``find_run_summary``, via ``_read_jsonl``) did not — a
turn with heavy tool use / large file reads produced a multi-MB journal fully
re-parsed on every poll.

The summary readers now read the bounded TAIL: ``last_seq`` /
``last_event_id`` / ``terminal_state`` are derived from the LAST events, so a
tail read keeps them correct for a large COMPLETED run (its terminal marker
lives at the end) without parsing the whole history.
"""
import json
import os

from api.run_journal import (
    RunJournalWriter,
    _read_jsonl,
    find_run_summary,
    latest_run_summary,
    read_run_events,
)
from api.run_journal import _SESSION_REPLAY_MAX_ROWS


def _write_n_events(session_dir, *, session_id, run_id, n, terminal_after=None):
    """Append n token events, optionally appending a terminal `done` at the end."""
    writer = RunJournalWriter(session_id, run_id, session_dir=session_dir)
    for i in range(n):
        writer.append_sse_event("token", {"text": f"tok-{i}", "i": i})
    if terminal_after is not None:
        writer.append_sse_event("done", {"session": {"session_id": session_id}})


def test_latest_run_summary_reads_tail_and_keeps_terminal_state(tmp_path):
    """A run with FAR more events than the row cap still reports the correct
    terminal_state and last_seq — proving the summary reads the tail (where the
    terminal marker lives), not a head cap that would misreport it as running."""
    n = _SESSION_REPLAY_MAX_ROWS + 1500  # well past the row cap
    _write_n_events(
        tmp_path,
        session_id="session_1",
        run_id="run_big",
        n=n,
        terminal_after=True,
    )
    summary = latest_run_summary("session_1", "run_big", session_dir=tmp_path)
    # The terminal `done` is the LAST event; tail-read must surface it.
    assert summary["terminal"] is True
    assert summary["terminal_state"] == "completed"
    # last_seq is the terminal event's seq == n + 1 (n tokens + 1 done).
    assert summary["last_seq"] == n + 1


def test_find_run_summary_reads_tail_and_keeps_terminal_state(tmp_path):
    """Same tail contract for find_run_summary (used by route status polling)."""
    n = _SESSION_REPLAY_MAX_ROWS + 500
    _write_n_events(
        tmp_path,
        session_id="session_z",
        run_id="run_zzz",
        n=n,
        terminal_after=True,
    )
    summary = find_run_summary("run_zzz", session_dir=tmp_path)
    assert summary is not None
    assert summary["terminal"] is True
    assert summary["terminal_state"] == "completed"
    assert summary["last_seq"] == n + 1


def test_read_jsonl_tail_returns_only_recent_rows(tmp_path):
    """_read_jsonl(tail=True) returns at most max_rows events from the END of the
    file (newest), discarding older head events."""
    path = tmp_path / "j.jsonl"
    lines = []
    for i in range(100):
        lines.append(json.dumps({"seq": i, "event": "token", "payload": {"i": i}}))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    events, _malformed = _read_jsonl(path, max_rows=10, tail=True)
    # Newest 10 events retained (seq 90..99); older head events dropped.
    assert len(events) == 10
    assert [e["seq"] for e in events] == list(range(90, 100))


def test_read_jsonl_tail_respects_byte_cap(tmp_path):
    """_read_jsonl(tail=True) reads at most max_bytes from the end of the file."""
    path = tmp_path / "j.jsonl"
    lines = []
    for i in range(1000):
        lines.append(json.dumps({"seq": i, "event": "token"}))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    full_size = path.stat().st_size
    # Ask for the last ~10% of the file.
    cap = full_size // 10
    events, _malformed = _read_jsonl(path, max_bytes=cap, tail=True)
    # The tail read returns only events within the byte window (bounded, not all
    # 1000). The LAST event is always included (it's within the window).
    assert len(events) < 1000
    assert events[-1]["seq"] == 999


def test_read_jsonl_default_unbounded_still_works(tmp_path):
    """Backward compatibility: with no caps, _read_jsonl reads the whole file."""
    path = tmp_path / "j.jsonl"
    path.write_text(
        "\n".join(json.dumps({"seq": i}) for i in range(5)) + "\n", encoding="utf-8"
    )
    events, _mal = _read_jsonl(path)
    assert [e["seq"] for e in events] == [0, 1, 2, 3, 4]


def test_read_run_events_accepts_optional_caps(tmp_path):
    """read_run_events now accepts max_bytes/max_rows (forward, head cap) without
    changing its default whole-file behavior or after_seq/max_seq filtering."""
    writer = RunJournalWriter("s", "r", session_dir=tmp_path)
    for i in range(5):
        writer.append_sse_event("token", {"i": i})
    # Default: all 5.
    j = read_run_events("s", "r", session_dir=tmp_path)
    assert len(j["events"]) == 5
    # Capped: head cap returns a prefix.
    j2 = read_run_events("s", "r", session_dir=tmp_path, max_rows=2)
    assert len(j2["events"]) == 2
    # after_seq filtering still applies on top.
    j3 = read_run_events("s", "r", session_dir=tmp_path, after_seq=3)
    assert [e["seq"] for e in j3["events"]] == [4, 5]


def test_existing_summary_classification_still_works(tmp_path):
    """The terminal-state classification assertions (from test_run_journal.py)
    still hold with the tail read."""
    w = RunJournalWriter("session_1", "run_done", session_dir=tmp_path)
    w.append_sse_event("token", {"text": "hi"})
    w.append_sse_event("done", {"session": {"session_id": "session_1"}})
    assert latest_run_summary("session_1", "run_done", session_dir=tmp_path)["terminal_state"] == "completed"

    w2 = RunJournalWriter("session_1", "run_cancelled", session_dir=tmp_path)
    w2.append_sse_event("cancel", {})
    assert latest_run_summary("session_1", "run_cancelled", session_dir=tmp_path)["terminal_state"] == "interrupted-by-user"


def test_read_jsonl_tail_line_numbers_correct_when_file_exceeds_cap(tmp_path):
    """Regression: malformed line numbers in tail mode were computed from the
    BYTE offset of the seek point, not from counting newlines — so a malformed
    line at real line 181 was reported as line ~6892 (the byte offset). The
    discarded-head newline count must drive the attribution, and the dropped
    partial line at the seek boundary accounts for the +2."""
    path = tmp_path / "big.jsonl"
    # 200 lines, ~50 bytes each (~10KB total). max_bytes well under that so we
    # seek into the middle of the file.
    lines = [json.dumps({"seq": i, "pad": "x" * 20}) for i in range(200)]
    malformed_real_line = 181  # 1-based
    lines[malformed_real_line - 1] = "BROKEN_LINE_NOT_JSON"
    path.write_text("".join(l + "\n" for l in lines), encoding="utf-8")

    events, malformed = _read_jsonl(path, max_bytes=2000, max_rows=10000, tail=True)
    assert len(events) < 200  # head events dropped (bounded tail window)
    assert len(malformed) == 1
    # The reported line number must be the TRUE 1-based line, not a byte offset.
    assert malformed[0]["line"] == malformed_real_line, (
        f"expected line {malformed_real_line}, got {malformed[0]['line']} "
        "(tail line-number attribution must count newlines, not byte offset)"
    )


def test_read_jsonl_tail_line_numbers_correct_with_rows_cap(tmp_path):
    """When the tail window keeps only the last N rows (rows_cap), the line
    numbers of malformed entries in that kept window must still reflect their
    true position in the WHOLE file, not be renumbered from 1."""
    path = tmp_path / "many.jsonl"
    lines = [json.dumps({"seq": i}) for i in range(100)]
    lines[90] = "BROKEN_AT_91"  # 1-based line 91
    path.write_text("".join(l + "\n" for l in lines), encoding="utf-8")

    # Keep only the last 20 lines (file lines 81..100). The malformed line is
    # file line 91, so it stays in the kept window and must be reported as 91.
    events, malformed = _read_jsonl(path, max_bytes=1 << 30, max_rows=20, tail=True)
    assert len(events) <= 20
    assert len(malformed) == 1
    assert malformed[0]["line"] == 91, f"expected 91, got {malformed[0]['line']}"


def test_read_jsonl_tail_line_numbers_correct_no_seek(tmp_path):
    """When the whole file fits in the window (no seek), line attribution starts
    at 1 and is simply the position in the file."""
    path = tmp_path / "small.jsonl"
    lines = [json.dumps({"seq": 0}), json.dumps({"seq": 1}), "BROKEN", json.dumps({"seq": 3})]
    path.write_text("".join(l + "\n" for l in lines), encoding="utf-8")

    events, malformed = _read_jsonl(path, max_bytes=1 << 30, max_rows=100, tail=True)
    assert len(events) == 3  # seq 0, 1, 3
    assert len(malformed) == 1
    assert malformed[0]["line"] == 3, f"expected 3, got {malformed[0]['line']}"


def test_terminal_state_correct_when_terminal_record_exceeds_tail_window(tmp_path):
    """Regression: streaming.py journals the terminal `done` event with the FULL
    transcript as its payload, so a large session's terminal record can be bigger
    than the 4 MiB tail window. The tail reader used to seek into the middle of
    that record, find its trailing newline as the only newline in the window,
    slice to an empty string, and return NO events — so latest_run_summary
    misclassified a COMPLETED run as `unknown` (a recovery bug). The fix recovers
    the last complete line by scanning backward from EOF when the window yields
    no whole parseable line."""
    from api.run_journal import _SESSION_REPLAY_MAX_BYTES

    writer = RunJournalWriter("session_1", "run_huge_done", session_dir=tmp_path)
    writer.append_sse_event("token", {"text": "hi"})
    # Append the terminal `done` with a payload larger than the tail window —
    # mirrors streaming.py journaling done with the full transcript.
    huge_payload = {"text": "X" * (_SESSION_REPLAY_MAX_BYTES + 100_000)}
    writer.append_sse_event(
        "done", {"session": {"session_id": "session_1"}, **huge_payload}
    )

    summary = latest_run_summary("session_1", "run_huge_done", session_dir=tmp_path)
    # Before the fix: terminal_state was "unknown", terminal False, last_seq 0.
    assert summary["terminal_state"] == "completed", (
        f"a completed run must stay completed even when its terminal record "
        f"exceeds the tail window; got {summary['terminal_state']!r}"
    )
    assert summary["terminal"] is True
    assert summary["last_seq"] == 2  # 1 token + 1 done

    # find_run_summary (the other summary reader) must agree.
    found = find_run_summary("run_huge_done", session_dir=tmp_path)
    assert found is not None
    assert found["terminal_state"] == "completed"
    assert found["last_seq"] == 2


def test_oversized_done_followed_by_trailing_events_reports_correct_terminal(tmp_path):
    """Regression (reviewer round 2): the production event order is
    done(tool_limit_reached, oversized) -> metering -> stream_end. The first-round
    fix only recovered the LAST complete line, so the oversized `done` in the
    MIDDLE was skipped and the tail read reported `completed` (from the trailing
    stream_end) while the authoritative full read reported `tool_limit_reached`.
    The fix extracts the boundary-straddling oversized record's summary via a
    bounded prefix and merges it before summarizing."""
    from api.run_journal import _SESSION_REPLAY_MAX_BYTES

    writer = RunJournalWriter("session_1", "run_oversized_middle", session_dir=tmp_path)
    writer.append_sse_event("token", {"text": "hi"})
    # Oversized done in the MIDDLE with a non-completed terminal_state, followed
    # by trailing complete records (the production order).
    huge = {"text": "X" * (_SESSION_REPLAY_MAX_BYTES + 100_000)}
    writer.append_sse_event(
        "done",
        {"session": {"session_id": "session_1"}, "terminal_state": "tool_limit_reached", **huge},
    )
    writer.append_sse_event("metering", {"usage": {"input": 100}})
    writer.append_sse_event("stream_end", {})

    summary = latest_run_summary("session_1", "run_oversized_middle", session_dir=tmp_path)
    # Authoritative full read (the whole point: tail must MATCH this).
    path = _run_path_of(writer)
    full_events, _ = read_events_via_full_read(path)
    authoritative = _summary_from_events_pub("session_1", "run_oversized_middle", full_events)
    assert summary["terminal_state"] == authoritative["terminal_state"] == "tool_limit_reached", (
        f"tail={summary['terminal_state']!r} but authoritative={authoritative['terminal_state']!r}; "
        "the oversized middle done must not be skipped"
    )
    assert summary["last_seq"] == authoritative["last_seq"]


def test_oversized_record_summary_does_not_materialize_payload(tmp_path):
    """Regression (reviewer round 2): the first-round fix recovered the oversized
    record by reading the WHOLE last line (multi-MB), defeating the memory-bound
    goal. The fix extracts ONLY the summary fields via a bounded prefix read —
    the payload must NOT be materialized."""
    from api.run_journal import (
        _SESSION_REPLAY_MAX_BYTES,
        _extract_boundary_record_summary,
        _find_record_start_before,
    )

    writer = RunJournalWriter("session_1", "run_oversized_last", session_dir=tmp_path)
    writer.append_sse_event("token", {"text": "hi"})
    huge = {"text": "X" * (_SESSION_REPLAY_MAX_BYTES + 100_000)}
    writer.append_sse_event("done", {"session": {"session_id": "session_1"}, **huge})

    path = _run_path_of(writer)
    size = path.stat().st_size
    read_bytes = min(size, _SESSION_REPLAY_MAX_BYTES)
    seek_pos = size - read_bytes
    with path.open("rb") as fh:
        size_pinned = os.fstat(fh.fileno()).st_size
        record_start = _find_record_start_before(fh, size_pinned, seek_pos)
        summary = _extract_boundary_record_summary(fh, record_start)
    assert summary is not None
    # The summary fields are present...
    assert summary["terminal_state"] == "completed"
    assert summary["seq"] == 2
    assert summary.get("_summary_extracted_from_oversized_record") is True
    # ...but the (multi-MB) payload was NOT materialized — replaced with {} .
    assert summary["payload"] == {}, "payload must not be materialized for an oversized record"


# ── tiny helpers so the regression tests read clearly ────────────────────────


def _run_path_of(writer):
    from api.run_journal import _run_path
    return _run_path(writer.session_id, writer.run_id, session_dir=writer.session_dir)


def read_events_via_full_read(path):
    from api.run_journal import _read_jsonl
    return _read_jsonl(path)


def _summary_from_events_pub(session_id, run_id, events):
    from api.run_journal import _summary_from_events
    return _summary_from_events(session_id, run_id, events)


def test_crash_truncated_oversized_done_stays_nonterminal(tmp_path):
    """Regression (reviewer round 3): a crash-truncated oversized `done` record
    (write interrupted mid-payload: no closing brace, no newline terminator) must
    NOT be fabricated into a terminal event. Origin/master reports `running` and
    emits the recovery-control `apperror`; the prefix-summary approach without a
    completeness check reported `completed` and suppressed that signal. The fix
    validates the boundary record is structurally complete before trusting its
    prefix summary — a truncated record is discarded and the run stays
    nonterminal (its apperror recovery survives)."""
    from api.run_journal import _SESSION_REPLAY_MAX_BYTES, _run_path

    writer = RunJournalWriter("session_1", "run_crash_truncated", session_dir=tmp_path)
    writer.append_sse_event("token", {"text": "hi"})
    # Append a PARTIAL oversized done: summary fields + partial payload, NO close.
    path = _run_path(writer.session_id, writer.run_id, session_dir=writer.session_dir)
    partial = (
        '{"version":1,"event_id":"run_crash_truncated:2","seq":2,'
        '"event":"done","type":"done","terminal":true,'
        '"terminal_state":"completed","payload":{"text":"'
        + "X" * (_SESSION_REPLAY_MAX_BYTES + 50_000)
    )  # no closing quote/brace/newline — crash mid-write
    with open(path, "a", encoding="utf-8") as f:
        f.write(partial)

    summary = latest_run_summary(writer.session_id, writer.run_id, session_dir=writer.session_dir)
    # Must NOT be falsely terminal — the run was interrupted.
    assert summary["terminal"] is False, (
        f"a crash-truncated done must not be accepted as terminal; got "
        f"terminal_state={summary['terminal_state']!r} terminal={summary['terminal']}"
    )
    # And not reported as completed (the dangerous false-positive).
    assert summary["terminal_state"] != "completed", (
        f"crash-truncated run misreported as completed: {summary['terminal_state']!r}"
    )


def test_crash_truncated_oversized_done_retains_preceding_event(tmp_path):
    """Regression (reviewer round 4): rejecting the crash-truncated boundary
    record also dropped the preceding VALID event, so event_count=0 / last_seq=0
    and stale_interrupted_event returned None → no apperror recovery emitted.
    Master reports running/last_seq=1 and emits the recovery signal. The fix
    retains the last complete event before the rejected boundary record."""
    from api.run_journal import _SESSION_REPLAY_MAX_BYTES, _run_path

    writer = RunJournalWriter("session_1", "run_trunc_preceding", session_dir=tmp_path)
    writer.append_sse_event("token", {"text": "hi"})  # seq=1, the preceding valid event
    path = _run_path(writer.session_id, writer.run_id, session_dir=writer.session_dir)
    partial = (
        '{"version":1,"event_id":"run_trunc_preceding:2","seq":2,'
        '"event":"done","type":"done","terminal":true,'
        '"terminal_state":"completed","payload":{"text":"'
        + "X" * (_SESSION_REPLAY_MAX_BYTES + 50_000)
    )  # no closing brace/newline — crash mid-write
    with open(path, "a", encoding="utf-8") as f:
        f.write(partial)

    summary = latest_run_summary(writer.session_id, writer.run_id, session_dir=writer.session_dir)
    # The preceding token (seq=1) must survive — not falsely completed.
    assert summary["terminal"] is False
    assert summary["terminal_state"] != "completed"
    assert summary["last_seq"] == 1, (
        f"preceding event lost: last_seq={summary['last_seq']} (expected 1)"
    )
    assert summary["event_count"] >= 1, (
        f"preceding event lost: event_count={summary['event_count']}"
    )


def test_oversized_done_closing_brace_without_newline_stays_nonterminal(tmp_path):
    """Regression (reviewer round 4): an oversized `done` ending at EOF with a
    closing `}` but NO JSONL newline terminator is a crash-truncated write
    (interrupted after the brace, before the \\n), NOT a completed record. The
    completeness scanner used to treat EOF-right-after-`}` as complete; it must
    require an actual \\n terminator."""
    from api.run_journal import _SESSION_REPLAY_MAX_BYTES, _run_path

    writer = RunJournalWriter("session_1", "run_brace_no_newline", session_dir=tmp_path)
    writer.append_sse_event("token", {"text": "hi"})
    path = _run_path(writer.session_id, writer.run_id, session_dir=writer.session_dir)
    # A record that closes with }} but has NO trailing newline.
    huge = (
        '{"version":1,"event_id":"run_brace_no_newline:2","seq":2,'
        '"event":"done","type":"done","terminal":true,'
        '"terminal_state":"completed","payload":{"text":"'
        + "X" * (_SESSION_REPLAY_MAX_BYTES + 50_000)
        + '"}}'
    )  # closes with }} but no \n — crash after the brace
    with open(path, "a", encoding="utf-8") as f:
        f.write(huge)

    summary = latest_run_summary(writer.session_id, writer.run_id, session_dir=writer.session_dir)
    assert summary["terminal"] is False, (
        f"a closing-brace-at-EOF-no-newline record must not be accepted as terminal; "
        f"got terminal_state={summary['terminal_state']!r}"
    )
    assert summary["terminal_state"] != "completed", (
        f"crash-truncated run (brace without newline) misreported as completed: "
        f"{summary['terminal_state']!r}"
    )


def test_bare_carriage_return_terminator_rejected(tmp_path):
    """Regression (reviewer round 5): a bare \\r (not \\r\\n) as the JSONL
    terminator was accepted as a complete record — but a write interrupted after
    a \\r (before the \\n of a CRLF pair) is crash-truncated. Only \\n or the
    complete \\r\\n pair is a valid terminator."""
    from api.run_journal import _SESSION_REPLAY_MAX_BYTES, _run_path

    writer = RunJournalWriter("session_1", "run_bare_cr", session_dir=tmp_path)
    writer.append_sse_event("token", {"text": "hi"})
    path = _run_path(writer.session_id, writer.run_id, session_dir=writer.session_dir)
    # Append an oversized done terminated with bare \r (not \r\n).
    huge = (
        '{"version":1,"event_id":"run_bare_cr:2","seq":2,'
        '"event":"done","type":"done","terminal":true,'
        '"terminal_state":"completed","payload":{"text":"'
        + "X" * (_SESSION_REPLAY_MAX_BYTES + 50_000)
        + '"}}\r'
    )
    with open(path, "a", encoding="utf-8") as f:
        f.write(huge)
    summary = latest_run_summary(writer.session_id, writer.run_id, session_dir=writer.session_dir)
    assert summary["terminal"] is False, (
        f"bare-\\r terminator must not be accepted as terminal; "
        f"got terminal_state={summary['terminal_state']!r}"
    )


def test_preceding_event_recovery_is_bounded(tmp_path):
    """Regression (reviewer round 5): _read_last_complete_line_before used to
    materialize the ENTIRE preceding record (verified at 4.27 MB), defeating the
    memory-bound goal. The fix uses the bounded prefix extractor when the
    preceding record is oversized, so the recovery path stays within the cap."""
    from api.run_journal import _SESSION_REPLAY_MAX_BYTES, _run_path, _read_last_complete_line_before

    writer = RunJournalWriter("session_1", "run_bounded_preceding", session_dir=tmp_path)
    # seq=1: an oversized but COMPLETE done (with newline)
    huge = {"text": "X" * (_SESSION_REPLAY_MAX_BYTES + 100_000)}
    writer.append_sse_event("done", {"session": {"session_id": "session_1"}, **huge})
    path = _run_path(writer.session_id, writer.run_id, session_dir=writer.session_dir)
    # seq=2: crash-truncated done (no close/newline)
    partial = (
        '{"version":1,"event_id":"run_bounded_preceding:2","seq":2,'
        '"event":"done","type":"done","terminal":true,'
        '"terminal_state":"completed","payload":{"text":"'
        + "X" * (_SESSION_REPLAY_MAX_BYTES + 50_000)
    )
    with open(path, "a", encoding="utf-8") as f:
        f.write(partial)
    with path.open("rb") as fh:
        size_pinned = os.fstat(fh.fileno()).st_size
        seek = size_pinned - min(size_pinned, _SESSION_REPLAY_MAX_BYTES)
        record_start = __import__("api.run_journal", fromlist=["_find_record_start_before"])._find_record_start_before(fh, size_pinned, seek)
        result = _read_last_complete_line_before(fh, size_pinned, record_start)
    assert result is not None
    # The preceding record's summary was extracted via bounded prefix, not materialized.
    assert result.get("_summary_extracted_from_oversized_record") is True, (
        "preceding oversized record was materialized instead of bounded-prefix extracted"
    )
    assert result["payload"] == {}, "payload should be empty (not materialized)"


def test_ordinary_size_done_bare_cr_rejected_both_readers(tmp_path):
    """Regression (reviewer round 6): an ORDINARY-SIZE done ending in bare \\r
    (not \\r\\n) was accepted as terminal by BOTH readers — splitlines() accepts
    bare \\r, and read_text() converts \\r to \\n via universal-newline. A
    crash-truncated record must leave the run running in BOTH readers."""
    import json as _json
    from api.run_journal import _read_jsonl, _read_jsonl_tail

    rec1 = _json.dumps({"seq": 1, "event": "token", "payload": {}})
    rec2 = _json.dumps({"seq": 2, "event": "done", "terminal": True,
                        "terminal_state": "completed", "payload": {}})
    p = tmp_path / "bare_cr.jsonl"
    p.write_bytes((rec1 + "\n" + rec2 + "\r").encode("utf-8"))
    ev_full, _ = _read_jsonl(p)
    ev_tail, _ = _read_jsonl_tail(p, max_bytes=10000, max_rows=100)
    # Neither reader should accept the bare-\r-terminated done.
    assert not any(e.get("event") == "done" for e in ev_full), "FULL reader accepts bare \\r"
    assert not any(e.get("event") == "done" for e in ev_tail), "TAIL reader accepts bare \\r"
    # Both readers agree: only the token (seq 1) survives.
    full_seqs = sorted(e["seq"] for e in ev_full)
    tail_seqs = sorted(e["seq"] for e in ev_tail)
    assert full_seqs == [1], f"FULL: {full_seqs}"
    assert tail_seqs == [1], f"TAIL: {tail_seqs}"


def test_ordinary_size_done_eof_no_newline_rejected_both_readers(tmp_path):
    """Regression (reviewer round 6): an ordinary-size done at EOF with no
    trailing \\n was silently accepted by splitlines(). Both readers must reject
    it — the record is unterminated / potentially crash-truncated."""
    import json as _json
    from api.run_journal import _read_jsonl, _read_jsonl_tail

    rec1 = _json.dumps({"seq": 1, "event": "token", "payload": {}})
    rec2 = _json.dumps({"seq": 2, "event": "done", "terminal": True,
                        "terminal_state": "completed", "payload": {}})
    p = tmp_path / "eof_no_nl.jsonl"
    p.write_bytes((rec1 + "\n" + rec2).encode("utf-8"))
    ev_full, _ = _read_jsonl(p)
    ev_tail, _ = _read_jsonl_tail(p, max_bytes=10000, max_rows=100)
    assert not any(e.get("event") == "done" for e in ev_full), "FULL accepts EOF-no-newline"
    assert not any(e.get("event") == "done" for e in ev_tail), "TAIL accepts EOF-no-newline"









# ── Greptile P1 (2026-07-20): TOCTOU — file deleted between stat() and open() ─
# The backward-scan helpers (_find_record_start_before, _rfind_byte_before,
# _record_is_structurally_complete) wrap stat() in try/except but used to leave
# the subsequent path.open() unguarded. A cleanup job racing a status poll could
# delete the journal in that window; the FileNotFoundError would escape to the
# HTTP handler → 500. Each helper must return its safe fallback instead.


def test_find_record_start_before_returns_fallback_if_handle_closed(tmp_path, monkeypatch):
    """_find_record_start_before: fh read/seek raises — must
    return 0 (not let FileNotFoundError escape to the HTTP handler)."""
    import json as _json
    import os
    from api import run_journal

    p = tmp_path / "race.jsonl"
    p.write_bytes((_json.dumps({"seq": 1, "event": "done"}) + "\n").encode("utf-8"))

    class RacingFileHandle:
        def __init__(self, underlying):
            self._underlying = underlying
            self._original_read = underlying.read
            self._original_seek = underlying.seek
            self._should_race = True

        def read(self, *a, **kw):
            if self._should_race:
                raise FileNotFoundError("Simulated TOCTOU: file deleted after open")
            return self._underlying.read(*a, **kw)

        def seek(self, *a, **kw):
            if self._should_race:
                raise FileNotFoundError("Simulated TOCTOU: file deleted after open")
            return self._underlying.seek(*a, **kw)

        def fileno(self):
            return self._underlying.fileno()

    with p.open("rb") as raw_fh:
        size = os.fstat(raw_fh.fileno()).st_size
        racing_fh = RacingFileHandle(raw_fh)
        result = run_journal._find_record_start_before(racing_fh, size, 50)

    assert result == 0


def test_rfind_byte_before_returns_fallback_if_handle_closed(tmp_path, monkeypatch):
    """_rfind_byte_before: fh read/seek raises FileNotFoundError — must return None."""
    import json as _json
    from api import run_journal

    p = tmp_path / "race.jsonl"
    p.write_bytes((_json.dumps({"seq": 1}) + "\n").encode("utf-8"))

    class RacingFileHandle:
        def __init__(self, underlying):
            self._underlying = underlying

        def read(self, *a, **kw):
            raise FileNotFoundError("Simulated TOCTOU: file deleted after open")

        def seek(self, *a, **kw):
            raise FileNotFoundError("Simulated TOCTOU: file deleted after open")

        def fileno(self):
            return self._underlying.fileno()

    with p.open("rb") as raw_fh:
        racing_fh = RacingFileHandle(raw_fh)
        result = run_journal._rfind_byte_before(racing_fh, b"\n", 50)

    assert result is None


def test_record_is_structurally_complete_returns_fallback_if_handle_error(
    tmp_path, monkeypatch
):
    """_record_is_structurally_complete: fh read/seek raises — must
    return False (not raise)."""
    import json as _json
    import os
    from api import run_journal

    p = tmp_path / "race.jsonl"
    p.write_bytes((_json.dumps({"seq": 1, "event": "done"}) + "\n").encode("utf-8"))

    class RacingFileHandle:
        def __init__(self, underlying):
            self._underlying = underlying

        def read(self, *a, **kw):
            raise FileNotFoundError("Simulated TOCTOU: file deleted after open")

        def seek(self, *a, **kw):
            raise FileNotFoundError("Simulated TOCTOU: file deleted after open")

        def fileno(self):
            return self._underlying.fileno()

    with p.open("rb") as raw_fh:
        size = os.fstat(raw_fh.fileno()).st_size
        racing_fh = RacingFileHandle(raw_fh)
        result = run_journal._record_is_structurally_complete(racing_fh, size, 0)

    assert result is False


def test_read_jsonl_tail_handles_file_deleted_during_boundary_scan(tmp_path, monkeypatch):
    """End-to-end TOCTOU: a journal large enough to trigger the boundary-scan
    path; the boundary helper raises FileNotFoundError mid-scan. latest_run_summary
    must return a safe summary (not propagate → 500 on the poll endpoint)."""
    from api import run_journal
    from api.run_journal import append_run_event

    # Build a journal whose tail window straddles a record (so the boundary
    # helpers fire). Substantial payloads so the tail cap excludes the first.
    big = "x" * 50000
    append_run_event("s1", "r1", "done", {"session": {}, "big": big}, session_dir=tmp_path)
    append_run_event("s1", "r1", "done",
                     {"terminal_state": "completed", "big": big},
                     session_dir=tmp_path)

    path = tmp_path / "_run_journal" / "s1" / "r1.jsonl"
    assert path.exists(), (
        f"journal not written where expected: {path} (glob: "
        f"{list(tmp_path.rglob('*.jsonl'))})"
    )

    # Patch the boundary helper to raise (simulating delete-during-scan) —
    # latest_run_summary must catch via the helper's own try/except and return
    # a safe summary, not propagate.
    original = run_journal._find_record_start_before

    def raising_find(p, seek_pos):
        if p == path:
            raise FileNotFoundError(str(path))
        return original(p, seek_pos)

    monkeypatch.setattr(run_journal, "_find_record_start_before", raising_find)
    summary = run_journal.latest_run_summary("s1", "r1", session_dir=tmp_path)
    # Must return a dict (safe summary), not raise.
    assert isinstance(summary, dict)


def test_blank_line_before_oversized_partial_done_recovers_preceding_event(tmp_path, monkeypatch):
    """Regression (reviewer round 7): a blank line between a valid event and a
    crash-truncated oversized boundary record defeated recovery — the single
    preceding-line scan found only the blank line, json.loads("") failed, and
    the function returned None instead of continuing back to the valid event.
    The fix loops backward across blank/malformed/non-dict lines until finding
    a valid event, so a shape like ``token\\n\\n<oversized partial done>`` recovers
    the token event (seq=1) and emits the recovery apperror."""
    import json as _json
    from api.run_journal import (
        _SESSION_REPLAY_MAX_BYTES,
        _run_path,
        _read_jsonl,
        _summary_from_events,
        stale_interrupted_event,
    )

    session_id = "session_1"
    run_id = "run_blank_before_oversized"
    writer = RunJournalWriter(session_id, run_id, session_dir=tmp_path)
    writer.append_sse_event("token", {"text": "hi"})  # seq=1, valid event

    path = _run_path(session_id, run_id, session_dir=tmp_path)
    cap = _SESSION_REPLAY_MAX_BYTES

    # Append a BLANK LINE, then an oversized crash-truncated done.
    # Shape: token\n\n<oversized partial done with no closing brace/newline>
    token_line = _json.dumps(
        {"version": 1, "event_id": f"{run_id}:1", "seq": 1,
         "event": "token", "type": "token", "terminal": False,
         "payload": {"text": "hi"}}
    )
    oversized_partial = (
        '{"version":1,"event_id":"' + run_id + ':2","seq":2,'
        '"event":"done","type":"done","terminal":true,'
        '"terminal_state":"completed","payload":{"t":"'
        + "X" * (cap + 1000)
    )  # no closing brace/newline — crash mid-write

    with path.open("wb") as fh:
        fh.write((token_line + "\n").encode("utf-8"))
        fh.write(b"\n")  # BLANK LINE (the gap)
        fh.write(oversized_partial.encode("utf-8"))

    # Both tail readers must recover the token event (seq=1), not return
    # last_seq=0 (the bug: they hit the blank line and stopped).
    tail_summary = latest_run_summary(session_id, run_id, session_dir=tmp_path)
    find_summary = find_run_summary(run_id, session_dir=tmp_path)

    # Authoritative full read (the baseline: must match this).
    full_events, _ = _read_jsonl(path)
    authoritative = _summary_from_events(session_id, run_id, full_events)

    # Both tail readers must agree with the full reader.
    assert tail_summary["last_seq"] == 1, (
        f"latest_run_summary returned last_seq={tail_summary['last_seq']} "
        f"(expected 1) — the blank line defeated recovery"
    )
    assert tail_summary["event_count"] == 1, (
        f"latest_run_summary returned event_count={tail_summary['event_count']} "
        f"(expected 1) — the blank line defeated recovery"
    )
    assert tail_summary["terminal"] is False, (
        f"latest_run_summary marked terminal={tail_summary['terminal']} "
        f"(expected False) — the crash-truncated boundary must not fabricate terminal"
    )

    assert find_summary is not None, (
        "find_run_summary returned None (expected dict with last_seq=1)"
    )
    assert find_summary["last_seq"] == 1, (
        f"find_run_summary returned last_seq={find_summary['last_seq']} "
        f"(expected 1) — the blank line defeated recovery"
    )
    assert find_summary["event_count"] == 1, (
        f"find_run_summary returned event_count={find_summary['event_count']} "
        f"(expected 1) — the blank line defeated recovery"
    )
    assert find_summary["terminal"] is False, (
        f"find_run_summary marked terminal={find_summary['terminal']} "
        f"(expected False)"
    )

    # Both tail readers must MATCH the authoritative full reader.
    assert tail_summary["last_seq"] == authoritative["last_seq"], (
        f"tail last_seq={tail_summary['last_seq']} but authoritative="
        f"{authoritative['last_seq']}"
    )
    assert find_summary["last_seq"] == authoritative["last_seq"], (
        f"find last_seq={find_summary['last_seq']} but authoritative="
        f"{authoritative['last_seq']}"
    )

    # The recovery apperror must be emitted (stale_interrupted_event returns it).
    monkeypatch.setattr("api.run_journal._default_session_dir", lambda: tmp_path)
    recovery = stale_interrupted_event(session_id, run_id)
    assert recovery is not None, (
        "stale_interrupted_event returned None (expected apperror recovery event)"
    )
    assert recovery["event"] == "apperror", (
        f"recovery event type={recovery['event']} (expected apperror)"
    )


def test_tail_read_uses_single_generation_under_delete_recreate(tmp_path, monkeypatch):
    """Regression (reviewer round 8): _read_jsonl_tail must open the journal ONCE
    and pin the size via os.fstat so all recovery helpers read from ONE inode
    generation. A delete-and-recreate between stages must never mix rows from
    different generations — e.g. tail rows from inode A with boundary/predecessor
    rows from inode B, producing impossible sequences like ``[100, 2]``.

    This is a pure generation-isolation proof: the 1st ``Path.open("rb")`` of the
    journal returns a real handle to inode-A content (token seq=1 + an oversized
    crash-truncated done); every SUBSEQUENT open returns a BytesIO of inode-B
    content (a completed run: done seq=100, terminal). With the single-open fix,
    only inode-A is ever read → the tail reader reports seq=1 / running. With a
    reopen bug, inode-B leaks in → seq=100 / terminal / mixed. The test asserts
    the result is purely inode-A and records how many times the journal was
    opened (must be exactly 1)."""
    import io as _io
    from pathlib import Path
    from api.run_journal import _SESSION_REPLAY_MAX_BYTES, _read_jsonl_tail, _run_path

    cap = _SESSION_REPLAY_MAX_BYTES
    session_id = "session_single_gen"
    run_id = "run_delete_recreate_race"
    path = _run_path(session_id, run_id, session_dir=tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)

    # inode-A content: token seq=1, then a blank line, then an oversized
    # crash-truncated done (no closing brace/newline). This forces the recovery
    # helpers (boundary scan, structural check, predecessor scan) to run, which
    # on the buggy multi-open code would each reopen the pathname.
    token_a = {"version": 1, "event_id": f"{run_id}:1", "seq": 1,
               "event": "token", "type": "token", "terminal": False,
               "payload": {"text": "inode-A-token"}}
    oversized_partial = (
        '{"version":1,"event_id":"' + run_id + ':2","seq":2,'
        '"event":"done","type":"done","terminal":true,'
        '"terminal_state":"completed","payload":{"t":"' + "X" * (cap + 1000)
    )  # no closing brace/newline — crash mid-write
    inode_a_bytes = (json.dumps(token_a) + "\n").encode("utf-8") + b"\n" + oversized_partial.encode("utf-8")
    path.write_bytes(inode_a_bytes)

    # inode-B content (what a recreated file might hold): a COMPLETED run with a
    # much higher seq, so any leak is unambiguous.
    done_b = {"version": 1, "event_id": f"{run_id}:100", "seq": 100,
              "event": "done", "type": "done", "terminal": True,
              "terminal_state": "completed", "payload": {"text": "inode-B"}}
    inode_b_bytes = (json.dumps(done_b) + "\n").encode("utf-8")

    open_count = {"n": 0}
    real_open = Path.open

    def generation_pinned_open(self, *args, **kwargs):
        if self == path and args and args[0] == "rb":
            open_count["n"] += 1
            if open_count["n"] == 1:
                # First (and with the fix, ONLY) open: real inode-A handle.
                return real_open(self, *args, **kwargs)
            # Any subsequent open of the journal returns inode-B content. With the
            # single-open fix this branch never executes; with a reopen bug it
            # leaks inode-B rows into the result.
            return _io.BytesIO(inode_b_bytes)
        return real_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", generation_pinned_open)

    events, _malformed = _read_jsonl_tail(path, max_bytes=cap, max_rows=4096)
    seqs = [e.get("seq") for e in events if isinstance(e, dict) and "seq" in e]

    # The journal must be opened exactly ONCE (single-generation contract).
    assert open_count["n"] == 1, (
        f"journal must be opened exactly once (single inode generation); "
        f"opened {open_count['n']} times — a reopen mixes generations"
    )
    # The result must be purely inode-A: the token (seq=1), running (no terminal).
    # inode-B (seq=100, terminal) must NEVER appear.
    assert 100 not in seqs, (
        f"inode-B row leaked into inode-A result (mixed generation): seqs={seqs}. "
        f"A reopen read the recreated file's completed-done as if it were the original."
    )
    assert any(s == 1 for s in seqs), (
        f"inode-A token (seq=1) must be recovered; got seqs={seqs}"
    )
    for e in events:
        if isinstance(e, dict):
            assert not e.get("terminal"), (
                f"a terminal event leaked from inode-B into the result: {e} "
                f"— the oversized boundary was crash-truncated (inode-A, non-terminal)"
            )


def test_invalid_predecessor_rows_skipped_until_valid_event(tmp_path, monkeypatch):
    """Regression (reviewer round 8): the backward predecessor scan must skip
    blank, malformed-JSON, JSON-scalar, JSON-list, and consecutive mixed invalid
    rows until it finds the preceding valid event dict. This is the reader-level
    coverage the round-8 review asked for beyond the blank-line separator case.

    Journal shape (a valid token, then a run of mixed invalid rows, then an
    oversized crash-truncated done that forces the recovery path):
        token(seq=1)
        {not-json}        <- malformed
        42                <- JSON scalar (non-dict)
        [1,2]             <- JSON list (non-dict)
                            <- blank line
        <oversized partial done, no }/\n>

    BOTH tail readers must recover the token (seq=1), report running, and emit
    the recovery apperror. None of the invalid rows may be accepted as the
    recovered event."""
    import json as _json
    from api.run_journal import (
        _SESSION_REPLAY_MAX_BYTES,
        _run_path,
        _read_jsonl,
        _summary_from_events,
        stale_interrupted_event,
    )

    session_id = "session_invalid_pred"
    run_id = "run_mixed_invalid"
    path = _run_path(session_id, run_id, session_dir=tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    cap = _SESSION_REPLAY_MAX_BYTES

    token_line = _json.dumps(
        {"version": 1, "event_id": f"{run_id}:1", "seq": 1,
         "event": "token", "type": "token", "terminal": False,
         "payload": {"text": "the-valid-token"}}
    )
    oversized_partial = (
        '{"version":1,"event_id":"' + run_id + ':2","seq":2,'
        '"event":"done","type":"done","terminal":true,'
        '"terminal_state":"completed","payload":{"t":"' + "X" * (cap + 1000)
    )
    # token, then a run of mixed invalid rows, then the oversized partial.
    with path.open("wb") as fh:
        fh.write((token_line + "\n").encode("utf-8"))
        fh.write(b"{not-json}\n")     # malformed JSON
        fh.write(b"42\n")             # JSON scalar (non-dict)
        fh.write(b"[1,2]\n")          # JSON list (non-dict)
        fh.write(b"\n")               # blank line
        fh.write(oversized_partial.encode("utf-8"))  # crash-truncated, no newline

    tail_summary = latest_run_summary(session_id, run_id, session_dir=tmp_path)
    find_summary = find_run_summary(run_id, session_dir=tmp_path)
    full_events, _ = _read_jsonl(path)
    authoritative = _summary_from_events(session_id, run_id, full_events)

    # Both tail readers recover the valid token (seq=1), agreeing with the full reader.
    assert tail_summary["last_seq"] == 1, (
        f"latest_run_summary last_seq={tail_summary['last_seq']} (expected 1) "
        f"— an invalid predecessor row was accepted instead of skipped"
    )
    assert tail_summary["event_count"] == 1
    assert tail_summary["last_seq"] == authoritative["last_seq"]
    assert find_summary is not None
    assert find_summary["last_seq"] == 1
    assert find_summary["last_seq"] == authoritative["last_seq"]
    # The oversized boundary was crash-truncated, so the run stays non-terminal.
    for e in (tail_summary, find_summary):
        assert not e.get("terminal"), "the oversized boundary was truncated (non-terminal)"

    monkeypatch.setattr("api.run_journal._default_session_dir", lambda: tmp_path)
    recovery = stale_interrupted_event(session_id, run_id)
    assert recovery is not None, "stale recovery must emit apperror (run is running)"
    assert recovery["event"] == "apperror"


def test_oversized_malformed_predecessor_not_accepted_via_fabricated_prefix(tmp_path):
    """Regression (reviewer round 8, point 5): an oversized predecessor that is
    structurally INCOMPLETE (crash-truncated mid-payload — brace depth never
    returns to 0) must NOT be accepted via _extract_boundary_record_summary's
    fabricated prefix as if it were a valid terminal event. The structural-
    completeness gate inside the backward scan must reject it and continue to
    the preceding complete event.

    Journal shape:
        token(seq=1)
        <oversized malformed done seq=2: has a newline terminator so it counts
         as a complete LINE, but the JSON itself is structurally incomplete
         (truncated mid-payload value), so _record_is_structurally_complete ->
         False while _extract_boundary_record_summary fabricates a terminal
         prefix from its head>
        <oversized partial done seq=3: the crash-truncated boundary, no newline>

    The scan for the predecessor of seq=3 hits the oversized seq=2 row first.
    Without the gate it would accept seq=2's fabricated terminal prefix; with
    the gate it skips seq=2 and recovers token(seq=1)."""
    import os
    from api.run_journal import (
        _SESSION_REPLAY_MAX_BYTES,
        _run_path,
        _read_last_complete_line_before,
    )

    session_id = "session_oversized_malformed_pred"
    run_id = "run_oversized_malformed_pred"
    path = _run_path(session_id, run_id, session_dir=tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    cap = _SESSION_REPLAY_MAX_BYTES

    token_line = '{"seq":1,"event":"token","payload":{"t":"valid"}}\n'
    # Oversized malformed predecessor: newline-terminated (a complete line) but
    # JSON structurally incomplete (no closing brace — truncated mid-value).
    # Its head still fabricates a terminal 'done/seq=2' prefix.
    oversized_malformed_pred = (
        '{"version":1,"seq":2,"event":"done","terminal":true,'
        '"terminal_state":"completed","payload":{"t":"' + "X" * (cap + 1000)
        + "\n"  # newline terminator makes it a complete LINE, but JSON is broken
    )
    # Final crash-truncated boundary (no newline) — forces the recovery scan.
    final_partial = (
        '{"seq":3,"event":"done","terminal":true,"payload":{"t":"'
        + "Y" * (cap + 1000)
    )
    token_bytes = token_line.encode("utf-8")
    pred_bytes = oversized_malformed_pred.encode("utf-8")
    with path.open("wb") as fh:
        fh.write(token_bytes)
        fh.write(pred_bytes)
        fh.write(final_partial.encode("utf-8"))

    final_start = len(token_bytes) + len(pred_bytes)
    with path.open("rb") as fh:
        size = os.fstat(fh.fileno()).st_size
        result = _read_last_complete_line_before(fh, size, final_start)

    # The oversized malformed predecessor (seq=2) must be SKIPPED via the gate;
    # the valid token (seq=1) is recovered — never the fabricated terminal done.
    assert result is not None, "the valid token (seq=1) must be recovered"
    assert result.get("seq") == 1, (
        f"expected seq=1 (the gate skipped the oversized malformed row); got "
        f"seq={result.get('seq')} — the fabricated terminal prefix was accepted "
        f"as if the structurally-incomplete row were valid"
    )
    assert not result.get("terminal"), (
        "a non-terminal token must be recovered, not a fabricated terminal done"
    )


def test_backward_predecessor_scan_budget_bounds_the_scan(tmp_path):
    """Regression (reviewer round 8, point 4): the backward predecessor scan
    must have an explicit aggregate row/byte budget so it cannot walk to byte
    zero across an arbitrarily long invalid-row streak. With a budget smaller
    than the streak, the scan must STOP (return None) rather than scanning the
    whole streak. With a budget large enough, it recovers the valid event."""
    import os
    from api.run_journal import (
        _run_path,
        _read_last_complete_line_before,
    )

    session_id = "session_budget"
    run_id = "run_budget"
    path = _run_path(session_id, run_id, session_dir=tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)

    # A valid token far back, then a long run of malformed rows (each ~20 bytes).
    token = '{"seq":1,"event":"token","payload":{"t":"v"}}\n'
    malformed_rows = b"{bad}\n" * 500  # 500 malformed rows (~6 bytes each)
    with path.open("wb") as fh:
        fh.write(token.encode("utf-8"))
        fh.write(malformed_rows)

    with path.open("rb") as fh:
        size = os.fstat(fh.fileno()).st_size
        # Tiny budget: cannot reach the token past 500 malformed rows.
        tight = _read_last_complete_line_before(fh, size, size, budget=50)
        # Large budget: reaches the token.
        generous = _read_last_complete_line_before(fh, size, size, budget=10 * 1024 * 1024)

    # Tight budget must STOP before reaching the token (budget gate fires).
    assert tight is None, (
        f"a 50-byte budget must not scan 500 malformed rows to the token; got {tight}"
    )
    # Generous budget must recover the token (seq=1).
    assert generous is not None and generous.get("seq") == 1, (
        f"a large budget must recover the valid token (seq=1); got {generous}"
    )
