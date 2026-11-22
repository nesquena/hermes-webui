"""Behavioral regression for the #6392/#6421 stale older-page race.

A same-session force reload installs a fresh authoritative transcript. If a
``_loadOlderMessages()`` continuation was in flight across that reload, its
post-await generation guard is the only thing that stops it from prepending
rows that belong to the previous transcript and rewinding the paging cursor.

``_loadOlderMessages()`` already compares ``_messagesGeneration`` against a
snapshot taken before its await; nothing bumped the counter on the wholesale
replace, so the guard always passed and the stale page was prepended onto the
fresh rows (measured on master ``ff60081ca``: the transcript became
``[m90..m99, fresh-200..fresh-204]`` and ``_oldestIdx`` went from ``200`` back
to ``90``).

The two cases here drive the production ``_loadOlderMessages`` and
``_ensureMessagesLoaded`` (extracted from ``static/sessions.js``, never
re-implemented) against a deferred fake transport:

1. stale page resolving after the reload must be dropped entirely;
2. the positive control: an older page with no intervening reload still
   prepends (the guard must not turn pagination off).
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.test_cross_session_message_load_isolation import _extract_function

REPO = Path(__file__).resolve().parents[1]
SESSIONS_SRC = (REPO / "static" / "sessions.js").read_text(encoding="utf-8")
NODE = shutil.which("node")

PROD_FUNCS_SRC = "\n\n".join(
    _extract_function(SESSIONS_SRC, name)
    for name in ("_bumpMessagesGeneration", "_loadOlderMessages", "_ensureMessagesLoaded")
)

_NODE_SCRIPT_TEMPLATE = r'''
import assert from 'node:assert/strict';

// ---- deferred transport ---------------------------------------------------
const calls = [];
const pending = [];
function enqueue(url) {
  const entry = { url: String(url), landed: false };
  entry.promise = new Promise((resolve) => { entry._resolve = resolve; });
  calls.push(entry);
  return entry;
}
async function api(url) {
  const want = String(url);
  const entry = calls.find((c) => !c.landed && c.url === want);
  if (!entry) {
    throw new Error('Unexpected API call: ' + want + '\nqueued=' + JSON.stringify(calls.map((c) => c.url)));
  }
  entry.landed = true;
  pending.push(entry);
  return entry.promise;
}
async function waitFor(url) {
  const want = String(url);
  for (let i = 0; i < 500; i++) {
    if (pending.some((p) => p.url === want)) return;
    await new Promise((r) => setImmediate(r));
  }
  throw new Error('request was never issued: ' + want);
}

// ---- module-scope state read by the extracted functions -------------------
globalThis._messagesGeneration = 0;
globalThis._loadingOlder = false;
globalThis._loadingSessionId = null;
globalThis._loadSessionGeneration = 0;
globalThis._messagesTruncated = false;
globalThis._oldestIdx = 0;
globalThis._msgLimitMax = 5;
globalThis._messageReloadLimitValue = 2;
globalThis._messageRenderWindowSize = 10;
globalThis._programmaticScroll = false;
globalThis._scrollPinned = true;
globalThis.MESSAGE_RENDER_WINDOW_DEFAULT = 50;
globalThis._MSG_LIMIT_MAX = 500;
globalThis._INITIAL_MSG_LIMIT = 2;

const renderCalls = [];
const toolSyncCalls = [];
const S = { session: null, messages: [], toolCalls: [], busy: false, activeStreamId: null };

function rows(prefix, from, count) {
  const out = [];
  for (let i = 0; i < count; i++) {
    out.push({ role: i % 2 === 0 ? 'user' : 'assistant', content: prefix + (from + i) });
  }
  return out;
}

// ---- stubs for helpers that live in other modules -------------------------
function _messageReloadLimitForSession() { return _messageReloadLimitValue; }
function _clearSameSessionForceReloadHint() {}
function _syncToolCallsForLoadedMessages(msgs) { toolSyncCalls.push((msgs || []).length); }
function clearLiveToolCards() {}
function clearVisibleMessageRowCache() {}
function _advanceVisibleBaselineForServerKnownRows() {}
function _currentMessageRenderWindowSize() { return 10; }
function _messageRenderableMessageCount() { return 10; }
function _messageIsRenderable() { return true; }
function _adoptRegenerationRevision() {}
function _syncCtxIndicator() {}
function _hydrateTodosFromSession() {}
function scheduleTodosRefresh() {}
function syncTopbar() {}
function _setSessionViewedCount() {}
function _isSessionActivelyViewedForList() { return true; }
function renderMessages() { renderCalls.push((S.messages || []).length); }
function $(id) { return null; }
function _sameTranscriptMessage() { return false; }
const window = {};
const performance = { now: () => 0 };
function requestAnimationFrame() {}

globalThis.window = window;
globalThis.performance = performance;
globalThis.requestAnimationFrame = requestAnimationFrame;
Object.assign(globalThis, {
  api, S, $, renderMessages,
  _messageReloadLimitForSession, _clearSameSessionForceReloadHint,
  _syncToolCallsForLoadedMessages, clearLiveToolCards, clearVisibleMessageRowCache,
  _advanceVisibleBaselineForServerKnownRows, _currentMessageRenderWindowSize,
  _messageRenderableMessageCount, _messageIsRenderable, _adoptRegenerationRevision,
  _syncCtxIndicator, _hydrateTodosFromSession, scheduleTodosRefresh, syncTopbar,
  _setSessionViewedCount, _isSessionActivelyViewedForList, _sameTranscriptMessage,
});

// ---- production source under test ----------------------------------------
__PROD_FUNCS_SRC__

const OLDER_URL = '/api/session?session_id=sid-a&messages=1&resolve_model=0&msg_before=100&msg_limit=2';
const RELOAD_URL = '/api/session?session_id=sid-a&messages=1&resolve_model=0&msg_limit=2&expand_renderable=1';

function resetState() {
  calls.length = 0;
  pending.length = 0;
  renderCalls.length = 0;
  toolSyncCalls.length = 0;
  globalThis._messagesGeneration = 0;
  globalThis._loadingOlder = false;
  globalThis._loadingSessionId = 'sid-a';
  globalThis._loadSessionGeneration = 1;
  globalThis._messagesTruncated = true;
  globalThis._oldestIdx = 100;
  globalThis._msgLimitMax = 5;
  globalThis._messageReloadLimitValue = 2;
  globalThis._messageRenderWindowSize = 10;
  globalThis._programmaticScroll = false;
  globalThis._scrollPinned = true;
  S.session = { session_id: 'sid-a', message_count: 110, tool_calls: [], regeneration_revision: 1 };
  S.messages = rows('m', 100, 10);
  S.toolCalls = [];
  S.busy = false;
  S.activeStreamId = null;
}

function freshTranscript() {
  return {
    session: {
      session_id: 'sid-a',
      _messages_truncated: true,
      _messages_offset: 200,
      messages: rows('fresh-', 200, 5),
      message_count: 205,
      tool_calls: [],
      regeneration_revision: 2,
    },
  };
}

function staleOlderPage() {
  return {
    session: {
      session_id: 'sid-a',
      _messages_truncated: true,
      _messages_offset: 90,
      messages: rows('m', 90, 10),
      message_count: 205,
      tool_calls: [],
    },
  };
}

function contents() {
  return (S.messages || []).map((m) => String(m.content));
}

// Case 1: the continuation was in flight BEFORE the reload started.
async function runStaleContinuationAcrossReload() {
  resetState();
  const older = enqueue(OLDER_URL);
  const reload = enqueue(RELOAD_URL);
  const olderRun = _loadOlderMessages();
  await waitFor(OLDER_URL);
  const reloadRun = _ensureMessagesLoaded('sid-a', { force: true, loadGeneration: 1 });
  await waitFor(RELOAD_URL);
  reload._resolve(freshTranscript());
  await reloadRun;
  const afterReload = contents();
  older._resolve(staleOlderPage());
  await olderRun;
  return { afterReload, final: contents(), oldestIdx: _oldestIdx, truncated: _messagesTruncated };
}

// Case 2: the continuation started DURING the reload's api() round-trip,
// after loadSession already bumped and before the publish bump.
async function runContinuationStartedDuringRoundTrip() {
  resetState();
  const reload = enqueue(RELOAD_URL);
  const older = enqueue(OLDER_URL);
  const reloadRun = _ensureMessagesLoaded('sid-a', { force: true, loadGeneration: 1 });
  await waitFor(RELOAD_URL);
  const olderRun = _loadOlderMessages();
  await waitFor(OLDER_URL);
  reload._resolve(freshTranscript());
  await reloadRun;
  const afterReload = contents();
  older._resolve(staleOlderPage());
  await olderRun;
  return { afterReload, final: contents(), oldestIdx: _oldestIdx, truncated: _messagesTruncated };
}

// Case 3 (control): no reload intervened, so the page must still land.
async function runOlderPageWithoutReload() {
  resetState();
  const older = enqueue(OLDER_URL);
  const olderRun = _loadOlderMessages();
  await waitFor(OLDER_URL);
  older._resolve(staleOlderPage());
  await olderRun;
  return { final: contents(), oldestIdx: _oldestIdx, truncated: _messagesTruncated };
}

const out = {
  staleAcrossReload: await runStaleContinuationAcrossReload(),
  startedDuringRoundTrip: await runContinuationStartedDuringRoundTrip(),
  olderPageWithoutReload: await runOlderPageWithoutReload(),
};
console.log(JSON.stringify(out));
'''.strip()


def _run_node(tmp_path: Path) -> dict:
    assert NODE is not None, "node is required"
    script = _NODE_SCRIPT_TEMPLATE.replace("__PROD_FUNCS_SRC__", PROD_FUNCS_SRC)
    script_path = tmp_path / "issue6421-older-page-generation.mjs"
    script_path.write_text(script, encoding="utf-8")
    completed = subprocess.run(
        [NODE, str(script_path)],
        cwd=str(REPO),
        text=True,
        capture_output=True,
        timeout=90,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr or completed.stdout
    lines = [line for line in completed.stdout.splitlines() if line.strip()]
    assert lines, f"node produced no output\nstdout={completed.stdout}\nstderr={completed.stderr}"
    return json.loads(lines[-1])


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_stale_older_page_is_dropped_after_a_same_session_reload(tmp_path):
    body = _run_node(tmp_path)
    case = body["staleAcrossReload"]
    assert case["afterReload"] == ["fresh-200", "fresh-201", "fresh-202", "fresh-203", "fresh-204"]
    # The continuation belonged to the replaced transcript: nothing it brings
    # back may land on top of the fresh rows or rewind the paging cursor.
    assert case["final"] == case["afterReload"], (
        "stale older page was prepended onto the reloaded transcript"
    )
    assert case["oldestIdx"] == 200


@pytest.mark.skipif(NODE is None, reason="node not on path")
def test_continuation_started_during_the_reload_round_trip_is_dropped(tmp_path):
    body = _run_node(tmp_path)
    case = body["startedDuringRoundTrip"]
    assert case["afterReload"] == ["fresh-200", "fresh-201", "fresh-202", "fresh-203", "fresh-204"]
    assert case["final"] == case["afterReload"], (
        "a continuation started inside the reload round-trip was prepended after the publish"
    )
    assert case["oldestIdx"] == 200


@pytest.mark.skipif(NODE is None, reason="node not on path")
def test_older_page_without_a_reload_still_prepends(tmp_path):
    body = _run_node(tmp_path)
    case = body["olderPageWithoutReload"]
    assert case["final"] == [f"m{index}" for index in range(90, 100)] + [
        f"m{index}" for index in range(100, 110)
    ]
    assert case["oldestIdx"] == 90
