"""Ownership helper regression tests for loadSession attach orchestration."""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
MESSAGES_JS = (ROOT / "static" / "messages.js").read_text(encoding="utf-8")
SESSIONS_JS = (ROOT / "static" / "sessions.js").read_text(encoding="utf-8")
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="node not on PATH")


def _extract_function(source: str, name: str) -> str:
    marker = f"function {name}("
    start = source.find(marker)
    assert start != -1, f"{name}() not found in source"
    open_paren = source.find("(", start)
    close_paren = source.find(")", open_paren)
    open_brace = source.find("{", close_paren)
    depth = 1
    i = open_brace + 1
    while i < len(source) and depth:
        if source[i] == "{":
            depth += 1
        elif source[i] == "}":
            depth -= 1
        i += 1
    assert depth == 0, f"{name}() did not close"
    return source[start:i]


def _extract_const(source: str, name: str) -> str:
    marker = f"const {name}"
    start = source.find(marker)
    assert start != -1, f"{name} declaration not found"
    end = source.find(";", start)
    assert end != -1, f"{name} declaration did not terminate"
    return source[start : end + 1]


def _run_node(script: str) -> dict:
    result = subprocess.run(
        [NODE],
        input=script,
        cwd=str(ROOT),
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )
    if result.returncode != 0:
        raise AssertionError(result.stderr.strip() or result.stdout.strip())
    output = result.stdout.strip()
    if not output:
        raise AssertionError("node script emitted no JSON output")
    return json.loads(output)


def _messages_owner_script() -> str:
    return "\n".join(
        [
            _extract_const(MESSAGES_JS, "_PENDING_LIVE_ATTACHES"),
            _extract_function(MESSAGES_JS, "_pendingLiveAttachIdentity"),
            _extract_function(MESSAGES_JS, "_claimPendingLiveAttach"),
            _extract_function(MESSAGES_JS, "_ownsPendingLiveAttach"),
            _extract_function(MESSAGES_JS, "_finishPendingLiveAttach"),
            _extract_function(MESSAGES_JS, "_invalidatePendingLiveAttachClaims"),
            _extract_function(MESSAGES_JS, "_attachLiveStreamWithOwnership"),
            _extract_function(MESSAGES_JS, "attachLiveStream"),
        ]
    )


def _sessions_restore_script() -> str:
    return "\n".join(
        [
            _extract_const(SESSIONS_JS, "_ACTIVE_SESSION_SCENE_RESTORE_HIDDEN_TIMEOUT_MS"),
            _extract_function(SESSIONS_JS, "_isActiveSessionSceneRestoreOwner"),
            _extract_function(SESSIONS_JS, "_deferActiveSessionSceneRestore"),
            _extract_function(SESSIONS_JS, "_deferActiveSessionSceneRestoreAndAttach"),
        ]
    )


def _run_ownership_script() -> dict:
    js = (
        "const __EMPTY = () => {};\n"
        + _messages_owner_script()
        + "\n"
        + _sessions_restore_script()
        + """
global.__openSources = [];
global.__apiQueue = Object.create(null);
global.document = {
  visibilityState: 'visible',
  hidden: false,
  addEventListener: () => {},
  removeEventListener: () => {},
};
global.location = { href: 'http://localhost/' };
global.setTimeout = (fn) => { fn(); return 1; };
global.clearTimeout = () => {};

class FakeEventSource {
  constructor(url, options) {
    this.url = String(url || '');
    this.options = options || {};
    this.readyState = 1;
    this.OPEN = 1;
    this.CONNECTING = 0;
    this.CLOSED = 2;
  }
  close() { this.readyState = this.CLOSED; }
}
global.EventSource = FakeEventSource;
global._wireSSE = (source) => {
  const streamId = new URL(source.url, 'http://localhost/').searchParams.get('stream_id');
  const sid = Object.keys(global.INFLIGHT).find((id) => String(global.INFLIGHT[id].streamId || '') === String(streamId || ''));
  global.__openSources.push({ sid: sid || null, streamId });
  if (sid) global.LIVE_STREAMS[sid] = { streamId, source };
};
global._runJournalReplayParams = () => '';
global._scheduleAnchorRegistryCleanup = __EMPTY;
global._clearOwnerInflightState = __EMPTY;
global._clearApprovalForOwner = __EMPTY;
global._clearClarifyForOwner = __EMPTY;
global._setActivePaneIdleIfOwner = __EMPTY;
global._clearStreamHidden = __EMPTY;
global._clearStreamNotificationBackground = __EMPTY;
global._suspendSessionStreamForLiveChat = __EMPTY;
global.renderMessages = __EMPTY;
global.showLiveRunStatus = __EMPTY;
global.autoResize = __EMPTY;
global.scrollToBottom = __EMPTY;
global._queueDrainSid = null;
global._isMessagePaneNearBottom = () => true;
global._isMessageReaderUnpinned = () => false;
global._isActiveSession = () => global.S && global.S.session && global.S.session.session_id === global._activeOwnerSid;
global.setBusy = __EMPTY;
global.setComposerStatus = __EMPTY;
global.removeThinking = __EMPTY;
global._markSessionViewed = __EMPTY;
global.trackBackgroundError = __EMPTY;
global._scheduleRender = __EMPTY;
global._startSessionStream = __EMPTY;
global._isCurrentOwner = () => true;

function queueStatus(streamId) {
  let resolve;
  let reject;
  const promise = new Promise((res, rej) => { resolve = res; reject = rej; });
  (global.__apiQueue[streamId] || (global.__apiQueue[streamId] = [])).push({ promise, resolve, reject });
  return global.__apiQueue[streamId][global.__apiQueue[streamId].length - 1];
}

global.api = (url) => {
  const streamId = new URL(String(url), 'http://localhost/').searchParams.get('stream_id');
  const queue = global.__apiQueue[streamId] || [];
  const ctl = queue.shift();
  return ctl ? ctl.promise : Promise.resolve({ active: true });
};

function resetState(sid, stream, generation) {
  global._activeOwnerSid = sid;
  global._loadSessionGeneration = generation;
  global.S = {
    session: { session_id: sid, active_stream_id: stream },
    activeStreamId: stream,
    messages: [],
  };
  global.INFLIGHT = {
    [sid]: { streamId: stream, messages: [], uploaded: [], toolCalls: [], reattach: true },
  };
  global.LIVE_STREAMS = Object.create(null);
  global.__apiQueue = Object.create(null);
  global.__openSources = [];
}

function claimAttach({ sid = 'A', stream = 'stream', generation = 1, ownerToken = 'owner' }) {
  const claimState = _claimPendingLiveAttach(sid, stream, {
    isCurrentOwner: () => _isActiveSessionSceneRestoreOwner(sid, stream, generation),
    ownerToken,
    loadGeneration: generation,
    onAttached: () => {
      const latest = INFLIGHT[sid];
      if (latest && String(latest.streamId || '') === String(stream)) latest.reattach = false;
    },
  });
  if (claimState && !claimState.shouldStart) { return Promise.resolve(true); }
  const claim = claimState && claimState.claim;
  return _attachLiveStreamWithOwnership({
    activeSid: sid,
    streamId: stream,
    reconnecting: true,
    ownsAttach: () => _ownsPendingLiveAttach(claim),
    finishAttach: (attached) => _finishPendingLiveAttach(claim, attached),
    statusDecision: (status) => ({
      shouldConnect: !!(status && (status.active || status.replay_available)),
      replayOnly: !!(status && !status.active && status.replay_available),
    }),
    replayParamsForAttach: () => '',
    connectSource: () => new EventSource(new URL('api/chat/stream?stream_id=' + encodeURIComponent(stream), 'http://localhost/').href, { withCredentials: true }),
    wireSource: global._wireSSE,
  });
}

async function run() {
  const results = {};
  let gate;
  let claim;

  resetState('A', 'stale-stream', 1);
  gate = queueStatus('stale-stream');
  claim = claimAttach({ sid: 'A', stream: 'stale-stream', generation: 1, ownerToken: 'old' });
  _loadSessionGeneration = 2;
  S.session.session_id = 'B';
  S.session.active_stream_id = 'other';
  _invalidatePendingLiveAttachClaims();
  gate.resolve({ active: true });
  results.stale = await claim;
  results.staleOpenSources = global.__openSources.length;
  results.pendingAfterStale = Object.keys(_PENDING_LIVE_ATTACHES).length;

  resetState('D', 'dup-stream', 3);
  const dupGate = queueStatus('dup-stream');
  const dupA = claimAttach({ sid: 'D', stream: 'dup-stream', generation: 3, ownerToken: 'dup' });
  const dupB = claimAttach({ sid: 'D', stream: 'dup-stream', generation: 3, ownerToken: 'dup' });
  dupGate.resolve({ active: true });
  results.duplicate = {
    first: await dupA,
    second: await dupB,
    openSources: global.__openSources.filter((s) => s.streamId === 'dup-stream').length,
    reattach: INFLIGHT.D.reattach,
  };

  resetState('F', 'fallback-stream', 4);
  gate = queueStatus('fallback-stream');
  gate.promise.catch(__EMPTY);
  claim = claimAttach({ sid: 'F', stream: 'fallback-stream', generation: 4, ownerToken: 'fallback' });
  gate.reject(new Error('status-api-flake'));
  results.apiFallback = await claim;
  results.apiFallbackOpenSources = global.__openSources.filter((s) => s.streamId === 'fallback-stream').length;

  resetState('I', 'inactive-stream', 5);
  gate = queueStatus('inactive-stream');
  claim = claimAttach({ sid: 'I', stream: 'inactive-stream', generation: 5, ownerToken: 'inactive' });
  gate.resolve({ active: false });
  results.inactive = await claim;
  results.inactiveOpenSources = global.__openSources.filter((s) => s.streamId === 'inactive-stream').length;
  results.pendingAfterInactive = Object.keys(_PENDING_LIVE_ATTACHES).length;

  _loadSessionGeneration = 1;
  resetState('A', 'sup-stream', 1);
  const oldGate = queueStatus('sup-stream');
  const old = claimAttach({ sid: 'A', stream: 'sup-stream', generation: 1, ownerToken: 'sup-old' });
  S.session = { session_id: 'B', active_stream_id: 'sup-stream-other' };
  _loadSessionGeneration = 2;
  _invalidatePendingLiveAttachClaims();
  S.session = { session_id: 'A', active_stream_id: 'sup-stream' };
  const fresh = claimAttach({ sid: 'A', stream: 'sup-stream', generation: 2, ownerToken: 'sup-new' });
  const newGate = queueStatus('sup-stream');
  oldGate.resolve({ active: true });
  newGate.resolve({ active: true });
  results.supersession = {
    old: await old,
    new: await fresh,
    openSources: global.__openSources.filter((s) => s.streamId === 'sup-stream').length,
  };

  resetState('R', 'restore-stream', 1);
  const order = [];
  const restoreResult = await _deferActiveSessionSceneRestoreAndAttach(
    'R',
    'restore-stream',
    1,
    () => { order.push('restore'); throw new Error('restore failed'); },
    () => { order.push('attach'); return true; },
  );
  results.restore = { attached: !!restoreResult.attached, order };
  results.pendingFinal = Object.keys(_PENDING_LIVE_ATTACHES).length;
  console.log(JSON.stringify(results));
}

run();
"""
  )
    return _run_node(js)
def test_ownership_helpers_are_used_in_attach_and_loadsession_chokepoints():
    attach_body = re.sub(r"\s+", "", _extract_function(MESSAGES_JS, "attachLiveStream"))
    load_body = re.sub(r"\s+", "", _extract_function(SESSIONS_JS, "loadSession"))
    sessions_body = re.sub(r"\s+", "", SESSIONS_JS)
    assert "_attachLiveStreamWithOwnership(" in attach_body and "_deferActiveSessionSceneRestoreAndAttach(" in load_body and "isCurrentOwner:()=>_isActiveSessionSceneRestoreOwner" in sessions_body

def test_live_ownership_claim_and_restore_sequences_behave():
    data = _run_ownership_script()
    assert data["stale"] is False and data["staleOpenSources"] == 0 and data["pendingAfterStale"] == 0
    assert data["duplicate"]["first"] is True and data["duplicate"]["second"] is True and data["duplicate"]["openSources"] == 1 and data["duplicate"]["reattach"] is False
    assert data["apiFallback"] is True and data["apiFallbackOpenSources"] == 1
    assert data["inactive"] is False and data["inactiveOpenSources"] == 0 and data["pendingAfterInactive"] == 0
    assert data["supersession"]["new"] is True and data["supersession"]["openSources"] == 1
    assert data["restore"]["order"] == ["restore", "attach"] and data["restore"]["attached"] is True and data["pendingFinal"] == 0
