"""Regression: sidebar "Mark as read" must persist the AUTHORITATIVE count (#5900).

The sidebar context-menu "Mark as read" action (#1748) clears a session's unread
badge without opening the conversation. It does so by writing the session's viewed
message-count watermark. The reviewer-reported bug: the action persisted the cached
sidebar row's ``message_count``, which can be STALE (lower) relative to the count a
background completion already recorded in the explicit completion-unread marker.

Consequence: the badge clears immediately, but the next ``/api/sessions`` refresh
returns the authoritative (higher) ``message_count`` and ``_hasUnreadForSession``
re-flags the session as unread — the badge disappears and then reappears after a
refresh.

The decide-side (``_hasUnreadForSession`` short-circuits on the completion marker,
which knows the authoritative count) and the act-side (``_markSessionRead`` persists
the viewed watermark) must resolve to the SAME authoritative value. This test drives
the real JS in node and asserts the observable badge state across a refresh.
"""

import json
import re
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SESSIONS_JS = (REPO / "static" / "sessions.js").read_text(encoding="utf-8")


def _extract_function(src: str, name: str) -> str:
    """Return the full ``function <name>(...) { ... }`` declaration via brace match."""
    match = re.search(rf"\bfunction {re.escape(name)}\b", src)
    assert match, f"function {name} not found in sessions.js"
    brace = src.index("{", match.start())
    depth = 0
    for i in range(brace, len(src)):
        ch = src[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return src[match.start() : i + 1]
    raise AssertionError(f"unbalanced braces extracting {name}")


REQUIRED_FUNCTIONS = [
    "_getSessionViewedCounts",
    "_saveSessionViewedCounts",
    "_setSessionViewedCount",
    "_getSessionCompletionUnread",
    "_saveSessionCompletionUnread",
    "_markSessionCompletionUnread",
    "_clearSessionCompletionUnread",
    "_hasSessionCompletionUnread",
    "_hasUnreadForSession",
    "_markSessionRead",
]


def _run_scenario() -> dict:
    functions = "\n".join(_extract_function(SESSIONS_JS, name) for name in REQUIRED_FUNCTIONS)
    script = f"""
// Minimal localStorage shim so the real persistence helpers run unchanged.
const _store = {{}};
const localStorage = {{
  getItem(k) {{ return Object.prototype.hasOwnProperty.call(_store, k) ? _store[k] : null; }},
  setItem(k, v) {{ _store[k] = String(v); }},
  removeItem(k) {{ delete _store[k]; }},
}};
let _sessionViewedCounts = null;
let _sessionCompletionUnread = null;
const SESSION_VIEWED_COUNTS_KEY = 'hermes-session-viewed-counts';
const SESSION_COMPLETION_UNREAD_KEY = 'hermes-session-completion-unread';
let _renderCalls = 0;
function renderSessionListFromCache() {{ _renderCalls++; }}

{functions}

const SID = 'sess-1';
// 1. User last viewed this session at 5 messages.
_setSessionViewedCount(SID, 5);
// 2. A background turn completes while the user is away, growing the session to 8
//    messages. The completion is recorded with the AUTHORITATIVE count (8) in the
//    explicit unread marker — exactly as the SSE 'done' handler and the polling
//    fallback (_markPollingCompletionUnreadTransitions) do.
_markSessionCompletionUnread(SID, 8);

// 3. The sidebar row the context menu opens from still carries a STALE cached
//    count (the session list has not re-fetched the higher count yet).
const staleRow = {{ session_id: SID, message_count: 5 }};
const unreadBefore = _hasUnreadForSession(staleRow);

// 4. User clicks "Mark as read".
_markSessionRead(staleRow);
const unreadAfterMark = _hasUnreadForSession(staleRow);

// 5. The session list refreshes from /api/sessions and returns the authoritative
//    message_count (8). The badge must NOT return.
const authoritativeRow = {{ session_id: SID, message_count: 8 }};
const unreadAfterRefresh = _hasUnreadForSession(authoritativeRow);

console.log(JSON.stringify({{
  unreadBefore,
  unreadAfterMark,
  unreadAfterRefresh,
  persistedViewedCount: _getSessionViewedCounts()[SID],
  renderCalls: _renderCalls,
}}));
"""
    result = subprocess.run(["node", "-e", script], check=True, capture_output=True, text=True)
    return json.loads(result.stdout)


def test_mark_as_read_survives_session_list_refresh():
    out = _run_scenario()
    # Badge was showing before the action (via the completion-unread marker).
    assert out["unreadBefore"] is True
    # Badge clears immediately after marking as read.
    assert out["unreadAfterMark"] is False
    # ...and STAYS cleared after the authoritative session-list refresh. This is the
    # regression: with the stale cached count persisted (5), the refresh count (8)
    # re-flagged the session as unread and the badge reappeared.
    assert out["unreadAfterRefresh"] is False, (
        "Mark as read persisted a stale viewed count; the badge reappears after "
        "the next /api/sessions refresh returns the authoritative message_count."
    )
    # The persisted watermark must be the authoritative completed count, not the
    # stale cached row count.
    assert out["persistedViewedCount"] == 8
    # The action re-renders the sidebar so the badge disappears without a reload.
    assert out["renderCalls"] >= 1
