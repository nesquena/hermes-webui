"""Idle session switch cache contract.

Switching A -> B -> A for idle, unchanged sessions should not show a loading
placeholder or re-fetch /api/session on every click. The browser already has the
full rendered state for A in memory; reuse it until the sidebar fingerprint says
A changed.
"""
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SESSIONS_JS = (REPO / "static" / "sessions.js").read_text(encoding="utf-8")


def _idx(text: str) -> int:
    pos = SESSIONS_JS.find(text)
    assert pos >= 0, f"missing marker: {text}"
    return pos


def _load_session_body() -> str:
    start = _idx("async function loadSession(sid)")
    end = _idx("// ── Handoff hint logic")
    return SESSIONS_JS[start:end]


def test_idle_session_switch_cache_helpers_exist():
    assert "const _IDLE_SESSION_SWITCH_CACHE_MAX" in SESSIONS_JS
    assert "const _idleSessionSwitchCache = new Map()" in SESSIONS_JS
    assert "function _rememberIdleSessionSwitchCache" in SESSIONS_JS
    assert "function _restoreIdleSessionSwitchCache" in SESSIONS_JS
    assert "function _sessionSwitchCacheFingerprint" in SESSIONS_JS


def test_switching_away_snapshots_idle_session_before_messages_are_cleared():
    body = _load_session_body()
    remember_idx = body.index("_rememberIdleSessionSwitchCache(currentSid")
    clear_idx = body.index("S.messages = []")
    assert remember_idx < clear_idx, (
        "The active idle session must be cached before loadSession clears "
        "S.messages for the next session."
    )


def test_cached_idle_switch_restores_before_metadata_fetch_and_loading_placeholder():
    body = _load_session_body()
    restore_idx = body.index("_restoreIdleSessionSwitchCache(sid")
    loading_idx = body.index("Loading conversation...")
    metadata_fetch_idx = body.index("messages=0&resolve_model=0")
    assert restore_idx < loading_idx
    assert restore_idx < metadata_fetch_idx


def _function_body(marker: str) -> str:
    start = _idx(marker)
    paren = SESSIONS_JS.index(")", start)
    while SESSIONS_JS[paren + 1 : paren + 2] != "{":
        paren = SESSIONS_JS.index(")", paren + 1)
    brace = paren + 1
    depth = 1
    i = brace + 1
    while i < len(SESSIONS_JS) and depth:
        if SESSIONS_JS[i] == "{":
            depth += 1
        elif SESSIONS_JS[i] == "}":
            depth -= 1
        i += 1
    return SESSIONS_JS[start:i]


def test_restore_path_requires_unchanged_idle_sidebar_fingerprint():
    body = _function_body("function _restoreIdleSessionSwitchCache")
    matcher = _function_body("function _sessionSwitchCacheFingerprintMatches")
    combined = body + matcher
    assert "forceReload" in body and "return false" in body
    assert "_sessionSwitchCacheFingerprint" in body
    assert "message_count" in combined
    assert "last_message_at" in combined
    assert "active_stream_id" in combined
    assert "pending_user_message" in combined
    assert "has_pending_user_message" in combined


def test_restore_path_does_not_call_api_session():
    body = _function_body("function _restoreIdleSessionSwitchCache")
    assert "/api/session" not in body
    assert "api(" not in body
