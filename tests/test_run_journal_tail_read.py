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
    """Regression (reviewer round 5, updated r9): the preceding oversized record
    must never be MATERIALIZED. Under r9 fail-closed (#6139 r9 item 2) an
    oversized predecessor is SKIPPED entirely — its full-record JSON validity
    cannot be proven without materializing the payload, so its fabricated prefix
    is never trusted. The scan continues to the preceding normal-sized valid
    event (recovered), proving the multi-MB payload was never parsed into a
    Python object. An explicit budget large enough to scan PAST the oversized
    record is passed so the skip is fail-closed (not a budget stop)."""
    import os
    from api.run_journal import _SESSION_REPLAY_MAX_BYTES, _run_path, _read_last_complete_line_before

    writer = RunJournalWriter("session_1", "run_bounded_preceding", session_dir=tmp_path)
    # seq=1: a normal-sized token (the recoverable preceding event).
    writer.append_sse_event("token", {"text": "ok"})
    # seq=2: an oversized but COMPLETE done (with newline) — must be SKIPPED, not materialized.
    huge = {"text": "X" * (_SESSION_REPLAY_MAX_BYTES + 100_000)}
    writer.append_sse_event("done", {"session": {"session_id": "session_1"}, **huge})
    path = _run_path(writer.session_id, writer.run_id, session_dir=writer.session_dir)
    # seq=3: crash-truncated done (no close/newline) — the boundary record.
    partial = (
        '{"version":1,"event_id":"run_bounded_preceding:3","seq":3,'
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
        # Budget large enough to scan PAST the oversized done (seq=2) to the token,
        # so the skip is fail-closed rather than a budget stop.
        result = _read_last_complete_line_before(
            fh, size_pinned, record_start, budget=_SESSION_REPLAY_MAX_BYTES + 200_000
        )
    # The oversized done (seq=2) is skipped (fail-closed); the token (seq=1) is recovered.
    assert result is not None, "the normal-sized token (seq=1) must be recovered"
    assert result.get("seq") == 1, (
        f"expected seq=1 (the oversized done was skipped fail-closed); got seq={result.get('seq')}"
    )
    assert result.get("event") == "token", "the recovered event is the token, not the oversized done"
    # The oversized payload was never materialized: no fabricated-summary marker,
    # and the token carries its real (small) payload, not the empty fabricated one.
    assert result.get("_summary_extracted_from_oversized_record") is None, (
        "the oversized record's prefix was fabricated instead of being skipped (fail-closed)"
    )
    assert result.get("payload") == {"text": "ok"}, (
        "the recovered token carries its real payload — the oversized done was skipped, not fabricated"
    )


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
    """End-to-end TOCTOU (reviewer round 9/10, item 3): a journal BIGGER than the
    tail cap so the boundary-scan path fires, and a read failure INSIDE the
    boundary scan (not the forward tail read) returns the safe fallback, never
    propagates to the HTTP handler.

    Discriminating (r10): the OLD test's ``_FailingHandle(fail_on_read=1)``
    raised on read #1 — which is the forward tail read populating ``raw`` — so
    ``_find_record_start_before`` and the whole boundary scan NEVER ran. The test
    passed whether or not the boundary helpers were TOCTOU-safe. This version
    (a) builds a >cap fixture, (b) asserts the boundary path recovers the
    straddling record under a normal read (non-vacuity), (c) patches
    ``_find_record_start_before`` to unlink the pinned file and raise OSError
    INSIDE the reached boundary call — so the descriptor that was opened by
    ``_read_jsonl_tail`` is the one that sees the failure, and (d) asserts the
    boundary hook actually ran and ``Path.open`` was called exactly once (the
    pinned descriptor is reused, not reopened)."""
    from pathlib import Path
    from api import run_journal
    from api.run_journal import _SESSION_REPLAY_MAX_BYTES, _read_jsonl_tail

    cap = _SESSION_REPLAY_MAX_BYTES
    token_line = '{"seq":1,"event":"token","payload":{"t":"ok"}}\n'
    oversized_done = (
        '{"seq":2,"event":"done","terminal":true,'
        '"terminal_state":"completed","payload":{"t":"'
        + "X" * (cap + 1000)
        + '"}}\n'
    )
    path = tmp_path / "_run_journal" / "s1" / "r1.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as fh:
        fh.write(token_line.encode("utf-8"))
        fh.write(oversized_done.encode("utf-8"))
    assert path.stat().st_size > cap, "fixture must exceed the cap so the boundary scan fires"

    # --- Part A: NON-VACUITY. A normal read recovers the straddling done's
    # summary (seq=2) — proving the boundary helpers actually fire.
    events_normal, _ = _read_jsonl_tail(path, max_bytes=cap, max_rows=None)
    seqs = {e.get("seq") for e in events_normal if isinstance(e, dict)}
    assert 2 in seqs, (
        f"the oversized done (seq=2) straddling summary must be recovered by the "
        f"boundary scan — if not, the fixture is vacuous; got seqs={seqs}"
    )

    # --- Part B: MUTATION-KILLING CONTRACT. Hook _find_record_start_before (the
    # FIRST boundary helper after the forward tail read succeeds) so a failure
    # INSIDE the reached boundary call returns the safe fallback, never raises,
    # and the pinned descriptor is reused (Path.open called exactly once).
    open_calls = {"n": 0}
    boundary_calls = {"n": 0}
    real_open = Path.open

    def patched_open(self, *args, **kwargs):
        open_calls["n"] += 1
        return real_open(self, *args, **kwargs)

    def patched_find(fh, size, seek_pos, *, budget=None):
        boundary_calls["n"] += 1
        # Unlink the pinned file from under the open descriptor and raise — the
        # realistic single-open TOCTOU (fd invalidated mid-recovery, inside the
        # boundary scan that the old test never reached).
        try:
            path.unlink()
        except OSError:
            pass
        raise OSError("simulated fd invalidation inside the boundary scan")

    monkeypatch.setattr(Path, "open", patched_open)
    monkeypatch.setattr(run_journal, "_find_record_start_before", patched_find)

    events, malformed = _read_jsonl_tail(path, max_bytes=cap, max_rows=None)
    assert isinstance(events, list) and isinstance(malformed, list), (
        f"_read_jsonl_tail must return a (list, list) safe fallback; got "
        f"({type(events).__name__}, {type(malformed).__name__})"
    )
    assert boundary_calls["n"] >= 1, (
        "the boundary helper _find_record_start_before was never reached — the "
        "test is non-discriminating (the failure fired on the forward tail read)"
    )
    assert open_calls["n"] == 1, (
        f"_read_jsonl_tail reopened the journal ({open_calls['n']} opens) instead "
        f"of reusing the pinned descriptor — single-open contract broken"
    )

    # The same contract holds through the summary reader's path (no propagation
    # to the HTTP handler). Re-create the file first since Part B unlinked it.
    with path.open("wb") as fh:
        fh.write(token_line.encode("utf-8"))
        fh.write(oversized_done.encode("utf-8"))
    open_calls["n"] = 0
    boundary_calls["n"] = 0
    summary = run_journal.latest_run_summary("s1", "r1", session_dir=tmp_path)
    assert isinstance(summary, dict), "latest_run_summary must return a dict, not raise"
    assert boundary_calls["n"] >= 1, "the boundary helper must be reached via latest_run_summary too"
    assert open_calls["n"] == 1, "latest_run_summary must reuse the pinned descriptor"


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
    """Regression (reviewer round 8, updated r9): under r9 fail-closed (#6139 r9
    item 2) EVERY oversized predecessor is skipped — its full-record JSON
    validity cannot be proven without materializing the payload, so the
    fabricated prefix is never trusted, regardless of whether the oversized row
    is brace-balanced, structurally complete, or malformed. This test pins the
    malformed case (the round-8 regression target): the predecessor is an
    oversized structurally-INCOMPLETE record, but it must be skipped for the r9
    reason (oversized), not accepted.

    Journal shape:
        token(seq=1)
        <oversized malformed done seq=2: newline-terminated but JSON structurally
         incomplete (no closing brace — truncated mid-payload value)>
        <oversized partial done seq=3: the crash-truncated boundary, no newline>

    The scan for the predecessor of seq=3 hits the oversized seq=2 row first.
    Under r9 fail-closed it skips seq=2 (oversized → untrustworthy) and recovers
    token(seq=1). The budget is sized to scan PAST the oversized predecessor's
    bytes so the skip is fail-closed (not a budget stop)."""
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
        # Budget sized to scan PAST the oversized predecessor's bytes (two
        # backward newline scans through ~cap+1000 bytes each) so the skip is
        # fail-closed rather than a budget stop. The token line parse adds ~50 B.
        result = _read_last_complete_line_before(
            fh, size, final_start, budget=2 * (cap + 1000) + 10_000
        )

    # The oversized malformed predecessor (seq=2) must be SKIPPED (fail-closed);
    # the valid token (seq=1) is recovered — never the fabricated terminal done.
    assert result is not None, "the valid token (seq=1) must be recovered"
    assert result.get("seq") == 1, (
        f"expected seq=1 (the oversized row was skipped fail-closed); got "
        f"seq={result.get('seq')} — the oversized predecessor was trusted instead of skipped"
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


def test_backward_scan_budget_bounds_physical_descriptor_reads(tmp_path):
    """Regression (reviewer round 9, item 1): the aggregate byte budget must
    bound the PHYSICAL bytes touched by ``fh.read`` across the whole backward
    scan — not just nominal candidate lengths charged after the fact. A
    descriptor-instrumented probe previously observed ~66 KB returned against a
    50-byte budget because ``_rfind_byte_before`` did uncharged backward reads.

    The test wraps the handle so every ``read`` records its requested + returned
    lengths and flags any read made AFTER the budget is exhausted. With a small
    budget it asserts (a) total bytes RETURNED <= budget, (b) no read after
    exhaustion, (c) result is None (the budget couldn't reach the token). With a
    large budget it asserts the token (seq=1) is recovered — proving the budget
    was the only thing stopping the small-budget call."""
    from api.run_journal import (
        _BOUNDARY_SUMMARY_PREFIX_BYTES,
        _run_path,
        _read_last_complete_line_before,
    )

    session_id = "session_phys_budget"
    run_id = "run_phys_budget"
    path = _run_path(session_id, run_id, session_dir=tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)

    # Shape: valid token far back, then a long run of malformed rows, then an
    # oversized truncated boundary (forces _rfind_byte_before to do real work,
    # and the oversized-predecessor branch in the scan).
    token = '{"seq":1,"event":"token","payload":{"t":"valid"}}\n'
    malformed_rows = b"{bad row}\n" * 500  # ~10 bytes each
    oversized_partial = (
        '{"seq":2,"event":"done","terminal":true,"payload":{"t":"'
        + "Z" * (_BOUNDARY_SUMMARY_PREFIX_BYTES + 5000)
    )  # no newline — crash-truncated boundary

    token_bytes = token.encode("utf-8")
    malformed_bytes = malformed_rows
    boundary_bytes = oversized_partial.encode("utf-8")
    with path.open("wb") as fh:
        fh.write(token_bytes)
        fh.write(malformed_bytes)
        fh.write(boundary_bytes)

    size = token_bytes.__len__() + len(malformed_bytes) + len(boundary_bytes)
    boundary_start = len(token_bytes) + len(malformed_bytes)

    class _TrackingHandle:
        """Records every read; tracks reads made after the budget is exhausted."""
        def __init__(self, real, budget_bytes):
            self._real = real
            self.total_returned = 0
            self.total_requested = 0
            self.read_calls = 0
            self.budget = budget_bytes
            self.reads_after_exhaustion = 0

        def fileno(self):
            return self._real.fileno()

        def seek(self, *a, **kw):
            return self._real.seek(*a, **kw)

        def read(self, n=-1):
            self.read_calls += 1
            self.total_requested += max(0, n)
            if self.total_returned >= self.budget:
                self.reads_after_exhaustion += 1
            data = self._real.read(n)
            self.total_returned += len(data)
            return data

        def close(self):
            return self._real.close()

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return self._real.__exit__(*a)

    SMALL_BUDGET = 256

    # Small-budget call: instrument every read.
    with path.open("rb") as real_fh:
        tracker = _TrackingHandle(real_fh, SMALL_BUDGET)
        result_small = _read_last_complete_line_before(
            tracker, size, boundary_start, budget=SMALL_BUDGET
        )

    assert result_small is None, (
        f"a {SMALL_BUDGET}-byte budget must not reach the token past 500 malformed "
        f"rows + the oversized boundary; got {result_small}"
    )
    assert tracker.total_returned <= SMALL_BUDGET, (
        f"physical bytes RETURNED by fh.read ({tracker.total_returned}) exceeded "
        f"the budget ({SMALL_BUDGET}) — the aggregate meter is not bounding "
        f"physical I/O (#6139 r9 item 1)"
    )
    assert tracker.reads_after_exhaustion == 0, (
        f"{tracker.reads_after_exhaustion} read(s) happened AFTER the budget was "
        f"exhausted — the scan did not stop at exhaustion (#6139 r9 item 1)"
    )

    # Large-budget call: must recover the token (seq=1), proving the budget was
    # the only thing stopping the small-budget call.
    with path.open("rb") as fh:
        result_large = _read_last_complete_line_before(
            fh, size, boundary_start, budget=10 * 1024 * 1024
        )
    assert result_large is not None and result_large.get("seq") == 1, (
        f"a large budget must recover the valid token (seq=1); got {result_large}"
    )


def test_balanced_invalid_oversized_predecessor_skipped_trailing_comma(tmp_path):
    """Regression (reviewer round 9, item 2): a brace-balanced, newline-
    terminated oversized row that is INVALID JSON (trailing comma here; see the
    companion test for a malformed nested value) must NOT be promoted into a
    fabricated terminal summary. Brace balance is necessary but not sufficient
    for JSON validity.

    Under r9 fail-closed, EVERY oversized predecessor is skipped (its full-
    record validity cannot be proven without materializing the payload), so the
    balanced-invalid row is skipped and the preceding valid token (seq=1) is
    recovered — never the fabricated terminal seq=2."""
    import os
    from api.run_journal import (
        _BOUNDARY_SUMMARY_PREFIX_BYTES,
        _run_path,
        _read_last_complete_line_before,
    )

    session_id = "session_balanced_invalid_tc"
    run_id = "run_balanced_invalid_tc"
    path = _run_path(session_id, run_id, session_dir=tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)

    big = "Q" * (_BOUNDARY_SUMMARY_PREFIX_BYTES + 5000)
    token_line = '{"seq":1,"event":"token","payload":{"t":"valid"}}\n'
    # Oversized predecessor: brace-balanced + newline-terminated but INVALID JSON
    # (trailing comma after the payload value). Its head fabricates a terminal
    # 'done/seq=2' prefix via _extract_boundary_record_summary.
    oversized_pred = (
        '{"seq":2,"event":"done","terminal":true,'
        '"terminal_state":"completed","payload":{"t":"' + big + '"},}\n'
    )
    # Final crash-truncated boundary (no newline) — forces the recovery scan.
    final_partial = (
        '{"seq":3,"event":"done","terminal":true,"payload":{"t":"'
        + "Y" * (_BOUNDARY_SUMMARY_PREFIX_BYTES + 5000)
    )
    token_bytes = token_line.encode("utf-8")
    pred_bytes = oversized_pred.encode("utf-8")
    with path.open("wb") as fh:
        fh.write(token_bytes)
        fh.write(pred_bytes)
        fh.write(final_partial.encode("utf-8"))

    final_start = len(token_bytes) + len(pred_bytes)
    with path.open("rb") as fh:
        size = os.fstat(fh.fileno()).st_size
        # Budget sized to scan PAST the oversized predecessor's bytes so the
        # skip is fail-closed (not a budget stop).
        result = _read_last_complete_line_before(
            fh, size, final_start, budget=2 * len(pred_bytes) + 10_000
        )

    assert result is not None, "the valid token (seq=1) must be recovered"
    assert result.get("seq") == 1, (
        f"expected seq=1 (the trailing-comma oversized row was skipped "
        f"fail-closed); got seq={result.get('seq')} — the fabricated terminal "
        f"prefix was accepted as if the brace-balanced-but-invalid row were valid JSON"
    )
    assert not result.get("terminal"), (
        "a non-terminal token must be recovered, not a fabricated terminal done"
    )


def test_balanced_invalid_oversized_predecessor_skipped_malformed_nested_value(tmp_path):
    """Companion to test_balanced_invalid_oversized_predecessor_skipped_trailing_comma
    (reviewer round 9, item 2): a brace-balanced, newline-terminated oversized row
    that is invalid JSON due to a malformed nested value (unquoted nested key) —
    also skipped fail-closed under r9, recovering the preceding valid token."""
    import os
    from api.run_journal import (
        _BOUNDARY_SUMMARY_PREFIX_BYTES,
        _run_path,
        _read_last_complete_line_before,
    )

    session_id = "session_balanced_invalid_nv"
    run_id = "run_balanced_invalid_nv"
    path = _run_path(session_id, run_id, session_dir=tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)

    big = "Q" * (_BOUNDARY_SUMMARY_PREFIX_BYTES + 5000)
    token_line = '{"seq":1,"event":"token","payload":{"t":"valid"}}\n'
    # Oversized predecessor: brace-balanced overall + newline-terminated but
    # INVALID JSON (unquoted nested key `bad` inside payload).
    oversized_pred = (
        '{"seq":2,"event":"done","terminal":true,'
        '"terminal_state":"completed","payload":{"t":"' + big + '",bad:nested}}\n'
    )
    final_partial = (
        '{"seq":3,"event":"done","terminal":true,"payload":{"t":"'
        + "Y" * (_BOUNDARY_SUMMARY_PREFIX_BYTES + 5000)
    )
    token_bytes = token_line.encode("utf-8")
    pred_bytes = oversized_pred.encode("utf-8")
    with path.open("wb") as fh:
        fh.write(token_bytes)
        fh.write(pred_bytes)
        fh.write(final_partial.encode("utf-8"))

    final_start = len(token_bytes) + len(pred_bytes)
    with path.open("rb") as fh:
        size = os.fstat(fh.fileno()).st_size
        result = _read_last_complete_line_before(
            fh, size, final_start, budget=2 * len(pred_bytes) + 10_000
        )

    assert result is not None, "the valid token (seq=1) must be recovered"
    assert result.get("seq") == 1, (
        f"expected seq=1 (the malformed-nested-value oversized row was skipped "
        f"fail-closed); got seq={result.get('seq')} — the fabricated terminal "
        f"prefix was accepted as if the brace-balanced-but-invalid row were valid JSON"
    )
    assert not result.get("terminal"), (
        "a non-terminal token must be recovered, not a fabricated terminal done"
    )


def test_read_jsonl_tail_boundary_scan_uses_shared_budget_no_read_after_exhaustion(tmp_path, monkeypatch):
    """Regression (reviewer round 10, item 1): the PRODUCTION boundary path in
    ``_read_jsonl_tail`` must use ONE shared ``_ReadBudget`` across boundary
    lookup, prefix extraction, validity proof, and the backward predecessor
    scan — so physical I/O is bounded and NO read occurs after exhaustion.

    The r9 physical-budget test only covered ``_read_last_complete_line_before``
    (the predecessor helper). The r9 PRODUCTION composition called the boundary
    helpers WITHOUT a shared budget (each ran unbounded). This test instruments
    the PRODUCTION reader end-to-end and asserts:
      (a) the boundary helpers are reached (the straddling seq=2 is recovered);
      (b) the SAME ``_ReadBudget`` instance is threaded through boundary lookup,
          prefix extraction, validity proof, AND the predecessor scan (the
          helpers receive a non-None budget, all with one shared id);
      (c) physical reads are bounded by the shared budget envelope.

    (b) is the discrimination the r9 test lacked: it proves the budget is
    SHARED, not just that the bytes happen to be bounded by a ceiling constant.
    A mutation that passes ``budget=None`` to any helper (the r9 bug) fails (b)."""
    from pathlib import Path
    from api import run_journal
    from api.run_journal import _SESSION_REPLAY_MAX_BYTES, _read_jsonl_tail

    cap = _SESSION_REPLAY_MAX_BYTES
    token_line = '{"seq":1,"event":"token","payload":{"t":"valid"}}\n'
    # A VALID oversized done: the validity proof returns True, exercising the
    # boundary lookup + prefix extraction + validity helpers on the happy path.
    oversized_done = (
        '{"seq":2,"event":"done","terminal":true,'
        '"terminal_state":"completed","payload":{"t":"'
        + "X" * (cap + 1000)
        + '"}}\n'
    )
    path = tmp_path / "_run_journal" / "s1" / "r1.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as fh:
        fh.write(token_line.encode("utf-8"))
        fh.write(oversized_done.encode("utf-8"))
    assert path.stat().st_size > cap

    # Instrument the four boundary helpers to record whether a non-None budget
    # was passed and its id(). The SAME id must appear across all of them.
    budget_ids_seen = {"find": [], "extract": [], "valid": []}
    real_find = run_journal._find_record_start_before
    real_extract = run_journal._extract_boundary_record_summary
    real_valid = run_journal._record_is_valid_complete

    def patched_find(fh, size, seek_pos, *, budget=None):
        if budget is not None:
            budget_ids_seen["find"].append(id(budget))
        return real_find(fh, size, seek_pos, budget=budget)

    def patched_extract(fh, record_start, *, budget=None):
        if budget is not None:
            budget_ids_seen["extract"].append(id(budget))
        return real_extract(fh, record_start, budget=budget)

    def patched_valid(fh, size, record_start, *, budget=None):
        if budget is not None:
            budget_ids_seen["valid"].append(id(budget))
        return real_valid(fh, size, record_start, budget=budget)

    monkeypatch.setattr(run_journal, "_find_record_start_before", patched_find)
    monkeypatch.setattr(run_journal, "_extract_boundary_record_summary", patched_extract)
    monkeypatch.setattr(run_journal, "_record_is_valid_complete", patched_valid)

    # Also count physical reads via the handle.
    real_open = Path.open
    captured = {}

    class _CountingHandle:
        def __init__(self, real):
            self._real = real
            self.total_returned = 0
        def fileno(self): return self._real.fileno()
        def seek(self, *a, **kw): return self._real.seek(*a, **kw)
        def read(self, n=-1):
            data = self._real.read(n)
            self.total_returned += len(data)
            return data
        def close(self): return self._real.close()
        def __enter__(self): return self
        def __exit__(self, *a): return self._real.__exit__(*a)

    def patched_open(self, *args, **kwargs):
        real = real_open(self, *args, **kwargs)
        mode = args[0] if args else kwargs.get("mode", "r")
        if "b" in mode:
            h = _CountingHandle(real)
            captured["handle"] = h
            return h
        return real

    monkeypatch.setattr(Path, "open", patched_open)
    events, _ = _read_jsonl_tail(path, max_bytes=cap, max_rows=None)

    # (a) Non-vacuity: the boundary helpers ran and recovered seq=2.
    seqs = {e.get("seq") for e in events if isinstance(e, dict)}
    assert 2 in seqs, (
        f"the boundary helpers must have run (seq=2 recovered); got {seqs}. "
        f"If absent the test is vacuous."
    )
    # (b) The SAME shared _ReadBudget threads through find + extract + valid.
    # (The predecessor scan only runs on rejection; here the valid done is
    # accepted so the predecessor helper is not reached. r11: the predecessor
    # scan now gets its OWN reserve budget separate from this shared meter, so
    # a >=budget invalid oversized record can't starve it — that is covered by
    # test_predecessor_recovery_not_starved_by_validity_proof_budget and
    # test_backward_scan_budget_bounds_physical_descriptor_reads.)
    assert budget_ids_seen["find"], "boundary lookup received no budget (budget=None) — not shared"
    assert budget_ids_seen["extract"], "prefix extraction received no budget (budget=None) — not shared"
    assert budget_ids_seen["valid"], "validity proof received no budget (budget=None) — not shared"
    shared_id = budget_ids_seen["find"][0]
    assert all(bid == shared_id for bid in budget_ids_seen["find"]), (
        f"boundary lookup used multiple budget instances: {budget_ids_seen['find']}"
    )
    assert all(bid == shared_id for bid in budget_ids_seen["extract"]), (
        f"prefix extraction used a different budget instance: find={shared_id} extract={budget_ids_seen['extract']}"
    )
    assert all(bid == shared_id for bid in budget_ids_seen["valid"]), (
        f"validity proof used a different budget instance: find={shared_id} valid={budget_ids_seen['valid']}"
    )
    # (c) Physical reads are bounded by the shared budget envelope.
    handle = captured.get("handle")
    assert handle is not None, "the patched Path.open never returned a binary handle"
    total = handle.total_returned
    boundary_helper_bytes = total - cap  # subtract the one forward tail-window read
    assert boundary_helper_bytes <= 2 * cap, (
        f"boundary-helper physical reads ({boundary_helper_bytes} bytes) exceeded "
        f"the 2*cap shared budget ({2*cap}) — the production path is not bounding "
        f"physical I/O via the shared meter (#6139 r10 item 1)"
    )
    assert total < 3 * cap, (
        f"total physical reads ({total}) far exceed the expected envelope; the "
        f"production boundary scan is reading unboundedly"
    )


def test_balanced_invalid_oversized_boundary_record_trailing_comma(tmp_path):
    """Regression (reviewer round 10, item 2): a brace-balanced, newline-
    terminated oversized BOUNDARY record (the straddling record itself, not a
    predecessor) that is INVALID JSON (trailing comma after the payload value)
    must NOT be fabricated into a terminal summary. The r9 tests covered only the
    predecessor helper; the PRODUCTION ``_read_jsonl_tail`` primary path still
    trusted the fabricated prefix when brace depth returned to zero.

    Full ``_read_jsonl`` correctly retains only the valid token (seq=1); the tail
    reader must MATCH it (not promote a fabricated terminal seq=2)."""
    from api.run_journal import _SESSION_REPLAY_MAX_BYTES, _read_jsonl, _read_jsonl_tail

    cap = _SESSION_REPLAY_MAX_BYTES
    big = "Q" * (cap + 50_000)
    token = '{"seq":1,"event":"token","payload":{"t":"valid"}}\n'
    oversized_boundary = (
        '{"seq":2,"event":"done","terminal":true,'
        '"terminal_state":"completed","payload":{"t":"' + big + '"},}\n'
    )
    path = tmp_path / "_run_journal" / "s1" / "r1.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as fh:
        fh.write(token.encode("utf-8"))
        fh.write(oversized_boundary.encode("utf-8"))

    full_events, _ = _read_jsonl(path)
    tail_events, _ = _read_jsonl_tail(path, max_bytes=cap, max_rows=None)
    full_seqs = {e.get("seq") for e in full_events if isinstance(e, dict)}
    tail_seqs = {e.get("seq") for e in tail_events if isinstance(e, dict)}
    assert 2 not in full_seqs, "baseline: the trailing-comma record is invalid JSON (full reader rejects it)"
    assert 2 not in tail_seqs, (
        f"the tail reader PROMOTED the brace-balanced-but-invalid oversized "
        f"boundary record as terminal (tail_seqs={tail_seqs}); the fabricated "
        f"prefix must not be trusted without whole-record JSON validity "
        f"(#6139 r10 item 2)"
    )
    # The tail reader must not mark terminal (the invalid record is rejected,
    # only the non-terminal token seq=1 survives — matching the full reader).
    tail_promoted = any(e.get("_summary_extracted_from_oversized_record") for e in tail_events)
    assert not tail_promoted, (
        "the invalid boundary record was fabricated into a summary with "
        "_summary_extracted_from_oversized_record=True"
    )
    # Through the summary reader too: must NOT report completed.
    summary = __import__("api.run_journal", fromlist=["latest_run_summary"]).latest_run_summary(
        "s1", "r1", session_dir=tmp_path
    )
    assert summary["terminal"] is False, (
        f"latest_run_summary marked terminal={summary['terminal']} for an invalid "
        f"(trailing-comma) oversized boundary record — fail-closed should leave it nonterminal"
    )
    assert summary["terminal_state"] != "completed", (
        f"latest_run_summary reported terminal_state={summary['terminal_state']!r} "
        f"for an invalid boundary record"
    )


def test_balanced_invalid_oversized_boundary_record_malformed_nested_value(tmp_path):
    """Companion to test_balanced_invalid_oversized_boundary_record_trailing_comma
    (reviewer round 10, item 2): a brace-balanced, newline-terminated oversized
    BOUNDARY record invalid JSON due to a malformed nested value (unquoted nested
    key) — also rejected by the production tail reader, matching the full reader."""
    from api.run_journal import _SESSION_REPLAY_MAX_BYTES, _read_jsonl, _read_jsonl_tail

    cap = _SESSION_REPLAY_MAX_BYTES
    big = "Q" * (cap + 50_000)
    token = '{"seq":1,"event":"token","payload":{"t":"valid"}}\n'
    oversized_boundary = (
        '{"seq":2,"event":"done","terminal":true,'
        '"terminal_state":"completed","payload":{"t":"' + big + '",bad:nested}}\n'
    )
    path = tmp_path / "_run_journal" / "s2" / "r2.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as fh:
        fh.write(token.encode("utf-8"))
        fh.write(oversized_boundary.encode("utf-8"))

    full_events, _ = _read_jsonl(path)
    tail_events, _ = _read_jsonl_tail(path, max_bytes=cap, max_rows=None)
    full_seqs = {e.get("seq") for e in full_events if isinstance(e, dict)}
    tail_seqs = {e.get("seq") for e in tail_events if isinstance(e, dict)}
    assert 2 not in full_seqs, "baseline: the malformed-nested-value record is invalid JSON"
    assert 2 not in tail_seqs, (
        f"the tail reader PROMOTED the brace-balanced-but-invalid oversized "
        f"boundary record (malformed nested value) as terminal (tail_seqs={tail_seqs}); "
        f"#6139 r10 item 2"
    )
    tail_promoted = any(e.get("_summary_extracted_from_oversized_record") for e in tail_events)
    assert not tail_promoted, "the invalid boundary record was fabricated into a summary"
    summary = __import__("api.run_journal", fromlist=["latest_run_summary"]).latest_run_summary(
        "s2", "r2", session_dir=tmp_path
    )
    assert summary["terminal"] is False, (
        f"latest_run_summary marked terminal={summary['terminal']} for an invalid "
        f"(malformed-nested-value) oversized boundary record"
    )
    assert summary["terminal_state"] != "completed"


def test_latest_run_summary_physical_read_bounded_constant_across_file_size(tmp_path, monkeypatch):
    """Regression (reviewer round 11, blocker): ``latest_run_summary`` must read
    at most ~1.5x the tail window of physical descriptor bytes REGARDLESS of file
    size. The r10 production path did an O(file-size) ``fh.seek(0)`` head scan in
    64 KiB chunks just to count newlines for line attribution in ``malformed``
    entries — which ``latest_run_summary`` DISCARDS (``events, _malformed = ...``).
    That scan was NOT charged to the budget, so physical reads scaled with file
    size: a 72 MiB journal read 79.7 MiB physically (19x the 4 MiB tail cap).

    The fix threads ``attribute_lines=False`` from both summary readers, skipping
    the head scan. This test instruments ``fh.read`` to sum physical descriptor
    bytes returned and asserts the read stays CONSTANT (within a small margin)
    as the journal grows from 8x to 16x the tail window — proving the read is
    bounded by the tail window, not the file size. A regression that re-enables
    the head scan (or any unattributed O(file-size) read) fails: physical bytes
    grow ~linearly with file size, far exceeding the 1.5x cap threshold."""
    from pathlib import Path
    from api import run_journal
    from api.run_journal import _SESSION_REPLAY_MAX_BYTES, latest_run_summary

    cap = _SESSION_REPLAY_MAX_BYTES

    # A counting file handle that sums bytes returned by read().
    class _CountingHandle:
        def __init__(self, real):
            self._real = real
            self.total_returned = 0
        def fileno(self): return self._real.fileno()
        def seek(self, *a, **kw): return self._real.seek(*a, **kw)
        def read(self, n=-1):
            data = self._real.read(n)
            self.total_returned += len(data)
            return data
        def write(self, b):
            return self._real.write(b)
        def close(self): return self._real.close()
        def __enter__(self): return self
        def __exit__(self, *a): return self._real.__exit__(*a)

    real_open = Path.open
    captured = {}

    def patched_open(self, *args, **kwargs):
        real = real_open(self, *args, **kwargs)
        mode = args[0] if args else kwargs.get("mode", "r")
        if "b" in mode:
            h = _CountingHandle(real)
            captured["handle"] = h
            return h
        return real

    def make_journal(target_bytes, session_id, run_id):
        path = tmp_path / "_run_journal" / session_id / f"{run_id}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        line_tmpl = '{"seq":%d,"event":"token","payload":{"t":"%s"}}\n'
        seq = 0
        written = 0
        with path.open("wb") as fh:
            while written < target_bytes:
                chunk = line_tmpl % (seq, "x" * 40)
                b = chunk.encode("utf-8")
                fh.write(b)
                written += len(b)
                seq += 1
            done = ('{"seq":%d,"event":"done","terminal":true,'
                    '"terminal_state":"completed","payload":{"ok":1}}\n' % seq)
            fh.write(done.encode("utf-8"))
        return path

    monkeypatch.setattr(Path, "open", patched_open)
    measured = []
    for multiple in (8, 12, 16):
        target = multiple * cap
        captured.clear()
        # Each session/run pair is distinct so the summary cache can't short-circuit.
        sid = f"sess_scale_{multiple}"
        rid = f"run_scale_{multiple}"
        make_journal(target, sid, rid)
        summary = latest_run_summary(sid, rid, session_dir=tmp_path)
        assert summary["terminal_state"] == "completed", (
            f"sanity: the run must be completed (got {summary['terminal_state']!r}); "
            f"if not, the scale test's fixture is broken, not the bound"
        )
        handle = captured.get("handle")
        assert handle is not None, "patched Path.open never returned a binary handle"
        measured.append(handle.total_returned)

    # The headline assertion: physical read must NOT grow with file size. The
    # journal grows 8x -> 16x cap (doubling), so physical reads must stay roughly
    # constant. We assert (a) every measurement is <= 1.5x cap, and (b) the
    # largest is not meaningfully larger than the smallest (no O(file-size) leak).
    for i, phys in enumerate(measured):
        multiple = (8, 12, 16)[i]
        assert phys <= int(1.5 * cap), (
            f"latest_run_summary read {phys} bytes ({phys/cap:.2f}x cap) for a "
            f"{multiple}x-cap journal — exceeds the 1.5x cap bound. Physical reads "
            f"are NOT bounded by the tail window (#6139 r11 blocker: the discarded "
            f"head is being scanned for line attribution that summary readers discard)"
        )
    # (b) The read must be CONSTANT, not scaling: the 16x journal must not read
    # meaningfully more than the 8x journal. Allow a small margin (one extra chunk
    # boundary) for the boundary-record validity proof landing at a slightly
    # different chunk offset. A mutation that re-adds the head scan makes the
    # largest ~2x the smallest.
    assert measured[2] <= measured[0] + 2 * run_journal._SESSION_REPLAY_READ_CHUNK_BYTES, (
        f"physical reads grew with file size: 8x-cap={measured[0]}, 12x-cap="
        f"{measured[1]}, 16x-cap={measured[2]} — the read is O(file-size), not "
        f"bounded by the tail window (#6139 r11 blocker)"
    )


def test_predecessor_recovery_not_starved_by_validity_proof_budget(tmp_path):
    """Regression (reviewer round 11, secondary): a >=budget invalid oversized
    boundary record must NOT starve predecessor recovery. Under r10 the boundary
    lookup, prefix extraction, validity proof, AND the backward predecessor scan
    shared ONE ``_ReadBudget(2*cap)``. An invalid oversized record ~= the full
    budget (e.g. an 8 MiB balanced-invalid record against an 8 MiB budget) let
    the validity proof charge the record's full length, exhausting the meter
    before the backward predecessor scan could recover the preceding valid
    terminal ``done`` — so a legitimately COMPLETED run was misreported
    non-terminal (master ``completed``/last_seq 3 -> candidate ``running``/
    last_seq 3, because the preceding ``done`` (seq=1) was lost).

    The fix gives predecessor recovery its OWN reserved allowance separate from
    the validity-proof budget, so a large validity charge can't starve it. This
    test reproduces the layout: a valid ``done`` (seq=1) followed by an invalid
    oversized boundary record ~= the full 2*cap budget, followed by a trailing
    valid token (seq=3) that forces the boundary into the oversized record. The
    preceding terminal must be recovered (terminal_state == completed)."""
    from api.run_journal import (
        _SESSION_REPLAY_MAX_BYTES,
        _read_jsonl,
        _read_jsonl_tail,
        latest_run_summary,
    )

    cap = _SESSION_REPLAY_MAX_BYTES
    budget = 2 * cap  # the shared boundary-recovery budget (r10 size)
    # A valid terminal done that MUST survive predecessor recovery.
    valid_done = ('{"seq":1,"event":"done","terminal":true,'
                  '"terminal_state":"completed","payload":{"ok":1}}\n')
    # An INVALID oversized record ~= the full budget. Brace-balanced + newline-
    # terminated but with a trailing comma -> invalid JSON. The validity proof
    # reads the record's full length, which under r10 exhausted the shared budget.
    big = "Q" * (budget - 500)
    invalid_oversized = (
        '{"seq":2,"event":"done","terminal":true,'
        '"terminal_state":"completed","payload":{"t":"' + big + '"},}\n'
    )
    # A trailing valid token so the tail window's boundary lands inside seq=2.
    trailing = '{"seq":3,"event":"token","payload":{"t":"z"}}\n'
    path = tmp_path / "_run_journal" / "s1" / "r1.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as fh:
        fh.write(valid_done.encode("utf-8"))
        fh.write(invalid_oversized.encode("utf-8"))
        fh.write(trailing.encode("utf-8"))
    assert path.stat().st_size > cap, "sanity: the journal must exceed the tail window"

    # Ground truth: the full reader keeps the valid done (seq=1) + trailing token.
    full_events, _ = _read_jsonl(path)
    full_seqs = {e.get("seq") for e in full_events if isinstance(e, dict)}
    assert 1 in full_seqs and 2 not in full_seqs and 3 in full_seqs, (
        f"baseline: full reader keeps seq=1 (valid done) + seq=3 (token), rejects "
        f"seq=2 (invalid); got {full_seqs}"
    )

    tail_events, _ = _read_jsonl_tail(path, max_bytes=cap, max_rows=None)
    tail_seqs = {e.get("seq") for e in tail_events if isinstance(e, dict)}
    assert 1 in tail_seqs, (
        f"the preceding valid terminal (seq=1) was NOT recovered under a "
        f">=budget invalid oversized boundary record (tail_seqs={tail_seqs}); the "
        f"validity-proof budget exhausted the predecessor-recovery allowance "
        f"(#6139 r11 secondary: budget starvation misreports a completed run)"
    )
    # The completed run must be reported completed, matching master.
    summary = latest_run_summary("s1", "r1", session_dir=tmp_path)
    assert summary["terminal_state"] == "completed", (
        f"a completed run with a >=budget invalid oversized tail was misreported "
        f"as {summary['terminal_state']!r} (expected 'completed'); the preceding "
        f"terminal done was starved by the validity-proof budget (#6139 r11 secondary)"
    )
    assert summary["terminal"] is True, (
        f"a completed run was misreported non-terminal (terminal={summary['terminal']})"
    )


def test_malformed_oversized_boundary_extraction_none_recovers_preceding(tmp_path, monkeypatch):
    """Regression (reviewer round 12): when ``_extract_boundary_record_summary()``
    itself returns ``None`` for the straddling oversized boundary record (the record
    is malformed before the top-level ``payload`` key, e.g. an unquoted token),
    predecessor recovery must STILL run. The r11 control-flow only recovered when
    extraction succeeded but validation failed (``boundary_summary is not None and
    not _record_is_valid_complete(...)``); the extraction-None path skipped the
    ENTIRE recovery block, so the valid terminal row immediately before the
    malformed boundary was dropped — the full reader reported ``completed`` while
    the tail reader reported ``running`` (a completed run misreported non-terminal).

    Layout: valid ``done`` seq=1 (completed), oversized seq=2 malformed before
    ``payload`` (extraction returns None), trailing valid token seq=3 (forces the
    boundary into seq=2). The fix recovers the preceding terminal on EITHER
    rejection path. This is a production-composed regression: it asserts the full
    reader, tail reader, ``latest_run_summary``, AND ``find_run_summary`` all agree
    (keep seq=1, report completed, exclude the malformed seq=2); that prefix
    extraction returned None for the fixture; that predecessor recovery is reached
    exactly once with a non-empty reserve budget; and that the journal is opened
    exactly once (no pathname reopen, no O(file-size) head scan)."""
    from pathlib import Path
    from api import run_journal
    from api.run_journal import (
        _SESSION_REPLAY_MAX_BYTES,
        _read_jsonl,
        _read_jsonl_tail,
        find_run_summary,
        latest_run_summary,
    )

    cap = _SESSION_REPLAY_MAX_BYTES
    # Valid terminal done that MUST survive predecessor recovery.
    valid_done = ('{"seq":1,"event":"done","terminal":true,'
                  '"terminal_state":"completed","payload":{"ok":1}}\n')
    # Oversized straddling boundary record malformed BEFORE the top-level
    # "payload" key: the bare `bad,` token makes the truncated head invalid JSON,
    # so _extract_boundary_record_summary returns None (it cannot parse the prefix
    # up to the payload key). Newline-terminated so the record is a real line.
    big = "X" * (cap + 50_000)
    malformed_oversized = (
        '{"seq":2,"event":"done","terminal":true,bad,'
        '"payload":{"t":"' + big + '"}}\n'
    )
    # Trailing valid token so the tail window's boundary lands inside seq=2.
    trailing = '{"seq":3,"event":"token","payload":{"t":"z"}}\n'
    path = tmp_path / "_run_journal" / "s1" / "r1.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as fh:
        fh.write(valid_done.encode("utf-8"))
        fh.write(malformed_oversized.encode("utf-8"))
        fh.write(trailing.encode("utf-8"))
    assert path.stat().st_size > cap, "sanity: the journal must exceed the tail window"

    # (1) The fixture must actually exercise the extraction-None path: the prefix
    # extractor returns None for the malformed oversized boundary record. This is
    # what makes the r11 control-flow skip recovery — a non-None extraction would
    # take a different branch and the test would not discriminate the r12 fix.
    import os
    with path.open("rb") as fh:
        size = os.fstat(fh.fileno()).st_size
        record_start = run_journal._find_record_start_before(fh, size, size - cap)
    with path.open("rb") as fh:
        extraction = run_journal._extract_boundary_record_summary(fh, record_start)
    assert extraction is None, (
        f"fixture precondition: _extract_boundary_record_summary must return None "
        f"for the malformed-before-payload oversized record (got {extraction!r}); "
        f"if it returns a summary, the test does not exercise the r12 path"
    )

    # (2) Ground truth: the full reader keeps the valid done (seq=1) + trailing
    # token (seq=3) and rejects the malformed seq=2.
    full_events, _ = _read_jsonl(path)
    full_seqs = {e.get("seq") for e in full_events if isinstance(e, dict)}
    assert full_seqs == {1, 3}, (
        f"baseline: full reader keeps seq=1 (valid done) + seq=3 (token), rejects "
        f"seq=2 (malformed); got {full_seqs}"
    )

    # (3) Instrument predecessor recovery and journal opens, scoped to ONE
    # authoritative _read_jsonl_tail call. The r11 control-flow skipped recovery
    # on the extraction-None path (count 0); the r12 fix reaches it exactly once.
    recovery_calls = {"n": 0, "budgets": []}
    real_recovery = run_journal._read_last_complete_line_before

    # The real signature is (fh, size_arg, end_offset, *, budget). Patch carefully:
    def patched_recovery_real(fh, size_arg, end_offset, *, budget=None):
        recovery_calls["n"] += 1
        recovery_calls["budgets"].append(budget)
        return real_recovery(fh, size_arg, end_offset, budget=budget)

    monkeypatch.setattr(run_journal, "_read_last_complete_line_before", patched_recovery_real)

    # Count journal opens for this ONE tail read: must be exactly one (single
    # pinned descriptor; no pathname reopen, no O(file-size) head scan).
    open_calls = {"n": 0}
    real_open = Path.open

    def patched_open(self, *args, **kwargs):
        open_calls["n"] += 1
        return real_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", patched_open)

    # (4) The tail reader must keep seq=1 (recovered predecessor) and exclude
    # seq=2, matching the full reader.
    tail_events, _ = _read_jsonl_tail(path, max_bytes=cap, max_rows=None)
    tail_seqs = {e.get("seq") for e in tail_events if isinstance(e, dict)}
    assert tail_seqs == {1, 3}, (
        f"the tail reader diverged from the full reader: tail_seqs={tail_seqs} "
        f"(expected {{1, 3}}); the extraction-None path dropped the preceding "
        f"terminal (seq=1) by skipping predecessor recovery (#6139 r12 blocker)"
    )
    assert 2 not in tail_seqs, "the malformed oversized boundary record was fabricated as a summary"

    # (5) Predecessor recovery was reached exactly ONCE for this single tail read,
    # with a non-empty reserve budget. A mutation that skips the extraction-None
    # path makes the count 0; a mutation that double-recovers makes it > 1.
    assert recovery_calls["n"] == 1, (
        f"predecessor recovery was reached {recovery_calls['n']} times for a single "
        f"tail read (expected exactly 1); the extraction-None path must trigger "
        f"recovery once (#6139 r12: the r11 control-flow skipped it entirely)"
    )
    budget = recovery_calls["budgets"][0]
    assert budget is not None, "predecessor recovery received a None budget (no reserve)"
    # The reserve is a fresh _ReadBudget(read_bytes_cap); assert it is non-empty.
    remaining = getattr(budget, "remaining", None)
    assert remaining is not None and remaining > 0, (
        f"predecessor recovery's reserve budget was empty or missing "
        f"(remaining={remaining!r}); a >=budget validity proof must not starve it"
    )

    # (6) The journal was opened exactly once for this single tail read (single
    # pinned descriptor; no pathname reopen, no O(file-size) head scan).
    assert open_calls["n"] == 1, (
        f"the journal was opened {open_calls['n']} times for a single tail read "
        f"(expected exactly 1); the tail reader must open once and pin the inode "
        f"(#6139 r8/r11)"
    )

    # (7) Production summary readers must report the run completed (end-to-end
    # correctness, separate from the call-count controls above). Clear the
    # instrumented counters first so the summary-reader calls don't pollute them.
    recovery_calls["n"] = 0
    recovery_calls["budgets"].clear()
    open_calls["n"] = 0
    summary = latest_run_summary("s1", "r1", session_dir=tmp_path)
    assert summary["terminal_state"] == "completed", (
        f"latest_run_summary reported terminal_state={summary['terminal_state']!r} "
        f"(expected 'completed'); a completed run was misreported non-terminal "
        f"because the extraction-None path skipped predecessor recovery (#6139 r12)"
    )
    assert summary["terminal"] is True, (
        f"latest_run_summary marked terminal={summary['terminal']} (expected True)"
    )
    found = find_run_summary("r1", session_dir=tmp_path)
    assert found is not None, "find_run_summary must locate the run"
    assert found["terminal_state"] == "completed", (
        f"find_run_summary reported terminal_state={found['terminal_state']!r} "
        f"(expected 'completed'); the extraction-None path skipped recovery on the "
        f"find_run_summary path too (#6139 r12)"
    )
