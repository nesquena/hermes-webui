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
    record_start = _find_record_start_before(path, seek_pos)
    summary = _extract_boundary_record_summary(path, record_start)
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
    size = path.stat().st_size
    seek = size - min(size, _SESSION_REPLAY_MAX_BYTES)
    record_start = __import__("api.run_journal", fromlist=["_find_record_start_before"])._find_record_start_before(path, seek)
    result = _read_last_complete_line_before(path, record_start)
    assert result is not None
    # The preceding record's summary was extracted via bounded prefix, not materialized.
    assert result.get("_summary_extracted_from_oversized_record") is True, (
        "preceding oversized record was materialized instead of bounded-prefix extracted"
    )
    assert result["payload"] == {}, "payload should be empty (not materialized)"






