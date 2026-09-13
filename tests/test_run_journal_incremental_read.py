"""RC2: the run journal read path must be incremental when continuity is proven.

During a live turn the journal grows on every streamed event, so every summary
invalidation forced a full re-read + re-parse of the whole file, and
``_run_journal_live_snapshot`` paid it twice per request (``find_run_summary``
+ ``read_run_events``). The incremental read cache in ``_read_jsonl`` must:

- return EXACTLY the historical rows (``_read_jsonl_legacy`` is the oracle);
- reuse the parsed prefix only when the append-only watermark continuity is
  verified (same inode, no shrink, byte-identical overlap before the watermark,
  first new row continues ``seq == last_seq + 1`` with a consistent envelope);
- fall back to the full read on any uncertainty (truncate, rotate, same-size
  rewrite, seq gap, malformed first tail row, undecodable bytes);
- treat a trailing partial line (writer mid-append) exactly like the historical
  reader did for that read, without consuming it — no loss, no duplication;
- keep ``find_run_summary``/``read_run_events`` results and shapes unchanged.
"""

import json

import pytest

from api import run_journal as RJ


@pytest.fixture(autouse=True)
def _clean_read_cache():
    with RJ._JOURNAL_READ_CACHE_LOCK:
        RJ._JOURNAL_READ_CACHE.clear()
        for key in list(RJ._JOURNAL_READ_PATH_STATS):
            RJ._JOURNAL_READ_PATH_STATS[key] = 0
    yield
    with RJ._JOURNAL_READ_CACHE_LOCK:
        RJ._JOURNAL_READ_CACHE.clear()


def _journal_path(tmp_path, session_id="session_1", run_id="run_1"):
    return tmp_path / "_run_journal" / session_id / f"{run_id}.jsonl"


def _append(session_dir, run_id="run_1", session_id="session_1", name="token", payload=None):
    return RJ.append_run_event(session_id, run_id, name, payload or {"text": "x"}, session_dir=session_dir)


def _stats():
    with RJ._JOURNAL_READ_CACHE_LOCK:
        return dict(RJ._JOURNAL_READ_PATH_STATS)


# ---------------------------------------------------------------------------
# Journal idle invariato: zero re-parse, risultato identico.
# ---------------------------------------------------------------------------

def test_idle_journal_repeated_reads_are_identical_and_zero_parse(tmp_path):
    _append(tmp_path, payload={"text": "uno"})
    _append(tmp_path, name="done", payload={"session": {}})

    first = RJ.read_run_events("session_1", "run_1", session_dir=tmp_path)
    assert [event["seq"] for event in first["events"]] == [1, 2]

    stats_before = _stats()
    for _ in range(4):
        repeated = RJ.read_run_events("session_1", "run_1", session_dir=tmp_path)
        assert repeated == first
    stats_after = _stats()
    assert stats_after["fast"] > stats_before["fast"], "unchanged journal must hit the watermark fast path"
    assert stats_after["full"] == stats_before["full"], "unchanged journal must never re-read from disk"


def test_idle_summary_reuse_and_find_run_summary_shape(tmp_path):
    _append(tmp_path, payload={"text": "uno"})
    _append(tmp_path, name="done", payload={"session": {}})

    first = RJ.find_run_summary("run_1", session_dir=tmp_path)
    assert first["session_id"] == "session_1"
    assert first["event_count"] == 2
    assert first["last_seq"] == 2
    assert first["terminal_state"] == "completed"
    assert first["path"].endswith("run_1.jsonl")
    again = RJ.find_run_summary("run_1", session_dir=tmp_path)
    assert again == first


# ---------------------------------------------------------------------------
# Journal durante streaming: append + letture consecutive, nessuna perdita
# o duplicazione, parità con la lettura full da zero.
# ---------------------------------------------------------------------------

def test_streaming_appends_are_read_incrementally_without_loss_or_duplication(tmp_path):
    for _ in range(3):
        _append(tmp_path)
    first = RJ.read_run_events("session_1", "run_1", session_dir=tmp_path)
    assert [event["seq"] for event in first["events"]] == [1, 2, 3]
    assert _stats()["full"] == 1 and _stats()["tail"] == 0

    for _ in range(4):
        _append(tmp_path)
    second = RJ.read_run_events("session_1", "run_1", session_dir=tmp_path)
    seqs = [event["seq"] for event in second["events"]]
    assert seqs == [1, 2, 3, 4, 5, 6, 7], "append-only growth must extend, never duplicate"
    assert _stats()["tail"] >= 1, "grown journal must parse only its tail"
    assert _stats()["full"] == 1, "verified append continuity must not force a full re-read"

    # Oracle: a cold full read of the same file must return exactly the same rows.
    with RJ._JOURNAL_READ_CACHE_LOCK:
        RJ._JOURNAL_READ_CACHE.clear()
    oracle = RJ.read_run_events("session_1", "run_1", session_dir=tmp_path)
    assert [event["event_id"] for event in oracle["events"]] == [
        event["event_id"] for event in second["events"]
    ]
    assert oracle["events"] == second["events"]


def test_consecutive_requests_during_one_stream_stay_contiguous(tmp_path):
    expected = []
    for turn in range(5):
        event = _append(tmp_path, payload={"text": f"tok{turn}"})
        expected.append(event["seq"])
        journal = RJ.read_run_events("session_1", "run_1", session_dir=tmp_path)
        assert [e["seq"] for e in journal["events"]] == expected
        summary = RJ.find_run_summary("run_1", session_dir=tmp_path)
        assert summary["event_count"] == len(expected)
        assert summary["last_seq"] == expected[-1]
        assert summary["terminal"] is False
    assert _stats()["full"] == 1, "only the cold read may parse the whole file"


def test_summary_and_read_agree_after_appends(tmp_path):
    _append(tmp_path)
    RJ.read_run_events("session_1", "run_1", session_dir=tmp_path)
    _append(tmp_path, name="done", payload={"session": {}})
    summary = RJ.find_run_summary("run_1", session_dir=tmp_path)
    assert summary["terminal"] is True and summary["terminal_state"] == "completed"
    assert summary["event_count"] == 2
    journal = RJ.read_run_events("session_1", "run_1", session_dir=tmp_path)
    assert journal["events"][-1]["event"] == "done"


def test_read_run_events_filters_over_cached_rows(tmp_path):
    for _ in range(4):
        _append(tmp_path)
    RJ.read_run_events("session_1", "run_1", session_dir=tmp_path)  # warm cache
    _append(tmp_path)
    journal = RJ.read_run_events("session_1", "run_1", session_dir=tmp_path, after_seq=2, max_seq=4)
    assert [event["seq"] for event in journal["events"]] == [3, 4]


def test_next_seq_seeding_uses_cached_rows_without_reparsing(tmp_path):
    _append(tmp_path)
    RJ.read_run_events("session_1", "run_1", session_dir=tmp_path)
    event = _append(tmp_path)  # seeds/reserves via the same read cache
    assert event["seq"] == 2
    journal = RJ.read_run_events("session_1", "run_1", session_dir=tmp_path)
    assert [event["seq"] for event in journal["events"]] == [1, 2]


# ---------------------------------------------------------------------------
# Ultima riga parziale: riportata come la lettura storica, mai consumata.
# ---------------------------------------------------------------------------

def test_partial_last_line_is_reported_then_parsed_once_when_complete(tmp_path):
    _append(tmp_path)
    path = _journal_path(tmp_path)
    first = RJ.read_run_events("session_1", "run_1", session_dir=tmp_path)
    assert [event["seq"] for event in first["events"]] == [1]

    with path.open("a", encoding="utf-8") as fh:
        fh.write('{"version":1,"seq":2,"run_id":"run_1","session_id":"session_1","event":"tok')

    during = RJ.read_run_events("session_1", "run_1", session_dir=tmp_path)
    legacy_during = RJ._read_jsonl_legacy(path)
    assert during["events"] == legacy_during[0], "partial fragment must be handled like the historical reader"
    assert during["malformed"] == legacy_during[1]
    assert len(during["events"]) == 1
    assert len(during["malformed"]) == 1 and during["malformed"][0]["line"] == 2

    with path.open("a", encoding="utf-8") as fh:
        fh.write('","payload":{"text":"fin"}}\n')

    completed = RJ.read_run_events("session_1", "run_1", session_dir=tmp_path)
    assert [event["seq"] for event in completed["events"]] == [1, 2], "completed line parsed exactly once"
    assert completed["malformed"] == [], "transient malformed row must disappear once the line completes"

    legacy = RJ._read_jsonl_legacy(path)
    assert completed["events"] == legacy[0] and completed["malformed"] == legacy[1]


def test_partial_line_that_stays_partial_never_leaks_into_cache(tmp_path):
    _append(tmp_path)
    path = _journal_path(tmp_path)
    with path.open("a", encoding="utf-8") as fh:
        fh.write('{"torn":')
    for _ in range(3):
        journal = RJ.read_run_events("session_1", "run_1", session_dir=tmp_path)
        assert [event["seq"] for event in journal["events"]] == [1]
        assert len(journal["malformed"]) == 1
    with path.open("a", encoding="utf-8") as fh:
        fh.write('1}\n')
    # The completed torn row is valid JSON ("{"torn":1}), so it becomes a row:
    # it must appear exactly once (no duplication of the transient fragment).
    journal = RJ.read_run_events("session_1", "run_1", session_dir=tmp_path)
    assert sum(1 for e in journal["events"] if e.get("torn") == 1) == 1
    legacy = RJ._read_jsonl_legacy(path)
    assert journal["events"] == legacy[0] and journal["malformed"] == legacy[1]


# ---------------------------------------------------------------------------
# Fallback a full read: ogni incertezza deve tornare al percorso storico.
# ---------------------------------------------------------------------------

def test_truncated_journal_falls_back_and_returns_shrunk_content(tmp_path):
    _append(tmp_path)
    _append(tmp_path)
    path = _journal_path(tmp_path)
    assert len(RJ.read_run_events("session_1", "run_1", session_dir=tmp_path)["events"]) == 2

    original = path.read_bytes()
    path.write_bytes(original[: len(original) // 2])  # same inode, shrunk

    stats_before = _stats()
    shrunk = RJ.read_run_events("session_1", "run_1", session_dir=tmp_path)
    assert _stats()["full"] > stats_before["full"], "shrink must fall back to the full read"
    assert len(shrunk["events"]) <= 2
    legacy = RJ._read_jsonl_legacy(path)
    assert shrunk["events"] == legacy[0] and shrunk["malformed"] == legacy[1]


def test_rotated_journal_new_inode_falls_back_to_full_read(tmp_path):
    _append(tmp_path, run_id="run_1")
    path = _journal_path(tmp_path, run_id="run_1")
    assert len(RJ.read_run_events("session_1", "run_1", session_dir=tmp_path)["events"]) == 1

    # Rotation = delete + recreate with fresh content (new inode).
    path.unlink()
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({
            "version": 1, "event_id": "run_1:1", "seq": 1, "run_id": "run_1",
            "session_id": "session_1", "event": "done", "type": "done",
            "created_at": 1.0, "terminal": True, "terminal_state": "completed",
            "payload": {},
        }) + "\n")

    stats_before = _stats()
    rotated = RJ.read_run_events("session_1", "run_1", session_dir=tmp_path)
    assert _stats()["full"] > stats_before["full"], "new inode must fall back to the full read"
    assert [event["event"] for event in rotated["events"]] == ["done"]


def test_same_size_rewrite_falls_back_to_full_read(tmp_path):
    _append(tmp_path, payload={"text": "aaaa"})
    path = _journal_path(tmp_path)
    assert len(RJ.read_run_events("session_1", "run_1", session_dir=tmp_path)["events"]) == 1

    # Same inode, same size, different bytes (and advanced ctime).
    content = path.read_bytes()
    replaced = content.replace(b'"aaaa"', b'"bbbb"')
    assert len(replaced) == len(content)
    path.write_bytes(replaced)

    stats_before = _stats()
    rewritten = RJ.read_run_events("session_1", "run_1", session_dir=tmp_path)
    assert _stats()["full"] > stats_before["full"], "same-size rewrite must fall back"
    assert rewritten["events"][0]["payload"]["text"] == "bbbb"


def test_seq_gap_in_tail_falls_back_but_keeps_rows(tmp_path):
    _append(tmp_path)
    path = _journal_path(tmp_path)
    assert len(RJ.read_run_events("session_1", "run_1", session_dir=tmp_path)["events"]) == 1

    gap_row = {
        "version": 1, "event_id": "run_1:5", "seq": 5, "run_id": "run_1",
        "session_id": "session_1", "event": "token", "type": "token",
        "created_at": 2.0, "terminal": False, "terminal_state": None,
        "payload": {"text": "gap"},
    }
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(gap_row) + "\n")

    stats_before = _stats()
    journal = RJ.read_run_events("session_1", "run_1", session_dir=tmp_path)
    assert _stats()["full"] > stats_before["full"], "seq gap must fall back to the full read"
    assert [event["seq"] for event in journal["events"]] == [1, 5], "fallback must not drop rows"


def test_malformed_first_tail_row_falls_back_to_full_read(tmp_path):
    _append(tmp_path)
    path = _journal_path(tmp_path)
    RJ.read_run_events("session_1", "run_1", session_dir=tmp_path)
    with path.open("a", encoding="utf-8") as fh:
        fh.write("{not json}\n")

    stats_before = _stats()
    journal = RJ.read_run_events("session_1", "run_1", session_dir=tmp_path)
    assert _stats()["full"] > stats_before["full"]
    assert len(journal["events"]) == 1 and len(journal["malformed"]) == 1


def test_undecodable_tail_falls_back_to_full_read_like_history(tmp_path):
    _append(tmp_path)
    path = _journal_path(tmp_path)
    RJ.read_run_events("session_1", "run_1", session_dir=tmp_path)
    with path.open("ab") as fh:
        fh.write(b"\xff\xfe\n")

    legacy_exc = None
    try:
        RJ._read_jsonl_legacy(path)
    except UnicodeDecodeError as exc:  # historical behavior: the decode error propagates
        legacy_exc = exc
    assert legacy_exc is not None
    with pytest.raises(UnicodeDecodeError):
        RJ.read_run_events("session_1", "run_1", session_dir=tmp_path)


def test_exotic_line_break_parity_with_historical_reader(tmp_path):
    _append(tmp_path)
    path = _journal_path(tmp_path)
    RJ.read_run_events("session_1", "run_1", session_dir=tmp_path)
    # A raw U+2028 inside a payload splits under str.splitlines(): the
    # incremental reader must reproduce the historical (malformed) rows, not
    # silently become more lenient.
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"version": 1, "seq": 2, "event": "token"}, ensure_ascii=False) + "\n")

    journal = RJ.read_run_events("session_1", "run_1", session_dir=tmp_path)
    legacy = RJ._read_jsonl_legacy(path)
    assert journal["events"] == legacy[0]
    assert journal["malformed"] == legacy[1]


# ---------------------------------------------------------------------------
# Cache fredda / nuovo processo: la prima lettura è full e corretta.
# ---------------------------------------------------------------------------

def test_cold_cache_first_read_is_full_and_correct(tmp_path):
    for _ in range(3):
        _append(tmp_path)
    stats_before = _stats()
    journal = RJ.read_run_events("session_1", "run_1", session_dir=tmp_path)
    assert _stats()["full"] == stats_before["full"] + 1
    assert [event["seq"] for event in journal["events"]] == [1, 2, 3]


def test_cache_is_per_path_and_isolated(tmp_path):
    _append(tmp_path, run_id="run_1")
    _append(tmp_path, run_id="run_2")
    first = RJ.read_run_events("session_1", "run_1", session_dir=tmp_path)
    second = RJ.read_run_events("session_1", "run_2", session_dir=tmp_path)
    _append(tmp_path, run_id="run_1")
    _append(tmp_path, run_id="run_2")
    first = RJ.read_run_events("session_1", "run_1", session_dir=tmp_path)
    second = RJ.read_run_events("session_1", "run_2", session_dir=tmp_path)
    assert [event["seq"] for event in first["events"]] == [1, 2]
    assert [event["seq"] for event in second["events"]] == [1, 2]
    assert first["run_id"] == "run_1" and second["run_id"] == "run_2"


def test_missing_journal_reads_empty_and_cache_stays_clean(tmp_path):
    journal = RJ.read_run_events("session_1", "run_1", session_dir=tmp_path)
    assert journal["events"] == [] and journal["malformed"] == []
    _append(tmp_path)
    journal = RJ.read_run_events("session_1", "run_1", session_dir=tmp_path)
    assert [event["seq"] for event in journal["events"]] == [1]
