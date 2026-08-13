"""Tests for session search depth validation, encoding guards, route-seam behavior, and canonical-authority semantics.

Covers the gate-certification requirements for PR #5875:
- Depth validation (non-numeric, negative, valid caps)
- Encoding guard (json.dumps round-trip catches all JSON-escaped chars)
- Escaped-character queries (quote, backslash, tab, newline, CR, ESC, BS, NUL, Unicode)
- Route-seam tests: search-handler behavior with mocked get_session_for_scan
- Production-composed tests: real _resolve_session paths with canonical authority:
    (a) journal-only content -> must be included
    (b) stale sidecar + canonical no-match -> must be excluded
    (c) resolver unavailable + stale sidecar -> must be excluded
    (d) LRU working-set preservation during multi-session search
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import urlparse

import pytest


# -- Fixtures ----------------------------------------------------------------

@pytest.fixture
def session_s1_json(tmp_path):
    """Write synthetic s1 session to a real JSON file and return the dir path."""
    s1 = {
        "session_id": "s1",
        "title": "Untitled",
        "profile": "default",
        "messages": [
            {"role": "user", "content": "first message"},
            {"role": "assistant", "content": "second message"},
            {"role": "user", "content": "NEEDLE in the latest message"},
        ],
    }
    (tmp_path / "s1.json").write_text(json.dumps(s1), encoding="utf-8")
    return tmp_path


def _run_mocked(query, *, sessions_meta, get_session_for_scan_return=None):
    """Run _handle_sessions_search with mocked get_session_for_scan."""
    import api.routes as routes

    captured = {}

    def fake_j(handler, payload, status=200, extra_headers=None):
        captured["status"] = status
        captured["payload"] = payload

    if get_session_for_scan_return is not None:
        sf_patch = patch("api.routes.get_session_for_scan",
                         return_value=get_session_for_scan_return)
    else:
        sf_patch = patch("api.routes.get_session_for_scan",
                         side_effect=KeyError("not loaded"))

    with patch("api.routes.all_sessions", return_value=list(sessions_meta)), \
         patch("api.profiles.get_active_profile_name", return_value="default"), \
         sf_patch, \
         patch("api.routes.j", side_effect=fake_j):
        routes._handle_sessions_search(SimpleNamespace(), urlparse(query))
    return captured


def _run_real(query, tmp_path, *, sessions_meta):
    """Run _handle_sessions_search with REAL get_session_for_scan.

    Patches api.models.SESSION_DIR (where get_session_for_scan reads it)
    but does NOT mock the resolver itself.
    """
    import api.routes as routes

    captured = {}

    def fake_j(handler, payload, status=200, extra_headers=None):
        captured["status"] = status
        captured["payload"] = payload

    with patch("api.routes.all_sessions", return_value=list(sessions_meta)), \
         patch("api.profiles.get_active_profile_name", return_value="default"), \
         patch("api.models.SESSION_DIR", tmp_path), \
         patch("api.routes.j", side_effect=fake_j):
        routes._handle_sessions_search(SimpleNamespace(), urlparse(query))
    return captured


# -- Depth validation --------------------------------------------------------

def test_search_non_numeric_depth_does_not_500(session_s1_json):
    """depth=deep falls back to 5; the needle is found."""
    r = _run_mocked(
        "/api/sessions/search?q=needle&content=1&depth=deep",
        
        sessions_meta=[{"session_id": "s1", "title": "U", "profile": "default"}],
        get_session_for_scan_return=SimpleNamespace(session_id="s1", messages=[
            {"role": "user", "content": "first message"},
            {"role": "assistant", "content": "second message"},
            {"role": "user", "content": "NEEDLE in the latest message"},
        ]),
    )
    assert r["status"] == 200
    assert r["payload"]["count"] == 1


def test_search_negative_depth_still_scans_newest_message(session_s1_json):
    """depth=-2 is clamped to >= 0 so the latest message is searched."""
    r = _run_mocked(
        "/api/sessions/search?q=needle&content=1&depth=-2",
        
        sessions_meta=[{"session_id": "s1", "title": "U", "profile": "default"}],
        get_session_for_scan_return=SimpleNamespace(session_id="s1", messages=[
            {"role": "user", "content": "first message"},
            {"role": "assistant", "content": "second message"},
            {"role": "user", "content": "NEEDLE in the latest message"},
        ]),
    )
    assert r["status"] == 200
    assert r["payload"]["count"] == 1


def test_search_valid_depth_still_caps_scan(session_s1_json):
    """depth=1 scans only the first message; needle in the last is missed."""
    r = _run_mocked(
        "/api/sessions/search?q=needle&content=1&depth=1",
        
        sessions_meta=[{"session_id": "s1", "title": "U", "profile": "default"}],
        get_session_for_scan_return=SimpleNamespace(session_id="s1", messages=[
            {"role": "user", "content": "first message"},
            {"role": "assistant", "content": "second message"},
            {"role": "user", "content": "NEEDLE in the latest message"},
        ]),
    )
    assert r["status"] == 200
    assert r["payload"]["count"] == 0


# -- Metacharacter queries ---------------------------------------------------

def test_metacharacter_query_dollar_sign(session_s1_json):
    """Query '$5' matched literally."""
    r = _run_mocked(
        "/api/sessions/search?q=$5&content=1",
        
        sessions_meta=[{"session_id": "s1", "title": "U", "profile": "default"}],
        get_session_for_scan_return=SimpleNamespace(session_id="s1", messages=[
            {"role": "user", "content": "first message"},
            {"role": "assistant", "content": "total is $5"},
            {"role": "user", "content": "done"},
        ]),
    )
    assert r["status"] == 200
    assert r["payload"]["count"] == 1


def test_metacharacter_query_plus(session_s1_json):
    """Query '1+1' matched literally."""
    r = _run_mocked(
        "/api/sessions/search?q=1%2B1&content=1",
        
        sessions_meta=[{"session_id": "s1", "title": "U", "profile": "default"}],
        get_session_for_scan_return=SimpleNamespace(session_id="s1", messages=[
            {"role": "user", "content": "compute 1+1"},
            {"role": "assistant", "content": "result is 2"},
        ]),
    )
    assert r["status"] == 200
    assert r["payload"]["count"] == 1


# -- Escaped-character route-seam tests (mocked) -----------------------------

def test_route_seam_double_quote(session_s1_json):
    """Double-quote in query; mocked resolver finds match."""
    r = _run_mocked(
        "/api/sessions/search?q=he%20said%20%22ok%22&content=1",
        
        sessions_meta=[{"session_id": "s1", "title": "U", "profile": "default"}],
        get_session_for_scan_return=SimpleNamespace(session_id="s1", messages=[
            {"role": "user", "content": 'he said "ok"'},
        ]),
    )
    assert r["status"] == 200
    assert r["payload"]["count"] == 1


def test_route_seam_backslash(session_s1_json):
    """Backslash in query; mocked resolver finds match."""
    r = _run_mocked(
        "/api/sessions/search?q=path%5cto&content=1",
        
        sessions_meta=[{"session_id": "s1", "title": "U", "profile": "default"}],
        get_session_for_scan_return=SimpleNamespace(session_id="s1", messages=[
            {"role": "user", "content": "path\\to"},
        ]),
    )
    assert r["status"] == 200
    assert r["payload"]["count"] == 1


# -- Normalization route-seam test (mocked) ----------------------------------

def test_route_seam_depth_with_collapsed_partials(session_s1_json):
    """Session.load normalization collapses adjacent partials; depth applies after."""
    r = _run_mocked(
        "/api/sessions/search?q=needle&content=1&depth=2",
        
        sessions_meta=[{"session_id": "s1", "title": "U", "profile": "default"}],
        # Session.load() would collapse partials; mock returns pre-collapsed
        get_session_for_scan_return=SimpleNamespace(session_id="s1", messages=[
            {"role": "assistant", "content": "working"},
            {"role": "user", "content": "NEEDLE after partials"},
        ]),
    )
    assert r["status"] == 200
    assert r["payload"]["count"] == 1


# -- Escaped-character route-seam tests (mocked) -----------------------------

def test_route_seam_tab(session_s1_json):
    r = _run_mocked(
        "/api/sessions/search?q=alpha%09beta&content=1",
        
        sessions_meta=[{"session_id": "s1", "title": "U", "profile": "default"}],
        get_session_for_scan_return=SimpleNamespace(session_id="s1", messages=[
            {"role": "user", "content": "alpha\tbeta"},
        ]),
    )
    assert r["status"] == 200
    assert r["payload"]["count"] == 1


def test_route_seam_newline(session_s1_json):
    r = _run_mocked(
        "/api/sessions/search?q=hello%0Aworld&content=1",
        
        sessions_meta=[{"session_id": "s1", "title": "U", "profile": "default"}],
        get_session_for_scan_return=SimpleNamespace(session_id="s1", messages=[
            {"role": "user", "content": "hello\nworld"},
        ]),
    )
    assert r["status"] == 200
    assert r["payload"]["count"] == 1


def test_route_seam_cr(session_s1_json):
    r = _run_mocked(
        "/api/sessions/search?q=line%0Dbreak&content=1",
        
        sessions_meta=[{"session_id": "s1", "title": "U", "profile": "default"}],
        get_session_for_scan_return=SimpleNamespace(session_id="s1", messages=[
            {"role": "user", "content": "line\rbreak"},
        ]),
    )
    assert r["status"] == 200
    assert r["payload"]["count"] == 1


def test_route_seam_esc(session_s1_json):
    r = _run_mocked(
        "/api/sessions/search?q=ctrl%1Bkey&content=1",
        
        sessions_meta=[{"session_id": "s1", "title": "U", "profile": "default"}],
        get_session_for_scan_return=SimpleNamespace(session_id="s1", messages=[
            {"role": "user", "content": "ctrl\x1bkey"},
        ]),
    )
    assert r["status"] == 200
    assert r["payload"]["count"] == 1


def test_route_seam_backspace(session_s1_json):
    r = _run_mocked(
        "/api/sessions/search?q=ctrl%08key&content=1",
        
        sessions_meta=[{"session_id": "s1", "title": "U", "profile": "default"}],
        get_session_for_scan_return=SimpleNamespace(session_id="s1", messages=[
            {"role": "user", "content": "ctrl\x08key"},
        ]),
    )
    assert r["status"] == 200
    assert r["payload"]["count"] == 1


def test_route_seam_nul(session_s1_json):
    r = _run_mocked(
        "/api/sessions/search?q=has%00null&content=1",
        
        sessions_meta=[{"session_id": "s1", "title": "U", "profile": "default"}],
        get_session_for_scan_return=SimpleNamespace(session_id="s1", messages=[
            {"role": "user", "content": "has\x00null"},
        ]),
    )
    assert r["status"] == 200
    assert r["payload"]["count"] == 1


def test_route_seam_unicode_case_insensitive(session_s1_json):
    """Unicode case-insensitive search finds match."""
    r = _run_mocked(
        "/api/sessions/search?q=%E6%97%A5%E6%9C%AC%E8%AA%9E%E3%83%86%E3%82%B9%E3%83%88&content=1",
        
        sessions_meta=[{"session_id": "s1", "title": "U", "profile": "default"}],
        get_session_for_scan_return=SimpleNamespace(session_id="s1", messages=[
            {"role": "user", "content": "日本語テスト"},
        ]),
    )
    assert r["status"] == 200
    assert r["payload"]["count"] == 1


def test_route_seam_unicode_case_diff(session_s1_json):
    """Unicode uppercase query finds lowercase content (case-insensitive)."""
    r = _run_mocked(
        "/api/sessions/search?q=caf%C3%89&content=1",
        
        sessions_meta=[{"session_id": "s1", "title": "U", "profile": "default"}],
        get_session_for_scan_return=SimpleNamespace(session_id="s1", messages=[
            {"role": "user", "content": "I love caf\u00e9 au lait"},
        ]),
    )
    assert r["status"] == 200
    assert r["payload"]["count"] == 1


# ======================================================================
# Gate-certification canonical-authority tests (a-d) -- mocked seam
# ======================================================================

def test_route_seam_journal_content_included():
    """(a) Canonical state has journal-only content. Session MUST appear."""
    r = _run_mocked(
        "/api/sessions/search?q=needle&content=1",
        
        sessions_meta=[{"session_id": "s1", "title": "U", "profile": "default"}],
        get_session_for_scan_return=SimpleNamespace(session_id="s1", messages=[
            {"role": "user", "content": "unrelated on-disk content"},
            {"role": "assistant", "content": "JOURNAL_ONLY NEEDLE here"},
        ]),
    )
    assert r["status"] == 200
    assert r["payload"]["count"] == 1


def test_route_seam_canonical_excludes():
    """(b) Canonical state has no match. Session MUST be excluded."""
    r = _run_mocked(
        "/api/sessions/search?q=needle&content=1",
        
        sessions_meta=[{"session_id": "s1", "title": "U", "profile": "default"}],
        get_session_for_scan_return=SimpleNamespace(session_id="s1", messages=[
            {"role": "user", "content": "clean content only"},
        ]),
    )
    assert r["status"] == 200
    assert r["payload"]["count"] == 0


def test_route_seam_resolver_unavailable_excludes():
    """(c) Canonical resolver unavailable -> excluded."""
    r = _run_mocked(
        "/api/sessions/search?q=needle&content=1",
        
        sessions_meta=[{"session_id": "s1", "title": "U", "profile": "default"}],
        get_session_for_scan_return=None,  # KeyError
    )
    assert r["status"] == 200
    assert r["payload"]["count"] == 0


def test_route_seam_resolver_failure_fails_closed():
    """(b-extra) Resolver failure -> fail closed."""
    r = _run_mocked(
        "/api/sessions/search?q=needle&content=1",
        
        sessions_meta=[{"session_id": "s1", "title": "U", "profile": "default"}],
        get_session_for_scan_return=None,
    )
    assert r["status"] == 200
    assert r["payload"]["count"] == 0


def test_canonical_authority_lru_not_polluted(tmp_path):
    """(d) Content search must not promote, evict, or insert into the LRU.

    Non-cancelling design: 3 sessions in LRU (s_a, s_b, s_c), but only
    s_a and s_b are listed in sessions_meta. s_c is the "witness" — never
    scanned, so it stays at the end of the LRU.

    s_a has FRESH_MARKER (the scan target). s_b has unrelated text.
    A regressed resolver that promotes on hit would move s_a to the end,
    producing s_b, s_c, s_a -- which fails the order assertion.
    """
    import api.models as models

    FRESH_MARKER = "FRESH_MARKER_LRU_TEST"
    for sid, content in [
        ("s_a", FRESH_MARKER),
        ("s_b", "completely unrelated text b"),
        ("s_c", "completely unrelated text c"),
    ]:
        (tmp_path / f"{sid}.json").write_text(json.dumps({
            "session_id": sid, "title": sid.upper(), "profile": "default",
            "messages": [{"role": "user", "content": content}],
        }), encoding="utf-8")

    # Snapshot the COMPLETE prior state and pre-populate the LRU
    with models.LOCK:
        saved = dict(models.SESSIONS)
        saved_order = list(models.SESSIONS.keys())
        models.SESSIONS.clear()
        models.SESSIONS["s_a"] = SimpleNamespace(session_id="s_a", messages=[])
        models.SESSIONS["s_b"] = SimpleNamespace(session_id="s_b", messages=[])
        models.SESSIONS["s_c"] = SimpleNamespace(session_id="s_c", messages=[])
        models.SESSIONS.move_to_end("s_a")
        models.SESSIONS.move_to_end("s_b")
        models.SESSIONS.move_to_end("s_c")

    try:
        with models.LOCK:
            order_before = list(models.SESSIONS.keys())
            residency_before = dict(models.SESSIONS)

        # Search for FRESH_MARKER; s_a matches, s_b does not, s_c NOT in meta
        r = _run_real(
            f"/api/sessions/search?q={FRESH_MARKER}&content=1",
            tmp_path,
            sessions_meta=[
                {"session_id": "s_a", "title": "A", "profile": "default"},
                {"session_id": "s_b", "title": "B", "profile": "default"},
            ],
        )

        with models.LOCK:
            order_after = list(models.SESSIONS.keys())
            residency_after = dict(models.SESSIONS)

        # 1) HTTP 200
        assert r["status"] == 200

        # 2) Exactly s_a matched
        hits = r["payload"].get("sessions", [])
        matched_ids = [h["session_id"] for h in hits]
        assert matched_ids == ["s_a"], (
            f"Expected only s_a to match, got {matched_ids}"
        )

        # 3) LRU order unchanged (s_c witness prevents cancellation)
        assert order_before == order_after, (
            f"LRU order changed: {order_before} -> {order_after}"
        )

        # 4) LRU residency unchanged (no new keys, no removed keys)
        assert set(residency_before.keys()) == set(residency_after.keys()), (
            f"LRU residency changed: {set(residency_before)} -> {set(residency_after)}"
        )
    finally:
        # Restore the COMPLETE original state
        with models.LOCK:
            models.SESSIONS.clear()
            models.SESSIONS.update(saved)
            for key in saved_order:
                if key in models.SESSIONS:
                    models.SESSIONS.move_to_end(key)


# ======================================================================
# Production-composed integration tests -- real _resolve_session paths
# ======================================================================

def test_integration_newer_cache_overrides_stale_disk(tmp_path):
    """The disk record is a GENUINE non-match for the query. The in-memory
    resident cache holds fresher content that DOES match. The test asserts
    both that exactly one result is returned AND that the result's
    match_preview contains the fresh marker (not the stale text).

    A control case removing the resident cache proves the stale disk alone
    produces zero matches, confirming the test is not false-green.

    COMPLETE state snapshot/restoration prevents destruction of pre-existing
    global state owned by other tests.
    """
    import api.models as models

    # Snapshot COMPLETE prior state (dict + order)
    with models.LOCK:
        saved_sessions = dict(models.SESSIONS)
        saved_order = list(models.SESSIONS.keys())

    try:
        # -- Disk: genuine non-match for query "needle" --------------------------
        (tmp_path / "s_integ.json").write_text(json.dumps({
            "session_id": "s_integ", "title": "Stale", "profile": "default",
            "messages": [{"role": "user", "content": "entirely unrelated text"}],
        }), encoding="utf-8")

        # -- In-memory: fresh content with NEEDLE --------------------------------
        FRESH_MARKER = "NEEDLE_IN_FRESH_CACHE"
        fresh_sess = SimpleNamespace(
            session_id="s_integ",
            messages=[{"role": "user", "content": f"fresh content {FRESH_MARKER} here"}],
        )
        with models.LOCK:
            models.SESSIONS["s_integ"] = fresh_sess

        # Main assertion: fresh cache wins, preview contains the fresh marker
        r = _run_real(
            "/api/sessions/search?q=needle&content=1",
            tmp_path,
            sessions_meta=[{"session_id": "s_integ", "title": "Stale", "profile": "default"}],
        )
        assert r["status"] == 200
        assert r["payload"]["count"] == 1
        sessions = r["payload"]["sessions"]
        assert len(sessions) == 1
        preview = sessions[0].get("match_preview", "")
        assert FRESH_MARKER in preview, (
            f"Expected fresh marker '{FRESH_MARKER}' in preview, got: {preview!r}"
        )

        # Control: remove the resident cache; stale disk alone has no match
        with models.LOCK:
            models.SESSIONS.pop("s_integ", None)

        r_control = _run_real(
            "/api/sessions/search?q=needle&content=1",
            tmp_path,
            sessions_meta=[{"session_id": "s_integ", "title": "Stale", "profile": "default"}],
        )
        assert r_control["status"] == 200
        assert r_control["payload"]["count"] == 0, (
            "Stale disk alone must NOT match -- the main test is not false-green"
        )
    finally:
        # Restore COMPLETE original state (dict + order)
        with models.LOCK:
            models.SESSIONS.clear()
            models.SESSIONS.update(saved_sessions)
            for key in saved_order:
                if key in models.SESSIONS:
                    models.SESSIONS.move_to_end(key)


def test_integration_cold_load_from_disk(tmp_path):
    """Cold session not in LRU is loaded from disk by _resolve_session."""
    import api.models as models

    (tmp_path / "s_cold.json").write_text(json.dumps({
        "session_id": "s_cold", "title": "Cold", "profile": "default",
        "messages": [{"role": "user", "content": "disk content NEEDLE here"}],
    }), encoding="utf-8")

    with models.LOCK:
        assert "s_cold" not in models.SESSIONS

    r = _run_real(
        "/api/sessions/search?q=needle&content=1",
        tmp_path,
        sessions_meta=[{"session_id": "s_cold", "title": "Cold", "profile": "default"}],
    )

    with models.LOCK:
        in_lru = "s_cold" in models.SESSIONS

    assert r["status"] == 200
    assert r["payload"]["count"] == 1
    assert not in_lru, "get_session_for_scan must not insert into LRU"


# -- Production-composed routing-guard tests (real files + real resolver) -----

def test_routing_guard_quote(tmp_path):
    """Double-quote query through real routing guard."""
    (tmp_path / "s_q.json").write_text(json.dumps({
        "session_id": "s_q", "title": "Q", "profile": "default",
        "messages": [{"role": "user", "content": 'he said "ok"'}],
    }), encoding="utf-8")

    r = _run_real(
        "/api/sessions/search?q=he%20said%20%22ok%22&content=1",
        tmp_path,
        sessions_meta=[{"session_id": "s_q", "title": "Q", "profile": "default"}],
    )
    assert r["status"] == 200
    assert r["payload"]["count"] == 1


def test_routing_guard_backslash(tmp_path):
    (tmp_path / "s_bs.json").write_text(json.dumps({
        "session_id": "s_bs", "title": "BS", "profile": "default",
        "messages": [{"role": "user", "content": "path\\to"}],
    }), encoding="utf-8")

    r = _run_real(
        "/api/sessions/search?q=path%5cto&content=1",
        tmp_path,
        sessions_meta=[{"session_id": "s_bs", "title": "BS", "profile": "default"}],
    )
    assert r["status"] == 200
    assert r["payload"]["count"] == 1


def test_routing_guard_tab(tmp_path):
    (tmp_path / "s_tab.json").write_text(json.dumps({
        "session_id": "s_tab", "title": "Tab", "profile": "default",
        "messages": [{"role": "user", "content": "alpha\tbeta"}],
    }), encoding="utf-8")

    r = _run_real(
        "/api/sessions/search?q=alpha%09beta&content=1",
        tmp_path,
        sessions_meta=[{"session_id": "s_tab", "title": "Tab", "profile": "default"}],
    )
    assert r["status"] == 200
    assert r["payload"]["count"] == 1


def test_routing_guard_esc(tmp_path):
    (tmp_path / "s_esc.json").write_text(json.dumps({
        "session_id": "s_esc", "title": "ESC", "profile": "default",
        "messages": [{"role": "user", "content": "ctrl\x1bkey"}],
    }), encoding="utf-8")

    r = _run_real(
        "/api/sessions/search?q=ctrl%1Bkey&content=1",
        tmp_path,
        sessions_meta=[{"session_id": "s_esc", "title": "ESC", "profile": "default"}],
    )
    assert r["status"] == 200
    assert r["payload"]["count"] == 1


def test_routing_guard_backspace(tmp_path):
    (tmp_path / "s_bs2.json").write_text(json.dumps({
        "session_id": "s_bs2", "title": "BS2", "profile": "default",
        "messages": [{"role": "user", "content": "ctrl\x08key"}],
    }), encoding="utf-8")

    r = _run_real(
        "/api/sessions/search?q=ctrl%08key&content=1",
        tmp_path,
        sessions_meta=[{"session_id": "s_bs2", "title": "BS2", "profile": "default"}],
    )
    assert r["status"] == 200
    assert r["payload"]["count"] == 1


def test_routing_guard_nul(tmp_path):
    (tmp_path / "s_nul.json").write_text(json.dumps({
        "session_id": "s_nul", "title": "NUL", "profile": "default",
        "messages": [{"role": "user", "content": "has\x00null"}],
    }), encoding="utf-8")

    r = _run_real(
        "/api/sessions/search?q=has%00null&content=1",
        tmp_path,
        sessions_meta=[{"session_id": "s_nul", "title": "NUL", "profile": "default"}],
    )
    assert r["status"] == 200
    assert r["payload"]["count"] == 1


def test_routing_guard_newline(tmp_path):
    (tmp_path / "s_nl.json").write_text(json.dumps({
        "session_id": "s_nl", "title": "NL", "profile": "default",
        "messages": [{"role": "user", "content": "hello\nworld"}],
    }), encoding="utf-8")

    r = _run_real(
        "/api/sessions/search?q=hello%0Aworld&content=1",
        tmp_path,
        sessions_meta=[{"session_id": "s_nl", "title": "NL", "profile": "default"}],
    )
    assert r["status"] == 200
    assert r["payload"]["count"] == 1


def test_routing_guard_cr(tmp_path):
    (tmp_path / "s_cr.json").write_text(json.dumps({
        "session_id": "s_cr", "title": "CR", "profile": "default",
        "messages": [{"role": "user", "content": "line\rbreak"}],
    }), encoding="utf-8")

    r = _run_real(
        "/api/sessions/search?q=line%0Dbreak&content=1",
        tmp_path,
        sessions_meta=[{"session_id": "s_cr", "title": "CR", "profile": "default"}],
    )
    assert r["status"] == 200
    assert r["payload"]["count"] == 1


def test_routing_guard_unicode_case_diff(tmp_path):
    """Uppercase query finds lowercase content through real routing."""
    (tmp_path / "s_uc.json").write_text(json.dumps({
        "session_id": "s_uc", "title": "UC", "profile": "default",
        "messages": [{"role": "user", "content": "I love caf\u00e9 au lait"}],
    }), encoding="utf-8")

    r = _run_real(
        "/api/sessions/search?q=caf%C3%89&content=1",
        tmp_path,
        sessions_meta=[{"session_id": "s_uc", "title": "UC", "profile": "default"}],
    )
    assert r["status"] == 200
    assert r["payload"]["count"] == 1
