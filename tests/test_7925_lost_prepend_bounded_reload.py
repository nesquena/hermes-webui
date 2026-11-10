"""#7925 re-gate (2026-11-09): CharlesMcquade's lost-prepend repro.

A bounded same-session reload (``_ensureMessagesLoaded`` with a width the
500-row ceiling can express) replaces ``S.messages`` with the response's
tail window. That replace used to run without checking what the held view
had become while the request was in flight: completing *Load older*
(``_loadOlderMessages``) during the reload prepends server rows onto the
head, and the reload's response — sized for the pre-prepend view — then
starts NEWER than that head, so taking it wholesale silently drops every
prepended row. The view read ``100-150 @ offset 100`` where
``70-150 @ offset 70`` was on screen; the no-prepend control is correct.

The fix keeps the by-construction contract (no merge of two responses'
rows): when a response's ``_messages_offset`` is newer than the held
``_oldestIdx``, the reload re-fetches authoritatively from the live view —
a widened bounded window covering the held head when that width fits under
the ceiling, else master's bare full document, the only response that
starts at row 0.

These tests drive the REAL ``_ensureMessagesLoaded`` and
``_loadOlderMessages`` from static/sessions.js in a Node harness against a
fake server that mirrors api/routes.py's window contract, holding the
reload's response until the prepend has completed.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
SESSIONS_SRC = (_ROOT / "static" / "sessions.js").read_text(encoding="utf-8")

NODE_BIN = shutil.which("node")
_node_tests = pytest.mark.skipif(NODE_BIN is None, reason="node not on PATH")

#: The reload-path helpers extracted from sessions.js and fed to the harness,
#: so the REAL logic (the replace path under test and the paging prepend that
#: races it) runs here.
_HARNESS_FUNCTIONS = (
    "_ensureMessagesLoaded",
    "_loadOlderMessages",
    "_messageReloadLimitForSession",
    "_clearSameSessionForceReloadHint",
    "_appendAnchorRowFingerprint",
    "_markPrefixProvenRows",
    "_heldRowsArePrefixProven",
    "_adoptPrefixProof",
    "_mergeSessionToolCards",
    "_advanceVisibleBaselineForServerKnownRows",
    # The suffix-continuity check _loadOlderMessages runs on its page.
    "_sameTranscriptMessage",
    "_messageComparableText",
    "_stripAttachedFilesMarker",
    "_stripForcedSkillEnvelope",
    "_normalizeUserTranscriptText",
)

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
"""

#: Stub preamble: every free variable the extracted helpers touch is defined
#: here so the harness can drive the REAL functions without a DOM.
_STUBS = r"""
'use strict';
const _INITIAL_MSG_LIMIT = 30;
const _MSG_LIMIT_MAX = 500;
const MESSAGE_RENDER_WINDOW_DEFAULT = 50;
const INFLIGHT = {};
// CharlesMcquade's repro runs the prepend; the control does not.
const PREPEND = __PREPEND__;

let _loadingSessionId = 's1';
let _loadSessionGeneration = null;
let _oldestIdx = 100;
let _messagesTruncated = true;
let _msgLimitMax = 500;
let _sameSessionForceReloadHint = null;
let _pendingCarryForwardSnapshot = null;
// #7925 re-gate (2026-10-27, silent finding): the loaded card list kept
// across loadSession's metadata replacement.
let _pendingSessionToolCalls = null;
// The prefix digest the client holds for its view and the held end it
// covers — seeded below exactly like the load that established the view.
let _sessionPrefixFp = '';
let _sessionPrefixFpEnd = -1;
// Per-row provenance: which held rows are verbatim server rows.
let _prefixProvenRows = new WeakSet();
let _messageRenderWindowSize = 50;
let _loadingOlder = false;
let _messagesGeneration = 0;
let _scrollPinned = false;
const window = { _carryForwardEphemeralTurnFields: (prev, next) => next };
globalThis.requestAnimationFrame = (fn) => setTimeout(fn, 0);

// ── The transcript ─────────────────────────────────────────────────────────
// 150 rows to begin with; row 150 lands while the reload is in flight, so
// the released tail runs through row 150 (the repro's numbers).
const SERVER_ROWS = Array.from({length: 150}, (_, i) => ({
  role: i % 2 === 0 ? 'user' : 'assistant',
  content: 'm' + i,
  timestamp: 1700000000 + i,
}));
function clone(rows) { return rows.map(r => Object.assign({}, r)); }
function isRenderable(r) { return r.role === 'user' || r.role === 'assistant'; }
// Mirror api/routes.py's tail-window extraction: the newest `limit`
// RENDERABLE rows, offset = their absolute start.
function tailWindow(limit) {
  let start = 0, count = 0;
  for (let i = SERVER_ROWS.length - 1; i >= 0; i--) {
    if (isRenderable(SERVER_ROWS[i])) {
      count++;
      if (count >= limit) { start = i; break; }
    }
  }
  return {window: SERVER_ROWS.slice(start), offset: start};
}
function prefixFpFor(end) { return 'fp-' + end; }

// ── The fake server ────────────────────────────────────────────────────────
// Mirrors the window contract the reload and paging paths rely on:
//  - numeric msg_limit ≤ ceiling: the tail window of that many renderable
//    rows, offset = its absolute start;
//  - msg_before=N + msg_limit: the page of rows ending at N;
//  - no msg_limit: the full transcript (master's bare GET).
// The reload's request is HELD until the driver releases it, which is the
// window in which the Load older completes.
const apiCalls = [];
let phase = 'primary';
let releasePrimary = null;
const primaryHeld = new Promise(res => { releasePrimary = res; });

const S = {
  session: {
    session_id: 's1',
    transcript_generation: 7,
    // The sidebar poll ran ahead of the message fetch: it already counts
    // row 150, so the reload width is 50 held + 1 appended = 51.
    message_count: 151,
    visible_message_count: 151,
    regeneration_revision: null,
    tool_calls: [],
    _visibleBaselineLocal: 50,
    _visibleBaselineServer: 151,
  },
  messages: [],
  busy: false,
  lastUsage: {},
};

function _sessionPayload(offset, truncated, fpEnd, messages) {
  return {session: {
    session_id: 's1',
    transcript_generation: S.session.transcript_generation,
    message_count: SERVER_ROWS.length,
    visible_message_count: SERVER_ROWS.length,
    _msg_limit_max: 500,
    _messages_offset: offset,
    _messages_truncated: truncated,
    prefix_fp: prefixFpFor(fpEnd),
    tool_calls: [],
    messages: messages,
  }};
}

async function api(url) {
  apiCalls.push(url);
  const q = new URLSearchParams(url.split('?')[1] || '');
  const msgLimitRaw = q.get('msg_limit');
  const limit = (msgLimitRaw === null || msgLimitRaw === 'all') ? null : parseInt(msgLimitRaw, 10);
  if (q.has('msg_before')) {
    // _loadOlderMessages' backward page: the rows ending at _oldestIdx.
    const before = Number(q.get('msg_before'));
    const start = Math.max(0, before - (limit === null ? _INITIAL_MSG_LIMIT : limit));
    return _sessionPayload(start, start > 0, before, clone(SERVER_ROWS.slice(start, before)));
  }
  if (phase === 'primary') {
    // The bounded reload: held until the prepend has completed.
    await primaryHeld;
  }
  if (phase === 'released' && SERVER_ROWS.length === 150) {
    // Row 150 lands server-side as the original tail is released.
    SERVER_ROWS.push({role: 'user', content: 'm150', timestamp: 1700000150});
  }
  if (limit === null) {
    return _sessionPayload(0, false, SERVER_ROWS.length, clone(SERVER_ROWS));
  }
  const tw = tailWindow(limit);
  return _sessionPayload(tw.offset, tw.offset > 0, tw.offset + tw.window.length, clone(tw.window));
}

// ── DOM / renderer stubs ───────────────────────────────────────────────────
function _syncToolCallsForLoadedMessages() {}
function clearLiveToolCards() {}
function clearVisibleMessageRowCache() {}
function renderMessages() {}
function $(id) { return null; }
function _messageIsRenderable(m) { return !!m && (m.role === 'user' || m.role === 'assistant'); }
function _messageRenderableMessageCount() { return (S.messages || []).length; }
function _currentMessageRenderWindowSize() { return _messageRenderWindowSize; }
function _adoptRegenerationRevision() {}
function _hydrateTodosFromSession() {}
function scheduleTodosRefresh() {}
function _isSessionActivelyViewedForList() { return true; }
function _setSessionViewedCount() {}
function syncTopbar() {}
"""

#: The driver: seed the held view, start the bounded reload, complete the
#: Load older while it is in flight, then release the original tail.
_DRIVER = r"""
(async () => {
  // The held view: rows [100, 150) — the newest 50 of the 150-row
  // transcript, truncated (rows 0-99 unloaded). The load that established
  // it vouched for every row and minted the digest over [0, held end).
  S.messages = clone(SERVER_ROWS.slice(100, 150));
  _oldestIdx = 100;
  _messagesTruncated = true;
  _sessionPrefixFp = prefixFpFor(150);
  _sessionPrefixFpEnd = 150;
  _prefixProvenRows = new WeakSet();
  for (const m of S.messages) _prefixProvenRows.add(m);
  // The width hint loadSession captured before the reload: 50 loaded rows
  // of a 150-row transcript …
  _sameSessionForceReloadHint = {
    session_id: 's1', loaded_renderable_count: 50, loaded_message_count: 50,
    message_count: 150, truncated: true,
  };

  phase = 'primary';
  const reloadDone = _ensureMessagesLoaded('s1', {force: true});
  await new Promise(r => setTimeout(r, 0));
  if (PREPEND) {
    // … complete Load older while the reload is in flight: the wider tail
    // window [70, 150) comes back and its older rows [70, 100) are
    // prepended, leaving rows [70, 150) at offset 70 …
    phase = 'paging';
    await _loadOlderMessages();
  }
  // … then release the original tail, now running through row 150.
  phase = 'released';
  releasePrimary();
  await reloadDone;

  const contents = S.messages.map(m => m.content);
  console.log('RESULT:' + JSON.stringify({
    apiCalls: apiCalls,
    rows: S.messages.length,
    first: contents.length ? contents[0] : null,
    last: contents.length ? contents[contents.length - 1] : null,
    contents: contents,
    oldestIdx: _oldestIdx,
    truncated: _messagesTruncated,
    prependedSurvived: contents.indexOf('m70') >= 0 && contents.indexOf('m99') >= 0,
  }));
})().catch(e => {
  console.log('HARNESS_ERROR:' + (e && e.stack ? e.stack : String(e)));
  process.exit(1);
});
"""


def _run_harness(prepend: bool, tmp_path: Path) -> dict:
    assert NODE_BIN is not None, "node is required"
    stubs = _STUBS.replace("__PREPEND__", "true" if prepend else "false")
    script = (
        "const SESSIONS_JS = " + repr(SESSIONS_SRC) + ";\n"
        + _VM_COMMON
        + stubs
        + "\n"
        + "\n".join(_extract(name) for name in _HARNESS_FUNCTIONS)
        + "\n"
        + _DRIVER
    )
    script_path = tmp_path / "harness.js"
    script_path.write_text(script, encoding="utf-8")
    completed = subprocess.run(
        [NODE_BIN, str(script_path)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    result_lines = [
        line for line in completed.stdout.splitlines()
        if line.startswith("RESULT:")
    ]
    assert result_lines, (
        f"node produced no parseable output\nstdout={completed.stdout}\nstderr={completed.stderr}"
    )
    return json.loads(result_lines[-1][len("RESULT:"):])


def _extract(name: str) -> str:
    marker = f"function {name}("
    start = SESSIONS_SRC.index(marker)
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


@_node_tests
def test_bounded_reload_drops_no_prepended_rows(tmp_path):
    """The repro: a bounded reload held while Load older completes must not
    discard the prepended rows when its tail is released.

    Held view rows [100, 150); Load older prepends rows [70, 100); the
    released tail runs through row 150. The window must end at 70-150 @
    offset 70, not 100-150 @ offset 100.
    """
    result = _run_harness(prepend=True, tmp_path=tmp_path)
    # The prepended head survived …
    assert result["prependedSurvived"] is True, result
    # … and the released tail EXTENDED the paged view instead of replacing it:
    # 70-150 at offset 70 (CharlesMcquade's expected window).
    assert result["oldestIdx"] == 70, result
    assert result["rows"] == 81, result
    assert result["first"] == "m70", result
    assert result["last"] == "m150", result
    assert result["truncated"] is True, result
    assert result["contents"] == [f"m{i}" for i in range(70, 151)], result
    # The remedy stayed inside the by-construction contract and the #7899
    # budget: a widened authoritative window (bounded, no merge), NOT
    # master's bare full-transcript GET and not a msg_before page.
    requests = result["apiCalls"]
    assert len(requests) == 3, requests
    assert "msg_limit=51" in requests[0], requests          # the held bounded reload
    assert "msg_limit=80" in requests[1], requests          # the Load older window
    widened = requests[2]
    assert "msg_before=" not in widened and "msg_after=" not in widened, requests
    assert "msg_limit=all" not in widened, requests
    width = int(widened.split("msg_limit=")[1].split("&")[0])
    assert 80 <= width <= 500, (
        f"the covering window must span the held view under the ceiling, got {width}"
    )


@_node_tests
def test_bounded_reload_without_a_prepend_keeps_the_tail_replace(tmp_path):
    """The no-prepend control: with nothing paged in during the flight the
    reload stays the single bounded tail replace (the window starts exactly
    at the held head), so the guard costs no extra request.
    """
    result = _run_harness(prepend=False, tmp_path=tmp_path)
    requests = result["apiCalls"]
    assert len(requests) == 1, requests
    assert "msg_limit=51" in requests[0], requests
    assert result["oldestIdx"] == 100, result
    assert result["rows"] == 51, result
    assert result["first"] == "m100", result
    assert result["last"] == "m150", result
    assert result["truncated"] is True, result
