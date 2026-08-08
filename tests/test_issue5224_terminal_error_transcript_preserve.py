"""Regression tests for #5224: preserve terminal-visible transcript on recovery."""

import json
import subprocess
import shutil
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).parent.parent.resolve()
MESSAGES_JS = REPO_ROOT / "static" / "messages.js"
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="node is required to execute message runtime tests")


_DRIVER = r"""
const fs = require('fs');

const src = fs.readFileSync(process.argv[2], 'utf8');
const scenario = JSON.parse(process.argv[3] || '{}');

function extractFunction(source, name) {
  const markers = [`async function ${name}(`, `function ${name}(`];
  let start = -1;
  for (const marker of markers) {
    start = source.indexOf(marker);
    if (start >= 0) break;
  }
  if (start < 0) {
    throw new Error(`missing ${name}`);
  }
  let i = source.indexOf('{', start);
  if (i < 0) {
    throw new Error(`missing function body for ${name}`);
  }
  let depth = 1;
  i++;
  while (i < source.length) {
    const ch = source[i];
    if (ch === '{') depth++;
    if (ch === '}') {
      depth--;
      if (depth === 0) return source.slice(start, i + 1);
    }
    i++;
  }
  throw new Error(`unterminated function body for ${name}`);
}

function extractFunctionByName(name) {
  return extractFunction(src, name);
}

function installRuntimeHelpers() {
  const helpers = [
    "_isMarkerOnlyAssistantMessage",
    "_streamRecoveryControlMessageText",
    "_streamRecoveryControlMessage",
    "_filterRecoveryControlMessages",
    "_replaceMarkerOnlyAssistantWithStreamError",
    "_messageIdentityKey",
    "_isHistoricalAnchorActivityScene",
    "_carryForwardEphemeralTurnFields",
    "_isTerminalStreamErrorMarkerMessage",
    "_ensureSingleTerminalStreamErrorMarker",
    "_restoreSettledSession",
    "_handleStreamError",
  ];
  for (const helper of helpers) {
    const body = extractFunctionByName(helper);
    const factory = new Function(`${body}; return ${helper};`);
    globalThis[helper] = factory();
  }
}

function buildRuntime() {
  const activeSid = scenario.activeSid || 'session-5224';
  const streamId = scenario.streamId || 'stream-5224';
  const calls = [];
  globalThis.activeSid = activeSid;
  globalThis.streamId = streamId;
  globalThis.assistantText = false;
  globalThis.S = JSON.parse(JSON.stringify(scenario.state || {}));
  if (!globalThis.S.session) {
    globalThis.S.session = { session_id: activeSid };
  }
  if (!Object.prototype.hasOwnProperty.call(globalThis.S, 'activeStreamId')) {
    globalThis.S.activeStreamId = streamId;
  }
  globalThis.INFLIGHT = {};
  globalThis._EPHEMERAL_TURN_FIELDS = [
    '_turnUsage',
    '_turnDuration',
    '_turnTps',
    '_gatewayRouting',
    '_statusCard',
    '_anchor_stream_id',
    '_anchor_activity_scene',
  ];
  globalThis._isActiveSession = () => scenario.isActiveSession !== false;
  globalThis._isSessionCurrentPane = () => scenario.isSessionCurrentPane !== false;
  globalThis._isSessionActivelyViewed = () => !!scenario.isSessionActivelyViewed;
  globalThis._closeSource = () => calls.push('closeSource');
  globalThis._clearStreamEndRecovery = () => calls.push('clearStreamEndRecovery');
  globalThis._clearOwnerInflightState = () => calls.push('clearOwnerInflight');
  globalThis.clearLiveToolCards = () => calls.push('clearLiveToolCards');
  globalThis.removeThinking = () => calls.push('removeThinking');
  globalThis._flushReasoningToAnchor = () => calls.push('flushReasoning');
  globalThis._applyToAnchor = () => calls.push('applyToAnchor');
  globalThis._attachProjectedAnchorSceneToLastAssistant = () => calls.push('attachProjected');
  globalThis._hydrateTodosFromSession = () => calls.push('hydrateTodos');
  globalThis._scheduleAnchorRegistryCleanup = () => calls.push('scheduleAnchorRegistryCleanup');
  globalThis._smdEndParser = () => calls.push('smdEndParser');
  globalThis._markSessionCompletionUnread = () => calls.push('markCompletionUnread');
  globalThis._markSessionViewed = () => calls.push('markSessionViewed');
  globalThis.localStorage = {
    setItem: () => calls.push('setLocalStorageItem'),
    getItem: () => null,
    removeItem: () => calls.push('removeLocalStorageItem'),
  };
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
  globalThis._setActivePaneIdleIfOwner = () => calls.push('setActivePaneIdle');
  globalThis.setBusy = () => calls.push('setBusy');
  globalThis.setComposerStatus = () => calls.push('setComposerStatus');
  globalThis.setStatus = () => calls.push('setStatus');
  globalThis._messageRenderableMessageCount = () => scenario.messageRenderableCount || 50;
  globalThis._currentMessageRenderWindowSize = () => scenario.currentWindowSize || 12;
  globalThis._messageRenderWindowSize = 20;
  globalThis._streamFinalized = !!scenario.streamFinalized;
  globalThis._persistTimer = null;
  // #2761: _restoreSettledSession reads the STREAM-OWNED continuation-sid
  // binding that attachLiveStream() provides. The standalone extraction has no
  // attachLiveStream scope, so mirror the closure default ('' = no rotation).
  globalThis._streamCompressionContinuationSid = '';
  globalThis.api = async () => scenario.apiPayload || { session: null };
  globalThis.msgContent = undefined;
  globalThis._isPreservedCompressionTaskListMarkerOnlyText = () => false;
  return calls;
}

(async () => {
  installRuntimeHelpers();
  const calls = buildRuntime();
  if (scenario.action === 'restore_shorter_terminal') {
    const status = await _restoreSettledSession({}, {
      status: true,
      preserveVisibleOnShorterTerminalSnapshot: true,
    });
    const messages = Array.isArray(S.messages) ? S.messages : [];
    const terminalMarkerCount = messages.filter(_isTerminalStreamErrorMarkerMessage).length;
    console.log(JSON.stringify({
      action: scenario.action,
      status,
      messages: messages.map((m) => ({ role: m.role, content: m.content })),
      terminalMarkerCount,
      calls,
    }));
    return;
  }

  if (scenario.action === 'restore_fuller_terminal') {
    const status = await _restoreSettledSession({}, {
      status: true,
      preserveVisibleOnShorterTerminalSnapshot: true,
    });
    const messages = Array.isArray(S.messages) ? S.messages : [];
    const terminalMarkerCount = messages.filter(_isTerminalStreamErrorMarkerMessage).length;
    console.log(JSON.stringify({
      action: scenario.action,
      status,
      messages: messages.map((m) => ({ role: m.role, content: m.content })),
      terminalMarkerCount,
      calls,
    }));
    return;
  }

  if (scenario.action === 'terminal_marker_idempotent') {
    const messages = JSON.parse(JSON.stringify(scenario.messages || []));
    _ensureSingleTerminalStreamErrorMarker(messages);
    _ensureSingleTerminalStreamErrorMarker(messages);
    const terminalMarkerCount = messages.filter(_isTerminalStreamErrorMarkerMessage).length;
    console.log(JSON.stringify({
      action: scenario.action,
      messages: messages.map((m) => ({ role: m.role, content: m.content })),
      terminalMarkerCount,
      calls,
    }));
    return;
  }

  if (scenario.action === 'restore_two_stream_race') {
    // Two-stream recovery (#6689 re-gate finding #1): stream A settles via
    // _restoreSettledSession. While A's /api/session poll is in flight, a
    // newer continuation turn B starts and takes stream ownership
    // (S.activeStreamId -> 'stream-B'). A's late settle must be rejected as
    // stale and must NOT clear B's stream id, replace S.session, or replace
    // S.messages with A's archived transcript. Then B's own terminal settle
    // must be honored ('restored'), not rejected as stale.
    globalThis.api = async () => {
      // B starts in the interval after A's active_stream_id is cleared but
      // before A's done lands.
      S.activeStreamId = 'stream-B';
      return scenario.aPayload || { session: null };
    };
    const aStatus = await _restoreSettledSession({}, { status: true });
    const afterA = {
      status: aStatus,
      activeStreamId: S.activeStreamId,
      session: S.session ? { session_id: S.session.session_id } : null,
      messages: Array.isArray(S.messages) ? S.messages.map((m) => ({ role: m.role, content: m.content })) : [],
      calls: calls.slice(),
    };
    // Phase 2: B's own settle (B owns the stream now).
    globalThis.streamId = 'stream-B';
    globalThis._streamFinalized = false;
    globalThis.api = async () => scenario.bPayload || { session: null };
    const bStatus = await _restoreSettledSession({}, { status: true });
    console.log(JSON.stringify({
      action: scenario.action,
      a: afterA,
      b: {
        status: bStatus,
        session: S.session ? { session_id: S.session.session_id } : null,
        messages: Array.isArray(S.messages) ? S.messages.map((m) => ({ role: m.role, content: m.content })) : [],
        calls: calls.slice(),
      },
    }));
    return;
  }

  if (scenario.action === 'restore_rotation_during_await') {
    // #6689 re-gate (finding #2): the continuation session id rotates WHILE
    // the /api/session poll for the archived parent session is in flight. The
    // stale archived-parent payload must be discarded (no settle, no state
    // mutation, no _streamFinalized), and the recovery must re-poll the
    // CURRENT continuation id so ONLY the continuation payload settles.
    let apiCalls = 0;
    globalThis.api = async () => {
      apiCalls += 1;
      if (apiCalls === 1) {
        // Rotation arrives during request #1 (archived parent) — as if the
        // `compressed` SSE event landed mid-roundtrip.
        _streamCompressionContinuationSid = 'session-cont-1';
        return scenario.parentPayload || { session: null };
      }
      return scenario.contPayload || { session: null };
    };
    const status = await _restoreSettledSession({}, { status: true });
    const messages = Array.isArray(S.messages) ? S.messages : [];
    console.log(JSON.stringify({
      action: scenario.action,
      status,
      apiCalls,
      session: S.session ? { session_id: S.session.session_id } : null,
      messages: messages.map((m) => ({ role: m.role, content: m.content })),
      streamFinalized: !!_streamFinalized,
      calls,
    }));
    return;
  }

  if (scenario.action === 'restore_rotation_concurrent') {
    // #6689 re-gate (finding #2, STALE-OLD reproduction): TWO recovery
    // requests race. The archived-parent poll (A) is in flight when the
    // continuation session id rotates, and a SECOND recovery request (B)
    // for the continuation is in flight concurrently — the exact
    // interleaving the maintainer's Codex reproduction ended with
    // (STALE-OLD: the concurrent continuation request returned 'restored'
    // but got suppressed by _streamFinalized, leaving the wrong archived
    // session settled). A's stale archived-parent payload must be discarded
    // by the post-await re-read, and the continuation payload must be the
    // ONLY one to settle — the archived parent must never set
    // _streamFinalized nor mutate S.session / S.messages.
    let apiCalls = 0;
    globalThis.api = async () => {
      apiCalls += 1;
      if (apiCalls === 1) {
        // Rotation lands while A's archived-parent poll is in flight (the
        // `compressed` SSE event lands mid-roundtrip).
        _streamCompressionContinuationSid = 'session-cont-1';
        return scenario.parentPayload || { session: null };
      }
      return scenario.contPayload || { session: null };
    };
    const aPromise = _restoreSettledSession({}, { status: true });
    const bPromise = _restoreSettledSession({}, { status: true });
    const [aStatus, bStatus] = await Promise.all([aPromise, bPromise]);
    const messages = Array.isArray(S.messages) ? S.messages : [];
    console.log(JSON.stringify({
      action: scenario.action,
      aStatus,
      bStatus,
      apiCalls,
      session: S.session ? { session_id: S.session.session_id } : null,
      messages: messages.map((m) => ({ role: m.role, content: m.content })),
      streamFinalized: !!_streamFinalized,
      calls,
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
    driver = tmp_path_factory.mktemp("issue5224_driver") / "driver.js"
    driver.write_text(_DRIVER, encoding="utf-8")
    return str(driver)


def _run_scenario(driver_path: str, scenario: dict) -> dict:
    command = [NODE, driver_path, str(MESSAGES_JS), json.dumps(scenario)]
    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
        timeout=30,
    )
    if result.returncode != 0:
        raise RuntimeError(f"node driver failed: {result.stderr}")
    return json.loads(result.stdout.strip())


def test_terminal_error_restore_preserves_visible_transcript_when_server_snapshot_shorter(driver_path):
    """Terminal recovery must keep richer visible transcript when restored snapshot is shorter."""
    outcome = _run_scenario(driver_path, {
        "action": "restore_shorter_terminal",
        "state": {
            "session": {"session_id": "session-5224", "message_count": 5},
            "messages": [
                {"role": "user", "content": "Question about data?", "_ts": "u1"},
                {"role": "assistant", "content": "Visible assistant answer segment one", "_ts": "a1"},
                {"role": "assistant", "content": "Visible assistant answer segment two", "_ts": "a2"},
                {"role": "assistant", "content": "**Connection interrupted:** The browser lost the live SSE connection before the response finished.", "_ts": "err"},
            ],
            "activeStreamId": "stream-5224",
        },
        "apiPayload": {
            "session": {
                "session_id": "session-5224",
                "active_stream_id": None,
                "pending_user_message": None,
                "messages": [
                    {"role": "user", "content": "Question about data?", "_ts": "u1"},
                    {"role": "assistant", "content": "Visible assistant answer segment one", "_ts": "a1"},
                ],
            },
        },
        "activeSid": "session-5224",
        "streamId": "stream-5224",
        "isActiveSession": True,
        "isSessionCurrentPane": True,
        "isSessionActivelyViewed": False,
    })

    assert outcome["status"] == "restored"
    roles_and_contents = [(item["role"], item["content"]) for item in outcome["messages"]]
    assert ("assistant", "**Connection interrupted:** The browser lost the live SSE connection before the response finished.") in roles_and_contents, (
        f"terminal error marker missing: {roles_and_contents}"
    )
    assert outcome["terminalMarkerCount"] == 1, f"expected exactly one terminal marker, got {outcome['terminalMarkerCount']}"
    assert any("Visible assistant answer segment two" in content for _, content in roles_and_contents), (
        "visible transcript tail was dropped after stale shorter restore"
    )


def test_terminal_error_restore_replaces_when_snapshots_are_fuller(driver_path):
    """A fuller settled snapshot must replace the visible transcript to keep the pane current."""
    outcome = _run_scenario(driver_path, {
        "action": "restore_fuller_terminal",
        "state": {
            "session": {"session_id": "session-5224", "message_count": 1},
            "messages": [
                {"role": "assistant", "content": "Old interrupted shell", "_ts": "old"},
            ],
            "activeStreamId": "stream-5224",
        },
        "apiPayload": {
            "session": {
                "session_id": "session-5224",
                "active_stream_id": None,
                "pending_user_message": None,
                "messages": [
                    {"role": "user", "content": "Question about data?", "_ts": "u1"},
                    {"role": "assistant", "content": "Settled first segment", "_ts": "a1"},
                    {"role": "assistant", "content": "Settled second segment", "_ts": "a2"},
                    {"role": "assistant", "content": "Settled third segment", "_ts": "a3"},
                ],
            },
        },
        "activeSid": "session-5224",
        "streamId": "stream-5224",
        "isActiveSession": True,
        "isSessionCurrentPane": True,
        "isSessionActivelyViewed": False,
    })

    assert outcome["status"] == "restored"
    expected = [
        ("user", "Question about data?"),
        ("assistant", "Settled first segment"),
        ("assistant", "Settled second segment"),
        ("assistant", "Settled third segment"),
    ]
    observed = [(item["role"], item["content"]) for item in outcome["messages"]]
    assert observed == expected, f"fuller settled snapshot should replace visible messages: {observed}"


def test_terminal_error_restore_replaces_shorter_authoritative_snapshot_when_prefix_does_not_match(driver_path):
    """A shorter settled snapshot that changes the assistant turn must replace stale live fragments."""
    outcome = _run_scenario(driver_path, {
        "action": "restore_shorter_terminal",
        "state": {
            "session": {"session_id": "session-5224", "message_count": 5},
            "messages": [
                {"role": "user", "content": "Question about data?", "_ts": "u1"},
                {"role": "assistant", "content": "Visible assistant fragment one", "_ts": "a1"},
                {"role": "assistant", "content": "Visible assistant fragment two", "_ts": "a2"},
                {"role": "assistant", "content": "**Connection interrupted:** The browser lost the live SSE connection before the response finished.", "_ts": "err"},
            ],
            "activeStreamId": "stream-5224",
        },
        "apiPayload": {
            "session": {
                "session_id": "session-5224",
                "active_stream_id": None,
                "pending_user_message": None,
                "messages": [
                    {"role": "user", "content": "Question about data?", "_ts": "u1"},
                    {"role": "assistant", "content": "Settled final answer", "_ts": "final"},
                ],
            },
        },
        "activeSid": "session-5224",
        "streamId": "stream-5224",
        "isActiveSession": True,
        "isSessionCurrentPane": True,
        "isSessionActivelyViewed": False,
    })

    observed = [(item["role"], item["content"]) for item in outcome["messages"]]
    assert observed == [
        ("user", "Question about data?"),
        ("assistant", "Settled final answer"),
    ], f"authoritative settled snapshot should replace stale live tail: {observed}"
    assert outcome["terminalMarkerCount"] == 0, (
        f"terminal marker should not survive authoritative settled replacement, got {outcome['terminalMarkerCount']}"
    )


def test_terminal_error_restore_does_not_preserve_from_historical_marker_on_older_turn(driver_path):
    """An old terminal marker earlier in the session must not preserve a later unrelated live tail."""
    outcome = _run_scenario(driver_path, {
        "action": "restore_shorter_terminal",
        "state": {
            "session": {"session_id": "session-5224", "message_count": 7},
            "messages": [
                {"role": "user", "content": "Earlier question", "_ts": "u0"},
                {"role": "assistant", "content": "Earlier answer", "_ts": "a0"},
                {"role": "assistant", "content": "**Connection interrupted:** The browser lost the live SSE connection before the response finished.", "_ts": "err0"},
                {"role": "user", "content": "Current question", "_ts": "u1"},
                {"role": "assistant", "content": "Current fragment one", "_ts": "a1"},
                {"role": "assistant", "content": "Current fragment two", "_ts": "a2"},
            ],
            "activeStreamId": "stream-5224",
        },
        "apiPayload": {
            "session": {
                "session_id": "session-5224",
                "active_stream_id": None,
                "pending_user_message": None,
                "messages": [
                    {"role": "user", "content": "Earlier question", "_ts": "u0"},
                    {"role": "assistant", "content": "Earlier answer", "_ts": "a0"},
                    {"role": "assistant", "content": "**Connection interrupted:** The browser lost the live SSE connection before the response finished.", "_ts": "err0"},
                    {"role": "user", "content": "Current question", "_ts": "u1"},
                    {"role": "assistant", "content": "Current fragment one", "_ts": "a1"},
                ],
            },
        },
        "activeSid": "session-5224",
        "streamId": "stream-5224",
        "isActiveSession": True,
        "isSessionCurrentPane": True,
        "isSessionActivelyViewed": False,
    })

    observed = [(item["role"], item["content"]) for item in outcome["messages"]]
    assert observed == [
        ("user", "Earlier question"),
        ("assistant", "Earlier answer"),
        ("assistant", "**Connection interrupted:** The browser lost the live SSE connection before the response finished."),
        ("user", "Current question"),
        ("assistant", "Current fragment one"),
    ], f"historical marker should not preserve later unrelated tail: {observed}"
    assert outcome["terminalMarkerCount"] == 1, (
        f"historical marker count should stay unchanged, got {outcome['terminalMarkerCount']}"
    )


def test_terminal_error_marker_is_single_instance_and_not_duplicated(driver_path):
    """Appending terminal error marker repeatedly should keep one marker and remove duplicates."""
    outcome = _run_scenario(driver_path, {
        "action": "terminal_marker_idempotent",
        "messages": [
            {"role": "user", "content": "Question", "_ts": "u1"},
            {"role": "assistant", "content": "Answer", "_ts": "a1"},
        ],
        "activeSid": "session-5224",
        "streamId": "stream-5224",
        "isActiveSession": True,
        "isSessionCurrentPane": True,
    })

    assert outcome["terminalMarkerCount"] == 1, f"terminal marker should be deduped to one, got {outcome['terminalMarkerCount']}"
    assert outcome["messages"][-1]["content"].startswith("**Connection interrupted:**")


def test_two_stream_recovery_newer_continuation_wins_over_stale_done(driver_path):
    """Two-stream recovery (#6689 re-gate finding #1): a newer continuation
    turn B starts in the interval after A's active_stream_id is cleared but
    before A's done lands. A's late settle must be rejected as stale and must
    NOT clear B's stream id, replace S.session, or clobber B's transcript;
    B's own terminal event must be honored (not rejected as stale)."""
    outcome = _run_scenario(driver_path, {
        "action": "restore_two_stream_race",
        "state": {
            "session": {"session_id": "session-5224", "message_count": 5},
            "messages": [
                {"role": "user", "content": "Question about data?", "_ts": "u1"},
                {"role": "assistant", "content": "B live answer segment", "_ts": "b1"},
            ],
            "activeStreamId": "stream-A",
        },
        "aPayload": {
            "session": {
                "session_id": "session-5224",
                "active_stream_id": None,
                "pending_user_message": None,
                "messages": [
                    {"role": "user", "content": "Question about data?", "_ts": "u1"},
                    {"role": "assistant", "content": "A stale archived answer", "_ts": "a1"},
                ],
            },
        },
        "bPayload": {
            "session": {
                "session_id": "session-5224",
                "active_stream_id": None,
                "pending_user_message": None,
                "messages": [
                    {"role": "user", "content": "Question about data?", "_ts": "u1"},
                    {"role": "assistant", "content": "B final answer", "_ts": "bFinal"},
                ],
            },
        },
        "activeSid": "session-5224",
        "streamId": "stream-A",
        "isActiveSession": True,
        "isSessionCurrentPane": True,
        "isSessionActivelyViewed": False,
    })

    a = outcome["a"]
    # A's late settle must be rejected as stale — the newer stream B owns the turn.
    assert a["status"] == "stale", f"A must be rejected as stale, got {a['status']}"
    # A must NOT clear B's stream id.
    assert a["activeStreamId"] == "stream-B", (
        f"A clobbered B's stream id: {a['activeStreamId']}"
    )
    # A must NOT replace S.session with its archived snapshot.
    assert a["session"] == {"session_id": "session-5224"}
    # A must NOT clobber B's live transcript with A's archived messages.
    a_contents = [item["content"] for item in a["messages"]]
    assert "B live answer segment" in a_contents, (
        f"B's live transcript was clobbered by A: {a_contents}"
    )
    assert "A stale archived answer" not in a_contents, (
        f"A replaced B's transcript with its archived snapshot: {a_contents}"
    )
    # B's own terminal event must be honored (not rejected as stale).
    b = outcome["b"]
    assert b["status"] == "restored", f"B's terminal settle must be honored, got {b['status']}"
    b_contents = [item["content"] for item in b["messages"]]
    assert "B final answer" in b_contents, (
        f"B's final answer must land in the transcript: {b_contents}"
    )


def test_rotation_during_await_settles_only_continuation_payload(driver_path):
    """Deferred-request regression (#6689 re-gate finding #2): the continuation
    session id rotates WHILE the archived-parent /api/session poll is in flight
    (the `compressed` SSE event lands mid-roundtrip). The stale archived-parent
    payload must be discarded — no settle, no S.session / S.messages /
    S.activeStreamId mutation, no _streamFinalized — and the recovery must
    re-poll the CURRENT continuation id so ONLY the continuation payload
    settles."""
    outcome = _run_scenario(driver_path, {
        "action": "restore_rotation_during_await",
        "state": {
            "session": {"session_id": "session-2761", "message_count": 5},
            "messages": [
                {"role": "user", "content": "Question about data?", "_ts": "u1"},
                {"role": "assistant", "content": "Live pre-compression fragment", "_ts": "a1"},
            ],
            "activeStreamId": "stream-2761",
        },
        "parentPayload": {
            "session": {
                "session_id": "session-2761",
                "active_stream_id": None,
                "pending_user_message": None,
                "messages": [
                    {"role": "user", "content": "Question about data?", "_ts": "u1"},
                    {"role": "assistant", "content": "ARCHIVED parent answer (must be discarded)", "_ts": "aParent"},
                ],
            },
        },
        "contPayload": {
            "session": {
                "session_id": "session-cont-1",
                "active_stream_id": None,
                "pending_user_message": None,
                "messages": [
                    {"role": "user", "content": "Question about data?", "_ts": "u1"},
                    {"role": "assistant", "content": "CONTINUATION final answer", "_ts": "aCont"},
                ],
            },
        },
        "activeSid": "session-2761",
        "streamId": "stream-2761",
        "isActiveSession": True,
        "isSessionCurrentPane": True,
        "isSessionActivelyViewed": False,
    })

    # The recovery must settle ('restored'), not give up or settle the archive.
    assert outcome["status"] == "restored", f"got {outcome['status']}"
    # Exactly two polls: the discarded archived-parent fetch + the retry
    # against the current continuation id. The stale payload must NOT settle.
    assert outcome["apiCalls"] == 2, (
        f"expected archived-parent poll + continuation retry (2 calls), got {outcome['apiCalls']}"
    )
    # Only the continuation payload may settle: S.session is the continuation
    # session and the archived-parent answer never lands in the transcript.
    assert outcome["session"] == {"session_id": "session-cont-1"}, (
        f"archived parent settled instead of continuation: {outcome['session']}"
    )
    contents = [item["content"] for item in outcome["messages"]]
    assert "CONTINUATION final answer" in contents, (
        f"continuation payload did not settle: {contents}"
    )
    assert "ARCHIVED parent answer" not in contents, (
        f"stale archived-parent payload settled: {contents}"
    )
    # A legit settle finalizes the stream; the stale payload must not have been
    # the one to do it (it never reached the mutation block).
    assert outcome["streamFinalized"] is True


def test_concurrent_continuation_request_never_settles_archived_parent(driver_path):
    """#6689 re-gate (finding #2, STALE-OLD reproduction): the archived-parent
    /api/session poll is in flight when the continuation session id rotates
    AND a second recovery request for the continuation is already in flight —
    the exact interleaving the maintainer's Codex reproduction ended with.
    The stale archived-parent payload must be discarded by the post-await
    re-read; ONLY the continuation payload may settle (never the archived
    parent, which must not set _streamFinalized or mutate S.session /
    S.messages)."""
    outcome = _run_scenario(driver_path, {
        "action": "restore_rotation_concurrent",
        "state": {
            "session": {"session_id": "session-2761", "message_count": 5},
            "messages": [
                {"role": "user", "content": "Question about data?", "_ts": "u1"},
                {"role": "assistant", "content": "Live pre-compression fragment", "_ts": "a1"},
            ],
            "activeStreamId": "stream-2761",
        },
        "parentPayload": {
            "session": {
                "session_id": "session-2761",
                "active_stream_id": None,
                "pending_user_message": None,
                "messages": [
                    {"role": "user", "content": "Question about data?", "_ts": "u1"},
                    {"role": "assistant", "content": "ARCHIVED parent answer (must be discarded)", "_ts": "aParent"},
                ],
            },
        },
        "contPayload": {
            "session": {
                "session_id": "session-cont-1",
                "active_stream_id": None,
                "pending_user_message": None,
                "messages": [
                    {"role": "user", "content": "Question about data?", "_ts": "u1"},
                    {"role": "assistant", "content": "CONTINUATION final answer", "_ts": "aCont"},
                ],
            },
        },
        "activeSid": "session-2761",
        "streamId": "stream-2761",
        "isActiveSession": True,
        "isSessionCurrentPane": True,
        "isSessionActivelyViewed": False,
    })

    # The archived parent never settles: S.session is the continuation and the
    # archived answer never lands in the transcript.
    assert outcome["session"] == {"session_id": "session-cont-1"}, (
        f"archived parent settled instead of continuation: {outcome['session']}"
    )
    contents = [item["content"] for item in outcome["messages"]]
    assert "CONTINUATION final answer" in contents, (
        f"continuation payload did not settle: {contents}"
    )
    assert "ARCHIVED parent answer" not in contents, (
        f"stale archived-parent payload settled: {contents}"
    )
    # Both concurrent requests resolve 'restored': one settles the
    # continuation, the other is suppressed by _streamFinalized — never the
    # archived parent.
    assert outcome["aStatus"] == "restored", f"parent-side recovery: {outcome['aStatus']}"
    assert outcome["bStatus"] == "restored", f"continuation-side recovery: {outcome['bStatus']}"
    # Exactly three polls: (1) A's archived-parent poll — discarded by the
    # post-await re-read; (2) B's continuation poll — settles; (3) A's retry
    # against the current continuation id — suppressed by _streamFinalized.
    # The archived parent is polled exactly once and never settles.
    assert outcome["apiCalls"] == 3, (
        f"expected archived-parent poll + continuation poll + retry (3 calls), "
        f"got {outcome['apiCalls']}"
    )
    # The stream is finalized by the continuation settle (never the archive).
    assert outcome["streamFinalized"] is True
