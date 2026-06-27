"""Idle session switch cache contract.

Switching A -> B -> A for idle, unchanged sessions should not show a loading
placeholder or re-fetch /api/session on every click. The browser already has the
full rendered state for A in memory; reuse it until the sidebar fingerprint says
A changed.
"""
from pathlib import Path
import shutil
import subprocess

REPO = Path(__file__).resolve().parent.parent
SESSIONS_JS = (REPO / "static" / "sessions.js").read_text(encoding="utf-8")
NODE = shutil.which("node")


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
    assert "const _SESSION_DISPLAY_SWITCH_CACHE_MAX" in SESSIONS_JS
    assert "const _sessionDisplaySwitchCache = new Map()" in SESSIONS_JS
    assert "function _rememberSessionSwitchDisplayCache" in SESSIONS_JS
    assert "function _restoreSessionSwitchDisplayCache" in SESSIONS_JS


def test_switching_away_snapshots_idle_session_before_messages_are_cleared():
    body = _load_session_body()
    remember_idx = body.index("_rememberIdleSessionSwitchCache(currentSid")
    clear_idx = body.index("S.messages = []")
    assert remember_idx < clear_idx, (
        "The active idle session must be cached before loadSession clears "
        "S.messages for the next session."
    )


def test_switching_away_snapshots_display_cache_before_messages_are_cleared():
    body = _load_session_body()
    remember_idx = body.index("_rememberSessionSwitchDisplayCache(currentSid")
    clear_idx = body.index("S.messages = []")
    assert remember_idx < clear_idx, (
        "The visible transcript/display state must be cached before loadSession "
        "clears S.messages for the next session, including active/pending turns."
    )


def test_cached_idle_switch_restores_before_metadata_fetch_and_loading_placeholder():
    body = _load_session_body()
    restore_idx = body.index("_restoreIdleSessionSwitchCache(sid")
    loading_idx = body.index("Loading conversation...")
    metadata_fetch_idx = body.index("messages=0&resolve_model=0")
    assert restore_idx < loading_idx
    assert restore_idx < metadata_fetch_idx


def test_display_cache_restores_before_loading_placeholder_but_still_fetches_metadata():
    body = _load_session_body()
    restore_idx = body.index("_restoreSessionSwitchDisplayCache(sid")
    loading_idx = body.index("Loading conversation...")
    metadata_fetch_idx = body.index("messages=0&resolve_model=0")
    assert restore_idx < loading_idx
    assert restore_idx < metadata_fetch_idx
    assert "!_restoredSessionDisplayBeforeFetch" in body


def test_cached_idle_switch_restores_before_blocking_draft_flush():
    """A hot A→B→A switch should not wait on draft persistence before repainting.

    The outgoing idle session can be snapshotted synchronously, then the target
    idle cache should restore before the awaited /api/session/draft call. Draft
    persistence may still happen, but it must not be on the visible cache-hit
    path that should avoid `Loading conversation...` entirely.
    """
    body = _load_session_body()
    remember_idx = body.index("_rememberIdleSessionSwitchCache(currentSid")
    restore_idx = body.index("_restoreIdleSessionSwitchCache(sid")
    await_draft_idx = body.index("await _saveComposerDraftNow(currentSid")
    assert remember_idx < restore_idx < await_draft_idx


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


def _extract_function(name: str) -> str:
    return _function_body(f"function {name}")


def _run_node(source: str) -> str:
    assert NODE, "node not on PATH"
    result = subprocess.run(
        [NODE],
        input=source,
        cwd=str(REPO),
        capture_output=True,
        encoding="utf-8",
        text=True,
        timeout=30,
    )
    if result.returncode != 0:
        raise AssertionError(result.stderr)
    return result.stdout.strip()


def test_restore_treats_missing_sidebar_counts_as_unknown_not_mismatch():
    """A lightweight sidebar row must not invalidate a hot transcript cache.

    Some list projections omit counts/timestamps while the full session already
    has them. Treating those missing fields as zero makes A→B→A miss the idle
    cache and show `Loading conversation...` again.
    """
    source = "\n".join([
        "var _idleSessionSwitchCache = new Map();",
        "var _IDLE_SESSION_SWITCH_CACHE_MAX = 12;",
        "var _allSessions = [{session_id:'a', title:'A'}];",
        "var _sessionListSnapshotById = new Map();",
        "var INFLIGHT = {};",
        "var _messagesTruncated = false;",
        "var _oldestIdx = 0;",
        "var _pendingCarryForwardSnapshot = null;",
        "var _loadingSessionId = 'a';",
        "var S = {busy:false, activeStreamId:null, session:{session_id:'a', message_count:42, last_message_at:100}, messages:[{role:'user', content:'hi'}], toolCalls:[], lastUsage:{}};",
        "var localStorage = {setItem(){}};",
        "function _clearSameSessionForceReloadHint(){}",
        "function updateSendBtn(){}",
        "function setStatus(){}",
        "function setComposerStatus(){}",
        "function _setSessionViewedCount(){}",
        "function _clearSessionCompletionUnread(){}",
        "function _setActiveSessionUrl(){}",
        "function startSessionStream(){}",
        "function updateQueueBadge(){}",
        "function syncTopbar(){}",
        "function renderMessages(){}",
        _extract_function("_cloneSessionSwitchCacheValue"),
        _extract_function("_sessionSwitchCacheFingerprint"),
        _extract_function("_sessionSwitchCacheRowForSid"),
        _extract_function("_sessionSwitchCacheFingerprintMatches"),
        _extract_function("_rememberIdleSessionSwitchCache"),
        _extract_function("_restoreIdleSessionSwitchCache"),
        "if(!_rememberIdleSessionSwitchCache('a')) throw new Error('remember failed');",
        "S.session = {session_id:'b'}; S.messages = [{role:'user', content:'other'}];",
        "const restored = _restoreIdleSessionSwitchCache('a');",
        "console.log(JSON.stringify({restored, sid:S.session&&S.session.session_id, messages:S.messages.length}));",
    ])
    assert _run_node(source) == '{"restored":true,"sid":"a","messages":1}'


def test_restore_allows_truncated_long_session_count_drift_when_latest_timestamp_matches():
    """Long sessions can expose different count projections for the same latest turn.

    The loaded session metadata may use `updated_at` while the sidebar row uses
    `last_message_at`; both can differ by milliseconds/rounding while pointing
    at the same latest message. For truncated long transcripts, a count drift
    alone must not force a reload when the latest timestamp is effectively the
    same.
    """
    source = "\n".join([
        "var _idleSessionSwitchCache = new Map();",
        "var _IDLE_SESSION_SWITCH_CACHE_MAX = 12;",
        "var _allSessions = [{session_id:'a', title:'A', message_count:3412, last_message_at:1782591337.688856, updated_at:1782591337.732746, is_streaming:false, active_stream_id:null}];",
        "var _sessionListSnapshotById = new Map();",
        "var INFLIGHT = {};",
        "var _messagesTruncated = true;",
        "var _oldestIdx = 3340;",
        "var _pendingCarryForwardSnapshot = null;",
        "var _loadingSessionId = 'a';",
        "var S = {busy:false, activeStreamId:null, session:{session_id:'a', message_count:3166, updated_at:1782591337.732746}, messages:[{role:'user', content:'tail'}], toolCalls:[], lastUsage:{}};",
        "var localStorage = {setItem(){}};",
        "function _clearSameSessionForceReloadHint(){}",
        "function updateSendBtn(){}",
        "function setStatus(){}",
        "function setComposerStatus(){}",
        "function _setSessionViewedCount(){}",
        "function _clearSessionCompletionUnread(){}",
        "function _setActiveSessionUrl(){}",
        "function startSessionStream(){}",
        "function updateQueueBadge(){}",
        "function syncTopbar(){}",
        "function renderMessages(){}",
        _extract_function("_cloneSessionSwitchCacheValue"),
        _extract_function("_sessionSwitchCacheFingerprint"),
        _extract_function("_sessionSwitchCacheRowForSid"),
        _extract_function("_sessionSwitchCacheFingerprintMatches"),
        _extract_function("_rememberIdleSessionSwitchCache"),
        _extract_function("_restoreIdleSessionSwitchCache"),
        "if(!_rememberIdleSessionSwitchCache('a')) throw new Error('remember failed');",
        "S.session = {session_id:'b'}; S.messages = [{role:'user', content:'other'}];",
        "const restored = _restoreIdleSessionSwitchCache('a');",
        "console.log(JSON.stringify({restored, sid:S.session&&S.session.session_id, messages:S.messages.length, truncated:_messagesTruncated}));",
    ])
    assert _run_node(source) == '{"restored":true,"sid":"a","messages":1,"truncated":true}'
