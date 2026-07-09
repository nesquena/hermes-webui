"""Regression tests — the session index is parsed from bytes, not a decoded str.

`/api/sessions` and its rebuild path read the session index (a multi-MB JSON
list on a busy install) with `json.loads(SESSION_INDEX_FILE.read_text(
encoding='utf-8'))`. That decodes the whole file to a Python `str` first, then
re-scans it in the JSON parser. Handing the raw `bytes` to `json.loads` lets its
C parser decode UTF-8 in one pass — measured ~22% faster on a real 6.2 MB index
(46 ms -> 36 ms), on every cache-miss rebuild.

`json.loads` accepts `bytes` and auto-detects the (UTF-8) encoding, producing an
identical object for our BOM-less index. These pin the equivalence (including
non-ASCII content) and that no index reader silently reverts to the slower
decoded-string path.
"""
import json
from pathlib import Path

import api.models as models
import api.routes as routes


def test_bytes_and_text_parse_identically(tmp_path: Path):
    """A UTF-8 index with non-ASCII titles parses identically from bytes."""
    index = [
        {"session_id": "s1", "title": "Grüße 🌍", "last_message_at": 1.0},
        {"session_id": "s2", "title": "日本語のセッション", "last_message_at": 2.0},
    ]
    idx = tmp_path / "_index.json"
    idx.write_text(json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8")

    from_text = json.loads(idx.read_text(encoding="utf-8"))
    from_bytes = json.loads(idx.read_bytes())
    assert from_bytes == from_text == index


def test_all_sessions_reads_unicode_index_via_bytes(tmp_path, monkeypatch):
    """End-to-end: all_sessions reads a non-ASCII index correctly through the
    bytes path (a str/bytes decode mismatch would corrupt or drop titles)."""
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    idx = session_dir / "_index.json"
    entries = [
        {"session_id": "unic-1", "title": "Café ☕", "last_message_at": 10.0},
    ]
    idx.write_text(json.dumps(entries, ensure_ascii=False), encoding="utf-8")

    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", idx)
    # Treat the indexed id as persisted so the prune step keeps it.
    monkeypatch.setattr(models, "_persisted_session_ids_snapshot", lambda: frozenset({"unic-1"}))
    monkeypatch.setattr(models, "_active_stream_ids", lambda: set())

    result = models.all_sessions(include_lineage_metadata=False)

    titles = {s.get("session_id"): s.get("title") for s in result}
    assert titles.get("unic-1") == "Café ☕"


def test_no_index_reader_uses_decoded_str(tmp_path):
    """Guard against a refactor reintroducing the slower read_text path on the
    session index."""
    for module in (models, routes):
        src = Path(module.__file__).read_text(encoding="utf-8")
        assert "SESSION_INDEX_FILE.read_text" not in src, (
            f"{module.__name__} reads the session index via read_text; "
            "use read_bytes so json.loads parses UTF-8 in one pass"
        )
        assert "session_index_file.read_text" not in src
