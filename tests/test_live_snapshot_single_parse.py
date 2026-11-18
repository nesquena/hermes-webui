"""Regression: live-snapshot rebuild locates the journal with a single parse.

``_run_journal_live_snapshot`` used ``find_run_summary`` (full parse) and
then ``read_run_events`` (second full parse) - two passes over the whole
journal per rebuild (~0.6s+ of a 4.4s rebuild on a 22.7k-row live run).
``find_run_file`` now locates the file without parsing and the durable
summary is derived from the single events parse; the historical
``find_run_summary`` lookup remains as the fallback seam when no journal
file exists on disk.
"""

import json

import api.routes as routes
from api import run_journal as RJ


def _write_journal(root, session_id, run_id, count=5):
    session_root = root / RJ.RUN_JOURNAL_DIR_NAME / session_id
    session_root.mkdir(parents=True, exist_ok=True)
    path = session_root / f"{run_id}.jsonl"
    rows = []
    for seq in range(1, count + 1):
        rows.append(
            json.dumps(
                {
                    "version": 1,
                    "event_id": f"{run_id}:{seq}",
                    "seq": seq,
                    "run_id": run_id,
                    "session_id": session_id,
                    "event": "token",
                    "type": "token",
                    "created_at": 1000 + seq,
                    "payload": {"text": "x"},
                }
            )
        )
    path.write_text("\n".join(rows) + "\n", encoding="utf-8")
    return path


def test_find_run_file_locates_without_parsing(tmp_path):
    path = _write_journal(tmp_path, "session_1", "run_1")
    located = RJ.find_run_file("run_1", session_dir=tmp_path)
    assert located is not None
    session_id, located_path = located
    assert session_id == "session_1"
    assert located_path == path
    assert RJ.find_run_file("missing_run", session_dir=tmp_path) is None
    assert RJ.find_run_file("../evil", session_dir=tmp_path) is None


def _forbid_summary_parse(*_a, **_k):
    raise AssertionError(
        "find_run_summary must not be parsed when the journal file exists on "
        "disk - the single-parse locate path regressed"
    )


def test_live_snapshot_rebuilds_from_single_parse(tmp_path, monkeypatch):
    _write_journal(tmp_path, "session_1", "run_1", count=3)
    monkeypatch.setattr(RJ, "_default_session_dir", lambda: tmp_path)
    # The double-parse fix: when a journal file exists, the rebuild must NOT
    # route through find_run_summary (that would parse the file a second time).
    monkeypatch.setattr(routes, "find_run_summary", _forbid_summary_parse)

    snapshot = routes._run_journal_live_snapshot("run_1")

    assert snapshot is not None
    assert snapshot["session_id"] == "session_1"
    assert snapshot["stream_id"] == "run_1"
    assert snapshot["last_seq"] == 3
    assert snapshot["event_count"] == 3
    assert snapshot["last_assistant_text"] == "xxx"


def test_live_snapshot_summary_fallback_seam(tmp_path, monkeypatch):
    """No journal file on disk -> the historical find_run_summary seam is used."""
    monkeypatch.setattr(RJ, "_default_session_dir", lambda: tmp_path)
    monkeypatch.setattr(
        routes,
        "find_run_summary",
        lambda stream_id: {
            "session_id": "session_1",
            "run_id": stream_id,
            "last_seq": 1,
            "last_event_id": f"{stream_id}:1",
        },
    )
    monkeypatch.setattr(
        routes,
        "read_run_events",
        lambda sid, rid: {
            "events": [
                {
                    "version": 1,
                    "event_id": f"{rid}:1",
                    "seq": 1,
                    "run_id": rid,
                    "session_id": sid,
                    "event": "token",
                    "type": "token",
                    "created_at": 1000,
                    "payload": {"text": "hello"},
                }
            ],
        },
    )

    snapshot = routes._run_journal_live_snapshot("run_1")

    assert snapshot is not None
    assert snapshot["last_assistant_text"] == "hello"