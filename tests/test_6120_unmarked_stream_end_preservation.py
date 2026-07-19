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

function installRuntimeHelpers() {
  const helpers = [
    "_isMarkerOnlyAssistantMessage",
    "_streamRecoveryControlMessageText",
    "_streamRecoveryControlMessage",
    "_filterRecoveryControlMessages",
    "_replaceMarkerOnlyAssistantWithStreamError",
    "_messageIdentityKey",
    "_carryForwardEphemeralTurnFields",
    "_isTerminalStreamErrorMarkerMessage",
    "_ensureSingleTerminalStreamErrorMarker",
    "_restoreSettledSession",
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
  globalThis.api = async () => scenario.apiPayload || { session: null };
  globalThis.msgContent = undefined;
  globalThis._isPreservedCompressionTaskListMarkerOnlyText = () => false;
  return calls;
}

(async () => {
  installRuntimeHelpers();
  const calls = buildRuntime();

  const mode = scenario.mode || 'direct';

  if (mode === 'direct') {
    // Direct stream_end-without-done path: simulate the handler calling
    // _restoreSettledSession with allowUnmarkedShorterTerminalSnapshot:true.
    // In production this is the non-active-scene fallback branch in the
    // stream_end event listener.
    const status = await _restoreSettledSession({}, {
      status: true,
      allowUnmarkedShorterTerminalSnapshot: true,
    });
    const messages = Array.isArray(S.messages) ? S.messages : [];
    console.log(JSON.stringify({
      mode,
      status,
      messages: messages.map((m) => ({ role: m.role, content: m.content })),
      calls,
    }));
    return;
  }

  if (mode === 'retry') {
    // Active→settled retry path: simulate _runStreamEndRecovery calling
    // _restoreSettledSession with allowUnmarkedShorterTerminalSnapshot:true
    // after the first direct restore returned 'active'.
    const status = await _restoreSettledSession({}, {
      status: true,
      allowUnmarkedShorterTerminalSnapshot: true,
    });
    const messages = Array.isArray(S.messages) ? S.messages : [];
    console.log(JSON.stringify({
      mode,
      status,
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
    """Direct stream_end-without-done: preserve tail when the server snapshot
    is a prefix of a longer visible transcript."""
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
    """Direct stream_end-without-done: replace with authoritative server snapshot
    when the prefix identity check fails."""
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


def test_unmarked_retry_preserves_matching_visible_tail(driver_path):
    """Active→settled retry: preserve tail when the server snapshot is a prefix
    of a longer visible transcript."""
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
    """Active→settled retry: replace with authoritative server snapshot when the
    prefix identity check fails."""
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

    assert outcome["status"] == "restored"
    observed = [(item["role"], item["content"]) for item in outcome["messages"]]
    expected = [
        ("user", "Tell me about Berlin."),
        ("assistant", "Berlin became the capital in 1990."),
    ]
    assert observed == expected, (
        f"unmarked retry restore should replace when authoritative prefix diverges: {observed}"
    )


def test_unmarked_preservation_ignores_historic_terminal_marker(driver_path):
    """Even without a terminal marker on the final visible message, an older
    historic marker must NOT trigger preservation — the gate only engages via
    allowUnmarkedShorterTerminalSnapshot, not the marker check."""
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
        "streamId": "session-6120",
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
    ]
    # The visible tail "Current final reply" should NOT be preserved because
    # the server snapshot is an authoritative prefix. The historic terminal
    # marker from an earlier turn must not preserve the tail.
    assert observed == expected, (
        f"unmarked preservation must replace with authoritative server snapshot (no marker on tail): {observed}"
    )
