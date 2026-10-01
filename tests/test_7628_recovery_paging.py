"""Behavioral tests for #7628 review round: /compress paging restore + suffix
strategy for repeated identical turns.

Round-3 review (nesquena-hermes 2026-09-19) found two data-integrity blockers:

1. [CORE] A successful /compress returns the FULL transcript, but
   `_applyManualCompressionResult` did not restore `_messagesTruncated` /
   `_oldestIdx` — the bounded preflight's values (e.g. truncated=true,
   oldestIdx=70) survived, producing a false "70 older messages" affordance
   and shifting every absolute transcript index by 70.

2. [SILENT] `_restoreSettledSession` preservation used
   `(_stagedMatchesCurrentPrefix || _stagedMatchesCurrentSuffix)`, so when
   identical repeated turns made BOTH comparisons succeed the prefix branch
   spliced at the wrong offset — silently dropping and duplicating rows.
   The server truncation signal now decides: truncated => suffix-exclusive.

Node-backed tests extract the real functions from static/ and drive them
against a stub DOM (same harness pattern as test_issue5224).
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
_COMMANDS_JS = _ROOT / "static" / "commands.js"
_MESSAGES_JS = _ROOT / "static" / "messages.js"
_SESSIONS_JS = _ROOT / "static" / "sessions.js"
_NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(_NODE is None, reason="node is required to execute message runtime tests")


_DRIVER = r"""
const fs = require('fs');
const commandsSrc = fs.readFileSync(process.argv[2], 'utf8');
const messagesSrc = fs.readFileSync(process.argv[3], 'utf8');
const sessionsSrc = fs.readFileSync(process.argv[4], 'utf8');
const scenario = JSON.parse(process.argv[5] || '{}');

function extractFunction(source, name) {
  const markers = [`async function ${name}(`, `function ${name}(${name}`, `function ${name}(`, `${name}=async(`, `${name}=async (`];
  let start = -1;
  for (const marker of markers) {
    start = source.indexOf(marker);
    if (start >= 0) break;
  }
  if (start < 0) throw new Error(`missing ${name}`);
  let i = source.indexOf('{', start);
  if (i < 0) throw new Error(`missing body for ${name}`);
  let depth = 1; i++;
  while (i < source.length) {
    const ch = source[i];
    if (ch === '{') depth++;
    if (ch === '}') { depth--; if (depth === 0) return source.slice(start, i + 1); }
    i++;
  }
  throw new Error(`unterminated body for ${name}`);
}

function install(name, src) {
  const body = extractFunction(src, name);
  // Drop the `async function name(` or `function name(` prefix, keep body.
  const eq = body.indexOf('{');
  const inner = body.slice(eq);
  globalThis[name] = new Function(`return (${name === 'jumpToSessionStart' ? 'async function' : 'function'} ${inner})`)();
}

// ---- helpers from messages.js (mirror test_issue5224 set) ----
const MESSAGE_HELPERS = [
  "_isMarkerOnlyAssistantMessage", "_streamRecoveryControlMessageText",
  "_streamRecoveryControlMessage", "_filterRecoveryControlMessages",
  "_replaceMarkerOnlyAssistantWithStreamError", "_messageIdentityKey",
  "_isHistoricalAnchorActivityScene", "_carryForwardEphemeralTurnFields",
  "_isTerminalStreamErrorMarkerMessage", "_restoreSettledSession",
  "_mergeSettledToolCallsWithLiveMetadata",
];
for (const h of MESSAGE_HELPERS) {
  const body = extractFunction(messagesSrc, h);
  const factory = new Function(`${body}; return ${h};`);
  globalThis[h] = factory();
}

// ---- compress result applier from commands.js ----
const COMPRESS_BODY = extractFunction(commandsSrc, '_applyManualCompressionResult');
// Rebuild as a normal function (it contains `await` – keep async).
globalThis._applyManualCompressionResult = new Function(
  'return (' + COMPRESS_BODY + ')'
)();

function runtimeStubs() {
  const calls = [];
  globalThis.S = { session: { session_id: 's1' }, messages: [], toolCalls: [] };
  globalThis.activeSid = 's1';
  globalThis.streamId = 'stream-1';
  globalThis.assistantText = false;
  globalThis._messagesTruncated = !!scenario.prestateTruncated;
  globalThis._oldestIdx = scenario.prestateOldestIdx || 0;
  globalThis._messagesGeneration = 1;
  globalThis._loadingOlder = false;
  globalThis._loadingSessionId = null;
  globalThis._messageRenderWindowSize = 50;
  globalThis._msgLimitMax = scenario.msgLimitMax || 200;
  globalThis._INITIAL_MSG_LIMIT = 50;
  globalThis._EPHEMERAL_TURN_FIELDS = ['_turnUsage','_turnDuration','_turnTps','_gatewayRouting','_statusCard','_anchor_stream_id','_anchor_activity_scene'];
  globalThis._isActiveSession = () => scenario.isActiveSession !== false;
  globalThis._bailOutOfTerminalEventsFromStaleStream = () => false;
  globalThis._isSessionCurrentPane = () => scenario.isSessionCurrentPane !== false;
  globalThis._isSessionActivelyViewed = () => !!scenario.isSessionActivelyViewed;
  globalThis._closeSource = () => calls.push('closeSource');
  globalThis._clearStreamEndRecovery = () => calls.push('clearStreamEndRecovery');
  globalThis._clearOwnerInflightState = () => calls.push('clearOwnerInflight');
  globalThis.clearLiveToolCards = () => calls.push('clearLiveToolCards');
  globalThis.removeThinking = () => calls.push('removeThinking');
  globalThis._flushReasoningToAnchor = () => calls.push('flushReasoning');
  globalThis._applyToAnchor = () => calls.push('applyToAnchor');
  globalThis._hydrateTodosFromSession = () => calls.push('hydrateTodos');
  globalThis._scheduleAnchorRegistryCleanup = () => calls.push('scheduleAnchorRegistryCleanup');
  globalThis._smdEndParser = () => calls.push('smdEndParser');
  globalThis._markSessionCompletionUnread = () => calls.push('markCompletionUnread');
  globalThis._markSessionViewed = () => calls.push('markSessionViewed');
  globalThis.localStorage = { setItem: () => calls.push('setLocalStorageItem'), getItem: () => null, removeItem: () => calls.push('removeLocalStorageItem') };
  globalThis._setActiveSessionUrl = () => calls.push('setActiveSessionUrl');
  globalThis.showToast = () => calls.push('showToast');
  globalThis._clearApprovalForOwner = () => calls.push('clearApprovalForOwner');
  globalThis._clearClarifyForOwner = () => calls.push('clearClarifyForOwner');
  globalThis._streamFadeCleanupReduceMotionListener = () => calls.push('streamFadeCleanup');
  globalThis._cancelThrottledSnapshotTimer = () => calls.push('cancelThrottledSnapshot');
  globalThis._clearAnchorProseIncrementalNode = () => calls.push('clearAnchorProse');
  globalThis._cancelAnimationFramePendingStreamRender = () => calls.push('cancelRaf');
  globalThis.finalizeThinkingCard = () => calls.push('finalizeThinkingCard');
  globalThis.syncTopbar = () => calls.push('syncTopbar');
  globalThis.renderMessages = () => calls.push('renderMessages');
  globalThis.renderSessionList = () => calls.push('renderSessionList');
  globalThis.updateQueueBadge = () => calls.push('updateQueueBadge');
  globalThis._setActivePaneIdleIfOwner = () => calls.push('setActivePaneIdle');
  globalThis.setBusy = () => calls.push('setBusy');
  globalThis.setComposerStatus = () => calls.push('setComposerStatus');
  globalThis.setCompressionUi = () => calls.push('setCompressionUi');
  globalThis.setStatus = () => calls.push('setStatus');
  globalThis.msgContent = (m) => (m && m.content && typeof m.content === 'string' ? m.content : '');
  globalThis._isContextCompactionMessage = () => false;
  globalThis._setCompressionSessionLock = () => calls.push('setCompressionSessionLock');
  globalThis._messageRenderableMessageCount = () => scenario.messageRenderableCount || 50;
  globalThis._currentMessageRenderWindowSize = () => scenario.currentWindowSize || 12;
  globalThis._restoreMessageRenderWindowAfterSettledRender = () => calls.push('restoreRenderWindow');
  globalThis.projectSessionArtifactsForOwner = () => calls.push('projectArtifacts');
  globalThis._streamFinalized = false;
  globalThis._persistTimer = null;
  globalThis._attachProjectedAnchorSceneToLastAssistant = () => calls.push('attachProjected');
  globalThis._isMessagePaneNearBottom = () => true;
  globalThis._isMessageReaderUnpinned = () => false;
  globalThis._queueDrainSid = null;
  // reducer used by _restoreSettledSession (mirrors live code)
  return calls;
}

(async () => {
  const calls = runtimeStubs();

  if (scenario.action === 'compress_restores_paging') {
    // Pre-state: bounded preflight left truncated=true, oldestIdx=70.
    globalThis._messagesTruncated = true;
    globalThis._oldestIdx = 70;
    const fullRows = [];
    for (let i = 0; i < 100; i++) {
      fullRows.push({ role: i % 2 === 0 ? 'user' : 'assistant', content: `row-${i}`, _ts: `ts-${i}` });
    }
    const payload = {
      session: {
        session_id: 's1',
        message_count: 100,
        messages: fullRows,
        tool_calls: [],
        _messages_truncated: false,
        _messages_offset: 0,
      },
    };
    await globalThis._applyManualCompressionResult(payload, '', 100, '/compress');
    console.log(JSON.stringify({
      action: 'compress_restores_paging',
      messagesTruncated: !!globalThis._messagesTruncated,
      oldestIdx: globalThis._oldestIdx,
      messageCountAfter: Array.isArray(S.messages) ? S.messages.length : -1,
      calls,
    }));
    return;
  }

  if (scenario.action === 'settle_repeated_turns') {
    // Bounded settle against a transcript containing repeated identical turns
    // (same role/content but distinct _ids). Round-2 bug: BOTH prefix and
    // suffix matched, prefix won, and rows were dropped+duplicated.
    const state = scenario.state || {};
    globalThis.S.session = { session_id: 's1', message_count: state.messageCount };
    globalThis.S.messages = state.messages || [];
    globalThis.streamId = 'stream-7628';
    globalThis.S.activeStreamId = 'stream-7628';
    globalThis.api = async () => scenario.apiPayload || { session: null };
    const status = await globalThis._restoreSettledSession({}, { status: true, preserveVisibleOnShorterTerminalSnapshot: true });
    const msgs = Array.isArray(globalThis.S.messages) ? globalThis.S.messages : [];
    console.log(JSON.stringify({
      action: 'settle_repeated_turns',
      status,
      messages: msgs.map((m) => ({ role: m.role, content: m.content, _ts: m._ts, _id: m._id || null })),
      oldestIdx: globalThis._oldestIdx,
      messagesTruncated: !!globalThis._messagesTruncated,
      terminalMarkerCount: msgs.filter(globalThis._isTerminalStreamErrorMarkerMessage).length,
      calls,
    }));
    return;
  }

  if (scenario.action === 'load_older_paging') {
    // After a bounded recovery (offset=70), "Load earlier" must request a page
    // keyed off _oldestIdx (no gap and no overlap), and merge must close the
    // window: final oldestIdx === requested offset.
    globalThis._messagesTruncated = true;
    globalThis._oldestIdx = scenario.oldestIdx || 70;
    const existing = [];
    for (let i = 70; i < 100; i++) existing.push({ role: i % 2 === 0 ? 'user' : 'assistant', content: `row-${i}`, _ts: `ts-${i}` });
    globalThis.S.messages = existing;
    globalThis.S.session = { session_id: 's1', message_count: 100 };
    const requested = [];
    globalThis.api = async (url, opts) => {
      requested.push(String(url));
      // Return one older page of size 50 (rows 20..69).
      const older = [];
      for (let i = 20; i < 70; i++) older.push({ role: i % 2 === 0 ? 'user' : 'assistant', content: `row-${i}`, _ts: `ts-${i}` });
      return {
        session: { session_id: 's1', message_count: 100, messages: older, _messages_truncated: true, _messages_offset: 20 },
      };
    };
    // Extract the live loader from sessions.js.
    const loaderBody = extractFunction(sessionsSrc, '_loadOlderMessages');
    const loaderFactory = new Function(`${loaderBody}; return _loadOlderMessages;`);
    const loader = loaderFactory();
    await loader.call(globalThis);
    console.log(JSON.stringify({
      action: 'load_older_paging',
      requested,
      messagesTruncated: !!globalThis._messagesTruncated,
      oldestIdx: globalThis._oldestIdx,
      messagesLen: Array.isArray(S.messages) ? S.messages.length : -1,
      loadingOlder: !!globalThis._loadingOlder,
    }));
    return;
  }

  throw new Error(`unknown action: ${scenario.action}`);
})().catch((err) => {
  console.error(err && err.stack ? err.stack : String(err));
  process.exit(1);
});
"""


@pytest.fixture(scope="module")
def driver_path(tmp_path_factory):
    driver = tmp_path_factory.mktemp("7628_driver") / "driver.js"
    driver.write_text(_DRIVER, encoding="utf-8")
    return str(driver)


def _run_scenario(driver_path: str, scenario: dict) -> dict:
    command = [_NODE, driver_path, str(_COMMANDS_JS), str(_MESSAGES_JS), str(_SESSIONS_JS), json.dumps(scenario)]
    result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", check=False, timeout=30)
    if result.returncode != 0:
        raise RuntimeError(f"node driver failed: {result.stderr}")
    return json.loads(result.stdout.strip())


def test_compress_success_restores_paging_state(driver_path):
    """A successful /compress returning the full transcript must reset the
    bounded preflight's paging signals before render (#7628 blocker 1)."""
    outcome = _run_scenario(driver_path, {"action": "compress_restores_paging"})
    assert outcome["messagesTruncated"] is False, "compress success must clear _messagesTruncated"
    assert outcome["oldestIdx"] == 0, f"compress success must reset _oldestIdx to 0, got {outcome['oldestIdx']}"
    assert outcome["messageCountAfter"] == 100


def test_settle_bounded_preserves_no_drop_no_dup(driver_path):
    """A bounded settle against repeated identical turns must not drop or
    duplicate rows (#7628 blocker 2, exact reviewer reproduction).

    All user rows share one identity key (same role/content/_ts), all assistant
    rows share one too — _messageIdentityKey collides across every pair. A
    bounded tail [u3,a3,u2,a2] therefore matches BOTH the visible prefix
    (u1,a1,u2,a2…) and the visible suffix (…u2,a2) at key level. The round-2
    `||` code preferred the prefix branch: splice offset = staged.length
    dropped u1/a1 and re-emitted u3/a3 (duplicate). The server truncation
    signal must force suffix-exclusive splicing.
    """
    _u = {"role": "user", "content": "What's the status?", "_ts": "t-u"}
    _a = {"role": "assistant", "content": "All good.", "_ts": "t-a"}
    # Visible before settle: u1,a1,u2,a2,u3,a3 + Connection-interrupted marker.
    state_messages = [
        {**_u, "_id": 1}, {**_a, "_id": 2},
        {**_u, "_id": 3}, {**_a, "_id": 4},
        {**_u, "_id": 5}, {**_a, "_id": 6},
        {"role": "assistant", "content": "**Connection interrupted:** The browser lost the live SSE connection before the response finished.", "_ts": "t-err", "_id": 7},
    ]
    outcome = _run_scenario(driver_path, {
        "action": "settle_repeated_turns",
        "isSessionCurrentPane": True,
        "state": {
            "messageCount": 12,
            "messages": state_messages,
        },
        "apiPayload": {
            "session": {
                "session_id": "s1",
                "active_stream_id": None,
                "pending_user_message": None,
                "message_count": 12,
                "messages": [
                    # Reviewer's tail: [u3,a3,u2,a2] — identity keys collide
                    # with u1/a1/u2/a2/u3/a3 above, so prefix AND suffix both
                    # compare equal.
                    {**_u, "_id": 8}, {**_a, "_id": 9},
                    {**_u, "_id": 10}, {**_a, "_id": 11},
                ],
                "_messages_truncated": True,
                "_messages_offset": 4,
            },
        },
    })
    assert outcome["status"] == "restored", outcome
    ids = [m["_id"] for m in outcome["messages"]]
    # No dropped id, no dup id (the prefix-wins bug duplicated u3/a3 and
    # dropped u1/a1 — every retained row must be unique).
    assert len(ids) == len(set(ids)), f"duplicate rows survived settle: {ids}"
    # Staged (authoritative) rows must all be present, plus the marker once.
    assert set(ids) == {8, 9, 10, 11, 7}, f"unexpected transcript after fix: {ids}"
    # Identity (role|_ts|content) counts: staged rows legitimately repeat
    # (two identical user pairs in the tail), but the bug added a THIRD copy by
    # splicing the visible tail back in. user<=2, assistant<=2, marker exactly 1.
    keys = [(m["role"], m["_ts"], m["content"]) for m in outcome["messages"]]
    kc = {}
    for k in keys:
        kc[k] = kc.get(k, 0) + 1
    user_key = ("user", "t-u", "What's the status?")
    asst_key = ("assistant", "t-a", "All good.")
    assert kc.get(user_key, 0) == 2, f"user rows must appear exactly twice (staged), got {kc.get(user_key, 0)}: {keys}"
    assert kc.get(asst_key, 0) == 2, f"assistant rows must appear exactly twice (staged), got {kc.get(asst_key, 0)}: {keys}"
    assert outcome["terminalMarkerCount"] == 1, outcome
    assert outcome["oldestIdx"] == 4, f"offset must follow server, got {outcome['oldestIdx']}"
    assert outcome["messagesTruncated"] is True


def test_bounded_recovery_prepares_gap_free_load_earlier(driver_path):
    """After a bounded recovery (oldestIdx=70), Load Earlier must page from the
    authoritative offset so the fetched range has no gap and no overlap
    (#7628)."""
    outcome = _run_scenario(driver_path, {"action": "load_older_paging", "oldestIdx": 70, "msgLimitMax": 80})
    assert outcome["requested"], "loader must have issued an api call"
    url = outcome["requested"][0]
    assert "msg_before=70" in url, f"load-earlier must page from authoritative offset: {url}"
    assert "msg_limit=" in url, url