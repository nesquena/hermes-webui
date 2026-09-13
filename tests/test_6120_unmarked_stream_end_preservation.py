"""Regression coverage for #6120: scoped allowUnmarkedShorterTerminalSnapshot.

The new scoped option preserves a matching visible final tail when stream_end
arrives without a preceding done event — but replaces it when the authoritative
prefix diverges. Covers both the direct path and the active→settled retry path.
"""

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

// Extract the real production `stream_end` listener registration block from
// _wireSSE so the test can dispatch the ACTUAL registered handler.
function extractStreamEndListener(sourceText) {
  const marker = "source.addEventListener('stream_end'";
  const start = sourceText.indexOf(marker);
  if (start < 0) {
    throw new Error('missing stream_end listener registration');
  }
  const arrowIdx = sourceText.indexOf('=>', start);
  if (arrowIdx < 0) {
    throw new Error('missing arrow in stream_end listener');
  }
  const bodyStart = sourceText.indexOf('{', arrowIdx);
  if (bodyStart < 0) {
    throw new Error('missing body in stream_end listener');
  }
  let depth = 0;
  let i = bodyStart;
  for (; i < sourceText.length; i++) {
    const ch = sourceText[i];
    if (ch === '{') depth++;
    else if (ch === '}') {
      depth--;
      if (depth === 0) break;
    }
  }
  let end = i + 1;
  while (end < sourceText.length && '); \n'.includes(sourceText[end])) end++;
  return sourceText.slice(start, end);
}

function installRuntimeHelpers() {
  const helpers = [
    "_isMarkerOnlyAssistantMessage",
    "_streamRecoveryControlMessageText",
    "_streamRecoveryControlMessage",
    "_filterRecoveryControlMessages",
    "_replaceMarkerOnlyAssistantWithStreamError",
    "_messageIdentityKey",
    "_messageAuthoritativeKey",
    "_carryForwardEphemeralTurnFields",
    "_isHistoricalAnchorActivityScene",
    "_isTerminalStreamErrorMarkerMessage",
    "_ensureSingleTerminalStreamErrorMarker",
    "_restoreSettledSession",
    "_scheduleStreamEndRecovery",
    "_runStreamEndRecovery",
    "_clearStreamEndRecovery",
  ];
  for (const helper of helpers) {
    const body = extractFunctionByName(helper);
    const factory = new Function(`${body}; return ${helper};`);
    globalThis[helper] = factory();
  }
}

function buildRuntime() {
  const activeSid = scenario.activeSid || 'session-6120';
  const streamId = scenario.streamId || 'stream-6120';
  const calls = [];
  const timers = [];
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
  globalThis._bailOutOfTerminalEventsFromStaleStream = () => false;
  globalThis._liveStreamEndScenePresent = () => false;
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
  // Stream-end recovery module state (mirrors the module-scope lets).
  globalThis._terminalStateReached = false;
  globalThis._pendingStreamEndRecovery = false;
  globalThis._streamEndRecoveryAttempts = 0;
  globalThis._streamEndRecoveryTimer = null;
  globalThis._queueDrainSid = null;
  // Capture scheduled timers so the test can run the ACTUAL scheduled
  // _runStreamEndRecovery callback after the API settles.
  globalThis.setTimeout = (fn, delay) => { timers.push({ fn, delay }); return timers.length; };
  globalThis.clearTimeout = () => {};
  globalThis._finalizeStreamEndFallback = () => calls.push('finalizeFallback');
  // Mutable API payload: the retry scenario swaps to a settled snapshot
  // between the first restore (active) and the scheduled recovery run.
  let apiPayload = scenario.apiPayload || { session: null };
  globalThis.api = async () => apiPayload;
  globalThis.msgContent = undefined;
  globalThis._isPreservedCompressionTaskListMarkerOnlyText = () => false;

  // Observability wrappers around the REAL production functions — the
  // production implementation still runs, we just record statuses/calls.
  const realRestore = globalThis._restoreSettledSession;
  const realSchedule = globalThis._scheduleStreamEndRecovery;
  const realClear = globalThis._clearStreamEndRecovery;
  const realRun = globalThis._runStreamEndRecovery;
  const restoreStatuses = [];
  globalThis._restoreSettledSession = async (s, o) => {
    const st = await realRestore(s, o);
    restoreStatuses.push(st);
    return st;
  };
  globalThis._scheduleStreamEndRecovery = (s, delay) => {
    calls.push('scheduleStreamEndRecovery');
    realSchedule(s, delay);
  };
  globalThis._clearStreamEndRecovery = () => {
    calls.push('clearStreamEndRecovery');
    realClear();
  };
  let recoveryFinished = false;
  globalThis._runStreamEndRecovery = async (s) => {
    await realRun(s);
    recoveryFinished = true;
    calls.push('runStreamEndRecovery');
  };

  return { calls, timers, restoreStatuses, setApiPayload: (p) => { apiPayload = p; }, getRecoveryFinished: () => recoveryFinished };
}

(async () => {
  installRuntimeHelpers();
  const runtime = buildRuntime();
  const { calls, timers, restoreStatuses } = runtime;

  // Register the REAL production stream_end handler on a fake source and
  // dispatch it exactly like the browser EventSource would.
  const streamEndBlock = extractStreamEndListener(src);
  const source = {
    listeners: {},
    addEventListener(type, fn) { this.listeners[type] = fn; },
    close() { calls.push('sourceClose'); },
  };
  eval(streamEndBlock);
  const streamEndHandler = source.listeners['stream_end'];
  if (!streamEndHandler) {
    throw new Error('stream_end handler was not registered on the fake source');
  }

  const mode = scenario.mode || 'direct';

  if (mode === 'direct') {
    // Real path: dispatch the registered production stream_end listener. The
    // handler runs _restoreSettledSession with the scoped unmarked option
    // (non-active-scene fallback branch) exactly as in production.
    await streamEndHandler({ data: '{}' });
    const messages = Array.isArray(S.messages) ? S.messages : [];
    console.log(JSON.stringify({
      mode,
      status: restoreStatuses[restoreStatuses.length - 1] || null,
      restoreStatuses,
      messages: messages.map((m) => ({ role: m.role, content: m.content })),
      calls,
    }));
    return;
  }

  if (mode === 'retry') {
    // First dispatch: the API still reports the stream active, so the real
    // handler gets 'active' back and schedules _scheduleStreamEndRecovery.
    await streamEndHandler({ data: '{}' });
    const scheduled = timers.length > 0;
    const scheduledDelay = timers.length ? timers[0].delay : null;
    // Settle the API, then run the ACTUAL scheduled callback — the timer was
    // registered by the real _scheduleStreamEndRecovery and invokes the real
    // _runStreamEndRecovery.
    runtime.setApiPayload(scenario.apiPayloadSettled || scenario.apiPayload);
    if (timers.length) {
      timers[0].fn();
      for (let k = 0; k < 20 && !runtime.getRecoveryFinished(); k++) {
        await new Promise((r) => setImmediate(r));
      }
    }
    const messages = Array.isArray(S.messages) ? S.messages : [];
    console.log(JSON.stringify({
      mode,
      status: restoreStatuses[restoreStatuses.length - 1] || null,
      restoreStatuses,
      scheduled,
      scheduledDelay,
      recoveryFinished: runtime.getRecoveryFinished(),
      messages: messages.map((m) => ({ role: m.role, content: m.content })),
      calls,
    }));
    return;
  }

  throw new Error(`unknown mode: ${mode}`);
})().catch((err) => {
  console.error(err && err.stack ? err.stack : String(err));
  process.exit(1);
});
"""


@pytest.fixture(scope="module")
def driver_path(tmp_path_factory):
    driver = tmp_path_factory.mktemp("issue6120_driver") / "driver.js"
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


def test_unmarked_direct_preserves_matching_visible_tail(driver_path):
    """Direct stream_end-without-done: the REAL registered stream_end listener
    preserves the tail when the server snapshot is a prefix of a longer
    visible transcript (scoped allowUnmarkedShorterTerminalSnapshot)."""
    outcome = _run_scenario(driver_path, {
        "mode": "direct",
        "state": {
            "session": {"session_id": "session-6120", "message_count": 4},
            "messages": [
                {"role": "user", "content": "What is the capital of France?", "_ts": "u1"},
                {"role": "assistant", "content": "The capital of France is Paris.", "_ts": "a1"},
                {"role": "assistant", "content": "Paris is known as the City of Light.", "_ts": "a2"},
            ],
            "activeStreamId": "stream-6120",
        },
        "apiPayload": {
            "session": {
                "session_id": "session-6120",
                "active_stream_id": None,
                "pending_user_message": None,
                "messages": [
                    {"role": "user", "content": "What is the capital of France?", "_ts": "u1"},
                    {"role": "assistant", "content": "The capital of France is Paris.", "_ts": "a1"},
                ],
            },
        },
        "activeSid": "session-6120",
        "streamId": "stream-6120",
        "isActiveSession": True,
        "isSessionCurrentPane": True,
    })

    assert outcome["status"] == "restored"
    # The real production stream_end handler calls _clearStreamEndRecovery()
    # before restoring; the direct _restoreSettledSession path does not. Its
    # presence proves the registered listener was actually dispatched.
    assert "clearStreamEndRecovery" in outcome["calls"], (
        "real stream_end listener must have been dispatched: "
        f"calls={outcome['calls']}"
    )
    observed = [(item["role"], item["content"]) for item in outcome["messages"]]
    expected = [
        ("user", "What is the capital of France?"),
        ("assistant", "The capital of France is Paris."),
        ("assistant", "Paris is known as the City of Light."),
    ]
    assert observed == expected, (
        f"unmarked direct restore should preserve matching visible tail: {observed}"
    )


def test_unmarked_direct_replaces_when_prefix_diverges(driver_path):
    """Direct stream_end-without-done: the REAL registered stream_end listener
    replaces with the authoritative server snapshot when the prefix identity
    check fails."""
    outcome = _run_scenario(driver_path, {
        "mode": "direct",
        "state": {
            "session": {"session_id": "session-6120", "message_count": 4},
            "messages": [
                {"role": "user", "content": "What is the capital of France?", "_ts": "u1"},
                {"role": "assistant", "content": "The capital of France is Paris.", "_ts": "a1"},
                {"role": "assistant", "content": "Paris is known as the City of Light.", "_ts": "a2"},
            ],
            "activeStreamId": "stream-6120",
        },
        "apiPayload": {
            "session": {
                "session_id": "session-6120",
                "active_stream_id": None,
                "pending_user_message": None,
                "messages": [
                    {"role": "user", "content": "What is the capital of France?", "_ts": "u1"},
                    {"role": "assistant", "content": "Paris became the capital in 508 CE.", "_ts": "a1"},
                ],
            },
        },
        "activeSid": "session-6120",
        "streamId": "stream-6120",
        "isActiveSession": True,
        "isSessionCurrentPane": True,
    })

    assert outcome["status"] == "restored"
    observed = [(item["role"], item["content"]) for item in outcome["messages"]]
    expected = [
        ("user", "What is the capital of France?"),
        ("assistant", "Paris became the capital in 508 CE."),
    ]
    assert observed == expected, (
        f"unmarked direct restore should replace when authoritative prefix diverges: {observed}"
    )


def test_unmarked_direct_replaces_when_authoritative_tail_diverges(driver_path):
    """Divergent-tail regression (direct): role/timestamp/first-160 chars match
    but later content diverges — the prefix decision must use the FULL
    authoritative content, so the server snapshot replaces the visible tail."""
    long_visible = "A" * 200
    long_server = ("A" * 160) + ("B" * 40)
    assert long_visible[:160] == long_server[:160]
    assert long_visible != long_server
    outcome = _run_scenario(driver_path, {
        "mode": "direct",
        "state": {
            "session": {"session_id": "session-6120", "message_count": 4},
            "messages": [
                {"role": "user", "content": "Q", "_ts": "u1"},
                {"role": "assistant", "content": long_visible, "_ts": "a1"},
                {"role": "assistant", "content": "visible tail after divergent reply", "_ts": "a2"},
            ],
            "activeStreamId": "stream-6120",
        },
        "apiPayload": {
            "session": {
                "session_id": "session-6120",
                "active_stream_id": None,
                "pending_user_message": None,
                "messages": [
                    {"role": "user", "content": "Q", "_ts": "u1"},
                    {"role": "assistant", "content": long_server, "_ts": "a1"},
                ],
            },
        },
        "activeSid": "session-6120",
        "streamId": "stream-6120",
        "isActiveSession": True,
        "isSessionCurrentPane": True,
    })

    assert outcome["status"] == "restored"
    observed = [(item["role"], item["content"]) for item in outcome["messages"]]
    expected = [
        ("user", "Q"),
        ("assistant", long_server),
    ]
    assert observed == expected, (
        "staged/visible share role+timestamp+first-160-chars but diverge later — "
        f"must replace from the authoritative server snapshot: {observed}"
    )


def test_unmarked_retry_preserves_matching_visible_tail(driver_path):
    """Active→settled retry: the REAL stream_end listener gets 'active' back,
    schedules _scheduleStreamEndRecovery, and the ACTUAL scheduled
    _runStreamEndRecovery callback preserves the tail after settlement."""
    outcome = _run_scenario(driver_path, {
        "mode": "retry",
        "state": {
            "session": {"session_id": "session-6120", "message_count": 5},
            "messages": [
                {"role": "user", "content": "Tell me about Berlin.", "_ts": "u1"},
                {"role": "assistant", "content": "Berlin is the capital of Germany.", "_ts": "a1"},
                {"role": "assistant", "content": "It has a population of about 3.7 million.", "_ts": "a2"},
                {"role": "assistant", "content": "Berlin is famous for its history and culture.", "_ts": "a3"},
            ],
            "activeStreamId": "stream-6120",
        },
        "apiPayload": {
            "session": {
                "session_id": "session-6120",
                "active_stream_id": "stream-6120",
                "pending_user_message": None,
                "messages": [
                    {"role": "user", "content": "Tell me about Berlin.", "_ts": "u1"},
                    {"role": "assistant", "content": "Berlin is the capital of Germany.", "_ts": "a1"},
                ],
            },
        },
        "apiPayloadSettled": {
            "session": {
                "session_id": "session-6120",
                "active_stream_id": None,
                "pending_user_message": None,
                "messages": [
                    {"role": "user", "content": "Tell me about Berlin.", "_ts": "u1"},
                    {"role": "assistant", "content": "Berlin is the capital of Germany.", "_ts": "a1"},
                    {"role": "assistant", "content": "It has a population of about 3.7 million.", "_ts": "a2"},
                ],
            },
        },
        "activeSid": "session-6120",
        "streamId": "stream-6120",
        "isActiveSession": True,
        "isSessionCurrentPane": True,
    })

    assert outcome["scheduled"] is True, "retry must schedule stream-end recovery"
    assert outcome["scheduledDelay"] == 200, (
        f"recovery must be scheduled at 200ms, got {outcome['scheduledDelay']}"
    )
    assert outcome["recoveryFinished"] is True, (
        "the actual scheduled _runStreamEndRecovery callback must run"
    )
    assert "runStreamEndRecovery" in outcome["calls"], (
        "the real _runStreamEndRecovery must be invoked by the scheduled callback"
    )
    assert outcome["status"] == "restored"
    observed = [(item["role"], item["content"]) for item in outcome["messages"]]
    expected = [
        ("user", "Tell me about Berlin."),
        ("assistant", "Berlin is the capital of Germany."),
        ("assistant", "It has a population of about 3.7 million."),
        ("assistant", "Berlin is famous for its history and culture."),
    ]
    assert observed == expected, (
        f"unmarked retry restore should preserve matching visible tail: {observed}"
    )


def test_unmarked_retry_replaces_when_prefix_diverges(driver_path):
    """Active→settled retry: the scheduled _runStreamEndRecovery replaces with
    the authoritative server snapshot when the prefix identity check fails."""
    outcome = _run_scenario(driver_path, {
        "mode": "retry",
        "state": {
            "session": {"session_id": "session-6120", "message_count": 5},
            "messages": [
                {"role": "user", "content": "Tell me about Berlin.", "_ts": "u1"},
                {"role": "assistant", "content": "Berlin is the capital of Germany.", "_ts": "a1"},
                {"role": "assistant", "content": "It has a population of about 3.7 million.", "_ts": "a2"},
                {"role": "assistant", "content": "Berlin is famous for its history and culture.", "_ts": "a3"},
            ],
            "activeStreamId": "stream-6120",
        },
        "apiPayload": {
            "session": {
                "session_id": "session-6120",
                "active_stream_id": "stream-6120",
                "pending_user_message": None,
                "messages": [
                    {"role": "user", "content": "Tell me about Berlin.", "_ts": "u1"},
                    {"role": "assistant", "content": "Berlin is the capital of Germany.", "_ts": "a1"},
                ],
            },
        },
        "apiPayloadSettled": {
            "session": {
                "session_id": "session-6120",
                "active_stream_id": None,
                "pending_user_message": None,
                "messages": [
                    {"role": "user", "content": "Tell me about Berlin.", "_ts": "u1"},
                    {"role": "assistant", "content": "Berlin became the capital in 1990.", "_ts": "a1"},
                ],
            },
        },
        "activeSid": "session-6120",
        "streamId": "stream-6120",
        "isActiveSession": True,
        "isSessionCurrentPane": True,
    })

    assert outcome["scheduled"] is True, "retry must schedule stream-end recovery"
    assert outcome["scheduledDelay"] == 200
    assert outcome["recoveryFinished"] is True
    assert "runStreamEndRecovery" in outcome["calls"]
    assert outcome["status"] == "restored"
    observed = [(item["role"], item["content"]) for item in outcome["messages"]]
    expected = [
        ("user", "Tell me about Berlin."),
        ("assistant", "Berlin became the capital in 1990."),
    ]
    assert observed == expected, (
        f"unmarked retry restore should replace when authoritative prefix diverges: {observed}"
    )


def test_unmarked_retry_replaces_when_authoritative_tail_diverges(driver_path):
    """Divergent-tail regression (retry): the scheduled _runStreamEndRecovery
    must replace from the server snapshot when role/timestamp/first-160 chars
    match but later content diverges."""
    long_visible = "C" * 200
    long_server = ("C" * 160) + ("D" * 40)
    assert long_visible[:160] == long_server[:160]
    assert long_visible != long_server
    outcome = _run_scenario(driver_path, {
        "mode": "retry",
        "state": {
            "session": {"session_id": "session-6120", "message_count": 5},
            "messages": [
                {"role": "user", "content": "Q", "_ts": "u1"},
                {"role": "assistant", "content": long_visible, "_ts": "a1"},
                {"role": "assistant", "content": "visible tail after divergent reply", "_ts": "a2"},
            ],
            "activeStreamId": "stream-6120",
        },
        "apiPayload": {
            "session": {
                "session_id": "session-6120",
                "active_stream_id": "stream-6120",
                "pending_user_message": None,
                "messages": [
                    {"role": "user", "content": "Q", "_ts": "u1"},
                    {"role": "assistant", "content": long_visible, "_ts": "a1"},
                ],
            },
        },
        "apiPayloadSettled": {
            "session": {
                "session_id": "session-6120",
                "active_stream_id": None,
                "pending_user_message": None,
                "messages": [
                    {"role": "user", "content": "Q", "_ts": "u1"},
                    {"role": "assistant", "content": long_server, "_ts": "a1"},
                ],
            },
        },
        "activeSid": "session-6120",
        "streamId": "stream-6120",
        "isActiveSession": True,
        "isSessionCurrentPane": True,
    })

    assert outcome["scheduled"] is True, "retry must schedule stream-end recovery"
    assert outcome["scheduledDelay"] == 200
    assert outcome["recoveryFinished"] is True
    assert "runStreamEndRecovery" in outcome["calls"]
    assert outcome["status"] == "restored"
    observed = [(item["role"], item["content"]) for item in outcome["messages"]]
    expected = [
        ("user", "Q"),
        ("assistant", long_server),
    ]
    assert observed == expected, (
        "retry staged/visible share role+timestamp+first-160-chars but diverge later — "
        f"must replace from the authoritative server snapshot: {observed}"
    )


def test_unmarked_preservation_ignores_historic_terminal_marker(driver_path):
    """Even without a terminal marker on the final visible message, an older
    historic marker must NOT trigger preservation — the gate only engages via
    allowUnmarkedShorterTerminalSnapshot, not the marker check. Because the
    server snapshot is an exact prefix of the visible transcript and the
    scoped unmarked option is active, the matching visible final reply
    ('Current final reply') is preserved."""
    outcome = _run_scenario(driver_path, {
        "mode": "direct",
        "state": {
            "session": {"session_id": "session-6120", "message_count": 7},
            "messages": [
                {"role": "user", "content": "Earlier question", "_ts": "u0"},
                {"role": "assistant", "content": "Earlier answer", "_ts": "a0"},
                {"role": "assistant", "content": "**Connection interrupted:** The browser lost the live SSE connection before the response finished.", "_ts": "err0"},
                {"role": "user", "content": "Current question", "_ts": "u1"},
                {"role": "assistant", "content": "Current final reply", "_ts": "a1"},
            ],
            "activeStreamId": "stream-6120",
        },
        "apiPayload": {
            "session": {
                "session_id": "session-6120",
                "active_stream_id": None,
                "pending_user_message": None,
                "messages": [
                    {"role": "user", "content": "Earlier question", "_ts": "u0"},
                    {"role": "assistant", "content": "Earlier answer", "_ts": "a0"},
                    {"role": "assistant", "content": "**Connection interrupted:** The browser lost the live SSE connection before the response finished.", "_ts": "err0"},
                    {"role": "user", "content": "Current question", "_ts": "u1"},
                ],
            },
        },
        "activeSid": "session-6120",
        "streamId": "stream-6120",
        "isActiveSession": True,
        "isSessionCurrentPane": True,
    })

    assert outcome["status"] == "restored"
    observed = [(item["role"], item["content"]) for item in outcome["messages"]]
    expected = [
        ("user", "Earlier question"),
        ("assistant", "Earlier answer"),
        ("assistant", "**Connection interrupted:** The browser lost the live SSE connection before the response finished."),
        ("user", "Current question"),
        ("assistant", "Current final reply"),
    ]
    # The server snapshot is an exact prefix and the scoped unmarked option is
    # active, so the matching visible final reply is preserved.
    assert observed == expected, (
        f"unmarked preservation must keep the matching visible final reply: {observed}"
    )
