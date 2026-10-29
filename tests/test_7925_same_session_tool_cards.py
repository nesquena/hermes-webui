"""#7925 re-gate (2026-10-29, silent finding 1): a same-session reload must not
drop the older session-level tool cards.

``loadSession`` replaces ``S.session`` with the metadata response, which
carries ``tool_calls: []``. The capture that keeps the loaded card list for
the append path therefore has to snapshot the PRIOR list BEFORE the
replacement — after it, the older cards are already gone, so a focus reload
with no server change dropped every session-level card the transcript had
rendered.

This test drives the REAL ``loadSession`` (not a pre-seeded harness): the
loader, the metadata replacement, the capture and the append merge are the
production functions. Only the transport and the unrelated chrome are
controlled seams — the chrome is a no-op Proxy, so the loader's own control
flow stays real.
"""
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
_SESSIONS = (_ROOT / "static" / "sessions.js").read_text(encoding="utf-8")

NODE_BIN = shutil.which("node")
_node_tests = pytest.mark.skipif(NODE_BIN is None, reason="node not on PATH")


def _function(src: str, name: str) -> str:
    """Extract one top-level function definition verbatim."""
    marker = f"function {name}("
    start = src.index(marker)
    if src[max(0, start - 6):start] == "async ":
        start -= 6
    i = src.index("{", start) + 1
    depth = 1
    while depth and i < len(src):
        if src[i] == "{":
            depth += 1
        elif src[i] == "}":
            depth -= 1
        i += 1
    return src[start:i]


def _run_node(script: str) -> str:
    assert NODE_BIN, "node is required for this test"
    fd, path = tempfile.mkstemp(suffix=".cjs", dir=os.environ.get("TMPDIR", "/tmp"))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(script)
        result = subprocess.run(
            [NODE_BIN, path], capture_output=True, text=True, check=False, timeout=60
        )
    finally:
        os.unlink(path)
    assert result.returncode == 0, result.stderr or result.stdout
    return result.stdout.strip()


# The chrome seam: any global the loader touches that is not part of this
# scenario resolves to a no-op. Real functions are installed in the sandbox
# and always win over the Proxy.
_CHROME = r"""
const vm = require('vm');
const NOOP = function () {};
const BASE = {
  console, JSON, Math, Object, Array, String, Number, Boolean, Date, Promise, Error,
  Map, Set, WeakMap, WeakSet, RegExp, Symbol, parseInt, parseFloat, isNaN, isFinite,
  encodeURIComponent, decodeURIComponent, setTimeout, clearTimeout, setInterval,
  clearInterval, queueMicrotask, structuredClone, URL, URLSearchParams, Intl,
  TextEncoder, TextDecoder, Buffer, process, __dirname, __filename, require,
};
const sandbox = new Proxy(BASE, {
  get(target, prop) {
    if (prop in target) return target[prop];
    if (typeof prop === 'symbol') return undefined;
    return NOOP;
  },
  has() { return true; },
  set(target, prop, value) { target[prop] = value; return true; },
});
const ctx = vm.createContext(sandbox);
"""


def _harness(*, append_response_cards) -> str:
    """Node script: the real loadSession plus a controllable transport.

    The transport answers two shapes:
      * the session-metadata URL — the metadata response, which carries
        ``tool_calls: []`` (the server's metadata payload never carries the
        session-level cards) and the transcript.
      * ``msg_after=`` — the append window, optionally carrying new cards.
    """
    cards = json.dumps(append_response_cards)
    prelude = (
        """
const S = {
  session: {
    session_id: 'sid-7925-cards',
    transcript_generation: 3,
    tool_calls: [{tid: 'older-card', assistant_msg_idx: 5, summary: 'older card'}],
    message_count: 601,
    visible_message_count: 601,
  },
  messages: Array.from({length: 601}, (_, i) => ({role: i % 2 === 0 ? 'user' : 'assistant', content: 'm' + i})),
  toolCalls: [{tid: 'older-card', assistant_msg_idx: 5, summary: 'older card', done: true}],
  busy: false,
};
let _loadingSessionId = null;
let _loadSessionGeneration = 1;
let _oldestIdx = 0;
let _messagesTruncated = false;
let _msgLimitMax = 500;
let _pendingCarryForwardSnapshot = null;
let _sessionPrefixFp = 'fp-held-601';
let _sessionPrefixFpEnd = 601;
let _pendingSessionToolCalls = null;
let _messageRenderWindowSize = 50;
const MESSAGE_RENDER_WINDOW_DEFAULT = 50;
const INFLIGHT = {};
const apiCalls = [];
const SESSION_ID = 'sid-7925-cards';
const CARDS_IN_RESPONSE = CARDS_PLACEHOLDER;

function api(url, opts) {
  apiCalls.push(url);
  if (url.indexOf('msg_after=') >= 0) {
    // The append window: the server returns only the rows after the anchor,
    // plus any session-level cards that landed with them.
    return Promise.resolve({session: {
      _append_only: true,
      _messages_truncated: false,
      _messages_offset: 601,
      _msg_limit_max: 500,
      message_count: 602,
      visible_message_count: 602,
      messages: [{role: 'assistant', content: 'streamed answer'}],
      prefix_fp: 'fp-after-append',
      tool_calls: CARDS_IN_RESPONSE,
    }});
  }
  // The metadata response carries NO session-level cards — this is the shape
  // that made the capture site always see an empty list.
  return Promise.resolve({session: {
    _messages_truncated: false,
    _messages_offset: 0,
    _msg_limit_max: 500,
    message_count: 601,
    visible_message_count: 601,
    session_id: SESSION_ID,
    transcript_generation: 3,
    messages: S.messages.map(m => Object.assign({}, m)),
    tool_calls: [],
  }});
}
function _rearmActiveSessionStream() {}
function _adoptRegenerationRevision() {}
function _clearEmptyComposerModelOverride() {}
function _clearRestoredGoalContinuationDraft() {}
function _clearExplicitPickerPick() {}
function _hydrateTodosFromSession() {}
function _acknowledgeSessionVisit() {}
function _sessionVisitHasUnreadState() { return false; }
function _uploadPendingFilesSyncProgressForSession() {}
function _messageUserUnpinned() {}
function _clearQueueCardDisplay() {}
function _saveComposerDraftNow() {}
function _handleComposerDraftPostFailure() {}
function _messageRenderableMessageCount() { return S.messages.length; }
function _currentMessageRenderWindowSize() { return 50; }
function _messageReloadLimitForSession() { return 2000; }
function _isSessionActivelyViewedForList() { return true; }
function _appendAnchorRowFingerprint(row) { return row ? 'fp-' + row.content : ''; }
function _carryForwardEphemeralTurnFields() { return arguments[1]; }
function syncTopbar() {}
function renderMessages() {}
function clearLiveToolCards() {}
function clearVisibleMessageRowCache() {}
function scheduleTodosRefresh() {}
function _isReadOnlySession() { return false; }
function _deferSessionSideEffect(sid, fn) { try { return Promise.resolve(fn()); } catch (e) { return Promise.resolve(); } }
function _ensureAllMessagesLoaded() {}
function _syncToolCallsForLoadedMessages() {}
function renderSessionList() {}
function showToast() {}
function t(k) { return k; }
const document = {createElement: () => ({style: {}, dataset: {}}), body: {appendChild() {}}, getElementById: () => null, querySelector: () => null};
const window = {location: {href: ''}};
const _allSessions = [];
"""
    ).replace("CARDS_PLACEHOLDER", cards)

    driver = """
(async () => {
  // loadSession() drives the real _ensureMessagesLoaded internally, so the
  // whole reload — metadata replacement, capture and append merge — happens
  // inside this one call.
  // keepStaleUntilLoaded is the focus-recovery path: the held transcript and
  // its prefix digest stay in place, so the append-only fast path can run
  // (this is the reload the maintainer reproduced in Chromium).
  await loadSession(SESSION_ID, {force: true, keepStaleUntilLoaded: true});
  const cards = (S.session.tool_calls || []).map(tc => tc.tid);
  console.log(JSON.stringify({cards, apiCalls: apiCalls.length}));
})().catch(e => { console.error(e && e.stack || e); process.exit(1); });
"""

    body = prelude + "\n" + _function(_SESSIONS, "loadSession") + "\n" + _function(
        _SESSIONS, "_ensureMessagesLoaded"
    ) + "\n"
    return (
        _CHROME
        + "const __BODY = " + repr(body)
        + ";\nconst __DRIVER = " + repr(driver)
        + ";\nvm.runInContext(__BODY, ctx);\nvm.runInContext(__DRIVER, ctx);\n"
    )


@_node_tests
def test_same_session_reload_keeps_older_session_level_cards():
    """The older session-level card survives the metadata replacement.

    RED before the fix: the capture site read ``S.session.tool_calls`` AFTER
    ``S.session=data.session`` had replaced it with the metadata response's
    empty ``tool_calls``, so the kept list was always null and the older card
    was dropped.
    """
    script = _harness(append_response_cards=[])
    out = json.loads(_run_node(script))
    assert out["cards"] == ["older-card"], (
        "the older session-level card was dropped by the same-session reload: "
        f"{out['cards']}"
    )


@_node_tests
def test_append_path_keeps_older_card_and_adds_the_new_one():
    """The append merge keeps the older card and adds the new one.

    This is the maintainer's reported Chromium behaviour: after a new card
    lands the list must be ``['older-card', 'tc-new']``, not just
    ``['tc-new']``.
    """
    script = _harness(
        append_response_cards=[{"tid": "tc-new", "assistant_msg_idx": 601, "summary": "new card"}]
    )
    out = json.loads(_run_node(script))
    assert out["cards"] == ["older-card", "tc-new"], (
        f"expected the older card to survive the append: {out['cards']}"
    )
