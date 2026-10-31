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

    body = (
        prelude
        + "\n"
        + _function(_SESSIONS, "_mergeSessionToolCards")
        + "\n"
        + _function(_SESSIONS, "loadSession")
        + "\n"
        + _function(_SESSIONS, "_ensureMessagesLoaded")
        + "\n"
    )
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


# ── 2026-10-31 re-gate, finding 2: a prepend must keep the held cards ───────
# The prepend path handed ``_syncToolCallsForLoadedMessages`` ONLY the incoming
# page's cards, so every session-level card the held view already owned was
# dropped. A later focus reload cannot repair it: on an over-ceiling reload the
# append-only fast path answers with an empty window whose card list is empty
# by construction (``_tool_calls_for_message_window`` selects the returned
# rows' owners), so the merge is skipped and the dropped card stays missing —
# where master's full reload delivers every card. The prepend now keeps the
# held cards, shifts their owner indices by the prepended length and merges the
# page's cards before the sync.


def _helper_slice() -> str:
    """The sessions.js helper block ``_loadOlderMessages`` leans on."""
    start = _SESSIONS.find("function _messageComparableText")
    end = _SESSIONS.find("// Load older messages", start)
    assert start != -1 and end != -1, "sessions.js helper slice markers not found"
    return _SESSIONS[start:end]


def _merge_helper_src() -> str:
    """The production card merge, or a stand-in for the PRE-FIX prepend.

    The RED control has to run on a tree that does not have the fix yet, where
    ``_mergeSessionToolCards`` does not exist. The stand-in reproduces exactly
    what the unfixed prepend did — hand the sync the incoming page's cards and
    nothing else — so the same test fails there with the maintainer's symptom
    (the held view's card is dropped) instead of erroring out.
    """
    marker = "function _mergeSessionToolCards("
    if marker in _SESSIONS:
        return _function(_SESSIONS, "_mergeSessionToolCards")
    return (
        "// PRE-FIX stand-in: the unfixed prepend handed _syncToolCallsForLoadedMessages\n"
        "// ONLY the incoming page's cards.\n"
        "function _mergeSessionToolCards(existingCards, incomingCards) {\n"
        "  return Array.isArray(incomingCards) ? incomingCards.slice() : [];\n"
        "}\n"
    )


def _load_older_harness(*, page_cards) -> str:
    """Node script: the real ``_loadOlderMessages`` (backward paging) followed
    by the real focus-reload loader, against a controllable transport.

    The scenario is the maintainer's Chromium repro: a 602-row session whose
    client holds the 500-row tail window (absolute rows 102..602) together with
    the session-level card owned by absolute row 400; the user pages older,
    which prepends absolute rows 0..102 and delivers the card owned by absolute
    row 5; a focus reload then takes the append-only fast path with an EMPTY
    window. Master's full reload keeps both cards.
    """
    prelude = """
// The canonical 602-row transcript: row 5 and row 400 are assistant rows.
const TRANSCRIPT = Array.from({length: 602}, (_, i) => ({
  role: i % 2 === 0 ? 'user' : 'assistant',
  content: 'm' + i,
  timestamp: 1700000000 + i,
}));
const S = {
  session: {
    session_id: 'sid-7925-page',
    transcript_generation: 3,
    tool_calls: __HELD_CARDS__,
    message_count: 602,
    visible_message_count: 602,
  },
  messages: TRANSCRIPT.slice(102),
  toolCalls: __HELD_CARDS__.map(tc => Object.assign({}, tc, {done: true})),
  busy: false,
};
let _loadingSessionId = null;
let _loadSessionGeneration = 1;
let _oldestIdx = 102;
let _messagesTruncated = true;
let _msgLimitMax = 500;
let _pendingCarryForwardSnapshot = null;
let _sessionPrefixFp = 'fp-held-602';
let _sessionPrefixFpEnd = 602;
let _pendingSessionToolCalls = null;
let _messageRenderWindowSize = 50;
let _messagesGeneration = 0;
let _loadingOlder = false;
const MESSAGE_RENDER_WINDOW_DEFAULT = 50;
const _INITIAL_MSG_LIMIT = 30;
const INFLIGHT = {};
const apiCalls = [];
const SESSION_ID = 'sid-7925-page';
const PAGE_CARDS = __PAGE_CARDS__;

function api(url, opts) {
  apiCalls.push(url);
  if (url.indexOf('msg_before=102') >= 0) {
    // The older page: absolute rows 0..102, rebased onto the page, carrying
    // the card owned by absolute row 5.
    return Promise.resolve({session: {
      messages: TRANSCRIPT.slice(0, 102),
      _messages_offset: 0,
      _messages_truncated: false,
      _msg_limit_max: 500,
      message_count: 602,
      visible_message_count: 602,
      session_id: SESSION_ID,
      transcript_generation: 3,
      tool_calls: PAGE_CARDS,
    }});
  }
  if (url.indexOf('msg_after=') >= 0) {
    // The focus reload's EMPTY append window: no new rows, and therefore no
    // session-level cards (the server selects the returned rows' owners).
    return Promise.resolve({session: {
      _append_only: true,
      _messages_truncated: false,
      _messages_offset: 602,
      _msg_limit_max: 500,
      message_count: 602,
      visible_message_count: 602,
      messages: [],
      prefix_fp: 'fp-after-append',
      tool_calls: [],
    }});
  }
  // The metadata response carries NO session-level cards.
  return Promise.resolve({session: {
    _messages_truncated: false,
    _messages_offset: 0,
    _msg_limit_max: 500,
    message_count: 602,
    visible_message_count: 602,
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
function renderSessionList() {}
function showToast() {}
function t(k) { return k; }
function $() { return null; }
function msgContent(m) { return String((m && m.content) || ''); }
const document = {createElement: () => ({style: {}, dataset: {}}), body: {appendChild() {}}, getElementById: () => null, querySelector: () => null};
const window = {location: {href: ''}};
const _allSessions = [];
"""
    # The held view's one card is owned by absolute row 400, i.e. view-relative
    # 298 against the tail window that starts at absolute row 102.
    held_cards = [{"tid": "newer-card", "assistant_msg_idx": 298, "summary": "newer card"}]
    prelude = (
        prelude.replace("__PAGE_CARDS__", json.dumps(page_cards))
        .replace("__HELD_CARDS__", json.dumps(held_cards))
    )

    driver = """
(async () => {
  // Phase 1: backward paging prepends absolute rows 0..102.
  await _loadOlderMessages();
  const afterPage = (S.toolCalls || []).map(tc => tc.tid);
  const afterPageIdx = (S.toolCalls || []).map(tc => tc.assistant_msg_idx);
  const messagesAfterPage = (S.messages || []).length;
  // Phase 2: the focus reload takes the append-only fast path (empty window).
  await loadSession(SESSION_ID, {force: true, keepStaleUntilLoaded: true});
  const cards = (S.toolCalls || []).map(tc => tc.tid);
  const idxs = (S.toolCalls || []).map(tc => tc.assistant_msg_idx);
  console.log(JSON.stringify({afterPage, afterPageIdx, cards, idxs, messagesAfterPage, apiCalls: apiCalls.length}));
})().catch(e => { console.error(e && e.stack || e); process.exit(1); });
"""

    body = (
        prelude
        + "\n"
        + _helper_slice()
        + "\n"
        + _merge_helper_src()
        + "\n"
        + _function(_SESSIONS, "_advanceVisibleBaselineForServerKnownRows")
        + "\n"
        + _function(_SESSIONS, "_bumpMessagesGeneration")
        + "\n"
        + _function(_SESSIONS, "_loadOlderMessages")
        + "\n"
        + _function(_SESSIONS, "_syncToolCallsForLoadedMessages")
        + "\n"
        + _function(_SESSIONS, "loadSession")
        + "\n"
        + _function(_SESSIONS, "_ensureMessagesLoaded")
        + "\n"
    )
    return (
        _CHROME
        + "const __BODY = " + repr(body)
        + ";\nconst __DRIVER = " + repr(driver)
        + ";\nvm.runInContext(__BODY, ctx);\nvm.runInContext(__DRIVER, ctx);\n"
    )


@_node_tests
def test_backward_paging_keeps_the_held_session_level_cards():
    """Finding 2 end to end: a prepend keeps the held cards (owner indices
    shifted by the prepended length) and merges the page's cards, so the focus
    reload's empty append cannot leave a card missing.

    RED-before: the prepend hands the sync only the page's cards, so the card
    owned by a row the client already held is dropped and the empty append
    never restores it — master's full reload brings it back.
    """
    page_cards = [{"tid": "older-card", "assistant_msg_idx": 5, "summary": "older card"}]
    out = json.loads(_run_node(_load_older_harness(page_cards=page_cards)))
    assert out["messagesAfterPage"] == 602, (
        f"the prepend did not land: {out['messagesAfterPage']} rows held"
    )
    # Held cards keep their relative order at the head of the merged list; the
    # page's cards follow (the same convention the append merge uses).
    assert out["afterPage"] == ["newer-card", "older-card"], (
        "backward paging dropped the session-level cards the held view owned: "
        f"{out['afterPage']}"
    )
    assert out["afterPageIdx"] == [400, 5], (
        "the held card's owner index must shift by the prepended length "
        "(298 + 102 = 400): "
        f"{out['afterPageIdx']}"
    )
    assert out["cards"] == ["newer-card", "older-card"], (
        "the focus reload's empty append lost a session-level card that "
        "master's full reload restores: "
        f"{out['cards']}"
    )
    assert out["idxs"] == [400, 5], out["idxs"]


@_node_tests
def test_backward_paging_merges_an_overlapping_card_from_the_page():
    """The same card arriving on the page must not duplicate or double-shift.

    A cumulative-tail page (or a re-paged window) can deliver a card the held
    view already owns; the merge keys on tid and the incoming record wins, so
    the list stays one entry per card with the page's rebased index. Green
    before and after the fix — it pins the merge boundary so the fix cannot
    over-merge (a duplicate card, or a card shifted twice).
    """
    page_cards = [
        {"tid": "older-card", "assistant_msg_idx": 5, "summary": "older card"},
        # The held card, re-delivered on the page with its absolute index.
        {"tid": "newer-card", "assistant_msg_idx": 400, "summary": "newer card"},
    ]
    out = json.loads(_run_node(_load_older_harness(page_cards=page_cards)))
    assert sorted(out["cards"]) == ["newer-card", "older-card"], (
        f"an overlapping page card duplicated the held card: {out['cards']}"
    )
    assert sorted(out["idxs"]) == [5, 400], out["idxs"]
