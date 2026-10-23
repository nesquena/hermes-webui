"""#7925 re-gate finding 2 (2026-10-21): a local row that lands while the
append-only fast-path fetch is in flight must not be duplicated.

The client holds 601 rows and asks for the rows after its anchor. A streaming
turn (or a delegation wakeup, or a sibling loader) appends a row to
``S.messages`` while that request is in flight; the server's window contains
that same row because it landed server-side too. Concatenating the window onto
the grown transcript renders the row twice. The fix snapshots the shape the
anchor described (length, last row, ``_oldestIdx``) before the await and routes
any drift to the authoritative full fetch — exactly the over-cap fallback.
"""
import json
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
SESSIONS_SRC = (_ROOT / "static" / "sessions.js").read_text(encoding="utf-8")

NODE_BIN = shutil.which("node")
_node_tests = pytest.mark.skipif(NODE_BIN is None, reason="node not on PATH")


def _run_node_vm(source: str) -> str:
    if NODE_BIN is None:
        pytest.skip("node not on PATH")
    with tempfile.NamedTemporaryFile(
        "w", suffix=".cjs", encoding="utf-8", dir=_ROOT, delete=False
    ) as script:
        script.write(source)
        script_path = Path(script.name)
    try:
        result = subprocess.run(
            [NODE_BIN, str(script_path)],
            cwd=str(_ROOT),
            capture_output=True,
            text=True,
            timeout=60,
        )
    finally:
        script_path.unlink(missing_ok=True)
    if result.returncode != 0:
        raise RuntimeError(result.stderr)
    return result.stdout.strip()


def _fn(name: str) -> str:
    marker = f"function {name}("
    start = SESSIONS_SRC.index(marker)
    # Keep the `async` keyword when the source declares one (the marker itself
    # is a substring of `async function name(`).
    if SESSIONS_SRC[max(0, start - 6):start] == "async ":
        start -= 6
    i = SESSIONS_SRC.index("{", start) + 1
    depth = 1
    while depth and i < len(SESSIONS_SRC):
        if SESSIONS_SRC[i] == "{":
            depth += 1
        elif SESSIONS_SRC[i] == "}":
            depth -= 1
        i += 1
    return SESSIONS_SRC[start:i]


_VM_COMMON = r"""
function extractFunc(name) {
  const start = SESSIONS_JS.indexOf('function ' + name + '(');
  if (start < 0) throw Error(name + ' missing');
  let i = SESSIONS_JS.indexOf('{', start) + 1, depth = 1;
  while (depth && i < SESSIONS_JS.length) {
    if (SESSIONS_JS[i] === '{') depth++;
    else if (SESSIONS_JS[i] === '}') depth--;
    i++;
  }
  return SESSIONS_JS.slice(start, i);
}
const t = (k) => k;
function renderSessionListFromCache() {}
function closeSessionActionMenu() {}
const document = {createElement: () => ({style: {}, dataset: {}})};
const window = {};
const _allSessions = [];
"""


def _harness(local_row_lands_midflight: bool, tool_cards_in_response=None) -> str:
    """Build the VM source. When *local_row_lands_midflight* the stubbed api()
    appends a local row to S.messages while the append request is in flight —
    the same row the server's window will carry back. *tool_cards_in_response*
    is the session-level tool_calls array the append window carries back."""
    midflight = (
        """
    // A streaming turn lands locally while the fetch is in flight; the
    // server-side transcript contains the same row.
    S.messages.push({role: 'assistant', content: 'streamed answer'});
"""
        if local_row_lands_midflight
        else ""
    )
    cards_json = json.dumps(tool_cards_in_response or [])
    return (
        "const SESSIONS_JS = " + repr(SESSIONS_SRC) + ";\n"
        + _VM_COMMON
        + r"""
let _loadingSessionId = 'sid-7925-f2';
let _loadSessionGeneration = 1;
let _oldestIdx = 0;
let _messagesTruncated = false;
let _msgLimitMax = 500;
let _pendingCarryForwardSnapshot = null;
const INFLIGHT = {};
const MESSAGE_RENDER_WINDOW_DEFAULT = 50;
const apiCalls = [];
const S = {
  session: {
    session_id: 'sid-7925-f2',
    transcript_generation: 3,
    message_count: 601,
    visible_message_count: 601,
    regeneration_revision: null,
    tool_calls: [{tid: 't1', assistant_msg_idx: 5, summary: 'older card'}],
  },
  messages: Array.from({length: 601}, (_, i) => ({
    role: i % 2 === 0 ? 'user' : 'assistant',
    content: 'm' + i,
  })),
  toolCalls: [{tid: 't1', assistant_msg_idx: 5, summary: 'older card', done: true}],
  busy: false,
};
function _ownsLoad() { return true; }
function _clearSameSessionForceReloadHint() {}
function _adoptRegenerationRevision() {}
function _setSessionViewedCount() {}
function syncTopbar() {}
function clearLiveToolCards() {}
function renderMessages() {}
function clearVisibleMessageRowCache() {}
function _hydrateTodosFromSession() {}
function scheduleTodosRefresh() {}
function _isSessionActivelyViewedForList() { return true; }
function _messageRenderableMessageCount() { return S.messages.length; }
function _currentMessageRenderWindowSize() { return 50; }
function _appendAnchorRowFingerprint(row) { return row ? 'fp-' + row.content : ''; }
function _messageReloadLimitForSession() { return 2000; }
function _carryForwardEphemeralTurnFields() { return arguments[1]; }
async function api(url) {
  apiCalls.push(url);
  if (url.indexOf('msg_after=') >= 0) {
"""
        + midflight
        + r"""
    return {session: {
      _append_only: true,
      _messages_truncated: false,
      _messages_offset: 601,
      _msg_limit_max: 500,
      message_count: 602,
      visible_message_count: 602,
      messages: [{role: 'assistant', content: 'streamed answer'}],
      tool_calls: """
        + cards_json
        + r""",
    }};
  }
  // Authoritative full document: 601 held rows + the row that landed
  // (locally and server-side), exactly once.
  return {session: {
    _messages_truncated: false,
    _messages_offset: 0,
    _msg_limit_max: 500,
    message_count: 602,
    visible_message_count: 602,
    messages: S.messages.map(m => Object.assign({}, m)),
  }};
}
"""
        + _fn("_syncToolCallsForLoadedMessages")
        + "\n"
        + _fn("_ensureMessagesLoaded")
        + r"""
(async () => {
  await _ensureMessagesLoaded('sid-7925-f2', {loadGeneration: 1});
  const contents = S.messages.map(m => m.content);
  const dupes = contents.filter((c, i) => contents.indexOf(c) !== i);
  const sessionCards = (S.session.tool_calls || []).map(tc => ({
    tid: tc.tid, idx: tc.assistant_msg_idx,
  }));
  const liveCards = (S.toolCalls || []).map(tc => ({
    tid: tc.tid, idx: tc.assistant_msg_idx,
  }));
  console.log(JSON.stringify({
    apiCalls: apiCalls.length,
    rows: S.messages.length,
    dupes: dupes,
    streamedCount: contents.filter(c => c === 'streamed answer').length,
    sessionCards: sessionCards,
    liveCards: liveCards,
  }));
})();
"""
    )


@_node_tests
def test_local_row_landing_midflight_does_not_duplicate():
    """A local row landing mid-fetch routes the response to the full fetch;
    the row appears exactly once."""
    result = json.loads(_run_node_vm(_harness(local_row_lands_midflight=True)))
    assert result["apiCalls"] == 2, (
        f"expected append + full-fallback requests, got {result['apiCalls']} — "
        "the concurrent-change guard did not fire"
    )
    assert result["dupes"] == [], f"duplicated rows after append: {result['dupes']}"
    assert result["streamedCount"] == 1, (
        f"the streamed row appears {result['streamedCount']} times"
    )


@_node_tests
def test_clean_append_still_concats_without_a_second_request():
    """Control: with no local drift the clean append stays on the fast path —
    one request, one appended row, no full fetch."""
    result = json.loads(_run_node_vm(_harness(local_row_lands_midflight=False)))
    assert result["apiCalls"] == 1, (
        f"clean append must not re-fetch; got {result['apiCalls']} requests"
    )
    assert result["rows"] == 602, result["rows"]
    assert result["dupes"] == [], result["dupes"]


@_node_tests
def test_append_window_tool_cards_merge_with_rebased_index():
    """#7925 re-gate finding 3: a session-level tool card carried by the append
    window must land in the client's card list, rebased from the window
    coordinate space into the combined transcript's.

    The server rebases full-transcript ``assistant_msg_idx`` values into the
    returned window (``idx - window_start``); the client renders the combined
    transcript, where that same row sits at ``idx + (_messages_offset -
    _oldestIdx)``. Without the rebase the card anchors to the wrong row."""
    result = json.loads(_run_node_vm(_harness(
        local_row_lands_midflight=False,
        tool_cards_in_response=[
            {"tid": "t9", "assistant_msg_idx": 0, "summary": "new card"},
        ],
    )))
    session_cards = {c["tid"]: c["idx"] for c in result["sessionCards"]}
    assert "t1" in session_cards, "the pre-existing card was dropped"
    assert session_cards["t1"] == 5
    assert "t9" in session_cards, (
        "the append window's session-level tool card never reached the client"
    )
    # window-relative 0 + (_messages_offset 601 - _oldestIdx 0) = 601, the
    # appended row's index in the combined transcript.
    assert session_cards["t9"] == 601, session_cards
