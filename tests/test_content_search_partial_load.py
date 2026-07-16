"""Tests for partial-message loading in session content search.

Regression: ``GET /api/sessions/search?content=1`` scans only the first
``depth`` messages (default 5) per session, but used to call ``get_session()``
which materializes the ENTIRE transcript (often hundreds of KB to multiple MB)
just to read ~5 messages. A content search across many large sessions was a
per-request memory spike proportional to total transcript bytes.

``Session.load_messages_head(sid, limit)`` now streams the on-disk JSON and
peels off only the first ``limit`` elements of the ``messages`` array via
``json.raw_decode`` — the message tail and ``anchor_activity_scenes`` bodies are
never parsed. The search handler calls it instead of ``get_session()``.
"""
from __future__ import annotations

from collections import OrderedDict
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import urlparse


# ── Session fixture ──────────────────────────────────────────────────────────

_SESSION_ID = "sess-search-partial-1234"


def _write_session_file(session_dir, *, total_messages, first_payloads=None):
    """Write a real session JSON to ``session_dir`` and return its path.

    The metadata carries ``message_count`` (the true total) and the messages
    array is followed by a large ``anchor_activity_scenes`` body — mirroring the
    production on-disk layout. ``first_payloads`` overrides the content of the
    leading messages so a test can place a needle precisely.
    """
    from api.models import Session
    from pathlib import Path

    session_dir = Path(session_dir)
    session_dir.mkdir(parents=True, exist_ok=True)
    messages = []
    for i in range(total_messages):
        content = (
            first_payloads[i]
            if first_payloads and i < len(first_payloads)
            else f"padding message number {i} " * 4
        )
        messages.append({"id": f"m{i}", "role": "user", "content": content})
    sess = Session(session_id=_SESSION_ID, title="Partial search test", messages=messages)
    # Round-trip through save() so the on-disk layout (metadata prefix with
    # message_count, then messages, then anchor_activity_scenes) is exactly the
    # production shape load_messages_head must stream over.
    sess.save()
    return session_dir / f"{_SESSION_ID}.json"


# ── load_messages_head unit tests ────────────────────────────────────────────


def test_load_messages_head_returns_only_first_n(tmp_path, monkeypatch):
    """load_messages_head(limit=N) returns exactly the first N messages and the
    true total_count, WITHOUT parsing the rest of the transcript."""
    import api.models as models

    session_dir = tmp_path / "sessions"
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSIONS", OrderedDict())
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", session_dir / "_index.json")
    _write_session_file(session_dir, total_messages=200)

    head, total = models.Session.load_messages_head(_SESSION_ID, limit=5)
    assert total == 200  # true on-disk count from the metadata prefix
    assert len(head) == 5
    # First 5 are the leading messages, in order.
    assert [m["id"] for m in head] == [f"m{i}" for i in range(5)]


def test_load_messages_head_does_not_parse_tail(tmp_path, monkeypatch):
    """THE regression proof: a 5000-message session (multi-MB tail) must return
    only 5 messages from load_messages_head — proving the tail is never
    materialized. Before the fix, get_session() parsed all 5000."""
    import api.models as models

    session_dir = tmp_path / "sessions"
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSIONS", OrderedDict())
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", session_dir / "_index.json")
    # 5000 messages with non-trivial bodies → a large file (the kind that used
    # to spike memory on every content-search poll).
    _write_session_file(session_dir, total_messages=5000)

    head, total = models.Session.load_messages_head(_SESSION_ID, limit=5)
    assert total == 5000
    assert len(head) == 5
    # The tail (messages 5..4999) was never decoded into the result.
    assert all(m["id"] in {f"m{i}" for i in range(5)} for m in head)


def test_load_messages_head_limit_zero_returns_all(tmp_path, monkeypatch):
    """limit=0 means 'no cap' (the search handler's depth==0 'whole transcript'
    opt-in branch). load_messages_head should still stream correctly and return
    everything (no truncation)."""
    import api.models as models

    session_dir = tmp_path / "sessions"
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSIONS", OrderedDict())
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", session_dir / "_index.json")
    _write_session_file(session_dir, total_messages=20)

    head, total = models.Session.load_messages_head(_SESSION_ID, limit=0)
    assert total == 20
    assert len(head) == 20


def test_load_messages_head_large_metadata_prefix(tmp_path, monkeypatch):
    """Regression: when the pre-``messages`` metadata exceeds one read chunk
    (~16 KiB), the streaming scanner used to lose brace-depth context across
    chunk boundaries and silently return ``([], total_count)`` — dropping all
    content-search matches for those sessions. The fallback to a full load must
    be layout-anomaly-driven (array never opened), not gated on total_count
    (which modern sessions always supply from message_count)."""
    import json as _json
    import api.models as models
    from pathlib import Path

    session_dir = tmp_path / "sessions"
    session_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSIONS", OrderedDict())
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", session_dir / "_index.json")

    # Stuff anchor_scene_index with enough content that the serialized metadata
    # BEFORE the "messages" key exceeds READ_CHUNK (16384 bytes).
    big_scene = {f"assistant-{i}": {"hash": "x" * 200} for i in range(120)}
    sid = "large-meta-session"
    doc = {
        "session_id": sid, "title": "t", "created_at": 1.0, "updated_at": 1.0,
        "message_count": 3,
        "anchor_scene_index": big_scene,
        "messages": [
            {"id": "m0", "role": "user", "content": "NEEDLE in first message"},
            {"id": "m1", "role": "assistant", "content": "reply"},
            {"id": "m2", "role": "user", "content": "third"},
        ],
        "tool_calls": [],
        "anchor_activity_scenes": {},
    }
    raw = _json.dumps(doc, ensure_ascii=False, indent=2)
    (session_dir / f"{sid}.json").write_text(raw, encoding="utf-8")
    # Sanity: the metadata prefix really is over one read chunk.
    assert raw.index('"messages"') > 16384

    head, total = models.Session.load_messages_head(sid, limit=5)
    # Before the fix this returned ([], 3) — silently dropping all matches.
    assert total == 3
    assert len(head) == 3
    assert head[0]["content"] == "NEEDLE in first message"
    assert [m["id"] for m in head] == ["m0", "m1", "m2"]


# ── Route integration tests ──────────────────────────────────────────────────


def _run_search(query, session_dir, *, first_payloads, total_messages):
    """Invoke _handle_sessions_search against one real on-disk session."""
    import api.models as models
    import api.routes as routes

    monkey_SESSION_DIR = session_dir
    models.SESSION_DIR = session_dir
    models.SESSIONS = OrderedDict()
    routes.SESSION_DIR = session_dir

    _write_session_file(
        session_dir, total_messages=total_messages, first_payloads=first_payloads
    )

    sessions_meta = [{"session_id": _SESSION_ID, "title": "Untitled", "profile": "default"}]
    captured = {}

    def fake_j(handler, payload, status=200, extra_headers=None):
        captured["status"] = status
        captured["payload"] = payload

    with patch("api.routes.all_sessions", return_value=list(sessions_meta)), patch(
        "api.profiles.get_active_profile_name", return_value="default"
    ), patch("api.routes.j", side_effect=fake_j):
        routes._handle_sessions_search(SimpleNamespace(), urlparse(query))
    return captured


def test_content_search_finds_match_in_first_n_messages(tmp_path, monkeypatch):
    """A needle in message #1 (within the default depth=5 head) is found —
    proving the partial loader feeds the search correctly."""
    import api.models as models

    session_dir = tmp_path / "sessions"
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    captured = _run_search(
        "/api/sessions/search?q=NEEDLE&content=1",
        session_dir,
        first_payloads=[
            "hello world",
            "the NEEDLE is here in message one",
            "third message",
        ],
        total_messages=300,  # huge tail the old code would fully parse
    )
    assert captured["status"] == 200
    results = captured["payload"]["sessions"]
    assert len(results) == 1
    assert results[0]["match_type"] == "content"
    assert "NEEDLE" in results[0].get("match_preview", "")


def test_content_search_miss_when_needle_beyond_depth(tmp_path, monkeypatch):
    """A needle ONLY in a message beyond `depth` must NOT be found — the
    correctness boundary of the head truncation. (depth default is 5.)"""
    import api.models as models

    session_dir = tmp_path / "sessions"
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    # Needle is at index 50, far beyond depth=5.
    payloads = {50: "DEEPNEEDLE deep in the transcript"}
    first_payloads = [payloads.get(i, f"padding {i}") for i in range(60)]
    captured = _run_search(
        "/api/sessions/search?q=DEEPNEEDLE&content=1&depth=5",
        session_dir,
        first_payloads=first_payloads,
        total_messages=60,
    )
    assert captured["status"] == 200
    results = captured["payload"]["sessions"]
    # No title match, and the content match is beyond depth → not found.
    assert len(results) == 0


def test_content_search_depth_zero_scans_whole_transcript(tmp_path, monkeypatch):
    """depth=0 keeps the 'search the whole transcript' meaning: a deep needle
    IS found (the opt-in full-load branch)."""
    import api.models as models

    session_dir = tmp_path / "sessions"
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    payloads = {25: "WHOLENEEDLE buried deep"}
    first_payloads = [payloads.get(i, f"padding {i}") for i in range(40)]
    captured = _run_search(
        "/api/sessions/search?q=WHOLENEEDLE&content=1&depth=0",
        session_dir,
        first_payloads=first_payloads,
        total_messages=40,
    )
    assert captured["status"] == 200
    results = captured["payload"]["sessions"]
    assert len(results) == 1
    assert results[0]["match_type"] == "content"
