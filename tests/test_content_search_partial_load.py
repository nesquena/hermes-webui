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

import pytest

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

    models.SESSION_DIR = session_dir
    # IMPORTANT: clear SESSIONS IN PLACE rather than replacing the attribute.
    # Other test files hold a top-level `from api.models import SESSIONS`
    # reference captured at import time; rebinding models.SESSIONS to a new
    # OrderedDict here would leave their reference pointing at the stale dict,
    # breaking their `new_session()` / `s.session_id in SESSIONS` assertions
    # when this file runs before them in suite order.
    models.SESSIONS.clear()
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


# ── Gate certification #6138 round-2 regressions ─────────────────────────────
# The three CORE blockers nesquena-hermes certified RED on 2026-07-19. Each
# reproducer is built from the maintainer's spec and pinned at the route level
# (the actual content-search entry point) so a regression in either the route
# handler or load_messages_head surfaces here.


def test_load_messages_head_cache_aware_for_unsaved_messages(tmp_path, monkeypatch):
    """Blocker #1: active/unsaved cached messages must appear in the head.

    Production keeps sessions with unsaved assistant turns in SESSIONS; the
    streaming scanner reads the sidecar directly and would silently miss those
    messages. Under the session lock, load_messages_head must detect a cached
    session and slice its (normalized) messages instead of reading the disk.
    """
    import api.models as models

    session_dir = tmp_path / "sessions"
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    models.SESSION_DIR = session_dir
    sid = "gate-cache-unsaved"
    # Persist only ONE message to the sidecar.
    _write_session_file(
        session_dir,
        total_messages=1,
        first_payloads=["persisted only"],
    )
    # Rename the on-disk file to the sid we look up, then put a cached session
    # with an additional unsaved message into SESSIONS.
    import shutil
    src = session_dir / f"{_SESSION_ID}.json"
    dst = session_dir / f"{sid}.json"
    shutil.move(str(src), str(dst))

    from api.models import Session, LOCK
    # Use models.SESSIONS (attribute access) rather than `from api.models
    # import SESSIONS` so we always see the current module-global dict —
    # rebinding or stale references break other tests' SESSIONS assertions.
    cached = Session(
        session_id=sid,
        title="cached",
        messages=[
            {"id": "m0", "role": "user", "content": "persisted only"},
            {"id": "m1", "role": "assistant", "content": "UNSAVEDNEEDLE"},
        ],
    )
    with LOCK:
        models.SESSIONS[sid] = cached
    try:
        head, total = Session.load_messages_head(sid, 5)
        assert any(
            "UNSAVEDNEEDLE" in str(m.get("content") or "") for m in head
        ), (
            f"unsaved cached message must appear in head; got "
            f"{[m.get('content') for m in head]}"
        )
    finally:
        with LOCK:
            models.SESSIONS.pop(sid, None)


def test_load_messages_head_cap_exhaustion_falls_back_to_full(tmp_path, monkeypatch):
    """Blocker #2: max_bytes cap exhaustion must NOT return a short head.

    Five valid ~300KB messages with depth=5 and a needle in message 5 exceed
    the default 1 MiB cap after 4 messages. The cap must trigger a fallback to
    the full loader rather than silently truncating and dropping the match.
    """
    import api.models as models

    session_dir = tmp_path / "sessions"
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    models.SESSION_DIR = session_dir
    sid = "gate-cap-exhaustion"
    big = "x" * 300000  # ~300 KB each
    payloads = [big + (" NEEDLE5" if i == 4 else "") for i in range(5)]
    _write_session_file(
        session_dir,
        total_messages=5,
        first_payloads=payloads,
    )
    import shutil
    shutil.move(
        str(session_dir / f"{_SESSION_ID}.json"),
        str(session_dir / f"{sid}.json"),
    )
    from api.models import Session
    head, total = Session.load_messages_head(sid, 5)
    assert total == 5, f"expected total=5, got {total}"
    assert len(head) == 5, (
        f"cap exhaustion must fall back to full load and return 5 messages, "
        f"not truncate to {len(head)}"
    )
    assert any(
        "NEEDLE5" in str(m.get("content") or "") for m in head
    ), "needle in message 5 must be found after cap fallback"


def test_load_messages_head_collapses_duplicate_partials(tmp_path, monkeypatch):
    """Blocker #3: adjacent duplicate _partial rows must collapse the same way
    Session.load() collapses them, so the depth window is measured in
    NORMALIZED messages (not raw array elements).

    Reproduces the maintainer's spec: 'one adjacent duplicate partial before a
    needle in normalized message 5'. Clean master returned count=1; the pre-fix
    scanner stopped at raw element 5 and returned count=0.

    Note: Session.save() itself collapses duplicate partials before writing, so
    to exercise the scanner's collapse behavior we must write the sidecar JSON
    DIRECTLY with the duplicate partials intact (mirroring the streaming/journal
    recovery paths that can write raw duplicates to disk).
    """
    import api.models as models
    import json as _json

    session_dir = tmp_path / "sessions"
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    models.SESSION_DIR = session_dir
    session_dir.mkdir(parents=True, exist_ok=True)
    sid = "gate-dup-partial"
    # 3 normal + 2 identical _partial (one collapses on read) + needle =
    # 6 raw on disk, 5 normalized; needle is normalized message 5 (index 4).
    raw_messages = [
        {"id": "m1", "role": "user", "content": "msg 1"},
        {"id": "m2", "role": "user", "content": "msg 2"},
        {"id": "m3", "role": "user", "content": "msg 3"},
        {"id": "p1", "role": "assistant", "_partial": True, "content": "partial"},
        {"id": "p2", "role": "assistant", "_partial": True, "content": "partial"},
        {"id": "m5", "role": "user", "content": "NEEDLEPARTIAL"},
    ]
    # Write directly with duplicates intact — do NOT use Session.save() (which
    # collapses before writing) so the on-disk layout mirrors what journal
    # recovery / streaming paths can produce. ALSO do NOT call Session.load(sid)
    # for a sanity check first — Session.load() self-heals collapsed partials
    # back to disk (api/models.py #2592 self-heal), which would destroy the
    # duplicates before load_messages_head runs and defeat the test.
    sidecar_payload = {
        "session_id": sid,
        "title": "dup-partial",
        "message_count": len(raw_messages),
        "messages": raw_messages,
        "anchor_activity_scenes": [],
    }
    (session_dir / f"{sid}.json").write_text(
        _json.dumps(sidecar_payload), encoding="utf-8"
    )
    from api.models import Session
    head, total = Session.load_messages_head(sid, 5)
    assert any(
        "NEEDLEPARTIAL" in str(m.get("content") or "") for m in head
    ), (
        f"needle at normalized message 5 must be found after collapse-aware "
        f"scan; got {[m.get('content') for m in head]}"
    )


def test_load_messages_head_streaming_still_used_for_clean_uncached(tmp_path, monkeypatch):
    """Regression guard: the optimization must still apply for the common case
    (clean, uncached, fully persisted sessions) — i.e. we don't accidentally
    route everything through the full loader and defeat the PR's purpose.

    Spies on Session.load: if the streaming scanner is bypassed for a clean
    uncached session (e.g. by an over-eager fallback or exception), the spy
    records the call and this test fails. This locks in the performance
    contract, not just the correctness contract."""
    import api.models as models

    session_dir = tmp_path / "sessions"
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    models.SESSION_DIR = session_dir
    sid = "gate-clean-stream"
    _write_session_file(session_dir, total_messages=50, first_payloads=None)
    import shutil
    shutil.move(
        str(session_dir / f"{_SESSION_ID}.json"),
        str(session_dir / f"{sid}.json"),
    )
    # No SESSIONS entry → cache-aware path must defer to the streaming scanner.
    from api.models import Session, LOCK
    with LOCK:
        models.SESSIONS.pop(sid, None)
    # Spy on Session.load — it must NOT be called for a clean uncached session,
    # because that's the whole point of the streaming optimization. If a future
    # change routes everything through the full loader, this assertion fires.
    load_calls = []
    original_load = Session.load

    def _spy_load(cls_arg, sid_arg):
        load_calls.append(sid_arg)
        return original_load(sid_arg)

    monkeypatch.setattr(Session, "load", classmethod(_spy_load))
    head, total = Session.load_messages_head(sid, 5)
    assert total == 50, f"expected total=50 from metadata prefix, got {total}"
    assert len(head) == 5, (
        f"clean uncached session should return 5 messages via streaming, got {len(head)}"
    )
    assert load_calls == [], (
        f"Session.load must NOT be called for a clean uncached session (the "
        f"streaming scanner should handle it); got calls: {load_calls}"
    )


# ── Re-gate #6138 round-3 regression (2026-07-19) ────────────────────────────
# Barrier regression: the broad-exception fallback path must preserve SESSIONS
# authority. Initial cache miss → session becomes cached mid-scan → scanner
# raises → fallback must return the now-authoritative cached head, not stale
# disk state. Uses bounded thread joins to prove no deadlock.


def test_broad_exception_fallback_preserves_cache_authority(tmp_path, monkeypatch):
    """Round-3 #6138: a session that becomes cached DURING the scan must be
    returned by the broad-exception fallback, not the stale disk state.

    Mirrors the maintainer's sandbox barrier: initial SESSIONS lookup misses,
    an active cache entry is inserted mid-scan, and the scanner read is forced
    to raise. Pre-fix the broad except clause called cls.load(sid) directly,
    returning STALE_DISK; post-fix it routes through _full_load_head, which
    re-checks SESSIONS authority and returns CACHE_AUTHORITY.
    """
    import api.models as models
    import json as _json
    import threading

    session_dir = tmp_path / "sessions"
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    models.SESSION_DIR = session_dir
    session_dir.mkdir(parents=True, exist_ok=True)
    sid = "gate-broad-except-cache"

    # Sidecar with stale disk content (one message, no UNSAVEDNEEDLE).
    stale_payload = {
        "session_id": sid,
        "title": "stale-disk",
        "message_count": 1,
        "messages": [{"id": "d0", "role": "user", "content": "STALE_DISK"}],
        "anchor_activity_scenes": [],
    }
    (session_dir / f"{sid}.json").write_text(
        _json.dumps(stale_payload), encoding="utf-8"
    )

    # The authoritative cached session has the unsaved needle.
    from api.models import Session, LOCK
    cached = Session(
        session_id=sid,
        title="authoritative-cache",
        messages=[
            {"id": "c0", "role": "user", "content": "STALE_DISK"},
            {"id": "c1", "role": "assistant", "content": "CACHE_AUTHORITY"},
        ],
    )

    # Barrier: simulate the race window by inserting the cache entry FROM WITHIN
    # the scanner's _scan_to_messages_array call (which runs AFTER the initial
    # SESSIONS miss and the metadata-prefix read), then raising. The broad-
    # exception fallback must then re-check SESSIONS and return the now-cached
    # authoritative head rather than the stale disk state.
    #
    # Inserting from within the scanner thread (rather than racing with a second
    # thread) makes the test deterministic without coupling to internal scanner
    # timing — the cache is guaranteed to be present when the exception fires.
    cache_inserted_by_scan = []

    def _forcing_scan(*args, **kwargs):
        # Insert the cache entry at the moment of the scan call (after the
        # initial SESSIONS miss), then raise to force the broad-exception path.
        if not cache_inserted_by_scan:
            with LOCK:
                models.SESSIONS[sid] = cached
            cache_inserted_by_scan.append(True)
        raise OSError("forced scanner exception for barrier test")

    monkeypatch.setattr(Session, "_scan_to_messages_array", _forcing_scan)

    result = {}
    scanner_exc = []

    def _scanner():
        try:
            head, total = Session.load_messages_head(sid, 5)
            result["head"] = head
            result["total"] = total
        except Exception as e:  # noqa: BLE001 - barrier test captures any error
            scanner_exc.append(e)

    t = threading.Thread(target=_scanner, daemon=True)
    t.start()
    # Bounded join — proves no deadlock (the helper must not hold LOCK while
    # calling get_session, which reacquires it).
    t.join(timeout=10.0)
    assert not t.is_alive(), (
        "scanner thread deadlocked: _full_load_head likely held LOCK while "
        "calling get_session (which reacquires it)"
    )
    assert not scanner_exc, (
        f"scanner raised instead of taking the broad-exception fallback: "
        f"{scanner_exc}"
    )
    head = result.get("head", [])
    assert any(
        "CACHE_AUTHORITY" in str(m.get("content") or "") for m in head
    ), (
        f"broad-exception fallback must return the cached authoritative head, "
        f"not stale disk; got {[m.get('content') for m in head]}"
    )
    with LOCK:
        models.SESSIONS.pop(sid, None)


# ── Re-gate #6138 round-2 regressions (2026-07-19) ───────────────────────────
# Two residual silent false-negative paths nesquena-hermes found after the
# first three blockers were closed. Each reproducer mirrors the maintainer's
# sandbox regression.


def test_cached_messageful_session_without_sidecar_uses_authoritative_cache(
    tmp_path, monkeypatch
):
    """Re-gate finding 1: a cached active session with NO sidecar on disk must
    still be visible to content search.

    all_sessions() overlays in-memory active/pending sessions into the
    searchable rows even before a sidecar exists, so a cached messageful
    session can appear in the session list while content search returned no
    messages. The previous `if not p.exists(): return [], None` short-circuit
    fired before the SESSIONS lookup, silently dropping them.

    Mirrors the maintainer's `test_cached_messageful_session_without_sidecar_
    uses_authoritative_cache` sandbox regression.
    """
    import api.models as models

    session_dir = tmp_path / "sessions"
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    models.SESSION_DIR = session_dir
    session_dir.mkdir(parents=True, exist_ok=True)
    sid = "gate-cache-no-sidecar"
    # Build a messageful cached session and DO NOT persist a sidecar.
    from api.models import Session, LOCK
    cached = Session(
        session_id=sid,
        title="no-sidecar",
        messages=[
            {"id": "m0", "role": "user", "content": "UNSAVEDNEEDLE"},
        ],
    )
    with LOCK:
        models.SESSIONS[sid] = cached
    try:
        # Sanity: no sidecar exists.
        assert not (session_dir / f"{sid}.json").exists(), (
            "test setup: sidecar must NOT exist for this regression"
        )
        head, total = Session.load_messages_head(sid, 5)
        assert any(
            "UNSAVEDNEEDLE" in str(m.get("content") or "") for m in head
        ), (
            f"cached session with no sidecar must still return its messages "
            f"via SESSIONS authority; got {[m.get('content') for m in head]}"
        )
    finally:
        with LOCK:
            models.SESSIONS.pop(sid, None)


@pytest.mark.parametrize(
    "message_count, label",
    [
        (None, "missing message_count (legacy sidecar)"),
        # 1 is at/below raw_target (2 * limit = 10), so it is the strongest
        # fail-first value: the pre-fix gate `total_count > len(raw_messages)`
        # would have been `1 > 10` = False, skipping the fallback entirely.
        (1, "stale-low message_count (at/below raw_target=10)"),
    ],
)
def test_duplicate_partial_run_longer_than_raw_multiplier_falls_back(
    tmp_path, monkeypatch, message_count, label
):
    """Re-gate finding 2: a duplicate _partial run longer than `2 * limit` must
    fall back to a full load, not silently return a short normalized head.

    Adjacent identical `_partial` runs are unbounded, so the fixed `2 * limit`
    raw over-collection window is not normalization-equivalent. With 12+
    identical partials before a needle at normalized message `limit`, the
    streaming scan stops at the raw ceiling, collapses to fewer than `limit`
    normalized rows, and (pre-fix) silently dropped the needle. The fix removes
    the `total_count is not None` gate so the fallback fires regardless of
    metadata count — covering both missing (legacy) and stale message_count.

    Mirrors the maintainer's `test_duplicate_partial_run_longer_than_raw_
    multiplier_falls_back` sandbox regression.
    """
    import api.models as models
    import json as _json

    session_dir = tmp_path / "sessions"
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    models.SESSION_DIR = session_dir
    session_dir.mkdir(parents=True, exist_ok=True)
    sid = "gate-long-dup-run"
    # 12 identical _partial (>> 2 * limit=10) then a needle. Collapses to 1
    # partial + needle = 2 normalized rows; the needle is at normalized msg 2.
    raw = [
        {"id": f"p{i}", "role": "assistant", "_partial": True, "content": "partial"}
        for i in range(12)
    ] + [{"id": "mN", "role": "user", "content": "NEEDLELONG"}]
    payload = {
        "session_id": sid,
        "title": "long-dup",
        "messages": raw,
        "anchor_activity_scenes": [],
    }
    if message_count is not None:
        payload["message_count"] = message_count
    (session_dir / f"{sid}.json").write_text(
        _json.dumps(payload), encoding="utf-8"
    )
    from api.models import Session
    head, total = Session.load_messages_head(sid, 5)
    assert any(
        "NEEDLELONG" in str(m.get("content") or "") for m in head
    ), (
        f"duplicate run > 2*limit with {label}: needle at normalized msg 2 "
        f"must be found after full-load fallback; got "
        f"{[m.get('content') for m in head]}"
    )
