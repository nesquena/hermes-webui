"""Regression: a stale sidebar unread dot must clear when a session is *visited*.

Salvage of #4946 (originally by @neaucode-bot), rebuilt fresh on master.

The yellow unread dot in the sidebar historically cleared only reliably when the
sidebar **row** was clicked. Opening/visiting a session through other paths — or
re-selecting the already-open session — could leave a stale dot, and a deferred
/api/sessions list poll landing across the async message-load gap could re-flag
the open session as unread.

The fix introduces `_acknowledgeSessionVisit()` (sync viewed count + polling
snapshot + repaint) and wires it into loadSession at three points:

  1. the same-session no-op guard (so re-selecting the open session clears a
     stale dot before returning),
  2. when the session metadata arrives, and
  3. again after the async message-load gap (so a deferred poll cannot leave a
     sticky dot).

Two invariants flagged in review are protected here and MUST NOT regress:

  (a) hidden/background completions must still be marked unread — the visit-ack
      does NOT loosen the focus gate on the completion paths, so a completion in
      a non-visible/non-focused tab is still flagged (concern a).
  (b) cleaning up a visited child's unread state must not strip a lineage
      PARENT's own unread dot — the visit repaints via
      renderSessionListFromCache(), which recomputes each row's aggregated
      unread authoritatively rather than doing ad-hoc DOM surgery (concern b).
"""
import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SESSIONS_JS = (ROOT / "static" / "sessions.js").read_text(encoding="utf-8")


def _load_session_block() -> str:
    start = SESSIONS_JS.index("async function loadSession(sid")
    end = SESSIONS_JS.index("function _resolveSessionModelForDisplaySoon", start)
    return SESSIONS_JS[start:end]


def _function_block(name: str, next_marker: str) -> str:
    start = SESSIONS_JS.index(f"function {name}")
    end = SESSIONS_JS.index(next_marker, start + 1)
    return SESSIONS_JS[start:end]


# ── Structural anchors ──────────────────────────────────────────────────────

def test_visit_ack_helpers_exist():
    assert "function _acknowledgeSessionVisit(sid, messageCount = 0, lastMessageAt = 0)" in SESSIONS_JS
    assert "function _syncSessionListSnapshotOnVisit(sid, messageCount, lastMessageAt)" in SESSIONS_JS
    assert "function _sessionVisitHasUnreadState(sid)" in SESSIONS_JS


def test_acknowledge_visit_syncs_viewed_snapshot_and_repaints():
    body = _function_block("_acknowledgeSessionVisit", "function _sessionVisitHasUnreadState")
    # Clears viewed count (which clears the stale completion-unread marker, #3020),
    # syncs the polling snapshot, and repaints the sidebar from cache.
    assert "_setSessionViewedCount(sid, messageCount);" in body
    assert "_syncSessionListSnapshotOnVisit(sid, messageCount, lastMessageAt);" in body
    assert "renderSessionListFromCache" in body


def test_load_session_acknowledges_visit_before_and_after_message_load():
    block = _load_session_block()
    # Metadata-arrival acknowledgment.
    first_ack = block.find("_acknowledgeSessionVisit(\n    S.session.session_id,")
    loading_clear = block.find("if (_isCurrentLoad()) _loadingSessionId = null;\n\n  // Re-acknowledge")
    second_ack = block.find("_acknowledgeSessionVisit(", loading_clear)

    assert first_ack != -1, "loadSession must acknowledge the visit when metadata arrives"
    assert loading_clear != -1, "loadSession must clear the in-flight marker before the final acknowledge"
    assert second_ack != -1 and first_ack < loading_clear < second_ack, (
        "loadSession must re-acknowledge after the async message-load gap so a "
        "deferred sidebar poll cannot leave a sticky unread dot"
    )


def test_post_load_reack_is_guarded_by_active_view():
    # #5917 gate finding: the post-load re-ack must be gated on
    # _isSessionActivelyViewedForList(sid). A completion that lands while
    # _ensureMessagesLoaded() is in flight AND the tab then goes hidden is
    # correctly marked unread — an UNCONDITIONAL post-load ack would wrongly
    # clear that hidden-tab-completion marker.
    block = _load_session_block()
    loading_clear = block.find("if (_isCurrentLoad()) _loadingSessionId = null;\n\n  // Re-acknowledge")
    guard = block.find("_isSessionActivelyViewedForList(sid)", loading_clear)
    second_ack = block.find("_acknowledgeSessionVisit(", loading_clear)
    assert guard != -1 and guard < second_ack, (
        "the post-load re-acknowledge must be guarded by "
        "_isSessionActivelyViewedForList(sid) so a hidden-tab completion stays unread"
    )


def test_same_session_reselect_clears_stale_unread():
    block = _load_session_block()
    guard = block.find("if(currentSid===sid && !forceReload && (!_loadingSessionId || _loadingSessionId===sid)){")
    unread_check = block.find("_sessionVisitHasUnreadState(sid)", guard)
    acknowledge = block.find("_acknowledgeSessionVisit(", unread_check)
    ret = block.find("return;", acknowledge)

    assert guard != -1, "same-session no-op guard must still exist"
    assert unread_check != -1 and guard < unread_check, (
        "re-selecting the already-open session must check for stale unread state"
    )
    assert acknowledge != -1 and unread_check < acknowledge < ret, (
        "re-selecting the already-open session must acknowledge the visit (clearing "
        "the stale dot) before returning"
    )


def test_completion_paths_keep_focus_gate_for_hidden_tab_completions():
    """Concern (a): the visit-ack must NOT loosen the completion paths' focus gate.

    A background completion in a hidden/unfocused tab must still be flagged
    unread, so the background + polling completion paths must keep using the
    focus-gated _isSessionActivelyViewedForList, not a focus-independent variant.
    """
    background = _function_block("_markSessionCompletionUnreadIfBackground", "function _clearSessionCompletionUnread")
    assert "_isSessionActivelyViewedForList(sid)" in background, (
        "background completion must keep the focus-gated read check so a hidden-tab "
        "completion is not prematurely marked read"
    )

    polling_start = SESSIONS_JS.index("function _markPollingCompletionUnreadTransitions(sessions)")
    polling_end = SESSIONS_JS.index("const staleRuntimeStateSids", polling_start)
    polling = SESSIONS_JS[polling_start:polling_end]
    assert "!_isSessionActivelyViewedForList(sid)" in polling, (
        "polling completion must keep the focus-gated read check so a hidden-tab "
        "completion is not prematurely marked read"
    )


# ── Functional behavior via node ────────────────────────────────────────────

def _extract(name: str) -> str:
    """Extract a top-level `function name(...) { ... }` definition by brace match."""
    marker = f"function {name}("
    start = SESSIONS_JS.index(marker)
    brace = SESSIONS_JS.index("{", start)
    depth = 0
    for i in range(brace, len(SESSIONS_JS)):
        ch = SESSIONS_JS[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return SESSIONS_JS[start:i + 1]
    raise AssertionError(f"could not brace-match {name}")


def _run_node(script: str) -> dict:
    result = subprocess.run(["node", "-e", script], check=True, capture_output=True, text=True)
    return json.loads(result.stdout)


def test_acknowledge_visit_clears_completion_unread_marker():
    """Visiting a session that carries an explicit completion-unread marker must
    clear it (so the yellow dot disappears) and repaint the sidebar."""
    ack = _extract("_acknowledgeSessionVisit")
    sync = _extract("_syncSessionListSnapshotOnVisit")
    set_viewed = _extract("_setSessionViewedCount")
    clear_unread = _extract("_clearSessionCompletionUnread")
    get_unread = _extract("_getSessionCompletionUnread")
    save_unread = _extract("_saveSessionCompletionUnread")
    get_counts = _extract("_getSessionViewedCounts")
    save_counts = _extract("_saveSessionViewedCounts")

    script = f"""
// Minimal localStorage shim.
const _store = {{}};
const localStorage = {{
  getItem: (k) => (k in _store ? _store[k] : null),
  setItem: (k, v) => {{ _store[k] = String(v); }},
}};
const SESSION_VIEWED_COUNTS_KEY = 'v';
const SESSION_COMPLETION_UNREAD_KEY = 'u';
let _sessionViewedCounts = null;
let _sessionCompletionUnread = null;
const _sessionListSnapshotById = new Map();
const _sessionStreamingById = new Map();
let S = {{ session: {{ session_id: 'open', message_count: 5 }} }};
let repaints = 0;
function renderSessionListFromCache() {{ repaints += 1; }}
function _forgetObservedStreamingSession() {{}}
{get_counts}
{save_counts}
{get_unread}
{save_unread}
{clear_unread}
{set_viewed}
{sync}
{ack}
// Seed a stale completion-unread marker for the open session.
_getSessionCompletionUnread()['open'] = {{message_count: 5, completed_at: 1}};
_saveSessionCompletionUnread();
const before = Object.prototype.hasOwnProperty.call(_getSessionCompletionUnread(), 'open');
_acknowledgeSessionVisit('open', 5, 10);
const after = Object.prototype.hasOwnProperty.call(_getSessionCompletionUnread(), 'open');
const snap = _sessionListSnapshotById.get('open');
console.log(JSON.stringify({{before, after, repaints, viewed: _getSessionViewedCounts()['open'], snap}}));
"""
    out = _run_node(script)
    assert out["before"] is True, "precondition: marker seeded"
    assert out["after"] is False, "visiting the session must clear the completion-unread marker"
    assert out["repaints"] >= 1, "visiting must repaint the sidebar from cache"
    assert out["viewed"] == 5, "viewed count must be synced to the current message count"
    assert out["snap"] == {"message_count": 5, "last_message_at": 10}, (
        "polling snapshot must be synced so a deferred list poll cannot re-flag the session"
    )


def test_visit_snapshot_prevents_deferred_poll_from_reflagging():
    """A deferred /api/sessions poll running just after a visit must NOT re-mark
    the open, unchanged session as a fresh background completion.

    This is the sticky-dot race: without the snapshot sync, the poll sees a
    session that "completed" relative to a stale/absent snapshot and re-flags it.
    """
    sync = _extract("_syncSessionListSnapshotOnVisit")
    set_viewed = _extract("_setSessionViewedCount")
    clear_unread = _extract("_clearSessionCompletionUnread")
    get_unread = _extract("_getSessionCompletionUnread")
    save_unread = _extract("_saveSessionCompletionUnread")
    get_counts = _extract("_getSessionViewedCounts")
    save_counts = _extract("_saveSessionViewedCounts")
    ack = _extract("_acknowledgeSessionVisit")
    transitions = _extract("_markPollingCompletionUnreadTransitions")
    effective = _extract("_isSessionEffectivelyStreaming")
    local = _extract("_isSessionLocallyStreaming")

    script = f"""
const _store = {{}};
const localStorage = {{
  getItem: (k) => (k in _store ? _store[k] : null),
  setItem: (k, v) => {{ _store[k] = String(v); }},
}};
const SESSION_VIEWED_COUNTS_KEY = 'v';
const SESSION_COMPLETION_UNREAD_KEY = 'u';
let _sessionViewedCounts = null;
let _sessionCompletionUnread = null;
const _sessionListSnapshotById = new Map();
const _sessionStreamingById = new Map();
// open + focused/visible so the focus gate treats it as actively viewed too.
let S = {{ session: {{ session_id: 'open', message_count: 5 }}, busy: false }};
let repaints = 0;
function renderSessionListFromCache() {{ repaints += 1; }}
function _forgetObservedStreamingSession(sid) {{}}
function _rememberObservedStreamingSession() {{}}
function _getSessionObservedStreaming() {{ return {{}}; }}
function _rememberSessionListSource() {{}}
function _hasPendingUserMessageSignal(s) {{ return !!(s && (s.pending_user_message || s.has_pending_user_message)); }}
function _markSessionCompletionUnread(sid, count) {{
  _getSessionCompletionUnread()[sid] = {{message_count: count, completed_at: 1}};
  _saveSessionCompletionUnread();
}}
const document = {{ visibilityState: 'visible', hasFocus: () => true }};
let _loadingSessionId = null;
const _allSessionsScope = null;
const _sessionListSourceById = new Map();
{get_counts}
{save_counts}
{get_unread}
{save_unread}
{clear_unread}
{set_viewed}
{local}
{effective}
{sync}
{ack}
function _isSessionActivelyViewedForList(sid) {{
  if (!sid || !S.session || S.session.session_id !== sid) return false;
  if (_loadingSessionId && _loadingSessionId !== sid) return false;
  if (document.visibilityState && document.visibilityState !== 'visible') return false;
  if (typeof document.hasFocus === 'function' && !document.hasFocus()) return false;
  return true;
}}
{transitions}
// Simulate: session was streaming, so a snapshot/streaming state exists.
_sessionStreamingById.set('open', true);
_sessionListSnapshotById.set('open', {{message_count: 4, last_message_at: 5}});
// User visits — acknowledge marks it read AND syncs the snapshot to current.
_acknowledgeSessionVisit('open', 5, 10);
// Now a deferred /api/sessions poll lands for the SAME (now idle) session.
_markPollingCompletionUnreadTransitions([
  {{session_id: 'open', is_streaming: false, active_stream_id: null, message_count: 5, last_message_at: 10, updated_at: 10}}
]);
const flagged = Object.prototype.hasOwnProperty.call(_getSessionCompletionUnread(), 'open');
console.log(JSON.stringify({{flagged}}));
"""
    out = _run_node(script)
    assert out["flagged"] is False, (
        "a deferred list poll after a visit must not re-flag the open, unchanged "
        "session as unread — the visit-ack synced snapshot + viewed count"
    )


# ── Functional: hidden-tab completion during message load stays unread ───────

def _extract_async(name: str) -> str:
    """Like _extract, but preserve an `async` prefix so `await` bodies stay valid."""
    body = _extract(name)
    idx = SESSIONS_JS.rindex("async ", 0, SESSIONS_JS.index(body))
    # Only treat it as async if the `async ` keyword immediately precedes it.
    if SESSIONS_JS[idx:idx + len("async ")] == "async " and \
       SESSIONS_JS[idx:].startswith("async function " + name):
        return "async " + body
    return body


def _hidden_completion_script(*, hidden: bool) -> str:
    """Build a Node harness that drives the REAL _ensureMessagesLoaded() through a
    delayed messages fetch, marks a completion mid-fetch, and reports whether the
    completion-unread marker survives."""
    ensure = _extract_async("_ensureMessagesLoaded")
    set_viewed = _extract("_setSessionViewedCount")
    clear_unread = _extract("_clearSessionCompletionUnread")
    get_unread = _extract("_getSessionCompletionUnread")
    save_unread = _extract("_saveSessionCompletionUnread")
    mark_unread = _extract("_markSessionCompletionUnread")
    get_counts = _extract("_getSessionViewedCounts")
    save_counts = _extract("_saveSessionViewedCounts")
    actively_viewed = _extract("_isSessionActivelyViewedForList")

    visibility = "'hidden'" if hidden else "'visible'"
    has_focus = "false" if hidden else "true"

    return f"""
// Minimal localStorage shim.
const _store = {{}};
const localStorage = {{
  getItem: (k) => (k in _store ? _store[k] : null),
  setItem: (k, v) => {{ _store[k] = String(v); }},
}};
const SESSION_VIEWED_COUNTS_KEY = 'v';
const SESSION_COMPLETION_UNREAD_KEY = 'u';
let _sessionViewedCounts = null;
let _sessionCompletionUnread = null;
let _messagesTruncated = false;
let _oldestIdx = 0;
let _messageRenderWindowSize = 0;
const _MSG_LIMIT_MAX = 500;
let _msgLimitMax = _MSG_LIMIT_MAX;
let _pendingCarryForwardSnapshot = null;
let _loadingSessionId = 'open';
let _loadSessionGeneration = 0;
const window = {{}};
// Tab starts VISIBLE+FOCUSED: the load begins while the user is watching.
let _visibility = 'visible';
let _focused = true;
const document = {{
  get visibilityState() {{ return _visibility; }},
  hasFocus: () => _focused,
}};
let S = {{ session: {{ session_id: 'open', message_count: 0 }}, messages: [], lastUsage: {{}} }};

// Stubs for the incidental side effects _ensureMessagesLoaded touches.
function _clearSameSessionForceReloadHint() {{}}
function _messageReloadLimitForSession() {{ return 0; }}
function _syncToolCallsForLoadedMessages() {{}}
function clearLiveToolCards() {{}}

// Delayed messages fetch: resolves only when we release it, simulating a slow
// /api/session?messages=1 response that spans the tab going hidden.
let _releaseApi;
const _apiResult = new Promise((res) => {{ _releaseApi = res; }});
let _apiCalled = false;
async function api(url) {{ _apiCalled = true; return _apiResult; }}

{get_counts}
{save_counts}
{get_unread}
{save_unread}
{clear_unread}
{mark_unread}
{set_viewed}
{actively_viewed}
{ensure}

function _hasMarker() {{
  return Object.prototype.hasOwnProperty.call(_getSessionCompletionUnread(), 'open');
}}

(async () => {{
  const p = _ensureMessagesLoaded('open', {{}});
  // Let the synchronous body run up to `await api(...)`.
  await Promise.resolve();
  await Promise.resolve();
  const apiIssued = _apiCalled;
  // Mid-fetch: the tab's visibility/focus flips to the test scenario and a
  // background completion lands, marking the session unread.
  _visibility = {visibility};
  _focused = {has_focus};
  _markSessionCompletionUnread('open', 6);
  const markerBefore = _hasMarker();
  // The delayed messages response finally arrives and the load finishes.
  _releaseApi({{ session: {{ session_id: 'open', message_count: 6, messages: [{{ role: 'assistant', content: 'x' }}] }} }});
  await p;
  const markerAfter = _hasMarker();
  const viewed = _getSessionViewedCounts()['open'];
  console.log(JSON.stringify({{
    apiIssued,
    markerBefore,
    markerAfter,
    viewed: viewed === undefined ? null : viewed,
  }}));
}})();
"""


def test_hidden_tab_completion_during_message_load_survives():
    """#5917 gate finding (SILENT): a completion that lands while the tab is
    hidden DURING the awaited message fetch inside _ensureMessagesLoaded() must
    NOT be silently marked read. The guarded viewed-count clear must skip when
    the session is no longer actively viewed."""
    out = _run_node(_hidden_completion_script(hidden=True))
    assert out["apiIssued"] is True, "precondition: the delayed messages fetch was issued"
    assert out["markerBefore"] is True, "precondition: the mid-fetch completion marked the session unread"
    assert out["markerAfter"] is True, (
        "a hidden-tab completion landing during the awaited message fetch must "
        "SURVIVE — _ensureMessagesLoaded() must not clear the unread marker via "
        "an unconditional _setSessionViewedCount when the tab is not actively viewing"
    )
    assert out["viewed"] is None, (
        "the viewed count must NOT be synced for a hidden/background session, "
        "otherwise the completion-unread marker would be cleared"
    )


def test_visible_tab_message_load_still_syncs_viewed_count():
    """Control: the guard must not over-block. An ACTIVELY-viewed session still
    updates its viewed count normally through the message-load path (clearing any
    marker), so the fix is scoped to hidden/background sessions only."""
    out = _run_node(_hidden_completion_script(hidden=False))
    assert out["apiIssued"] is True
    assert out["markerBefore"] is True
    assert out["markerAfter"] is False, (
        "an actively-viewed session must still clear its completion-unread marker "
        "when the message load finishes"
    )
    assert out["viewed"] == 6, (
        "an actively-viewed session must still sync its viewed count to the "
        "loaded message count"
    )
