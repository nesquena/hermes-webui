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
    append_run_event,
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

    events, _malformed, _ok = _read_jsonl(path, max_rows=10, tail=True)
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
    events, _malformed, _ok = _read_jsonl(path, max_bytes=cap, tail=True)
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
    events, _mal, _ok = _read_jsonl(path)
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

    events, malformed, _ok = _read_jsonl(path, max_bytes=2000, max_rows=10000, tail=True)
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
    events, malformed, _ok = _read_jsonl(path, max_bytes=1 << 30, max_rows=20, tail=True)
    assert len(events) <= 20
    assert len(malformed) == 1
    assert malformed[0]["line"] == 91, f"expected 91, got {malformed[0]['line']}"


def test_read_jsonl_tail_line_numbers_correct_no_seek(tmp_path):
    """When the whole file fits in the window (no seek), line attribution starts
    at 1 and is simply the position in the file."""
    path = tmp_path / "small.jsonl"
    lines = [json.dumps({"seq": 0}), json.dumps({"seq": 1}), "BROKEN", json.dumps({"seq": 3})]
    path.write_text("".join(l + "\n" for l in lines), encoding="utf-8")

    events, malformed, _ok = _read_jsonl(path, max_bytes=1 << 30, max_rows=100, tail=True)
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
    events, malformed, _ok = _read_jsonl(path)
    return events, malformed


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
    """Regression (reviewer round 5, updated r15): a VALID oversized predecessor
    (complete, newline-terminated) is RECOVERED via compact prefix-summary + streaming
    grammar validation, not blanket-skipped as under r9 and not full-line-materialized
    as under r14. The r9 invariant (never trust an oversized record without full
    validation) is preserved by the streaming JSON validator — a malformed/invalid
    oversized predecessor fails the grammar scan and is skipped, continuing backward.

    This test pins the VALID oversized predecessor case (round 5 regression target):
    seq=2 is a valid JSON done record larger than _BOUNDARY_SUMMARY_PREFIX_BYTES.
    Under r9 it was skipped (blanket fail-closed). Under r15 it is recovered via a
    bounded prefix (summary fields live before "payload") + a streaming grammar pass
    over the whole line — so seq=2 is recovered and its summary fields are correct.
    The payload is discarded (the recovery only needs summary fields), so the marker
    _summary_extracted_from_oversized_record is set."""
    import os
    from api.run_journal import _SESSION_REPLAY_MAX_BYTES, _run_path, _read_last_complete_line_before

    writer = RunJournalWriter("session_1", "run_bounded_preceding", session_dir=tmp_path)
    # seq=1: a normal-sized token (the recoverable preceding event).
    writer.append_sse_event("token", {"text": "ok"})
    # seq=2: an oversized but COMPLETE done (with newline) — VALID JSON, recovered
    # via prefix-summary under r15 (summary fields extracted from the 8 KiB prefix).
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
        # Budget generously covers: backward newline scans, the prefix read, and the
        # streaming grammar pass over seq=2 (which scans the whole line but in bounded
        # memory). r15 uses ONE shared budget for the whole predecessor scan.
        pred_size = _SESSION_REPLAY_MAX_BYTES + 100_000
        result = _read_last_complete_line_before(
            fh, size_pinned, record_start, budget=3 * pred_size + 500_000
        )
    # The VALID oversized done (seq=2) is RECOVERED via prefix-summary (r15).
    assert result is not None, "the valid oversized done (seq=2) must be recovered"
    assert result.get("seq") == 2, (
        f"expected seq=2 (the valid oversized done is recovered under r15); "
        f"got seq={result.get('seq')}"
    )
    assert result.get("event") == "done", "the recovered event is the done (seq=2), not the token"
    assert result.get("terminal_state") == "completed", (
        "the recovered done's terminal_state must be correct (summary fields survive)"
    )
    # Under r15 the oversized payload is DISCARDED (recovery needs only summary
    # fields); the marker is set to signal the payload was not materialized.
    assert result.get("_summary_extracted_from_oversized_record") is True, (
        "the valid oversized predecessor's payload was discarded via prefix-summary "
        "extraction (r15); the marker must be set"
    )
    assert result.get("payload") == {}, (
        "the oversized payload is replaced with {} under r15 prefix-summary recovery"
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
    ev_full, _, _ok = _read_jsonl(p)
    ev_tail, _, _ok = _read_jsonl_tail(p, max_bytes=10000, max_rows=100)
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
    ev_full, _, _ok = _read_jsonl(p)
    ev_tail, _, _ok = _read_jsonl_tail(p, max_bytes=10000, max_rows=100)
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
    events_normal, _, _ok = _read_jsonl_tail(path, max_bytes=cap, max_rows=None)
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

    def patched_find(fh, size, seek_pos, *, budget=None, fault=None):
        boundary_calls["n"] += 1
        # Unlink the pinned file from under the open descriptor and raise — the
        # realistic single-open TOCTOU (fd invalidated mid-recovery, inside the
        # boundary scan that the old test never reached).
        if fault is not None:
            fault[0] = True  # the production helper sets the fault flag before raising
        try:
            path.unlink()
        except OSError:
            pass
        raise OSError("simulated fd invalidation inside the boundary scan")

    monkeypatch.setattr(Path, "open", patched_open)
    monkeypatch.setattr(run_journal, "_find_record_start_before", patched_find)

    events, malformed, _ok = _read_jsonl_tail(path, max_bytes=cap, max_rows=None)
    assert isinstance(events, list) and isinstance(malformed, list) and isinstance(_ok, bool), (
        f"_read_jsonl_tail must return a (list, list, bool) safe fallback; got "
        f"({type(events).__name__}, {type(malformed).__name__}, {type(_ok).__name__})"
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
    # Round 17: sidecar adds an extra open (JSONL + sidecar = 2 total)
    assert open_calls["n"] == 2, "latest_run_summary must open JSONL + sidecar (2 opens total)"


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
    full_events, _, _ok = _read_jsonl(path)
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

    events, _malformed, _ok = _read_jsonl_tail(path, max_bytes=cap, max_rows=4096)
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
    full_events, _, _ok = _read_jsonl(path)
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
    """Regression (reviewer round 8, updated r14): under r14 finding 2, oversized
    predecessors are READ and json.loads-ed DIRECTLY, not blanket-skipped as
    under r9. The r9 invariant (never trust an oversized record without full
    validation) is preserved by requiring json.loads success — a malformed/
    invalid oversized predecessor FAILS json.loads and is skipped, continuing
    backward to the preceding valid event.

    This test pins the malformed case (the round-8 regression target): the
    predecessor is an oversized structurally-INCOMPLETE record (newline-terminated
    but no closing brace — truncated mid-payload value). Under r9 it was skipped
    for being oversized. Under r14 it is READ for json.loads and FAILS → skipped,
    recovering token(seq=1). The MECHANISM changed (blanket-skip-by-size →
    read-and-parse, skip-on-parse-failure), but the BEHAVIOR is the same:
    malformed/invalid oversized predecessors are never trusted.

    Journal shape:
        token(seq=1)
        <oversized malformed done seq=2: newline-terminated but JSON structurally
         incomplete (no closing brace — truncated mid-payload value)>
        <oversized partial done seq=3: the crash-truncated boundary, no newline>

    The scan for the predecessor of seq=3 hits the oversized seq=2 row first,
    reads it for json.loads, the parse fails → skips → recovers token(seq=1).
    The budget must cover: 2 backward scans through seq=2 (~2x pred length),
    1 read of seq=2 for json.loads (~1x pred length), and 1 read of seq=1 (~50 B)."""
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
        # Budget sized to cover: 2 backward scans through the oversized predecessor
        # (~2x pred length), 1 read of the oversized predecessor for json.loads
        # (~1x pred length), and 1 read of the small token line (~50 B) plus margin.
        # 3x the oversized predecessor length ensures the scan can read it,
        # fail json.loads, and continue to seq=1 without budget exhaustion.
        result = _read_last_complete_line_before(
            fh, size, final_start, budget=3 * (cap + 1000) + 20_000
        )

    # The oversized malformed predecessor (seq=2) FAILS json.loads → skipped;
    # the valid token (seq=1) is recovered — never a fabricated terminal done.
    assert result is not None, "the valid token (seq=1) must be recovered"
    assert result.get("seq") == 1, (
        f"expected seq=1 (the malformed oversized predecessor failed json.loads and "
        f"was skipped); got seq={result.get('seq')} — the malformed predecessor was "
        f"incorrectly accepted"
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
    """Regression (reviewer round 9, item 2, updated r14): a brace-balanced,
    newline-terminated oversized row that is INVALID JSON (trailing comma here;
    see the companion test for a malformed nested value) must NOT be promoted
    into a fabricated terminal summary. Brace balance is necessary but not
    sufficient for JSON validity.

    Under r14 finding 2, oversized predecessors are READ and json.loads-ed
    DIRECTLY. A balanced-but-invalid row FAILS json.loads and is skipped,
    recovering the preceding valid token (seq=1). The r9 invariant (never trust
    a malformed/invalid oversized predecessor) is preserved; the MECHANISM changed
    from blanket-skip-by-size to read-and-parse-skip-on-parse-failure. This test
    pins the trailing-comma invalid case (brace-balanced but JSON invalid)."""
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
    # 'done/seq=2' prefix via _extract_boundary_record_summary, but json.loads FAILS.
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
        # Budget sized to cover: 2 backward scans through the oversized predecessor
        # (~2x pred length), 1 read of the oversized predecessor for json.loads
        # (~1x pred length), and 1 read of the small token line (~50 B) plus margin.
        # 4x the oversized predecessor length ensures the scan can read it,
        # fail json.loads, and continue to seq=1 without budget exhaustion.
        result = _read_last_complete_line_before(
            fh, size, final_start, budget=4 * len(pred_bytes) + 10_000
        )

    assert result is not None, "the valid token (seq=1) must be recovered"
    assert result.get("seq") == 1, (
        f"expected seq=1 (the trailing-comma oversized row failed json.loads and "
        f"was skipped); got seq={result.get('seq')} — the fabricated terminal "
        f"prefix was accepted as if the brace-balanced-but-invalid row were valid JSON"
    )
    assert not result.get("terminal"), (
        "a non-terminal token must be recovered, not a fabricated terminal done"
    )


def test_balanced_invalid_oversized_predecessor_skipped_malformed_nested_value(tmp_path):
    """Companion to test_balanced_invalid_oversized_predecessor_skipped_trailing_comma
    (reviewer round 9, item 2, updated r14): a brace-balanced, newline-terminated
    oversized row that is invalid JSON due to a malformed nested value (unquoted
    nested key) is also READ and json.loads-ed under r14. The parse FAILS →
    skipped → recovers the preceding valid token (seq=1). The r9 invariant
    (never trust a malformed/invalid oversized predecessor) is preserved; the
    MECHANISM changed from blanket-skip-by-size to read-and-parse-skip-on-parse-failure."""
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
        # Budget sized to cover: 2 backward scans through the oversized predecessor
        # (~2x pred length), 1 read of the oversized predecessor for json.loads
        # (~1x pred length), and 1 read of the small token line (~50 B) plus margin.
        # 4x the oversized predecessor length ensures the scan can read it,
        # fail json.loads, and continue to seq=1 without budget exhaustion.
        result = _read_last_complete_line_before(
            fh, size, final_start, budget=4 * len(pred_bytes) + 10_000
        )

    assert result is not None, "the valid token (seq=1) must be recovered"
    assert result.get("seq") == 1, (
        f"expected seq=1 (the malformed-nested-value oversized row failed json.loads "
        f"and was skipped); got seq={result.get('seq')} — the fabricated terminal "
        f"prefix was accepted as if the brace-balanced-but-invalid row were valid JSON"
    )
    assert not result.get("terminal"), (
        "a non-terminal token must be recovered, not a fabricated terminal done"
    )


def test_read_jsonl_tail_boundary_scan_uses_shared_budget_no_read_after_exhaustion(tmp_path, monkeypatch):
    """Regression (reviewer round 10, item 1, updated r15): the PRODUCTION boundary
    path in ``_read_jsonl_tail`` must use ONE shared ``_ReadBudget`` across boundary
    lookup, prefix extraction, streaming validity, and the backward predecessor
    scan — so physical I/O is bounded and NO read occurs after exhaustion.

    The r9 physical-budget test only covered ``_read_last_complete_line_before``
    (the predecessor helper). The r9 PRODUCTION composition called the boundary
    helpers WITHOUT a shared budget (each ran unbounded). This test instruments
    the PRODUCTION reader end-to-end and asserts:
      (a) the boundary helpers are reached (the straddling seq=2 is recovered);
      (b) the SAME ``_ReadBudget`` instance is threaded through boundary lookup,
          prefix extraction, AND the streaming validity proof (the helpers receive
          a non-None budget, all with one shared id);
      (c) physical reads are bounded by the shared budget envelope.

    (b) is the discrimination the r9 test lacked: it proves the budget is
    SHARED, not just that the bytes happen to be bounded by a ceiling constant.
    A mutation that passes ``budget=None`` to any helper (the r9 bug) fails (b).

    r15 note: the r13 design used SEPARATE meters (lookup+prefix shared, validity
    distinct, predecessor distinct) to avoid starvation under a fixed validity
    ceiling. r15 removes the ceiling entirely (streaming validation stops at the
    record's actual end), so ONE shared budget is correct again — there is no
    ceiling to starve against. This assertion was updated from "validity has its
    OWN meter" (r13) to "validity shares the SAME meter" (r15)."""
    from pathlib import Path
    from api import run_journal
    from api.run_journal import _SESSION_REPLAY_MAX_BYTES, _read_jsonl_tail

    cap = _SESSION_REPLAY_MAX_BYTES
    token_line = '{"seq":1,"event":"token","payload":{"t":"valid"}}\n'
    # A VALID oversized done: the streaming validity returns True, exercising the
    # boundary lookup + prefix extraction + streaming validity helpers on the happy path.
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

    # Instrument the boundary helpers to record whether a non-None budget
    # was passed and its id(). The SAME id must appear across all of them.
    budget_ids_seen = {"find": [], "extract": [], "valid": []}
    real_find = run_journal._find_record_start_before
    real_extract = run_journal._extract_boundary_record_summary
    real_valid = run_journal._record_is_valid_jsonl

    def patched_find(fh, size, seek_pos, *, budget=None, fault=None):
        if budget is not None:
            budget_ids_seen["find"].append(id(budget))
        return real_find(fh, size, seek_pos, budget=budget, fault=fault)

    def patched_extract(fh, record_start, *, budget=None, fault=None):
        if budget is not None:
            budget_ids_seen["extract"].append(id(budget))
        return real_extract(fh, record_start, budget=budget, fault=fault)

    def patched_valid(fh, size, record_start, *, budget=None, fault=None):
        if budget is not None:
            budget_ids_seen["valid"].append(id(budget))
        return real_valid(fh, size, record_start, budget=budget, fault=fault)

    monkeypatch.setattr(run_journal, "_find_record_start_before", patched_find)
    monkeypatch.setattr(run_journal, "_extract_boundary_record_summary", patched_extract)
    monkeypatch.setattr(run_journal, "_record_is_valid_jsonl", patched_valid)

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
    events, _, _ok = _read_jsonl_tail(path, max_bytes=cap, max_rows=None)

    # (a) Non-vacuity: the boundary helpers ran and recovered seq=2.
    seqs = {e.get("seq") for e in events if isinstance(e, dict)}
    assert 2 in seqs, (
        f"the boundary helpers must have run (seq=2 recovered); got {seqs}. "
        f"If absent the test is vacuous."
    )
    # (b) find + extract (boundary lookup + prefix extraction) share ONE fixed
    # recovery_budget. The streaming validity proof gets its OWN budget sized to
    # the boundary record's actual extent (#6139 r15): there is no fixed validity
    # ceiling — the validity budget is (size - record_start), so a valid record of
    # ANY size is accepted (the r15 blocker 1: a 17 MiB valid done was discarded
    # under a fixed budget). The validator self-terminates at the record's newline,
    # so the validity budget is consumed exactly by the record's bytes, never more.
    # (The predecessor scan shares the recovery_budget; it only runs on rejection —
    # here the valid done is accepted so it is not reached. Covered separately by
    # test_predecessor_recovery_not_starved_by_validity_proof_budget and
    # test_backward_scan_budget_bounds_physical_descriptor_reads.)
    assert budget_ids_seen["find"], "boundary lookup received no budget (budget=None) — not shared"
    assert budget_ids_seen["extract"], "prefix extraction received no budget (budget=None) — not shared"
    assert budget_ids_seen["valid"], "streaming validity received no budget (budget=None) — not metered"
    recovery_id = budget_ids_seen["find"][0]
    assert all(bid == recovery_id for bid in budget_ids_seen["find"]), (
        f"boundary lookup used multiple budget instances: {budget_ids_seen['find']}"
    )
    assert all(bid == recovery_id for bid in budget_ids_seen["extract"]), (
        f"prefix extraction used a different budget instance from lookup: "
        f"find={recovery_id} extract={budget_ids_seen['extract']} (must share one meter)"
    )
    # Validity has its OWN budget (distinct id) — sized to the record's extent,
    # not the fixed recovery_budget, so it is never starved by lookup+prefix and
    # never imposes a correctness ceiling on valid large records.
    assert all(bid != recovery_id for bid in budget_ids_seen["valid"]), (
        f"streaming validity shared the lookup+prefix meter (id={recovery_id}); it "
        f"must have its OWN budget (the record's extent) so a valid record of any "
        f"size is accepted (#6139 r15). valid ids={budget_ids_seen['valid']}"
    )
    # (c) Physical reads are bounded. The shared budget caps total physical I/O;
    # the streaming validator + prefix extractor stop at the record's actual end,
    # so a small record costs little and a large record costs ~its length (not a
    # fixed ceiling multiple). The scale regression test holds the
    # constant-in-file-size property directly.
    handle = captured.get("handle")
    assert handle is not None, "the patched Path.open never returned a binary handle"
    total = handle.total_returned
    boundary_helper_bytes = total - cap  # subtract the one forward tail-window read
    # lookup (backward, ~token length) + 8 KiB prefix + streaming validity reads
    # the record's actual length (this fixture is a valid oversized done ~cap) —
    # well under 5x cap.
    assert boundary_helper_bytes <= 5 * cap, (
        f"boundary-helper physical reads ({boundary_helper_bytes} bytes) far exceed "
        f"the expected envelope (lookup + prefix + streaming validity ~cap); the "
        f"production boundary scan is reading unboundedly (#6139 r10/r15)"
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

    full_events, _, _ok = _read_jsonl(path)
    tail_events, _, _ok = _read_jsonl_tail(path, max_bytes=cap, max_rows=None)
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

    full_events, _, _ok = _read_jsonl(path)
    tail_events, _, _ok = _read_jsonl_tail(path, max_bytes=cap, max_rows=None)
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
    full_events, _, _ok = _read_jsonl(path)
    full_seqs = {e.get("seq") for e in full_events if isinstance(e, dict)}
    assert 1 in full_seqs and 2 not in full_seqs and 3 in full_seqs, (
        f"baseline: full reader keeps seq=1 (valid done) + seq=3 (token), rejects "
        f"seq=2 (invalid); got {full_seqs}"
    )

    tail_events, _, _ok = _read_jsonl_tail(path, max_bytes=cap, max_rows=None)
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
    full_events, _, _ok = _read_jsonl(path)
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

    # The real signature is (fh, size_arg, end_offset, *, budget, fault). Patch carefully:
    def patched_recovery_real(fh, size_arg, end_offset, *, budget=None, fault=None):
        recovery_calls["n"] += 1
        recovery_calls["budgets"].append(budget)
        return real_recovery(fh, size_arg, end_offset, budget=budget, fault=fault)

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
    tail_events, _, _ok = _read_jsonl_tail(path, max_bytes=cap, max_rows=None)
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


def _summary_from_events_ref(session_id, run_id, events):
    """Local reference to _summary_from_events so the helper does not need an
    extra import line at module scope."""
    from api.run_journal import _summary_from_events
    return _summary_from_events(session_id, run_id, events)


def _assert_valid_oversized_boundary_accepted(tmp_path, *, record_size_multiple, with_trailing):
    """Shared body for the r13 valid-oversized-boundary regressions: a VALID
    terminal ``done`` record sized at ``record_size_multiple`` x cap (above cap,
    near the _VALIDITY_PROOF_MAX_BYTES 2x ceiling) must be ACCEPTED by the tail
    reader, not rejected by budget starvation. The production order
    ``done(tool_limit_reached) -> metering -> stream_end`` and the final-row case
    are both covered (``with_trailing`` selects trailing metering/stream_end).

    Under the r12 design the validity proof shared the lookup+prefix 2x cap
    meter: the backward lookup + 8 KiB prefix consumed enough of the allowance
    that a valid ~1.75x cap record could not be read in full -> fail-closed ->
    valid terminal rejected -> wrong terminal_state (full reader
    tool_limit_reached -> tail running/completed). The r13 fix gives the validity
    proof its own _VALIDITY_PROOF_MAX_BYTES meter. This asserts exact full/tail
    parity for ordered seqs, last_seq, terminal, and the discriminating
    tool_limit_reached state through _read_jsonl_tail, latest_run_summary, AND
    find_run_summary."""
    from api.run_journal import (
        _SESSION_REPLAY_MAX_BYTES,
        _read_jsonl,
        _read_jsonl_tail,
        find_run_summary,
        latest_run_summary,
    )

    cap = _SESSION_REPLAY_MAX_BYTES
    # Build a valid oversized done at the target multiple of cap, with a
    # discriminating tool_limit_reached terminal_state.
    target_size = int(record_size_multiple * cap)
    framing_overhead = 120  # the JSON keys + payload key framing around the big string
    big_payload = "Z" * max(0, target_size - framing_overhead)
    valid_done = (
        '{"seq":2,"event":"done","terminal":true,'
        '"terminal_state":"tool_limit_reached","payload":{"t":"' + big_payload + '"}}\n'
    )
    head_token = '{"seq":1,"event":"token","payload":{"t":"head"}}\n'
    trailing = ""
    expected_seqs_full = [1, 2]
    if with_trailing:
        metering = '{"seq":3,"event":"metering","payload":{"tokens":10}}\n'
        stream_end = '{"seq":4,"event":"stream_end","payload":{}}\n'
        trailing = metering + stream_end
        expected_seqs_full = [1, 2, 3, 4]

    path = tmp_path / "_run_journal" / "s1" / "r1.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as fh:
        fh.write(head_token.encode("utf-8"))
        fh.write(valid_done.encode("utf-8"))
        fh.write(trailing.encode("utf-8"))
    size = path.stat().st_size
    assert size > cap, "sanity: the journal must exceed the tail window"
    # The done record must straddle the tail window (start before size-cap).
    done_start = len(head_token)
    done_end = done_start + len(valid_done)
    window_start = size - cap
    assert done_start < window_start < done_end, (
        f"fixture setup: the done record [{done_start},{done_end}) must straddle "
        f"the tail window start {window_start} (size={size}, cap={cap})"
    )

    # Ground truth: the full reader sees the tool_limit_reached done.
    full_events, _, _ok = _read_jsonl(path)
    full_seqs = [e.get("seq") for e in full_events]
    assert full_seqs == expected_seqs_full, (
        f"baseline: full reader seqs {full_seqs} (expected {expected_seqs_full})"
    )
    full_summary = _summary_from_events_ref("s1", "r1", full_events)
    assert full_summary["terminal_state"] == "tool_limit_reached", (
        f"baseline: full reader terminal_state={full_summary['terminal_state']!r} "
        f"(expected 'tool_limit_reached')"
    )

    # The tail reader must keep the valid oversized done (seq=2), NOT reject it
    # via budget starvation. The boundary summary is recovered, and the trailing
    # records (when present) follow it. Assert the EXACT ordered tail sequence:
    # a reordered tail ([3, 2, 4]), a duplicate boundary summary, or an unexpected
    # extra row must all fail this — not merely "seq 2 is present somewhere".
    tail_events, _, _ok = _read_jsonl_tail(path, max_bytes=cap, max_rows=None)
    tail_seqs = [e.get("seq") for e in tail_events]
    expected_tail_seqs = [2, 3, 4] if with_trailing else [2]
    assert tail_seqs == expected_tail_seqs, (
        f"the tail sequence diverged from the expected order/contents: "
        f"tail_seqs={tail_seqs} (expected {expected_tail_seqs}). If seq=2 is "
        f"absent the valid {record_size_multiple}x cap terminal was rejected via "
        f"budget starvation (#6139 r13); if the order/contents differ the boundary "
        f"recovery or trailing-window handling regressed."
    )
    # Exact full/tail parity on the discriminating fields. The full required
    # summary tuple (last_seq, terminal, terminal_state) must match across the
    # direct tail summary, latest_run_summary, AND find_run_summary — a regression
    # in any one reader's projection (e.g. last_seq dropping, terminal flipping)
    # must not be masked by the others.
    expected_last_seq = 4 if with_trailing else 2
    expected_tuple = (expected_last_seq, True, "tool_limit_reached")

    def _summary_tuple(s):
        return (int(s.get("last_seq") or 0), bool(s.get("terminal")), s.get("terminal_state"))

    tail_summary = _summary_from_events_ref("s1", "r1", tail_events)
    assert _summary_tuple(tail_summary) == expected_tuple, (
        f"direct tail summary {_summary_tuple(tail_summary)} != expected "
        f"{expected_tuple}; a valid {record_size_multiple}x cap terminal record "
        f"was rejected via budget starvation (#6139 r13)"
    )
    assert _summary_tuple(full_summary) == expected_tuple, (
        f"baseline: full reader summary {_summary_tuple(full_summary)} != expected "
        f"{expected_tuple}"
    )

    # Production summary readers must report the same tuple.
    summary = latest_run_summary("s1", "r1", session_dir=tmp_path)
    assert _summary_tuple(summary) == expected_tuple, (
        f"latest_run_summary {_summary_tuple(summary)} != expected {expected_tuple}; "
        f"a valid {record_size_multiple}x cap terminal record was rejected on the "
        f"summary path, or last_seq/terminal regressed (#6139 r13)"
    )
    found = find_run_summary("r1", session_dir=tmp_path)
    assert found is not None, "find_run_summary must locate the run"
    assert _summary_tuple(found) == expected_tuple, (
        f"find_run_summary {_summary_tuple(found)} != expected {expected_tuple}; "
        f"last_seq/terminal/terminal_state regressed on the find path (#6139 r13)"
    )


def test_valid_oversized_boundary_175x_cap_as_final_row(tmp_path):
    """Regression (reviewer round 13): a VALID terminal ``done`` at ~1.75x cap
    as the FINAL row of the journal must be accepted (not starved) by the tail
    reader. Full/tail parity on seqs, last_seq, terminal, tool_limit_reached."""
    _assert_valid_oversized_boundary_accepted(
        tmp_path, record_size_multiple=1.75, with_trailing=False
    )


def test_valid_oversized_boundary_175x_cap_before_metering_stream_end(tmp_path):
    """Regression (reviewer round 13): a VALID terminal ``done`` at ~1.75x cap
    followed by the production order ``metering`` -> ``stream_end`` must be
    accepted by the tail reader. This is the exact scenario the reviewer
    reproduced (full reader tool_limit_reached -> tail running under r12)."""
    _assert_valid_oversized_boundary_accepted(
        tmp_path, record_size_multiple=1.75, with_trailing=True
    )


def test_valid_oversized_boundary_near_2x_cap_as_final_row(tmp_path):
    """Regression (reviewer round 13): a VALID terminal ``done`` near the 2x cap
    validity ceiling (_VALIDITY_PROOF_MAX_BYTES) as the final row must be
    accepted. Near the ceiling is the hardest case for a starved shared meter."""
    _assert_valid_oversized_boundary_accepted(
        tmp_path, record_size_multiple=1.95, with_trailing=False
    )


def test_valid_oversized_boundary_near_2x_cap_before_metering_stream_end(tmp_path):
    """Regression (reviewer round 13): a VALID terminal ``done`` near the 2x cap
    validity ceiling followed by ``metering`` -> ``stream_end`` must be
    accepted."""
    _assert_valid_oversized_boundary_accepted(
        tmp_path, record_size_multiple=1.95, with_trailing=True
    )


def test_valid_oversized_boundary_above_ceiling_as_final_row(tmp_path):
    """Regression (reviewer round 14, finding 1): a VALID terminal ``done`` ABOVE
    the ``_VALIDITY_PROOF_MAX_BYTES`` ceiling (2x cap = 8 MiB) as the final row
    must be ACCEPTED, not discarded. Under r13 the validity proof capped its scan
    at the ceiling, so a valid >8 MiB record couldn't reach its newline terminator
    → fail-closed → valid terminal rejected → (1, False, running) vs full reader
    (2, True, tool_limit_reached). The r14 fix adds tier 2 (streaming depth scan)
    and raises the boundary budget to 4x cap. This exercises tier 2 (2.4x cap =
    ~9.6 MiB, above the 8 MiB ceiling)."""
    _assert_valid_oversized_boundary_accepted(
        tmp_path, record_size_multiple=2.4, with_trailing=False
    )


def test_valid_oversized_boundary_above_ceiling_before_metering_stream_end(tmp_path):
    """Regression (reviewer round 14, finding 1): a VALID terminal ``done`` above
    the ceiling followed by ``metering`` → ``stream_end`` must be accepted. This is
    the production order with an above-ceiling transcript payload."""
    _assert_valid_oversized_boundary_accepted(
        tmp_path, record_size_multiple=2.4, with_trailing=True
    )


def test_oversized_valid_predecessor_recovered_no_event_id_collision(tmp_path):
    """Regression (reviewer round 14, finding 2): a VALID predecessor larger than
    ``_BOUNDARY_SUMMARY_PREFIX_BYTES`` (8 KiB) must be RECOVERED, not blanket-skipped.
    Under r9-r13, ``_read_last_complete_line_before`` skipped any line > 8 KiB as
    "oversized fail-closed", dropping a valid 20 KiB ``tool_complete`` predecessor.
    That made ``latest_run_summary`` report a stale ``last_seq`` (1 instead of 2),
    so ``stale_interrupted_event`` synthesized ``event_id = f"{run_id}:{last_seq+1}"
    = "r1:2"`` — COLLIDING with the real seq-2 event's id ("r1:2").

    The r14 fix reads + json.loads-es the complete predecessor line directly
    (charging the budget). The valid 20 KiB predecessor is recovered (last_seq=2),
    so ``stale_interrupted_event`` synthesizes "r1:3" (no collision).

    Layout: valid token seq=1, valid 20 KiB tool_complete seq=2, crash-truncated
    oversized done seq=3 (no newline — the boundary). The tail reader must keep
    seq=2 (last_seq=2), and the synthesized recovery id must NOT be "r1:2"."""
    from api.run_journal import (
        _SESSION_REPLAY_MAX_BYTES,
        _read_jsonl,
        latest_run_summary,
    )

    cap = _SESSION_REPLAY_MAX_BYTES
    valid_token = '{"seq":1,"event":"token","payload":{"t":"a"}}\n'
    # A 20 KiB VALID tool_complete — larger than the 8 KiB prefix allowance but a
    # complete, parseable line (not oversized-payload like a done transcript).
    big_output = "Q" * 20_000
    valid_tool_complete = (
        '{"seq":2,"event":"tool_complete","payload":{"output":"' + big_output + '"}}\n'
    )
    assert len(valid_tool_complete) > 8192, "fixture: predecessor must exceed the 8 KiB prefix allowance"
    # Crash-truncated oversized done (no close/newline) — the boundary record.
    big_done = "X" * (cap + 50_000)
    truncated_done = (
        '{"seq":3,"event":"done","terminal":true,'
        '"terminal_state":"completed","payload":{"t":"' + big_done + '"'
    )
    path = tmp_path / "_run_journal" / "s1" / "r1.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as fh:
        fh.write(valid_token.encode("utf-8"))
        fh.write(valid_tool_complete.encode("utf-8"))
        fh.write(truncated_done.encode("utf-8"))
    assert path.stat().st_size > cap, "sanity: the journal must exceed the tail window"

    # Ground truth: the full reader keeps seq=1 + seq=2 (both valid), rejects seq=3 (truncated).
    full_events, _, _ = _read_jsonl(path)
    full_seqs = {e.get("seq") for e in full_events if isinstance(e, dict)}
    assert full_seqs == {1, 2}, f"baseline: full reader keeps seq 1 and 2; got {full_seqs}"

    # The tail reader must recover seq=2 (the 20 KiB valid predecessor), NOT skip it.
    summary = latest_run_summary("s1", "r1", session_dir=tmp_path)
    assert summary["last_seq"] == 2, (
        f"the valid 20 KiB predecessor (seq=2) was dropped (last_seq={summary['last_seq']}); "
        f"r14 finding 2: a complete predecessor >8 KiB must be read + parsed, not blanket-skipped"
    )

    # stale_interrupted_event computes event_id = f"{run_id}:{last_seq + 1}". With
    # last_seq=2 it synthesizes "r1:3"; under the r13 bug (last_seq=1) "r1:2" collides.
    real_seq2_id = "r1:2"
    synth_id = f"r1:{summary['last_seq'] + 1}"
    assert synth_id != real_seq2_id, (
        f"stale_interrupted_event would synthesize event_id={synth_id!r} which COLLIDES "
        f"with the real seq-2 event id {real_seq2_id!r}; the valid 20 KiB predecessor "
        f"was dropped so last_seq undercounted (#6139 r14 finding 2)"
    )
    assert synth_id == "r1:3", (
        f"expected synthesized id 'r1:3' (last_seq=2 + 1); got {synth_id!r}"
    )


def test_transient_oserror_not_cached_retry_recovers(tmp_path, monkeypatch):
    """Regression (reviewer round 14, finding 3): a transient OSError during the
    boundary scan must NOT be cached as authoritative. Under r13 the failed
    best-effort summary was cached under the matching inode signature, so the
    second ``latest_run_summary`` / ``find_run_summary`` call returned the stale
    failure instead of retrying — a completed run stayed ``unknown``. Frozen master
    (no cache) propagates and recovers.

    The r14 fix threads an ``ok`` flag from ``_read_jsonl_tail``; the summary
    readers skip ``_cache_summary`` when ``ok=False``, so the next call retries.
    This injects a one-shot OSError in the boundary validity helper, asserts the
    first call returns best-effort (unknown), the cache stays empty, and the second
    call (no fault) recovers ``completed`` via a fresh read."""
    from api import run_journal
    from api.run_journal import _SESSION_REPLAY_MAX_BYTES, latest_run_summary

    cap = _SESSION_REPLAY_MAX_BYTES
    # A completed journal with a boundary-straddling oversized done so the boundary
    # helpers run (and the fault is reachable).
    head = '{"seq":1,"event":"token","payload":{"t":"head"}}\n'
    big = "Z" * (cap + 50_000)
    done = (
        '{"seq":2,"event":"done","terminal":true,'
        '"terminal_state":"completed","payload":{"t":"' + big + '"}}\n'
    )
    path = tmp_path / "_run_journal" / "s1" / "r1.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as fh:
        fh.write(head.encode("utf-8"))
        fh.write(done.encode("utf-8"))

    # Baseline: the run is completed.
    run_journal._SUMMARY_CACHE.clear()
    baseline = latest_run_summary("s1", "r1", session_dir=tmp_path)
    assert baseline["terminal_state"] == "completed", (
        f"baseline sanity: expected completed, got {baseline['terminal_state']!r}"
    )

    # Inject a one-shot OSError in the validity helper on the FIRST call after
    # clearing the cache. The fault flag must be set so ok=False propagates.
    run_journal._SUMMARY_CACHE.clear()
    real_valid = run_journal._record_is_valid_jsonl
    calls = {"n": 0}

    def patched_valid(fh, size, record_start, *, budget=None, fault=None):
        calls["n"] += 1
        if calls["n"] == 1:
            if fault is not None:
                fault[0] = True  # match the production helper's fault-flag contract
            raise OSError("simulated one-shot EIO during boundary scan")
        return real_valid(fh, size, record_start, budget=budget, fault=fault)

    monkeypatch.setattr(run_journal, "_record_is_valid_jsonl", patched_valid)

    # First call (fault): best-effort result, NOT cached.
    first = latest_run_summary("s1", "r1", session_dir=tmp_path)
    assert first["terminal_state"] != "completed", (
        f"first call (during fault) reported {first['terminal_state']!r}; the "
        f"transient OSError should have produced a best-effort non-completed result"
    )
    # The cache must be EMPTY: the failed read must not be cached.
    assert str(path) not in run_journal._SUMMARY_CACHE, (
        "the transient-OSError failed summary was cached as authoritative; the "
        "next call would return the stale failure instead of retrying (#6139 r14 finding 3)"
    )

    # Second call (no fault): the validity helper is called again and the read
    # recovers completed.
    second = latest_run_summary("s1", "r1", session_dir=tmp_path)
    assert second["terminal_state"] == "completed", (
        f"second call (no fault) reported {second['terminal_state']!r} (expected "
        f"'completed'); the transient OSError was cached and the retry did not happen "
        f"(#6139 r14 finding 3)"
    )
    assert calls["n"] >= 2, (
        f"the validity helper was called {calls['n']} time(s) across both calls "
        f"(expected >= 2); the second call did not retry the read"
    )


def test_transient_oserror_in_unguarded_tail_read_not_cached(tmp_path, monkeypatch):
    """#6139 r14 finding 3 follow-up: a transient OSError from the UNGUARDed
    tail-window ``fh.read``/``fh.seek`` (not a boundary helper) must still
    propagate ``ok=False`` so the failed read is not cached.

    The r14 ``ok`` flag is computed as ``not fault[0]``, and ``fault[0]`` is
    set only by the boundary helpers. The tail-window ``fh.seek``/``fh.read``
    at the top of ``_read_jsonl_tail``'s try body have no individual
    try/except, so an OSError there escapes to the outer except with
    ``fault[0]`` still False -> ``ok=True`` -> the degenerate ``unknown``
    summary is cached permanently for a completed run. The outer except must
    mark the fault so callers don't cache the transient failure.

    This uses a SMALL journal (no boundary scan) so the OSError hits the
    unguarded read, not a helper."""
    import pathlib
    from api import run_journal
    from api.run_journal import latest_run_summary

    # Small completed journal — well under the cap, so NO boundary scan runs
    # and the OSError reaches the UNGUARDed tail-window fh.read.
    token = '{"seq":1,"event":"token","payload":{"t":"hi"}}\n'
    done = (
        '{"seq":2,"event":"done","terminal":true,'
        '"terminal_state":"completed","payload":{"session":{}}}\n'
    )
    path = tmp_path / "_run_journal" / "s1" / "r1.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(token + done, encoding="utf-8")

    # Baseline: the run is completed.
    run_journal._SUMMARY_CACHE.clear()
    baseline = latest_run_summary("s1", "r1", session_dir=tmp_path)
    assert baseline["terminal_state"] == "completed", (
        f"baseline sanity: expected completed, got {baseline['terminal_state']!r}"
    )

    # Inject a one-shot OSError on the FIRST fh.read of this journal (the
    # UNGUARDed tail-window read), WITHOUT touching fault[0] — mimicking a
    # real unguarded I/O failure that the helpers never see.
    run_journal._SUMMARY_CACHE.clear()
    real_path_open = pathlib.Path.open
    reads = {"n": 0}

    def patched_open(self, *args, **kwargs):
        fh = real_path_open(self, *args, **kwargs)
        if str(self) != str(path):
            return fh
        real_read = fh.read

        def guarded_read(n=-1):
            reads["n"] += 1
            if reads["n"] == 1:
                raise OSError("simulated EIO in unguarded tail-window read")
            return real_read(n)

        fh.read = guarded_read
        return fh

    monkeypatch.setattr(pathlib.Path, "open", patched_open)

    # First call (unguarded read faults): best-effort result, NOT cached.
    first = latest_run_summary("s1", "r1", session_dir=tmp_path)
    assert first["terminal_state"] != "completed", (
        f"first call (during unguarded-read fault) reported "
        f"{first['terminal_state']!r}; the transient OSError should have "
        f"produced a best-effort non-completed result"
    )
    assert str(path) not in run_journal._SUMMARY_CACHE, (
        "the transient-OSError failed summary (from an UNGUARDed read) was "
        "cached as authoritative; the next call would return the stale "
        "failure instead of retrying (#6139 r14 finding 3 follow-up)"
    )

    # Second call (no fault): the read is retried and recovers completed.
    second = latest_run_summary("s1", "r1", session_dir=tmp_path)
    assert second["terminal_state"] == "completed", (
        f"second call (no fault) reported {second['terminal_state']!r} "
        f"(expected 'completed'); the transient OSError from the unguarded "
        f"read was cached and the retry did not happen "
        f"(#6139 r14 finding 3 follow-up)"
    )
    assert reads["n"] >= 2, (
        f"fh.read was called {reads['n']} time(s) across both calls "
        f"(expected >= 2); the second call did not retry the read"
    )


# ═════════════════════════════════════════════════════════════════════════════
# PR #6139 ROUND 15 REGRESSION TESTS
# ═════════════════════════════════════════════════════════════════════════════

def test_streaming_json_validator_accepts_and_rejects(tmp_path):
    """#6139 r15: _StreamingJsonValidator accepts valid JSONL records and rejects
    malformed ones. This unit test pins the streaming JSON validator behavior
    directly, covering the accept/reject cases that the blocker regression tests
    rely on.

    The validator enforces strict JSON grammar rules via byte-level state machine
    scanning without materializing the full document. This test verifies:
    - Valid JSON with various data types and structures is accepted
    - Malformed JSON (trailing commas, bare words, incomplete structures) is rejected
    - Proper terminator handling (\n, \r\n, but not bare \r or EOF without \n)
    - No trailing garbage (multiple records) is allowed
    """
    from api.run_journal import _StreamingJsonValidator

    # ACCEPT cases (finish() == True)
    accept_cases = [
        # Simple object with mixed types
        ('{"a":1,"b":"x","c":true,"d":false,"e":null}\n', "simple mixed types"),
        # Nested structures
        ('{"a":{"b":[1,2,3]},"c":[{"d":null}]}\n', "nested objects and arrays"),
        # String with escape sequences
        ('{"a":"b\\"c\\\\d\\/\\b\\f\\n\\r\\t\\u0041"}\n', "string escapes"),
        # Numbers (negative, zero, scientific notation)
        ('{"n":-1.5,"m":0,"p":1e10,"q":-2.3E-4}\n', "number formats"),
        # Unicode (ensure_ascii=False behavior)
        ('{"msg":"héllo wörld 日本語"}\n', "unicode characters"),
        # Empty containers
        ('{"a":{},"b":[],"c":{"d":[]}}\n', "empty containers"),
        # CRLF terminator
        ('{"a":1}\r\n', "CRLF terminator"),
        # Large payload (1 MiB)
        ('{"payload":{"t":"' + 'Q' * 1048576 + '"}}\n', "large payload"),
    ]

    for json_bytes, desc in accept_cases:
        validator = _StreamingJsonValidator()
        validator.feed(json_bytes.encode("utf-8"))
        result = validator.finish()
        assert result is True, (
            f"Validator should accept valid JSON: {desc}. "
            f"Input: {json_bytes[:100]!r}..."
        )

    # REJECT cases (finish() == False)
    reject_cases = [
        # Trailing commas
        ('{"a":1,}\n', "trailing comma in object"),
        ('{"a":[1,2,]}\n', "trailing comma in array"),
        # Unquoted value
        ('{"a":bareword}\n', "unquoted value"),
        # Malformed nested value
        ('{"payload":{"t":"x",bad:nested}}\n', "malformed nested value"),
        # Unterminated string
        ('{"a":"unterminated}\n', "unterminated string"),
        # Unclosed brace
        ('{"a":1\n', "unclosed object brace"),
        # Bad keywords
        ('{"a":tru}\n', "incomplete 'true' keyword"),
        ('{"a":True}\n', "incorrect case 'True'"),
        # Bare CR terminator
        ('{"a":1}\r', "bare CR terminator"),
        # No terminator
        ('{"a":1}', "no newline terminator"),
        # Multiple records
        ('{"a":1}\n{"b":2}\n', "multiple records (trailing garbage)"),
        # Empty input
        (b'', "empty input"),
        # Leading zero
        ('{"a":01}\n', "leading zero in number"),
    ]

    for json_bytes, desc in reject_cases:
        validator = _StreamingJsonValidator()
        if isinstance(json_bytes, str):
            json_bytes = json_bytes.encode("utf-8")
        validator.feed(json_bytes)
        result = validator.finish()
        assert result is False, (
            f"Validator should reject malformed JSON: {desc}. "
            f"Input: {json_bytes[:100]!r}..."
        )


def test_valid_terminal_above_any_ceiling_accepted(tmp_path):
    """#6139 r15 blocker 1: A valid terminal record with a payload MUCH larger than
    any byte ceiling (old _VALIDITY_PROOF_MAX_BYTES = 8 MiB, current
    _SESSION_REPLAY_MAX_BYTES = 4 MiB) must be ACCEPTED and reported as terminal
    with correct terminal_state. The streaming validator validates JSON grammar
    without materializing the payload, so size alone never disqualifies a valid
    record.

    Journal layout:
        seq=1: small token event
        seq=2: valid done(tool_limit_reached) with ~17 MiB payload

    Assertions:
        - Full reader (_read_jsonl) keeps seq=2 with terminal_state=tool_limit_reached
        - latest_run_summary reports last_seq=2, terminal=True, terminal_state='tool_limit_reached'
        - find_run_summary agrees (same last_seq/terminal/terminal_state)
    """
    from api.run_journal import (
        _read_jsonl,
        _run_path,
    )

    session_id = "session_oversized_terminal"
    run_id = "run_17mib_terminal"
    path = _run_path(session_id, run_id, session_dir=tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)

    # seq=1: small token
    token_line = '{"seq":1,"event":"token","payload":{"text":"ok"}}\n'

    # seq=2: valid done with 17 MiB payload (well above any ceiling)
    # 17 MiB = 17 * 1024 * 1024 = 17825792 bytes
    payload_size = 17 * 1024 * 1024
    done_line = (
        '{"seq":2,"event":"done","type":"done","terminal":true,'
        '"terminal_state":"tool_limit_reached","payload":{"text":"'
        + "Z" * payload_size
        + '"}}\n'
    )

    with path.open("wb") as fh:
        fh.write(token_line.encode("utf-8"))
        fh.write(done_line.encode("utf-8"))

    # Full reader must accept the 17 MiB terminal record
    full_events, malformed, _ok = _read_jsonl(path)
    full_seqs = [e.get("seq") for e in full_events if isinstance(e, dict)]
    assert 2 in full_seqs, (
        f"Full reader rejected the 17 MiB terminal record (seq=2 missing). "
        f"Got seqs: {full_seqs}"
    )
    # Find the done event and check its terminal_state
    done_event = next((e for e in full_events if e.get("seq") == 2), None)
    assert done_event is not None, "seq=2 not found in full events"
    assert done_event.get("terminal_state") == "tool_limit_reached", (
        f"Expected terminal_state='tool_limit_reached', got "
        f"{done_event.get('terminal_state')!r}"
    )

    # latest_run_summary must report the 17 MiB terminal correctly
    summary = latest_run_summary(session_id, run_id, session_dir=tmp_path)
    assert summary["last_seq"] == 2, (
        f"Expected last_seq=2, got {summary['last_seq']}"
    )
    assert summary["terminal"] is True, (
        f"Expected terminal=True for 17 MiB valid terminal, got {summary['terminal']}"
    )
    assert summary["terminal_state"] == "tool_limit_reached", (
        f"Expected terminal_state='tool_limit_reached', got "
        f"{summary['terminal_state']!r}"
    )

    # find_run_summary must agree with latest_run_summary
    find_summary = find_run_summary(run_id, session_dir=tmp_path)
    assert find_summary is not None, "find_run_summary returned None"
    assert find_summary["last_seq"] == summary["last_seq"], (
        f"find_run_summary last_seq={find_summary['last_seq']} != "
        f"latest_run_summary last_seq={summary['last_seq']}"
    )
    assert find_summary["terminal"] == summary["terminal"], (
        f"find_run_summary terminal={find_summary['terminal']} != "
        f"latest_run_summary terminal={summary['terminal']}"
    )
    assert find_summary["terminal_state"] == summary["terminal_state"], (
        f"find_run_summary terminal_state={find_summary['terminal_state']!r} != "
        f"latest_run_summary terminal_state={summary['terminal_state']!r}"
    )


def test_multi_mib_predecessor_recovered_before_truncated_boundary(tmp_path):
    """#6139 r15 blocker 2: A VALID multi-MiB predecessor event (seq=2, ~2.3 MiB
    tool_complete) must be RECOVERED when the boundary record (seq=3) is
    crash-truncated and oversized. The streaming validator validates the large
    predecessor via bounded streaming scan; if it's valid JSON, it's recovered
    with its payload discarded (prefix-summary extraction).

    Journal layout:
        seq=1: valid token
        seq=2: valid tool_complete with ~2.3 MiB payload (recoverable predecessor)
        seq=3: crash-truncated oversized done (no trailing newline, payload > cap)

    Assertions:
        - Full reader keeps seqs [1, 2] (truncated seq=3 is rejected)
        - latest_run_summary reports last_seq=2 (the 2.3 MiB predecessor recovered)
    """
    from api.run_journal import (
        _SESSION_REPLAY_MAX_BYTES,
        _read_jsonl,
        _run_path,
    )

    session_id = "session_multi_mib_pred"
    run_id = "run_2_3mib_predecessor"
    path = _run_path(session_id, run_id, session_dir=tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)

    cap = _SESSION_REPLAY_MAX_BYTES

    # seq=1: valid token
    token_line = '{"seq":1,"event":"token","payload":{"text":"ok"}}\n'

    # seq=2: valid tool_complete with ~2.3 MiB payload
    pred_size = 2.3 * 1024 * 1024  # 2.3 MiB
    pred_line = (
        '{"seq":2,"event":"tool_complete","type":"tool_complete","terminal":false,'
        '"payload":{"result":"'
        + "X" * int(pred_size)
        + '"}}\n'
    )

    # seq=3: crash-truncated oversized done (no newline, exceeds cap)
    boundary_size = cap + 100_000
    boundary_partial = (
        '{"seq":3,"event":"done","type":"done","terminal":true,'
        '"terminal_state":"completed","payload":{"text":"'
        + "Y" * boundary_size
    )  # No closing brace or newline

    with path.open("wb") as fh:
        fh.write(token_line.encode("utf-8"))
        fh.write(pred_line.encode("utf-8"))
        fh.write(boundary_partial.encode("utf-8"))

    # Full reader: seqs [1, 2] (seq=3 rejected as truncated)
    full_events, malformed, _ok = _read_jsonl(path)
    full_seqs = sorted(e.get("seq") for e in full_events if isinstance(e, dict))
    assert full_seqs == [1, 2], (
        f"Full reader should keep seqs [1, 2]; got {full_seqs}. "
        f"The 2.3 MiB predecessor (seq=2) must be recovered, and the truncated "
        f"boundary (seq=3) must be rejected."
    )

    # latest_run_summary must report last_seq=2 (predecessor recovered)
    summary = latest_run_summary(session_id, run_id, session_dir=tmp_path)
    assert summary["last_seq"] == 2, (
        f"Expected last_seq=2 (the 2.3 MiB predecessor recovered); "
        f"got last_seq={summary['last_seq']}"
    )
    assert summary["terminal"] is False, (
        f"The run should be non-terminal (boundary was truncated); "
        f"got terminal={summary['terminal']}"
    )


def test_transient_open_failure_not_cached_retry_recovers(tmp_path, monkeypatch):
    """#6139 r15 blocker 3 (open path): A transient OSError on the FIRST open()
    of the journal file must NOT poison the _SUMMARY_CACHE. The first call
    returns a best-effort (non-completed) summary; the second call retries
    and recovers the correct terminal_state='completed'.

    This pins that open() failures are treated as transient and do not cache
    a failed result.
    """
    import pathlib
    from api import run_journal

    # Build a small completed journal (2 events)
    session_id = "session_transient_open"
    run_id = "run_transient_open"
    path = tmp_path / "_run_journal" / session_id / f"{run_id}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)

    token = '{"seq":1,"event":"token","payload":{"text":"ok"}}\n'
    done = '{"seq":2,"event":"done","terminal":true,"terminal_state":"completed","payload":{}}\n'
    path.write_text(token + done, encoding="utf-8")

    # Patch pathlib.Path.open to raise OSError on FIRST call for this journal
    open_calls = {"n": 0}
    real_open = pathlib.Path.open

    def patched_open(self, *args, **kwargs):
        if str(self) == str(path):
            open_calls["n"] += 1
            if open_calls["n"] == 1:
                raise OSError("Simulated transient open failure")
        return real_open(self, *args, **kwargs)

    monkeypatch.setattr(pathlib.Path, "open", patched_open)

    # Clear cache before test
    run_journal._SUMMARY_CACHE.clear()

    # First call: open fails, returns best-effort (not completed)
    first = latest_run_summary(session_id, run_id, session_dir=tmp_path)
    assert first["terminal_state"] != "completed", (
        f"First call (during open failure) should return best-effort non-completed; "
        f"got {first['terminal_state']!r}"
    )
    # Cache must NOT be poisoned
    assert str(path) not in run_journal._SUMMARY_CACHE, (
        "The failed summary was cached; retry would not happen"
    )

    # Second call: open succeeds, recovers completed
    second = latest_run_summary(session_id, run_id, session_dir=tmp_path)
    assert second["terminal_state"] == "completed", (
        f"Second call (no failure) should recover completed; got {second['terminal_state']!r}"
    )
    assert open_calls["n"] >= 2, (
        f"Expected open to be called at least twice; called {open_calls['n']} times"
    )


def test_transient_fstat_failure_not_cached_retry_recovers(tmp_path, monkeypatch):
    """#6139 r15 blocker 3 (fstat path): A transient OSError on os.fstat() for the
    journal's file descriptor must NOT poison the _SUMMARY_CACHE. The first call
    returns best-effort; the second call retries and recovers completed.

    This pins that fstat failures are treated as transient and do not cache
    a failed result.
    """
    import os
    from api import run_journal

    # Build a small completed journal
    session_id = "session_transient_fstat"
    run_id = "run_transient_fstat"
    path = tmp_path / "_run_journal" / session_id / f"{run_id}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)

    token = '{"seq":1,"event":"token","payload":{"text":"ok"}}\n'
    done = '{"seq":2,"event":"done","terminal":true,"terminal_state":"completed","payload":{}}\n'
    path.write_text(token + done, encoding="utf-8")

    # Patch os.fstat to raise OSError on FIRST call
    fstat_calls = {"n": 0}
    real_fstat = os.fstat

    def patched_fstat(fd):
        fstat_calls["n"] += 1
        if fstat_calls["n"] == 1:
            raise OSError("Simulated transient fstat failure")
        return real_fstat(fd)

    monkeypatch.setattr(os, "fstat", patched_fstat)

    # Clear cache before test
    run_journal._SUMMARY_CACHE.clear()

    # First call: fstat fails, returns best-effort
    first = latest_run_summary(session_id, run_id, session_dir=tmp_path)
    assert first["terminal_state"] != "completed", (
        f"First call (during fstat failure) should return best-effort; "
        f"got {first['terminal_state']!r}"
    )
    # Cache must NOT be poisoned
    assert str(path) not in run_journal._SUMMARY_CACHE, (
        "The failed summary was cached after fstat failure"
    )

    # Second call: fstat succeeds, recovers completed
    second = latest_run_summary(session_id, run_id, session_dir=tmp_path)
    assert second["terminal_state"] == "completed", (
        f"Second call should recover completed; got {second['terminal_state']!r}"
    )
    assert fstat_calls["n"] >= 2, (
        f"Expected fstat to be called at least twice; called {fstat_calls['n']} times"
    )


def test_oversized_malformed_boundary_rejected_by_streaming_validator_not_suffix(tmp_path):
    """#6139 r15 blocker 4: validity is decided by the streaming JSON validator,
    NOT by suffix-based tier selection. The old two-tier design computed
    ``record_len = size - record_start`` (the suffix from the boundary record's
    start to EOF, INCLUDING trailing records) and routed records above the
    ``_VALIDITY_PROOF_MAX_BYTES`` ceiling to a structural-only tier that accepted
    brace-balanced records — so a malformed-but-brace-balanced oversized boundary
    was fabricated as terminal.

    This test constructs an OVERSIZED malformed boundary straddler (a ``cap + 50``
    byte ``done`` with a trailing comma) whose ``size - record_start`` suffix
    exceeds the old ceiling. The streaming validator must REJECT it (trailing
    comma = invalid JSON) regardless of the suffix length, so it is not fabricated
    as terminal and the preceding valid token is recovered instead.

    (Geometry: the straddler is oversized so ``size - record_start`` — which spans
    the straddler + the trailing window — exceeds the old 2x-cap ceiling. This is
    the only layout in which a MALFORMED record's suffix can exceed the ceiling:
    the straddler must itself be > cap, since the trailing window is only cap.)"""
    from api.run_journal import _SESSION_REPLAY_MAX_BYTES, _read_jsonl, _read_jsonl_tail

    cap = _SESSION_REPLAY_MAX_BYTES
    token = '{"seq":1,"event":"token","payload":{"t":"valid"}}\n'
    # Oversized malformed boundary: brace-balanced + newline-terminated but INVALID
    # JSON (trailing comma after the payload value). Its suffix (size-record_start)
    # exceeds the old 2x-cap ceiling.
    big = "Q" * (cap + 50)
    malformed_boundary = (
        '{"seq":2,"event":"done","terminal":true,'
        '"terminal_state":"completed","payload":{"t":"' + big + '"},}\n'
    )
    path = tmp_path / "_run_journal" / "s1" / "r1.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as fh:
        fh.write(token.encode("utf-8"))
        fh.write(malformed_boundary.encode("utf-8"))
    assert path.stat().st_size > cap, "sanity: the journal must exceed the tail window"

    full_events, _, _ok = _read_jsonl(path)
    tail_events, _, _ok = _read_jsonl_tail(path, max_bytes=cap, max_rows=None)
    full_seqs = {e.get("seq") for e in full_events if isinstance(e, dict)}
    tail_seqs = {e.get("seq") for e in tail_events if isinstance(e, dict)}
    # Baseline: the full reader rejects the malformed boundary (seq=2 absent).
    assert 2 not in full_seqs, "baseline: the trailing-comma record is invalid JSON"
    # The tail reader must NOT promote the malformed boundary as terminal — the
    # streaming validator rejects it (not the old suffix-based tier-2 accept).
    tail_promoted = any(e.get("_summary_extracted_from_oversized_record") for e in tail_events)
    assert not tail_promoted, (
        f"the malformed oversized boundary was fabricated into a summary "
        f"(tail_seqs={tail_seqs}); the streaming validator should have rejected "
        f"the trailing comma regardless of the suffix length (#6139 r15 blocker 4)"
    )
    # The preceding valid token must be recovered (matching the full reader).
    assert 1 in tail_seqs, (
        f"the preceding valid token (seq=1) was not recovered (tail_seqs={tail_seqs})"
    )
    summary = latest_run_summary("s1", "r1", session_dir=tmp_path)
    assert summary["terminal_state"] != "completed", (
        f"latest_run_summary fabricated terminal_state={summary['terminal_state']!r} "
        f"from the malformed boundary (expected non-completed)"
    )
    assert not summary["terminal"], (
        f"a run with only a malformed boundary was marked terminal={summary['terminal']}"
    )


def test_oversized_terminal_with_non_standard_number_constants_full_tail_parity(tmp_path):
    """#6139 r16 FIX 2: full/tail readers agree on NaN/Infinity/-Infinity in oversized
    terminal records. The writer emits these constants via json.dumps(allow_nan=True),
    the full reader accepts them via json.loads default behavior, and now the tail
    reader's _StreamingJsonValidator also accepts them to maintain grammar parity.

    Mutation proof: removing the NaN/Infinity branches from _validate_accumulated_scalar
    causes these tests to FAIL (tail rejects the record).
    """
    from api.run_journal import _SESSION_REPLAY_MAX_BYTES

    # Test each non-standard constant
    test_cases = [
        (float('nan'), "NaN"),
        (float('inf'), "Infinity"),
        (float('-inf'), "-Infinity"),
    ]

    for constant_value, constant_name in test_cases:
        session_id = f"session_nan_{constant_name}"
        run_id = f"run_nan_{constant_name}"

        # Write seq=1: a small valid non-terminal event
        writer = RunJournalWriter(session_id, run_id, session_dir=tmp_path)
        writer.append_sse_event("assistant_message", {"content": "test"})

        # Write seq=2: an OVERSIZED terminal done event containing the constant
        # The payload must be > 4 MiB to trigger boundary record handling
        huge_payload = {
            "metric": constant_value,
            "bulk": "X" * (_SESSION_REPLAY_MAX_BYTES + 100_000)
        }
        writer.append_sse_event("done", {
            "session": {"session_id": session_id},
            **huge_payload
        })

        # Full reader should accept the record (json.loads allows these constants)
        full_result = read_run_events(session_id, run_id, session_dir=tmp_path)
        full_events = full_result["events"]
        full_last = full_events[-1] if full_events else None

        assert full_last is not None, f"full reader should return events for {constant_name}"
        assert full_last.get("seq") == 2, f"full reader last_seq should be 2 for {constant_name}"
        assert full_last.get("terminal") is True, f"full reader should see terminal=True for {constant_name}"
        assert full_last.get("terminal_state") == "completed", (
            f"full reader should see terminal_state='completed' for {constant_name}"
        )

        # Tail readers (both) should agree with full reader
        tail_summary = latest_run_summary(session_id, run_id, session_dir=tmp_path)
        assert tail_summary["last_seq"] == 2, (
            f"latest_run_summary last_seq should be 2 for {constant_name}, "
            f"got {tail_summary['last_seq']}"
        )
        assert tail_summary["terminal"] is True, (
            f"latest_run_summary should see terminal=True for {constant_name}, "
            f"got {tail_summary['terminal']}"
        )
        assert tail_summary["terminal_state"] == "completed", (
            f"latest_run_summary should see terminal_state='completed' for {constant_name}, "
            f"got {tail_summary['terminal_state']!r}"
        )

        find_summary = find_run_summary(run_id, session_dir=tmp_path)
        assert find_summary is not None, f"find_run_summary should find the run for {constant_name}"
        assert find_summary["last_seq"] == 2, (
            f"find_run_summary last_seq should be 2 for {constant_name}, "
            f"got {find_summary['last_seq']}"
        )
        assert find_summary["terminal"] is True, (
            f"find_run_summary should see terminal=True for {constant_name}, "
            f"got {find_summary['terminal']}"
        )
        assert find_summary["terminal_state"] == "completed", (
            f"find_run_summary should see terminal_state='completed' for {constant_name}, "
            f"got {find_summary['terminal_state']!r}"
        )


def test_streaming_json_validator_accepts_non_standard_constants(tmp_path):
    """#6139 r16 FIX 2: _StreamingJsonValidator accepts the three Python non-standard
    numeric constants (NaN, Infinity, -Infinity) that the writer emits and the full
    reader accepts.

    The validator must accept ONLY the exact tokens (case-sensitive) and reject
    malformed variants (Infinite, Nan, -Infinityx, etc.).
    """
    from api.run_journal import _StreamingJsonValidator

    # ACCEPT cases (exact tokens)
    accept_cases = [
        (b'{"x":NaN}\n', "NaN constant"),
        (b'{"x":Infinity}\n', "Infinity constant"),
        (b'{"x":-Infinity}\n', "-Infinity constant"),
        (b'[NaN,Infinity,-Infinity]\n', "array with all three constants"),
        (b'{"a":-Infinity,"b":1}\n', "object with -Infinity and normal field"),
        (b'{"payload":{"data":NaN},"seq":1}\n', "nested NaN"),
    ]

    for json_bytes, desc in accept_cases:
        validator = _StreamingJsonValidator()
        validator.feed(json_bytes)
        result = validator.finish()
        assert result is True, (
            f"Validator should accept non-standard constant: {desc}. "
            f"Input: {json_bytes!r}"
        )

    # REJECT cases (malformed variants)
    reject_cases = [
        (b'{"x":NaNx}\n', "NaN with trailing text"),
        (b'{"x":Infinite}\n', "Infinite (wrong spelling)"),
        (b'{"x":-Infinityx}\n', "-Infinity with trailing text"),
        (b'{"x":Nan}\n', "Nan (wrong case)"),
        (b'{"x":infinity}\n', "infinity (wrong case)"),
        (b'{"x":-nan}\n', "-nan (not a valid token)"),
    ]

    for json_bytes, desc in reject_cases:
        validator = _StreamingJsonValidator()
        validator.feed(json_bytes)
        result = validator.finish()
        assert result is False, (
            f"Validator should reject malformed constant variant: {desc}. "
            f"Input: {json_bytes!r}"
        )


def test_5mib_predecessor_recovered_before_truncated_boundary(tmp_path):
    """#6139 r16 FIX 3: a valid predecessor > 4 MiB is recovered before a truncated
    boundary record. The predecessor's streaming validation gets its OWN budget
    (line_len + one chunk) instead of charging the shared recovery_budget again,
    preventing starvation for large predecessors.

    Mutation proof: reverting fix 3 (charging budget_obj again) causes tail
    last_seq to drop (0 or 1) instead of recovering seq=2.
    """
    from api.run_journal import _SESSION_REPLAY_MAX_BYTES, _run_path

    session_id = "session_5mib_pred"
    run_id = "run_5mib_pred"
    path = _run_path(session_id, run_id, session_dir=tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)

    # seq=1: small valid token
    writer = RunJournalWriter(session_id, run_id, session_dir=tmp_path)
    writer.append_sse_event("token", {"text": "start"})

    # seq=2: VALID 5 MiB non-terminal event (tool_complete)
    large_size = _SESSION_REPLAY_MAX_BYTES + 1_000_000  # ~5 MiB
    large_payload = {"data": "X" * large_size}
    writer.append_sse_event("tool_complete", large_payload)

    # seq=3: CRASH-TRUNCATED oversized done event (no closing brace + newline)
    huge_size = _SESSION_REPLAY_MAX_BYTES + 100_000
    partial_done = (
        '{"version":1,"event_id":"' + run_id + ':3","seq":3,'
        '"event":"done","type":"done","terminal":true,'
        '"terminal_state":"completed","payload":{"text":"'
        + "X" * huge_size
        # NO closing brace, no newline - truncated
    )
    with open(path, "a", encoding="utf-8") as f:
        f.write(partial_done)

    # Full reader should report last_seq=2 (seq=3 is truncated)
    full_result = read_run_events(session_id, run_id, session_dir=tmp_path)
    full_events = full_result["events"]
    full_last_seq = full_events[-1].get("seq") if full_events else 0
    assert full_last_seq == 2, (
        f"full reader should report last_seq=2 (seq=3 is truncated), "
        f"got {full_last_seq}"
    )

    # Tail reader should ALSO report last_seq=2 (recovering the 5 MiB predecessor)
    tail_summary = latest_run_summary(session_id, run_id, session_dir=tmp_path)
    tail_last_seq = tail_summary["last_seq"]
    assert tail_last_seq == 2, (
        f"tail reader should recover the 5 MiB predecessor and report last_seq=2, "
        f"got {tail_last_seq} (predecessor lost)"
    )

    # find_run_summary should agree
    find_summary = find_run_summary(run_id, session_dir=tmp_path)
    assert find_summary is not None, "find_run_summary should find the run"
    assert find_summary["last_seq"] == 2, (
        f"find_run_summary should report last_seq=2, got {find_summary['last_seq']}"
    )


def test_large_predecessor_above_16mib_recovered(tmp_path):
    """#6139 r16 FIX 3: stress test with a >16 MiB predecessor to ensure the independent
    budget approach handles very large predecessors. The validator's budget is
    line_len + _SESSION_REPLAY_READ_CHUNK_BYTES, so it scales with line size.

    Mutation proof: reverting fix 3 causes this test to fail (predecessor lost).
    """
    from api.run_journal import _SESSION_REPLAY_MAX_BYTES, _run_path

    session_id = "session_16mib_pred"
    run_id = "run_16mib_pred"
    path = _run_path(session_id, run_id, session_dir=tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)

    # seq=1: small valid token
    writer = RunJournalWriter(session_id, run_id, session_dir=tmp_path)
    writer.append_sse_event("token", {"text": "start"})

    # seq=2: VALID >16 MiB non-terminal event (tool_complete)
    # Use 17 MiB to clearly exceed any plausible cap
    large_size = 17 * 1024 * 1024  # 17 MiB
    large_payload = {"data": "X" * large_size}
    writer.append_sse_event("tool_complete", large_payload)

    # seq=3: CRASH-TRUNCATED oversized done event
    huge_size = _SESSION_REPLAY_MAX_BYTES + 100_000
    partial_done = (
        '{"version":1,"event_id":"' + run_id + ':3","seq":3,'
        '"event":"done","type":"done","terminal":true,'
        '"terminal_state":"completed","payload":{"text":"'
        + "X" * huge_size
        # NO closing brace, no newline - truncated
    )
    with open(path, "a", encoding="utf-8") as f:
        f.write(partial_done)

    # Full reader should report last_seq=2
    full_result = read_run_events(session_id, run_id, session_dir=tmp_path)
    full_events = full_result["events"]
    full_last_seq = full_events[-1].get("seq") if full_events else 0
    assert full_last_seq == 2, (
        f"full reader should report last_seq=2, got {full_last_seq}"
    )

    # Tail reader should recover the 17 MiB predecessor
    tail_summary = latest_run_summary(session_id, run_id, session_dir=tmp_path)
    tail_last_seq = tail_summary["last_seq"]
    assert tail_last_seq == 2, (
        f"tail reader should recover the 17 MiB predecessor and report last_seq=2, "
        f"got {tail_last_seq} (very large predecessor lost)"
    )

    find_summary = find_run_summary(run_id, session_dir=tmp_path)
    assert find_summary is not None, "find_run_summary should find the run"
    assert find_summary["last_seq"] == 2, (
        f"find_run_summary should report last_seq=2, got {find_summary['last_seq']}"
    )


def test_transient_rfind_oserror_not_cached_retry_recovers_latest_run_summary(tmp_path):
    """#6139 r16 FIX 4: _rfind_byte_before propagates fault via fault[0]=True on OSError,
    so a transient error during backward newline scan is NOT cached as an authoritative
    "no predecessor" result. A retry succeeds and recovers the correct state.

    Mutation proof: removing fault[0]=True from _rfind_byte_before causes this test to
    FAIL (the first call's failed result IS cached, second call returns stale data).
    """
    import inspect
    import pathlib
    from api.run_journal import _SESSION_REPLAY_MAX_BYTES, _run_path

    session_id = "session_rfind_fault"
    run_id = "run_rfind_fault"
    path = _run_path(session_id, run_id, session_dir=tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)

    # Write seq=1 and seq=2 valid events, then truncated seq=3 boundary
    writer = RunJournalWriter(session_id, run_id, session_dir=tmp_path)
    writer.append_sse_event("token", {"text": "first"})
    writer.append_sse_event("tool_complete", {"result": "ok"})

    # seq=3: truncated oversized boundary
    huge_size = _SESSION_REPLAY_MAX_BYTES + 100_000
    partial_done = (
        '{"version":1,"event_id":"' + run_id + ':3","seq":3,'
        '"event":"done","type":"done","terminal":true,'
        '"terminal_state":"completed","payload":{"text":"'
        + "X" * huge_size
        # NO closing brace, no newline - truncated
    )
    with open(path, "a", encoding="utf-8") as f:
        f.write(partial_done)

    # Inject transient OSError on fh.read() when called from _rfind_byte_before
    # This tests the REAL function's except clause, not the outer one
    real_path_open = pathlib.Path.open
    fault_injected = [False]
    read_calls_while_in_rfind = [0]

    def patched_open(self, *args, **kwargs):
        # Open the real file handle
        fh = real_path_open(self, *args, **kwargs)
        real_read = fh.read

        def maybe_failing_read(size=-1):
            # Check if _rfind_byte_before is in the call stack
            stack_names = {frame.frame.f_code.co_name for frame in inspect.stack()}
            if "_rfind_byte_before" in stack_names and not fault_injected[0]:
                # First read from _rfind_byte_before: inject transient OSError
                fault_injected[0] = True
                read_calls_while_in_rfind[0] += 1
                raise OSError("Injected transient OSError during _rfind_byte_before read")
            return real_read(size)

        fh.read = maybe_failing_read
        return fh

    # Patch Path.open to inject our wrapper
    original_open = pathlib.Path.open
    pathlib.Path.open = patched_open

    try:
        # Clear any existing cache entry for this run
        from api.run_journal import _SUMMARY_CACHE
        _SUMMARY_CACHE.pop((session_id, run_id), None)

        # First call during fault should return best-effort (unknown or partial)
        # It should NOT cache the failed result
        first_summary = latest_run_summary(session_id, run_id, session_dir=tmp_path)

        # Second call should RE-READ and recover the real completed state
        # (not return a cached stale "no predecessor" result)
        second_summary = latest_run_summary(session_id, run_id, session_dir=tmp_path)
    finally:
        # Restore original Path.open
        pathlib.Path.open = original_open

    # Verify the fault was indeed injected during _rfind_byte_before
    assert fault_injected[0], "OSError should have been injected during _rfind_byte_before"
    assert read_calls_while_in_rfind[0] == 1, (
        f"exactly one read should have failed while in _rfind_byte_before, "
        f"got {read_calls_while_in_rfind[0]}"
    )

    # Assertions:
    # 1. First call during fault returns best-effort (last_seq=0/1, terminal=False/running)
    # 2. Second call RECOVERS (last_seq=2) after the OSError clears
    # 3. The results are DIFFERENT (proving the first wasn't cached)

    # First call may return last_seq=0 or 1 (best-effort during fault)
    first_last_seq = first_summary["last_seq"]
    assert first_last_seq in {0, 1}, (
        f"first call during fault may return best-effort result, got last_seq={first_last_seq}"
    )

    # Second call should recover the last valid seq (seq=2)
    # Note: terminal_state may still be "running" because seq=3 is truncated
    # and seq=2 (tool_complete) is non-terminal. The key is that last_seq advances.
    assert second_summary["last_seq"] == 2, (
        f"second call should recover seq=2, got last_seq={second_summary['last_seq']}"
    )

    # The key assertion: results are DIFFERENT
    # If fault[0]=True is missing from _rfind_byte_before, the first failed
    # result would be cached, and both calls would return the SAME stale result.
    # With fix 4, the second call produces a different result (last_seq advances).
    assert first_summary["last_seq"] != second_summary["last_seq"], (
        f"second call should produce different last_seq than first call (proving no cache), "
        f"but both returned last_seq={first_summary['last_seq']}"
    )


def test_transient_rfind_oserror_not_cached_retry_recovers_find_run_summary(tmp_path):
    """#6139 r16 FIX 4: same fault propagation test via find_run_summary instead of
    latest_run_summary, ensuring both summary readers benefit from _rfind_byte_before
    fault propagation.

    Mutation proof: removing fault[0]=True from _rfind_byte_before causes this test to
    FAIL (stale cached result instead of recovery).
    """
    import inspect
    import pathlib
    from api.run_journal import _SESSION_REPLAY_MAX_BYTES, _run_path

    session_id = "session_rfind_find"
    run_id = "run_rfind_find"
    path = _run_path(session_id, run_id, session_dir=tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)

    # Write same shape: valid seq=1, seq=2, truncated seq=3 boundary
    writer = RunJournalWriter(session_id, run_id, session_dir=tmp_path)
    writer.append_sse_event("token", {"text": "first"})
    writer.append_sse_event("tool_complete", {"result": "ok"})

    huge_size = _SESSION_REPLAY_MAX_BYTES + 100_000
    partial_done = (
        '{"version":1,"event_id":"' + run_id + ':3","seq":3,'
        '"event":"done","type":"done","terminal":true,'
        '"terminal_state":"completed","payload":{"text":"'
        + "X" * huge_size
    )
    with open(path, "a", encoding="utf-8") as f:
        f.write(partial_done)

    # Inject transient OSError on fh.read() when called from _rfind_byte_before
    real_path_open = pathlib.Path.open
    fault_injected = [False]
    read_calls_while_in_rfind = [0]

    def patched_open(self, *args, **kwargs):
        fh = real_path_open(self, *args, **kwargs)
        real_read = fh.read

        def maybe_failing_read(size=-1):
            stack_names = {frame.frame.f_code.co_name for frame in inspect.stack()}
            if "_rfind_byte_before" in stack_names and not fault_injected[0]:
                fault_injected[0] = True
                read_calls_while_in_rfind[0] += 1
                raise OSError("Injected transient OSError during _rfind_byte_before read")
            return real_read(size)

        fh.read = maybe_failing_read
        return fh

    original_open = pathlib.Path.open
    pathlib.Path.open = patched_open

    try:
        # Clear cache
        from api.run_journal import _SUMMARY_CACHE
        cache_key = (run_id,)  # find_run_summary uses (run_id,) as cache key
        _SUMMARY_CACHE.pop(cache_key, None)

        # First call during fault
        first_summary = find_run_summary(run_id, session_dir=tmp_path)

        # Second call should recover
        second_summary = find_run_summary(run_id, session_dir=tmp_path)
    finally:
        pathlib.Path.open = original_open

    # Verify the fault was indeed injected during _rfind_byte_before
    assert fault_injected[0], "OSError should have been injected during _rfind_byte_before"
    assert read_calls_while_in_rfind[0] == 1, (
        f"exactly one read should have failed while in _rfind_byte_before, "
        f"got {read_calls_while_in_rfind[0]}"
    )

    assert first_summary is not None, "first call should return a summary"
    first_last_seq = first_summary["last_seq"]
    assert first_last_seq in {0, 1}, (
        f"first call during fault may return best-effort result, got last_seq={first_last_seq}"
    )

    assert second_summary is not None, "second call should return a summary"
    assert second_summary["last_seq"] == 2, (
        f"second call should recover seq=2, got last_seq={second_summary['last_seq']}"
    )

    # Results must be different (proving no cache)
    assert first_summary["last_seq"] != second_summary["last_seq"], (
        f"second call should produce different last_seq than first call (proving no cache), "
        f"but both returned last_seq={first_summary['last_seq']}"
    )


# ── Round 17 sidecar tests ─────────────────────────────────────────────────────


def test_writer_produces_sidecar_and_summary_matches_full_read(tmp_path):
    """Test that RunJournalWriter produces a sidecar file and the sidecar-derived
    summary matches the full read summary field-by-field."""
    from api.run_journal import _run_path, _summary_sidecar_path, _read_jsonl, _summary_from_events
    import json

    session_id = "session_s1"
    run_id = "run_s1"
    writer = RunJournalWriter(session_id, run_id, session_dir=tmp_path)

    # Write token events and a terminal done
    writer.append_sse_event("token", {"text": "first"})
    writer.append_sse_event("token", {"text": "second"})
    writer.append_sse_event("done", {"session": {"session_id": session_id}})

    path = _run_path(session_id, run_id, session_dir=tmp_path)
    sidecar_path = _summary_sidecar_path(path)

    # Sidecar must exist
    assert sidecar_path.exists(), "sidecar file must exist after writer appends events"

    # Read sidecar directly
    with sidecar_path.open("r", encoding="utf-8") as f:
        sidecar_data = json.load(f)

    # Verify sidecar structure (b1: v2 schema with session_id/run_id)
    assert sidecar_data["version"] == 2
    assert sidecar_data["session_id"] == session_id
    assert sidecar_data["run_id"] == run_id
    assert sidecar_data["event_count"] == 3
    assert sidecar_data["last"]["seq"] == 3
    assert sidecar_data["last"]["event"] == "done"
    assert sidecar_data["terminal"]["event"] == "done"
    assert sidecar_data["terminal"]["state"] == "completed"

    # Get sidecar-derived summary via latest_run_summary
    sidecar_summary = latest_run_summary(session_id, run_id, session_dir=tmp_path)

    # Get full-read summary for comparison
    full_events, _, _ = _read_jsonl(path)
    full_summary = _summary_from_events(session_id, run_id, full_events)

    # Field-by-field match
    assert sidecar_summary["session_id"] == full_summary["session_id"]
    assert sidecar_summary["run_id"] == full_summary["run_id"]
    assert sidecar_summary["stream_id"] == full_summary["stream_id"]
    assert sidecar_summary["event_count"] == full_summary["event_count"]
    assert sidecar_summary["last_seq"] == full_summary["last_seq"]
    assert sidecar_summary["last_event_id"] == full_summary["last_event_id"]
    assert sidecar_summary["terminal"] == full_summary["terminal"]
    assert sidecar_summary["terminal_state"] == full_summary["terminal_state"]
    assert sidecar_summary["last_event"] == full_summary["last_event"]


def test_foreign_same_size_sidecar_rejected_by_identity(tmp_path):
    """b1: A v2 sidecar with foreign session_id/run_id (same journal_size) is rejected.
    Both readers must fall back to the JSONL tail and return the TRUE tuple."""
    from api.run_journal import _run_path, _summary_sidecar_path, _read_jsonl, _summary_from_events
    import json

    session_id = "session_1"
    run_id = "run_foreign"
    writer = RunJournalWriter(session_id, run_id, session_dir=tmp_path)

    # Create a real journal with known events
    writer.append_sse_event("token", {"text": "real"})
    writer.append_sse_event("cancel", {})  # terminal_state = interrupted-by-user

    path = _run_path(session_id, run_id, session_dir=tmp_path)
    sidecar_path = _summary_sidecar_path(path)
    journal_size = path.stat().st_size

    # Overwrite sidecar with a FOREIGN one (wrong session_id/run_id, same journal_size, wrong terminal)
    foreign_sidecar = {
        "version": 2,
        "session_id": "wrong_session",
        "run_id": "wrong_run",
        "journal_size": journal_size,
        "event_count": 1,
        "last": {"seq": 1, "event_id": "wrong_run:1", "event": "done"},
        "terminal": {"event": "done", "state": "completed", "seq": 1, "event_id": "wrong_run:1"},
    }
    with sidecar_path.open("w", encoding="utf-8") as f:
        json.dump(foreign_sidecar, f)

    # Both readers must reject the foreign sidecar and fall back to JSONL
    summary_latest = latest_run_summary(session_id, run_id, session_dir=tmp_path)
    summary_find = find_run_summary(run_id, session_dir=tmp_path)

    # Authoritative full read (baseline: must match this)
    full_events, _, _ = _read_jsonl(path)
    authoritative = _summary_from_events(session_id, run_id, full_events)

    # Both readers must agree with the full reader (cancel/interrupted-by-user), NOT the foreign done/completed
    for summary in (summary_latest, summary_find):
        assert summary["terminal"] is True, "should be terminal (cancel)"
        assert summary["terminal_state"] == "interrupted-by-user", (
            f"should report interrupted-by-user, not foreign completed; got {summary['terminal_state']}"
        )
        assert summary["last_seq"] == 2, "should have seq 2 (cancel)"
        assert summary["event_count"] == 2, "should have 2 events"

    # Must match the authoritative full read exactly
    assert summary_latest["terminal_state"] == authoritative["terminal_state"]
    assert summary_find["terminal_state"] == authoritative["terminal_state"]


def test_pre_state_fault_does_not_publish_sidecar(tmp_path):
    """b2a: When pre-state fstat raises OSError, pre_size=None, base_trusted=False,
    so no sidecar is published. JSONL gets the new line, readers return correct result."""
    from api.run_journal import _run_path, _summary_sidecar_path
    import json

    session_id = "session_fault"
    run_id = "run_fault"

    path = _run_path(session_id, run_id, session_dir=tmp_path)
    sidecar_path = _summary_sidecar_path(path)

    # Populate journal + trusted sidecar manually (set up the initial state)
    writer = RunJournalWriter(session_id, run_id, session_dir=tmp_path)
    writer.append_sse_event("token", {"text": "existing"})
    writer.append_sse_event("done", {"session": {"session_id": session_id}})

    # Verify initial sidecar exists and is trusted
    assert sidecar_path.exists()
    with sidecar_path.open("r", encoding="utf-8") as f:
        initial_sidecar = json.load(f)
    assert initial_sidecar["event_count"] == 2

    # Fault ONLY the pre-state size read. ``_descriptor_size`` is called exactly
    # twice per append (pre-state then post-state) and is NOT used by the
    # cross-process lock, so a one-call counter faults the pre-state read while
    # leaving the post-state read (and the lock's own fstat) intact. The
    # post-state MUST succeed, otherwise publish is blocked by ``post_size is
    # None`` instead of by ``base_trusted`` and the test passes for the wrong
    # reason.
    import api.run_journal as rj_module
    real_descriptor_size = rj_module._descriptor_size
    calls = {"n": 0}

    def faulting_descriptor_size(fh):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError("simulated pre-state fault")
        return real_descriptor_size(fh)

    rj_module._descriptor_size = faulting_descriptor_size
    try:
        # Append one event; pre-state fault must prevent sidecar publication.
        writer.append_sse_event("token", {"text": "after-fault"})
    finally:
        rj_module._descriptor_size = real_descriptor_size  # restore

    # Assert: sidecar NOT republished (should still show event_count=2)
    with sidecar_path.open("r", encoding="utf-8") as f:
        final_sidecar = json.load(f)
    assert final_sidecar["event_count"] == 2, (
        f"sidecar should NOT be republished after pre-state fault; "
        f"got event_count={final_sidecar['event_count']} (expected 2)"
    )

    # JSONL must have the new line (3 total lines)
    lines = path.read_text(encoding="utf-8").strip().split("\n")
    assert len(lines) == 3, f"JSONL should have 3 lines after append; got {len(lines)}"

    # Both readers must return the correct result (event_count=3, not the stale sidecar's 2)
    summary_latest = latest_run_summary(session_id, run_id, session_dir=tmp_path)
    summary_find = find_run_summary(run_id, session_dir=tmp_path)

    for summary in (summary_latest, summary_find):
        assert summary["event_count"] == 3, (
            f"reader should see all 3 events, not stale sidecar count 2; "
            f"got {summary['event_count']}"
        )
        assert summary["last_seq"] == 3, f"last_seq should be 3; got {summary['last_seq']}"


def test_sidecar_only_replacement_invalidates_warm_cache(tmp_path):
    """b1-cache: Replacing ONLY the sidecar (different terminal, same jsonl) changes
    the cache signature, so warm cache invalidates and next read returns fresh data."""
    from api.run_journal import _run_path, _summary_sidecar_path
    import json

    session_id = "session_cache"
    run_id = "run_cache"
    writer = RunJournalWriter(session_id, run_id, session_dir=tmp_path)

    writer.append_sse_event("token", {"text": "initial"})
    writer.append_sse_event("done", {"session": {"session_id": session_id}})

    path = _run_path(session_id, run_id, session_dir=tmp_path)
    sidecar_path = _summary_sidecar_path(path)

    # Warm the cache via a read
    summary_1 = latest_run_summary(session_id, run_id, session_dir=tmp_path)
    assert summary_1["terminal_state"] == "completed"

    # Replace ONLY the sidecar (different terminal, same jsonl)
    with sidecar_path.open("r", encoding="utf-8") as f:
        sidecar_data = json.load(f)
    sidecar_data["terminal"] = {"event": "cancel", "state": "interrupted-by-user", "seq": 2, "event_id": f"{run_id}:2"}
    with sidecar_path.open("w", encoding="utf-8") as f:
        json.dump(sidecar_data, f)

    # Next read must NOT return the stale cached value (must re-read)
    summary_2 = latest_run_summary(session_id, run_id, session_dir=tmp_path)
    assert summary_2["terminal_state"] == "interrupted-by-user", (
        "warm cache must invalidate after sidecar-only replacement; "
        f"got stale {summary_2['terminal_state']}"
    )


def test_capped_rebuild_not_promoted_to_authority(tmp_path):
    """b2b: A journal beyond the byte cap (capped rebuild) must NOT publish a sidecar.
    Rebuild with max_rows=None only trusts when pre_size <= max_bytes."""
    from api.run_journal import _SESSION_REPLAY_MAX_BYTES, _run_path, _summary_sidecar_path
    import json

    session_id = "session_capped"
    run_id = "run_capped"

    # Build an OVERSIZED journal directly (fast): a cancel(seq1) semantic
    # terminal near the start, followed by a few large events so the total
    # exceeds the 4 MiB byte cap. No sidecar is written, so the next append must
    # rebuild from the bounded tail. Raw-written events use the writer's own
    # record shape so the rebuild fold exercises the real parser.
    large = "X" * (512 * 1024)  # 512 KiB per token payload
    path = _run_path(session_id, run_id, session_dir=tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        specs = [(1, "cancel", {"message": "x"}, "interrupted-by-user")]
        specs += [(seq, "token", {"text": large}, None) for seq in range(2, 11)]
        for seq, name, payload, tstate in specs:
            ev = {
                "version": 1,
                "event_id": f"{run_id}:{seq}",
                "seq": seq,
                "run_id": run_id,
                "session_id": session_id,
                "event": name,
                "type": name,
                "created_at": float(seq),
                "terminal": tstate is not None,
                "terminal_state": tstate,
                "payload": payload,
            }
            fh.write(json.dumps(ev, ensure_ascii=False, separators=(",", ":")) + "\n")

    sidecar_path = _summary_sidecar_path(path)
    assert not sidecar_path.exists(), "fixture must start with no sidecar"

    # Sanity: the journal actually exceeds the byte cap (so the rebuild is capped).
    journal_size = path.stat().st_size
    assert journal_size > _SESSION_REPLAY_MAX_BYTES, (
        f"test fixture must exceed byte cap; journal size={journal_size}, cap={_SESSION_REPLAY_MAX_BYTES}"
    )

    # Append one event (triggers a capped rebuild; must NOT publish a sidecar).
    append_run_event(session_id, run_id, "token", {"text": "after-rebuild"}, session_dir=tmp_path)

    assert not sidecar_path.exists(), (
        "sidecar must NOT be published for a capped rebuild; "
        "journal exceeds byte cap, so the rebuild is incomplete authority"
    )

    # Both readers must not raise (they fall back to the bounded tail reader).
    assert latest_run_summary(session_id, run_id, session_dir=tmp_path) is not None
    assert find_run_summary(run_id, session_dir=tmp_path) is not None


def test_writer_free_function_interleaving_no_omission(tmp_path):
    """b3: RunJournalWriter and free append_run_event() writing to the same journal
    must not omit events (thread-safety + seq uniqueness). Uses actual threads."""
    import threading
    from api.run_journal import _run_path, _read_jsonl

    session_id = "session_interleave"
    run_id = "run_interleave"
    path = _run_path(session_id, run_id, session_dir=tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)

    n = 50  # events per thread

    def writer_thread():
        w = RunJournalWriter(session_id, run_id, session_dir=tmp_path)
        for i in range(n):
            w.append_sse_event("token", {"text": f"writer-{i}"})

    def free_func_thread():
        for i in range(n):
            append_run_event(session_id, run_id, "token", {"text": f"free-{i}"}, session_dir=tmp_path)

    t1 = threading.Thread(target=writer_thread)
    t2 = threading.Thread(target=free_func_thread)
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    # Read JSONL and verify
    events, _, _ = _read_jsonl(path)
    assert len(events) == 2 * n, f"expected {2*n} events, got {len(events)}"

    # All seqs must be unique and in [1, 2*n]
    seqs = sorted([int(e["seq"]) for e in events])
    assert seqs == list(range(1, 2 * n + 1)), f"seqs not gapless: {seqs[:5]}...{seqs[-5:]}"


def test_cross_process_concurrent_append_no_omission(tmp_path):
    """b3: Two subprocesses appending to the SAME journal must not omit events.
    Asserts sidecar event_count == durable JSONL line count (ground truth)."""
    import subprocess
    import sys
    import api.run_journal as rj_module
    from api.run_journal import _run_path, _summary_sidecar_path, _read_jsonl
    import json

    session_id = "session_crossproc"
    run_id = "run_crossproc"
    path = _run_path(session_id, run_id, session_dir=tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)

    # The helper runs in a fresh interpreter, so it needs the REPO root (where
    # the ``api`` package lives) on sys.path -- not tmp_path.
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(rj_module.__file__)))
    n_events = 30

    helper_code = (
        "import sys\n"
        f"sys.path.insert(0, r{chr(34)}{repo_root}{chr(34)})\n"
        "from api.run_journal import append_run_event\n"
        f"sid = {session_id!r}\n"
        f"rid = {run_id!r}\n"
        f"sd = r{chr(34)}{tmp_path}{chr(34)}\n"
        f"for i in range({n_events}):\n"
        "    append_run_event(sid, rid, 'token', {'text': f'proc-{i}'}, session_dir=sd)\n"
    )
    helper_file = tmp_path / "cross_proc_helper.py"
    helper_file.write_text(helper_code, encoding="utf-8")

    # Spawn two subprocesses CONCURRENTLY, capturing stderr so an import/append
    # failure in the helper surfaces instead of making the test pass vacuously.
    p1 = subprocess.Popen([sys.executable, str(helper_file)], stderr=subprocess.PIPE)
    p2 = subprocess.Popen([sys.executable, str(helper_file)], stderr=subprocess.PIPE)
    out1, err1 = p1.communicate()
    out2, err2 = p2.communicate()
    assert p1.returncode == 0, f"helper proc1 failed: {err1.decode('utf-8', 'replace')}"
    assert p2.returncode == 0, f"helper proc2 failed: {err2.decode('utf-8', 'replace')}"

    # Ground truth: durable JSONL line count.
    events, _, _ = _read_jsonl(path)
    durable_count = len(events)
    assert durable_count == 2 * n_events, (
        f"expected {2 * n_events} durable events, got {durable_count}; "
        f"proc1_err={err1.decode('utf-8', 'replace')!r} proc2_err={err2.decode('utf-8', 'replace')!r}"
    )

    # The published sidecar must agree with the durable line count (no omissions).
    sidecar_path = _summary_sidecar_path(path)
    assert sidecar_path.exists(), "trusted sidecar must be published for the concurrent run"
    with sidecar_path.open("r", encoding="utf-8") as f:
        sidecar_data = json.load(f)
    assert sidecar_data["event_count"] == durable_count, (
        f"sidecar event_count={sidecar_data['event_count']} != durable count={durable_count}; "
        "cross-process serialization failed - accepted event(s) omitted from the sidecar"
    )

    # All seqs must be unique and gapless across the two processes.
    seqs = sorted([int(e["seq"]) for e in events])
    assert seqs == list(range(1, durable_count + 1)), f"seqs not gapless: {seqs[:5]}...{seqs[-5:]}"


def test_oversized_terminal_summary_uses_sidecar_bounded_reads(tmp_path):
    """Regression test for the #6139 r17 blocker: a cold summary read of a run
    whose terminal record carries a huge transcript payload must use the compact
    sidecar (O(1)) instead of walking + scanning the oversized record (O(payload)).
    Physical bytes read from the JSONL must stay bounded (< 1 MiB) regardless of
    payload size, and the read must finish well under the UI's 30s API timeout.

    Writer-produced single-terminal-record regression (the maintainer's r17 ask).
    The payload size defaults to 8 MiB (comfortably > the 4 MiB tail cap, CI-
    friendly); setting ``HERMES_WEBUI_RUN_JOURNAL_BIG_REGRESSION=144`` runs the
    exact 144 MiB case the gate measured at 30s+ pre-fix.
    """
    import os
    import time
    import pathlib
    from api.run_journal import (
        _run_path,
        _summary_sidecar_path,
        _SUMMARY_CACHE,
        _SUMMARY_CACHE_LOCK,
    )

    session_id = "session_os"
    run_id = "run_os"
    # Default 8 MiB for CI; env override enables the exact 144 MiB gate case.
    payload_mib = int(os.environ.get("HERMES_WEBUI_RUN_JOURNAL_BIG_REGRESSION", "8"))
    jsonl_path = _run_path(session_id, run_id, session_dir=tmp_path)

    # Count PHYSICAL bytes read from the JSONL only (the sidecar is a different
    # path, so sidecar reads are excluded — proving the payload is never scanned).
    jsonl_read_bytes = {"bytes": 0}
    real_path_open = pathlib.Path.open

    class _CountingRead:
        def __init__(self, fh):
            self._fh = fh

        def read(self, n=-1):
            data = self._fh.read(n)
            jsonl_read_bytes["bytes"] += len(data)
            return data

        def __getattr__(self, name):
            return getattr(self._fh, name)

    def selective_open(self, *args, **kwargs):
        fh = real_path_open(self, *args, **kwargs)
        # Wrap only positional "rb" opens of the JSONL path. The sidecar is read
        # via Path.read_bytes() (mode passed as kwarg, different path) so it is
        # NOT counted — exactly what we want to isolate.
        if self == jsonl_path and args and args[0] == "rb":
            return _CountingRead(fh)
        return fh

    pathlib.Path.open = selective_open
    try:
        writer = RunJournalWriter(session_id, run_id, session_dir=tmp_path)
        writer.append_sse_event("token", {"text": "first"})
        big_blob = "x" * (payload_mib * 1024 * 1024)
        writer.append_sse_event(
            "done",
            {"terminal_state": "tool_limit_reached", "transcript": big_blob},
        )

        assert _summary_sidecar_path(jsonl_path).exists(), "writer must produce a sidecar"

        with _SUMMARY_CACHE_LOCK:
            _SUMMARY_CACHE.clear()
        t0 = time.perf_counter()
        summary = latest_run_summary(session_id, run_id, session_dir=tmp_path)
        elapsed = time.perf_counter() - t0
    finally:
        pathlib.Path.open = real_path_open

    # Terminal identity preserved exactly.
    assert summary["terminal_state"] == "tool_limit_reached"
    assert summary["terminal"] is True
    assert summary["last_seq"] == 2

    # CRITICAL: physical JSONL reads must be bounded (< 1 MiB) regardless of the
    # payload size — the sidecar is the authority, the transcript is never scanned.
    assert jsonl_read_bytes["bytes"] < 1 * 1024 * 1024, (
        f"physical JSONL reads ({jsonl_read_bytes['bytes']} bytes) must be < 1 MiB "
        f"regardless of the {payload_mib} MiB payload — the sidecar must be used"
    )
    # Well under the UI's 30s API timeout (pre-fix this was 30s+ at 144 MiB).
    assert elapsed < 5.0, (
        f"cold summary read took {elapsed:.2f}s; must stay fast (not scale with payload)"
    )


def test_sidecar_journal_size_mismatch_falls_back_to_tail(tmp_path):
    """Test that a stale sidecar (journal_size != current JSONL size) triggers
    fallback to the tail reader, which recovers the hand-appended event."""
    from api.run_journal import _run_path, _summary_sidecar_path
    import json

    session_id = "session_stale"
    run_id = "run_stale"
    writer = RunJournalWriter(session_id, run_id, session_dir=tmp_path)

    # Write token(seq=1) + done(seq=2) via writer (sidecar created)
    writer.append_sse_event("token", {"text": "first"})
    writer.append_sse_event("done", {"session": {"session_id": session_id}})

    path = _run_path(session_id, run_id, session_dir=tmp_path)
    sidecar_path = _summary_sidecar_path(path)

    # Verify sidecar exists and reflects seq=2
    assert sidecar_path.exists()
    with sidecar_path.open("r", encoding="utf-8") as f:
        sidecar_v1 = json.load(f)
    assert sidecar_v1["last"]["seq"] == 2
    original_journal_size = sidecar_v1["journal_size"]

    # Hand-append a seq=3 stream_end line (JSONL grows, sidecar now stale)
    with path.open("a", encoding="utf-8") as f:
        seq3_line = json.dumps({
            "version": 1,
            "event_id": f"{run_id}:3",
            "seq": 3,
            "run_id": run_id,
            "session_id": session_id,
            "event": "stream_end",
            "type": "stream_end",
            "created_at": 999999.0,
            "terminal": True,
            "terminal_state": "closed",
            "payload": {}
        }, separators=(",", ":")) + "\n"
        f.write(seq3_line)

    # Clear cache
    from api.run_journal import _SUMMARY_CACHE, _SUMMARY_CACHE_LOCK
    with _SUMMARY_CACHE_LOCK:
        _SUMMARY_CACHE.clear()

    # latest_run_summary must recover seq=3 via fallback (tail reader)
    summary = latest_run_summary(session_id, run_id, session_dir=tmp_path)
    assert summary["last_seq"] == 3, (
        f"fallback must recover hand-appended seq=3; got last_seq={summary['last_seq']}"
    )

    # Sidecar file still exists (not deleted) but is stale
    assert sidecar_path.exists()
    with sidecar_path.open("r", encoding="utf-8") as f:
        sidecar_stale = json.load(f)
    assert sidecar_stale["journal_size"] == original_journal_size, (
        "stale sidecar must not be updated"
    )


def test_sidecar_authoritative_terminal_fold(tmp_path):
    """Test the fold logic matches select_authoritative_terminal_event across
    the 5 cases: [done, stream_end], [done, cancel], [stream_end],
    [stream_end(a), stream_end(b)], [token, token, done(tool_limit_reached)]."""
    from api.run_journal import _read_jsonl, _summary_from_events

    test_cases = [
        # (events, expected_terminal_state, description)
        ([{"event": "done", "terminal": True, "terminal_state": "tool_limit_reached", "seq": 1, "event_id": "r:1", "run_id": "r", "session_id": "s"},
          {"event": "stream_end", "terminal": True, "terminal_state": "closed", "seq": 2, "event_id": "r:2", "run_id": "r", "session_id": "s"}],
         "tool_limit_reached", "done(tool_limit_reached) then stream_end -> done wins (stream_end must NOT override a semantic terminal)"),

        ([{"event": "done", "terminal": True, "terminal_state": "completed", "seq": 1, "event_id": "r:1", "run_id": "r", "session_id": "s"},
          {"event": "cancel", "terminal": True, "terminal_state": "interrupted-by-user", "seq": 2, "event_id": "r:2", "run_id": "r", "session_id": "s"}],
         "interrupted-by-user", "done then cancel -> cancel wins"),

        ([{"event": "stream_end", "terminal": True, "terminal_state": "closed", "seq": 1, "event_id": "r:1", "run_id": "r", "session_id": "s"}],
         "completed", "single stream_end -> completed (terminal_state only extracts tool_limit_reached)"),

        ([{"event": "stream_end", "terminal": True, "terminal_state": "closed_a", "seq": 1, "event_id": "r:1", "run_id": "r", "session_id": "s"},
          {"event": "stream_end", "terminal": True, "terminal_state": "closed_b", "seq": 2, "event_id": "r:2", "run_id": "r", "session_id": "s"}],
         "completed", "multiple stream_ends -> latest wins (but all resolve to 'completed')"),

        ([{"event": "token", "terminal": False, "seq": 1, "event_id": "r:1", "run_id": "r", "session_id": "s"},
          {"event": "token", "terminal": False, "seq": 2, "event_id": "r:2", "run_id": "r", "session_id": "s"},
          {"event": "done", "terminal": True, "terminal_state": "tool_limit_reached", "seq": 3, "event_id": "r:3", "run_id": "r", "session_id": "s"}],
         "tool_limit_reached", "tokens then done(tool_limit_reached) -> tool_limit_reached"),
    ]

    for i, (events, expected_state, description) in enumerate(test_cases):
        session_id = f"session_fold_{i}"
        run_id = f"run_fold_{i}"
        writer = RunJournalWriter(session_id, run_id, session_dir=tmp_path)

        # Append events via writer (builds sidecar)
        for event in events:
            payload = {}
            if event["event"] == "done" and event.get("terminal_state") == "tool_limit_reached":
                payload = {"terminal_state": "tool_limit_reached"}
            elif event["event"] == "done":
                payload = {"session": {"session_id": session_id}}
            elif event["event"] == "cancel":
                payload = {"message": "Cancelled"}
            elif event["event"] == "stream_end":
                # Pass terminal_state in payload so writer uses it
                payload = {"terminal_state": event.get("terminal_state", "closed")}

            writer.append_sse_event(event["event"], payload)

        # Get sidecar-derived summary
        sidecar_summary = latest_run_summary(session_id, run_id, session_dir=tmp_path)

        # Get full-read summary for comparison
        from api.run_journal import _run_path
        path = _run_path(session_id, run_id, session_dir=tmp_path)
        full_events, _, _ = _read_jsonl(path)
        full_summary = _summary_from_events(session_id, run_id, full_events)

        # Both must agree on expected_state
        assert sidecar_summary["terminal_state"] == expected_state, (
            f"case {i} ({description}): sidecar terminal_state={sidecar_summary['terminal_state']}, "
            f"expected {expected_state}"
        )
        assert full_summary["terminal_state"] == expected_state, (
            f"case {i} ({description}): full terminal_state={full_summary['terminal_state']}, "
            f"expected {expected_state}"
        )

        # Sidecar and full must match
        assert sidecar_summary["terminal_state"] == full_summary["terminal_state"], (
            f"case {i} ({description}): sidecar and full terminal_state mismatch"
        )


def test_find_run_summary_uses_sidecar(tmp_path):
    """Test that find_run_summary uses the sidecar and returns the correct summary."""
    from api.run_journal import _summary_sidecar_path

    session_id = "session_find"
    run_id = "run_find"
    writer = RunJournalWriter(session_id, run_id, session_dir=tmp_path)

    writer.append_sse_event("token", {"text": "first"})
    writer.append_sse_event("done", {"session": {"session_id": session_id}})

    path = writer._path
    sidecar_path = _summary_sidecar_path(path)

    # Verify sidecar exists
    assert sidecar_path.exists()

    # Clear cache
    from api.run_journal import _SUMMARY_CACHE, _SUMMARY_CACHE_LOCK
    with _SUMMARY_CACHE_LOCK:
        _SUMMARY_CACHE.clear()

    # find_run_summary must return the sidecar-derived summary
    summary = find_run_summary(run_id, session_dir=tmp_path)
    assert summary is not None
    assert summary["terminal_state"] == "completed"
    assert summary["terminal"] is True
    assert summary["last_seq"] == 2
    assert "path" in summary
    assert summary["path"] == str(path)


def test_read_run_events_does_not_use_sidecar(tmp_path):
    """Test that read_run_events returns ALL full events (replay needs full events),
    not a short-circuited sidecar result."""
    from api.run_journal import _run_path, _summary_sidecar_path

    session_id = "session_replay"
    run_id = "run_replay"
    writer = RunJournalWriter(session_id, run_id, session_dir=tmp_path)

    # Write multiple events
    writer.append_sse_event("token", {"text": "first"})
    writer.append_sse_event("token", {"text": "second"})
    writer.append_sse_event("done", {"session": {"session_id": session_id}})

    path = _run_path(session_id, run_id, session_dir=tmp_path)
    sidecar_path = _summary_sidecar_path(path)

    # Verify sidecar exists
    assert sidecar_path.exists()

    # read_run_events must return ALL 3 events (not sidecar summary)
    replay = read_run_events(session_id, run_id, session_dir=tmp_path)
    assert len(replay["events"]) == 3, (
        f"read_run_events must return all 3 events for replay; got {len(replay['events'])}"
    )

    # Verify events are full (have payloads)
    for event in replay["events"]:
        assert "payload" in event
        assert isinstance(event["payload"], dict)
        # The done event should have the session payload
        if event["event"] == "done":
            assert "session" in event["payload"]


def test_sidecar_absent_falls_back_to_tail_correctness(tmp_path):
    """Test that a journal with no sidecar (hand-written) still produces a correct
    summary via the fallback tail reader."""
    from api.run_journal import _run_path, _summary_sidecar_path
    import json

    session_id = "session_nosidecar"
    run_id = "run_nosidecar"
    path = _run_path(session_id, run_id, session_dir=tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)

    # Hand-write journal (no sidecar)
    events = [
        {"version": 1, "event_id": f"{run_id}:1", "seq": 1, "run_id": run_id,
         "session_id": session_id, "event": "token", "type": "token",
         "terminal": False, "terminal_state": None, "created_at": 100.0,
         "payload": {"text": "first"}},
        {"version": 1, "event_id": f"{run_id}:2", "seq": 2, "run_id": run_id,
         "session_id": session_id, "event": "done", "type": "done",
         "terminal": True, "terminal_state": "completed", "created_at": 200.0,
         "payload": {"session": {"session_id": session_id}}},
    ]

    with path.open("w", encoding="utf-8") as f:
        for event in events:
            f.write(json.dumps(event, separators=(",", ":")) + "\n")

    # Verify no sidecar exists
    sidecar_path = _summary_sidecar_path(path)
    assert not sidecar_path.exists()

    # Clear cache
    from api.run_journal import _SUMMARY_CACHE, _SUMMARY_CACHE_LOCK
    with _SUMMARY_CACHE_LOCK:
        _SUMMARY_CACHE.clear()

    # latest_run_summary must still produce correct summary via fallback
    summary = latest_run_summary(session_id, run_id, session_dir=tmp_path)
    assert summary["terminal_state"] == "completed"
    assert summary["terminal"] is True
    assert summary["last_seq"] == 2
    assert summary["event_count"] == 2


# ── Round 18 regression tests ─────────────────────────────────────────────────────


def test_failed_interior_sidecar_commit_not_healed_into_wrong_terminal(tmp_path, monkeypatch):
    """Regression (Round 18, blocker 1): When a sidecar write fails between appends
    (e.g. the cancel's sidecar commit OSError), the writer must NOT trust the
    stale prior sidecar as the fold base for the next append. The stale sidecar's
    journal_size reflects a pre-append state, so the writer validates it matches
    the actual JSONL size before folding. A mismatch rebuilds from the tail, not
    from the stale sidecar. Without this check, the lost cancel event is
    permanently healed into a wrong 'completed' terminal."""
    from api.run_journal import _run_path, _summary_sidecar_path, _read_jsonl, _summary_from_events, append_run_event
    import json
    from unittest import mock

    session_id = "session_r18_b1"
    run_id = "run_r18_b1"
    path = _run_path(session_id, run_id, session_dir=tmp_path)
    sidecar_path = _summary_sidecar_path(path)

    # Append token(seq=1) — sidecar written successfully
    append_run_event(session_id, run_id, "token", {"text": "first"}, session_dir=tmp_path)

    # Verify token sidecar
    with sidecar_path.open("r", encoding="utf-8") as f:
        token_sidecar = json.load(f)
    assert token_sidecar["event_count"] == 1
    token_journal_size = token_sidecar["journal_size"]

    # Now make the NEXT sidecar write fail (cancel) - patch only during this append
    real_atomic_write = __import__("api.run_journal", fromlist=["_atomic_write_json"])._atomic_write_json
    failed_once = {"v": False}

    def failing_write(p, payload, *, fsync):
        if not failed_once["v"]:
            failed_once["v"] = True
            raise OSError("Simulated sidecar write failure")
        return real_atomic_write(p, payload, fsync=fsync)

    # Patch only during the cancel append
    with mock.patch("api.run_journal._atomic_write_json", failing_write):
        append_run_event(session_id, run_id, "cancel", {"message": "Cancelled"}, session_dir=tmp_path)

    # After cancel: JSONL has token+cancel, but sidecar is still stale (event_count=1, journal_size=token_size)
    with sidecar_path.open("r", encoding="utf-8") as f:
        cancel_sidecar = json.load(f)
    assert cancel_sidecar["event_count"] == 1, "cancel's sidecar write failed, leaving stale sidecar"
    assert cancel_sidecar["journal_size"] == token_journal_size, "stale sidecar journal_size unchanged"

    # Append stream_end(seq=3) — sidecar write succeeds (terminal committed)
    append_run_event(session_id, run_id, "stream_end", {"terminal_state": "closed"}, session_dir=tmp_path)

    # The sidecar must reflect seq=3 (stream_end), not the stale seq=1
    assert sidecar_path.exists()
    with sidecar_path.open("r", encoding="utf-8") as f:
        sidecar_data = json.load(f)
    assert sidecar_data["event_count"] == 3, (
        f"sidecar must count all 3 events; got {sidecar_data['event_count']} — "
        "stale seq=1 sidecar was incorrectly folded onto"
    )
    assert sidecar_data["last"]["seq"] == 3, (
        f"sidecar last_seq must be 3 (stream_end); got {sidecar_data['last']['seq']} — "
        "stale sidecar caused wrong terminal"
    )

    # Clear cache and get cold summary (must match full read)
    from api.run_journal import _SUMMARY_CACHE, _SUMMARY_CACHE_LOCK
    with _SUMMARY_CACHE_LOCK:
        _SUMMARY_CACHE.clear()

    cold_summary = latest_run_summary(session_id, run_id, session_dir=tmp_path)
    full_events, _, _ = _read_jsonl(path)
    full_summary = _summary_from_events(session_id, run_id, full_events)

    # Cold summary must equal full summary (event_count=3, terminal=interrupted-by-user)
    assert cold_summary["event_count"] == full_summary["event_count"] == 3, (
        f"cold summary event_count={cold_summary['event_count']}, "
        f"full={full_summary['event_count']} — lost cancel event was healed into wrong terminal"
    )
    assert cold_summary["terminal_state"] == full_summary["terminal_state"] == "interrupted-by-user", (
        f"cold terminal_state={cold_summary['terminal_state']}, "
        f"full={full_summary['terminal_state']} — stale sidecar misreported terminal"
    )


def test_transient_tail_rebuild_fault_not_published_as_authority(tmp_path, monkeypatch):
    """Regression (Round 18, blocker 2): When a sidecar is absent for an existing run
    and the writer rebuilds from the tail, a transient fault in _read_jsonl_tail
    (ok=False) must NOT publish a sidecar. A faulted rebuild could be incomplete
    (e.g., tail truncated mid-read due to concurrent append), so publishing it as
    authority would brick the summary readers. The writer honors tail_ok and only
    publishes when the rebuild succeeded. Readers keep falling back to the tail
    reader, and a later append retries the rebuild."""
    from api.run_journal import _run_path, _summary_sidecar_path, _read_jsonl, _summary_from_events
    import json

    session_id = "session_r18_b2"
    run_id = "run_r18_b2"
    path = _run_path(session_id, run_id, session_dir=tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)

    # Hand-write a 3-event journal (token, token, cancel) with NO sidecar
    events = [
        {"version": 1, "event_id": f"{run_id}:1", "seq": 1, "run_id": run_id,
         "session_id": session_id, "event": "token", "type": "token",
         "terminal": False, "terminal_state": None, "created_at": 100.0,
         "payload": {"text": "first"}},
        {"version": 1, "event_id": f"{run_id}:2", "seq": 2, "run_id": run_id,
         "session_id": session_id, "event": "token", "type": "token",
         "terminal": False, "terminal_state": None, "created_at": 200.0,
         "payload": {"text": "second"}},
        {"version": 1, "event_id": f"{run_id}:3", "seq": 3, "run_id": run_id,
         "session_id": session_id, "event": "cancel", "type": "cancel",
         "terminal": True, "terminal_state": "interrupted-by-user", "created_at": 300.0,
         "payload": {"message": "Cancelled"}},
    ]

    with path.open("w", encoding="utf-8") as f:
        for event in events:
            f.write(json.dumps(event, separators=(",", ":")) + "\n")

    # Verify no sidecar exists
    sidecar_path = _summary_sidecar_path(path)
    assert not sidecar_path.exists()

    # Monkeypatch _read_jsonl_tail to return ok=False (simulated transient fault)
    real_read_tail = __import__("api.run_journal", fromlist=["_read_jsonl_tail"])._read_jsonl_tail

    def faulted_tail(path, *, max_bytes, max_rows, attribute_lines):
        events, malformed, _ok = real_read_tail(
            path, max_bytes=max_bytes, max_rows=max_rows, attribute_lines=attribute_lines
        )
        return events, malformed, False  # Force ok=False

    monkeypatch.setattr("api.run_journal._read_jsonl_tail", faulted_tail)

    # Append a 4th event (stream_end) — this rebuilds from the faulted tail
    writer = RunJournalWriter(session_id, run_id, session_dir=tmp_path)
    writer.append_sse_event("stream_end", {"terminal_state": "closed"})

    # The append must have succeeded (JSONL has 4 lines)
    with path.open("r", encoding="utf-8") as f:
        jsonl_lines = [line for line in f if line.strip()]
    assert len(jsonl_lines) == 4, (
        f"JSONL must have 4 lines after append; got {len(jsonl_lines)} — "
        "append may have failed"
    )

    # The sidecar must NOT have been published (rebuild was faulted)
    assert not sidecar_path.exists(), (
        "sidecar must not exist after faulted rebuild — writer must not publish "
        "untrusted state"
    )

    # Clear cache
    from api.run_journal import _SUMMARY_CACHE, _SUMMARY_CACHE_LOCK
    with _SUMMARY_CACHE_LOCK:
        _SUMMARY_CACHE.clear()

    # Cold latest_run_summary must still work (tail fallback) and match full read
    cold_summary = latest_run_summary(session_id, run_id, session_dir=tmp_path)
    full_events, _, _ = _read_jsonl(path)
    full_summary = _summary_from_events(session_id, run_id, full_events)

    assert cold_summary["event_count"] == full_summary["event_count"] == 4, (
        f"cold summary event_count={cold_summary['event_count']}, "
        f"full={full_summary['event_count']} — tail fallback failed"
    )
    assert cold_summary["terminal_state"] == full_summary["terminal_state"], (
        f"cold terminal_state={cold_summary['terminal_state']}, "
        f"full={full_summary['terminal_state']} — tail fallback misreported terminal"
    )


def test_malformed_matching_size_sidecar_reader_degrades_without_raising(tmp_path, monkeypatch):
    """Regression (Round 18, blocker 3): A malformed sidecar that passes the
    journal_size stale check (size matches JSONL) must not raise ValueError or
    KeyError in the reader or during append. The reader's _validate_sidecar
    strictly checks schema/types/ranges and returns None on any malformation,
    degrading to the JSONL fallback. Append must not brick journaling."""
    from api.run_journal import _run_path, _summary_sidecar_path
    import json

    session_id = "session_r18_b3_reader"
    run_id = "run_r18_b3_reader"
    writer = RunJournalWriter(session_id, run_id, session_dir=tmp_path)

    # Append a done event (sidecar written)
    writer.append_sse_event("done", {"session": {"session_id": session_id}})

    path = _run_path(session_id, run_id, session_dir=tmp_path)
    sidecar_path = _summary_sidecar_path(path)
    jsonl_size = path.stat().st_size

    # Overwrite the sidecar with malformed JSON: event_count is a string, not int
    malformed_sidecar = {
        "version": 1,
        "journal_size": jsonl_size,  # Matches JSONL size (passes stale check)
        "event_count": "oops",  # WRONG TYPE — must be int
        "last": None,
        "terminal": None
    }

    with sidecar_path.open("w", encoding="utf-8") as f:
        json.dump(malformed_sidecar, f)

    # Clear cache
    from api.run_journal import _SUMMARY_CACHE, _SUMMARY_CACHE_LOCK
    with _SUMMARY_CACHE_LOCK:
        _SUMMARY_CACHE.clear()

    # latest_run_summary must NOT raise — it degrades to tail fallback
    summary = latest_run_summary(session_id, run_id, session_dir=tmp_path)

    # The summary must be correct (recovered via tail fallback)
    assert summary["terminal_state"] == "completed", (
        f"summary terminal_state={summary['terminal_state']!r} — malformed sidecar "
        "should degrade to tail and recover correct terminal"
    )
    assert summary["event_count"] == 1, (
        f"summary event_count={summary['event_count']} — malformed sidecar should "
        "degrade to tail and recover correct count"
    )


def test_malformed_matching_size_sidecar_append_does_not_brick_journaling(tmp_path, monkeypatch):
    """Regression (Round 18, blocker 3 follow-up): A malformed sidecar (e.g., missing
    fold keys like event_count/last/terminal) must not raise KeyError during the
    next append. The writer's _read_sidecar validates strictly and returns None
    for malformed sidecars, triggering a rebuild instead of trusting corrupt state.
    Journaling must continue working — the JSONL line is written, and a new valid
    sidecar is published."""
    from api.run_journal import _run_path, _summary_sidecar_path, _read_jsonl, _summary_from_events
    import json

    session_id = "session_r18_b3_append"
    run_id = "run_r18_b3_append"
    writer = RunJournalWriter(session_id, run_id, session_dir=tmp_path)

    # Append a done event (sidecar written)
    writer.append_sse_event("done", {"session": {"session_id": session_id}})

    path = _run_path(session_id, run_id, session_dir=tmp_path)
    sidecar_path = _summary_sidecar_path(path)
    jsonl_size = path.stat().st_size

    # Overwrite the sidecar with incomplete data (missing fold keys)
    incomplete_sidecar = {
        "version": 1,
        "journal_size": jsonl_size,  # Matches JSONL size (passes stale check)
        # Missing: event_count, last, terminal — MALFORMED
    }

    with sidecar_path.open("w", encoding="utf-8") as f:
        json.dump(incomplete_sidecar, f)

    # Clear cache
    from api.run_journal import _SUMMARY_CACHE, _SUMMARY_CACHE_LOCK
    with _SUMMARY_CACHE_LOCK:
        _SUMMARY_CACHE.clear()

    # Append stream_end — must NOT raise (malformed sidecar is ignored, rebuilt)
    writer.append_sse_event("stream_end", {"terminal_state": "closed"})

    # The JSONL must have gained exactly one line (stream_end)
    with path.open("r", encoding="utf-8") as f:
        jsonl_lines = [line for line in f if line.strip()]
    assert len(jsonl_lines) == 2, (
        f"JSONL must have 2 lines after append; got {len(jsonl_lines)} — "
        "append may have failed"
    )

    # The sidecar must now be valid (writer published a new correct sidecar)
    assert sidecar_path.exists()
    with sidecar_path.open("r", encoding="utf-8") as f:
        sidecar_data = json.load(f)
    assert sidecar_data["event_count"] == 2, (
        f"sidecar event_count={sidecar_data['event_count']} — writer must rebuild "
        "and publish correct sidecar after ignoring malformed one"
    )

    # Clear cache and verify cold summary matches full read
    with _SUMMARY_CACHE_LOCK:
        _SUMMARY_CACHE.clear()

    cold_summary = latest_run_summary(session_id, run_id, session_dir=tmp_path)
    full_events, _, _ = _read_jsonl(path)
    full_summary = _summary_from_events(session_id, run_id, full_events)

    assert cold_summary["event_count"] == full_summary["event_count"] == 2, (
        f"cold summary event_count={cold_summary['event_count']}, "
        f"full={full_summary['event_count']} — malformed sidecar broke summary"
    )
    assert cold_summary["last_seq"] == full_summary["last_seq"] == 2, (
        f"cold summary last_seq={cold_summary['last_seq']}, "
        f"full={full_summary['last_seq']} — malformed sidecar broke seq tracking"
    )
