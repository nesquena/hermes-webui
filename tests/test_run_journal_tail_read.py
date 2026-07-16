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

