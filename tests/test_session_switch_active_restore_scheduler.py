#!/usr/bin/env python3
"""Scheduler behavior tests for active session scene restore deferral."""

import re
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SESSIONS_JS = (REPO_ROOT / "static" / "sessions.js").read_text(encoding="utf-8")


def _function_body(src: str, name: str) -> str:
    marker = f"function {name}("
    start = src.find(marker)
    assert start != -1, f"{name}() not found"
    brace = src.find("){", start)
    assert brace != -1, f"{name}() body not found"
    brace += 1
    depth = 1
    i = brace + 1
    while i < len(src) and depth:
        if src[i] == "{":
            depth += 1
        elif src[i] == "}":
            depth -= 1
        i += 1
    assert depth == 0, f"{name}() body did not close"
    return src[brace + 1 : i - 1]


def _extract_function(src: str, name: str) -> str:
    start = src.find(f"function {name}(")
    assert start != -1, f"{name}() not found"
    close_paren = src.find(")", start)
    brace = src.find("{", close_paren)
    assert close_paren != -1 and brace != -1, f"{name}() signature not found"
    depth = 1
    i = brace + 1
    while i < len(src) and depth:
        if src[i] == "{":
            depth += 1
        elif src[i] == "}":
            depth -= 1
        i += 1
    assert depth == 0, f"{name}() did not close"
    return src[start:i]


def _extract_functions() -> str:
    owner_body = _function_body(SESSIONS_JS, "_isActiveSessionSceneRestoreOwner")
    defer_body = _function_body(SESSIONS_JS, "_deferActiveSessionSceneRestore")
    pending_body = (
        _function_body(SESSIONS_JS, "_activeSessionSceneRestorePendingFor")
        if "function _activeSessionSceneRestorePendingFor(" in SESSIONS_JS
        else "return null;"
    )
    return (
        "let _activeSessionSceneRestorePending = null;\n"
        "function _isActiveSessionSceneRestoreOwner(sid, activeStreamId, loadGeneration)"
        "{" + owner_body + "}\n"
        "function _activeSessionSceneRestorePendingFor(sid, streamId)"
        "{" + pending_body + "}\n"
        "function _deferActiveSessionSceneRestore(sid, activeStreamId, loadGeneration, restoreFn)"
        "{" + defer_body + "}\n"
        + (
            _extract_function(SESSIONS_JS, "_deferActiveSessionSceneRestoreAndAttach") + "\n"
            if "function _deferActiveSessionSceneRestoreAndAttach(" in SESSIONS_JS
            else ""
        )
    )


def _run_node(script: str) -> str:
    proc = subprocess.run(
        ["node", "-e", script],
        check=False,
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        if proc.stderr:
            raise AssertionError(proc.stderr.strip())
        raise AssertionError(proc.stdout.strip())
    return proc.stdout.strip()


def _run_script(setup: str, assertions: str, extra_functions: str = "") -> None:
    body = _extract_functions()
    constants = "var _ACTIVE_SESSION_SCENE_RESTORE_HIDDEN_TIMEOUT_MS = 250;\n"
    wrapped_assertions = f";(async () => {{\n{assertions}\n}})();"
    script = "const assert = require('assert');\n" + constants + "\n" + body + "\n" + extra_functions + "\n" + setup + "\n" + wrapped_assertions
    output = _run_node(script)
    assert "ok" in output


def test_active_session_scene_restore_hidden_timeout_constant_is_1200_ms():
    """Keep a fast 250ms Node override, and pin the production default."""
    match = re.search(
        r"const\s+_ACTIVE_SESSION_SCENE_RESTORE_HIDDEN_TIMEOUT_MS\s*=\s*(\d+);",
        SESSIONS_JS,
    )
    assert match is not None, (
        "Production sessions.js must declare _ACTIVE_SESSION_SCENE_RESTORE_HIDDEN_TIMEOUT_MS"
    )
    assert match.group(1) == "1200", (
        "_ACTIVE_SESSION_SCENE_RESTORE_HIDDEN_TIMEOUT_MS should remain at 1200ms in production JS"
    )


def test_defer_active_session_scene_restore_visible_uses_two_frames_and_invokes_once():
    setup = """
const state = { sid: 'bench-sid', stream: 'bench-stream', gen: 7 };
global.S = { session: { session_id: state.sid, active_stream_id: state.stream }, activeStreamId: state.stream };
global._loadSessionGeneration = state.gen;
global.document = {
  visibilityState: 'visible',
  hidden: false,
  _handlers: {},
  addEventListener(type, fn) {
    (this._handlers[type] || (this._handlers[type] = [])).push(fn);
  },
  removeEventListener() {},
};

const rAFQueue = [];
let rafId = 1;
global.requestAnimationFrame = (cb) => {
  rAFQueue.push(cb);
  return rafId++;
};
global.cancelAnimationFrame = () => {};
let timeoutCalls = [];
let clearedTimeouts = [];
global.setTimeout = (fn, ms) => {
  const timer = { fn, ms, id: timeoutCalls.length + 1 };
  timeoutCalls.push(timer);
  return timer.id;
};
global.clearTimeout = (id) => { clearedTimeouts.push(id); };
global.__calls = [];

const restore = () => {
  global.__calls.push('restored');
  return 'ok';
};
"""

    assertions = """
const p = _deferActiveSessionSceneRestore(state.sid, state.stream, state.gen, restore);
assert.strictEqual(rAFQueue.length, 1);
assert.deepStrictEqual(__calls, []);
assert.strictEqual(timeoutCalls.length, 1, 'visible first-frame request must arm bounded fallback');

const frame1 = rAFQueue.shift();
frame1();
assert.strictEqual(rAFQueue.length, 1);
assert.deepStrictEqual(__calls, []);

const frame2 = rAFQueue.shift();
frame2();

return p.then((value) => {
  timeoutCalls[0].fn();
  assert.strictEqual(value, 'ok');
  assert.deepStrictEqual(__calls, ['restored']);
  assert.strictEqual(rAFQueue.length, 0);
  assert.deepStrictEqual(clearedTimeouts, [timeoutCalls[0].id]);
  console.log('ok');
});
"""
    _run_script(setup, assertions)


def test_defer_active_session_scene_restore_hidden_visibility_fallback_invokes_once():
    setup = """
const state = { sid: 'bench-sid', stream: 'bench-stream', gen: 11 };
global.S = { session: { session_id: state.sid, active_stream_id: state.stream }, activeStreamId: state.stream };
global._loadSessionGeneration = state.gen;
global.document = {
  visibilityState: 'visible',
  hidden: false,
  _handlers: {},
  addEventListener(type, fn) {
    (this._handlers[type] || (this._handlers[type] = [])).push(fn);
  },
  removeEventListener() {},
  dispatchEvent(event) {
    const handlers = this._handlers[event.type] || [];
    for (const fn of handlers) {
      fn(event);
    }
  }
};

global.requestAnimationFrame = (cb) => cb && cb();
global.cancelAnimationFrame = () => {};

let timeoutCbs = [];
global.setTimeout = (fn) => {
  timeoutCbs.push(fn);
  return 123;
};
global.clearTimeout = () => {};
global.__calls = [];

const restore = () => {
  global.__calls.push('restored');
  return 'ok';
};
"""

    assertions = """
document.hidden = true;
document.visibilityState = 'hidden';
const p = _deferActiveSessionSceneRestore(state.sid, state.stream, state.gen, restore);
document.dispatchEvent({ type: 'visibilitychange' });
assert.strictEqual(timeoutCbs.length, 1);
assert.deepStrictEqual(__calls, []);

return Promise.resolve(timeoutCbs.shift())
  .then((fn) => fn())
  .then(() => p)
  .then((value) => {
    assert.strictEqual(value, 'ok');
    assert.deepStrictEqual(__calls, ['restored']);
    console.log('ok');
  });
"""
    _run_script(setup, assertions)


def test_defer_active_session_scene_restore_stale_owner_noop_resolves_once():
    setup = """
const state = { sid: 'bench-sid', stream: 'bench-stream', gen: 7 };
global.S = {
  session: { session_id: 'bench-other', active_stream_id: 'bench-stream' },
  activeStreamId: 'bench-stream',
};
global._loadSessionGeneration = 8;

global.document = {
  visibilityState: 'visible',
  hidden: false,
  addEventListener(type, fn) {},
  removeEventListener() {},
};

global.requestAnimationFrame = (cb) => { cb && cb(); return 1; };
global.cancelAnimationFrame = () => {};

global.setTimeout = () => 1;
global.clearTimeout = () => {};
global.__calls = [];

const restore = () => {
  global.__calls.push('restored');
  return 'ok';
};
"""

    assertions = """
return _deferActiveSessionSceneRestore(state.sid, state.stream, state.gen, restore).then((value) => {
  assert.strictEqual(value, undefined);
  assert.deepStrictEqual(__calls, []);
  console.log('ok');
});
"""
    _run_script(setup, assertions)


def test_defer_active_session_scene_restore_stale_owner_stream_change_noop_resolves_once():
    setup = """
const state = { sid: 'bench-sid', stream: 'bench-stream', gen: 7 };
global.S = {
  session: { session_id: 'bench-sid', active_stream_id: 'bench-stream-other' },
  activeStreamId: 'bench-stream-other',
};
global._loadSessionGeneration = state.gen;

global.document = {
  visibilityState: 'visible',
  hidden: false,
  addEventListener(type, fn) {},
  removeEventListener() {},
};

global.requestAnimationFrame = (cb) => { cb && cb(); return 1; };
global.cancelAnimationFrame = () => {};

global.setTimeout = () => 1;
global.clearTimeout = () => {};
global.__calls = [];

const restore = () => {
  global.__calls.push('restored');
  return 'ok';
};
"""

    assertions = """
return _deferActiveSessionSceneRestore(state.sid, state.stream, state.gen, restore).then((value) => {
  assert.strictEqual(value, undefined);
  assert.deepStrictEqual(__calls, []);
  console.log('ok');
});
"""
    _run_script(setup, assertions)


def test_defer_active_session_scene_restore_stale_owner_generation_noop_resolves_once():
    setup = """
const state = { sid: 'bench-sid', stream: 'bench-stream', gen: 7 };
global.S = {
  session: { session_id: 'bench-sid', active_stream_id: 'bench-stream' },
  activeStreamId: 'bench-stream',
};
global._loadSessionGeneration = 8;

global.document = {
  visibilityState: 'visible',
  hidden: false,
  addEventListener(type, fn) {},
  removeEventListener() {},
};

global.requestAnimationFrame = (cb) => { cb && cb(); return 1; };
global.cancelAnimationFrame = () => {};

global.setTimeout = () => 1;
global.clearTimeout = () => {};
global.__calls = [];

const restore = () => {
  global.__calls.push('restored');
  return 'ok';
};
"""

    assertions = """
return _deferActiveSessionSceneRestore(state.sid, state.stream, state.gen, restore).then((value) => {
  assert.strictEqual(value, undefined);
  assert.deepStrictEqual(__calls, []);
  console.log('ok');
});
"""
    _run_script(setup, assertions)


def test_visible_stalled_raf_fallback_restores_then_attaches_once_and_cleans_up():
    setup = """
const state = { sid: 'bench-sid', stream: 'bench-stream', gen: 7 };
global.S = { session: { session_id: state.sid, active_stream_id: state.stream }, activeStreamId: state.stream };
global._loadSessionGeneration = state.gen;
const listeners = new Map();
let removedListeners = [];
global.document = {
  visibilityState: 'visible', hidden: false,
  addEventListener(type, fn) { listeners.set(type, fn); },
  removeEventListener(type, fn) { removedListeners.push([type, fn]); },
};
const rAFQueue = [];
let cancelledFrames = [];
global.requestAnimationFrame = (cb) => { rAFQueue.push(cb); return rAFQueue.length; };
global.cancelAnimationFrame = (id) => { cancelledFrames.push(id); };
const timers = [];
let clearedTimers = [];
global.setTimeout = (fn, ms) => { const timer = { fn, ms, id: timers.length + 1 }; timers.push(timer); return timer.id; };
global.clearTimeout = (id) => { clearedTimers.push(id); };
global.__calls = [];
"""
    assertions = """
let settled = false;
let result;
const pending = _deferActiveSessionSceneRestoreAndAttach(
  state.sid, state.stream, state.gen,
  () => { __calls.push('restore'); throw new Error('restore failed'); },
  () => { __calls.push('attach'); return true; },
);
pending.then((value) => { settled = true; result = value; });
for (const timer of timers.slice()) timer.fn();
await Promise.resolve();
await Promise.resolve();
assert.deepStrictEqual(__calls, ['restore', 'attach'], 'stalled visible rAF must fall back to restore and attach');
assert.strictEqual(settled, true);
assert.deepStrictEqual(result, { restoreResult: undefined, attached: true });
assert.strictEqual(timers.length, 1);
assert.strictEqual(timers[0].ms, 250);
assert.deepStrictEqual(clearedTimers, []);
assert.strictEqual(removedListeners.length, 1);
assert.strictEqual(cancelledFrames.length, 1);
assert.strictEqual(_activeSessionSceneRestorePendingFor(state.sid, state.stream), null);
for (const frame of rAFQueue) frame();
timers[0].fn();
await Promise.resolve();
assert.deepStrictEqual(__calls, ['restore', 'attach'], 'late frame/timer must not repeat restore or attach');
console.log('ok');
"""
    _run_script(setup, assertions)


def _idle_cleanup_functions() -> str:
    names = (
        "_isServerIdleSessionRow",
        "_reconcileActiveSessionIdleStateFromList",
        "_purgeStaleInflightEntries",
        "_hasOwnedOpenLiveStream",
    )
    return "\n".join(_extract_function(SESSIONS_JS, name) for name in names)


def _idle_cleanup_setup() -> str:
    return """
global.S = { session: { session_id: 'A', active_stream_id: 'stream-A', pending_user_message: 'prompt' }, activeStreamId: 'stream-A', busy: true };
global._loadSessionGeneration = 1;
global._sendInProgress = false;
global._sendInProgressSid = null;
global._sessionListSourceById = new Map();
global._allSessionsScope = null;
global._sessionStreamingById = new Map();
global._allSessions = [
  { session_id: 'A', is_streaming: false, active_stream_id: null, pending_user_message: null, has_pending_user_message: false, pending_started_at: null },
  { session_id: 'B', is_streaming: false, active_stream_id: null, pending_user_message: null, has_pending_user_message: false, pending_started_at: null },
];
global.INFLIGHT = { A: { streamId: 'stream-A', reattach: true }, B: { streamId: 'other-stream', reattach: true } };
global.LIVE_STREAMS = Object.create(null);
global.__clearedInflight = [];
global.clearInflightState = (sid) => { __clearedInflight.push(sid); };
global._forgetObservedStreamingSession = () => {};
global.hideApprovalCard = () => {};
global.hideLiveRunStatus = () => {};
global.clearLiveToolCards = () => {};
global.updateSendBtn = () => {};
global._scheduleActiveSessionIdleReload = () => {};
"""


def test_idle_list_reconciler_preserves_current_deferred_restore_window():
    setup = _idle_cleanup_setup() + """
global.document = { visibilityState: 'visible', hidden: false, addEventListener() {}, removeEventListener() {} };
global.requestAnimationFrame = (cb) => { global.__raf.push(cb); return global.__raf.length; };
global.cancelAnimationFrame = () => {};
global.setTimeout = (fn) => { global.__timers.push(fn); return global.__timers.length; };
global.clearTimeout = () => {};
global.__raf = [];
global.__timers = [];
"""
    assertions = """
const pending = _deferActiveSessionSceneRestore('A', 'stream-A', 1, () => 'restored');
assert.strictEqual(_reconcileActiveSessionIdleStateFromList(_allSessions), false);
assert.strictEqual(S.busy, true, 'idle reconciliation must retain current scheduled work');
assert.strictEqual(S.activeStreamId, 'stream-A');
assert.strictEqual(INFLIGHT.A.streamId, 'stream-A');
assert.deepStrictEqual(__clearedInflight, []);
for (let i = 0; i < __raf.length; i++) __raf[i]();
return pending.then(() => {
  assert.strictEqual(_reconcileActiveSessionIdleStateFromList(_allSessions), true, 'ordinary cleanup resumes after scheduler completion');
  assert.strictEqual(S.busy, false);
  assert.strictEqual(INFLIGHT.A, undefined);
  assert.strictEqual(_activeSessionSceneRestorePendingFor('A', 'stream-A'), null);
  console.log('ok');
});
"""
    _run_script(setup, assertions, _idle_cleanup_functions())


def test_idle_row_purge_preserves_only_matching_pending_stream_then_purges_after_window():
    setup = _idle_cleanup_setup() + """
global.document = { visibilityState: 'visible', hidden: false, addEventListener() {}, removeEventListener() {} };
global.requestAnimationFrame = (cb) => { global.__raf.push(cb); return global.__raf.length; };
global.cancelAnimationFrame = () => {};
global.setTimeout = (fn) => { global.__timers.push(fn); return global.__timers.length; };
global.clearTimeout = () => {};
global.__raf = [];
global.__timers = [];
"""
    assertions = """
const pending = _deferActiveSessionSceneRestore('A', 'stream-A', 1, () => 'restored');
_purgeStaleInflightEntries();
assert.strictEqual(INFLIGHT.A && INFLIGHT.A.streamId, 'stream-A', 'purge must preserve the pending scheduler-owned stream');
assert.strictEqual(INFLIGHT.B, undefined, 'purge must still remove an unrelated idle session');
assert.deepStrictEqual(__clearedInflight, ['B']);
for (let i = 0; i < __raf.length; i++) __raf[i]();
return pending.then(() => {
  S.busy = true;
  S.activeStreamId = 'stream-A';
  S.session.active_stream_id = 'stream-A';
  INFLIGHT.A.reattach = true;
  _purgeStaleInflightEntries();
  assert.strictEqual(INFLIGHT.A, undefined, 'completed scheduler must not preserve stale idle work via busy or reattach flags');
  assert.strictEqual(_activeSessionSceneRestorePendingFor('A', 'stream-A'), null);
  assert.deepStrictEqual(__clearedInflight, ['B', 'A']);
  console.log('ok');
});
"""
    _run_script(setup, assertions, _idle_cleanup_functions())


def test_pending_restore_does_not_preserve_inflight_from_another_stream():
    setup = _idle_cleanup_setup() + """
global.document = { visibilityState: 'visible', hidden: false, addEventListener() {}, removeEventListener() {} };
global.requestAnimationFrame = (cb) => { global.__raf.push(cb); return global.__raf.length; };
global.cancelAnimationFrame = () => {};
global.setTimeout = (fn) => { global.__timers.push(fn); return global.__timers.length; };
global.clearTimeout = () => {};
global.__raf = [];
global.__timers = [];
global.INFLIGHT = { A: { streamId: 'stream-A', reattach: true } };
"""
    assertions = """
const pending = _deferActiveSessionSceneRestore('A', 'stream-A', 1, () => 'restored');
INFLIGHT.A.streamId = 'older-stream';
_purgeStaleInflightEntries();
assert.strictEqual(INFLIGHT.A, undefined, 'pending restore for stream-A must not retain an older INFLIGHT stream');
assert.deepStrictEqual(__clearedInflight, ['A']);
assert.ok(_activeSessionSceneRestorePendingFor('A', 'stream-A'), 'the current scheduler owner itself remains pending');
for (let i = 0; i < __raf.length; i++) __raf[i]();
assert.strictEqual(await pending, 'restored');
console.log('ok');
"""
    _run_script(setup, assertions, _idle_cleanup_functions())


def test_pending_restore_cleanup_fails_closed_after_session_stream_or_generation_changes():
    setup = _idle_cleanup_setup() + """
global.document = { visibilityState: 'visible', hidden: false, addEventListener() {}, removeEventListener() {} };
global.__raf = [];
global.__timers = [];
global.requestAnimationFrame = (cb) => { __raf.push(cb); return __raf.length; };
global.cancelAnimationFrame = () => {};
global.setTimeout = (fn) => { __timers.push(fn); return __timers.length; };
global.clearTimeout = () => {};
"""
    assertions = """
for (const mismatch of ['session', 'stream', 'generation']) {
  _activeSessionSceneRestorePending = null;
  S = { session: { session_id: 'A', active_stream_id: 'stream-A' }, activeStreamId: 'stream-A', busy: true };
  _loadSessionGeneration = 1;
  INFLIGHT = { A: { streamId: 'stream-A', reattach: true } };
  __clearedInflight.length = 0;
  const frameIndex = __raf.length;
  const pending = _deferActiveSessionSceneRestore('A', 'stream-A', 1, () => 'restored');
  if (mismatch === 'session') S.session.session_id = 'B';
  if (mismatch === 'stream') { S.activeStreamId = 'new-stream'; S.session.active_stream_id = 'new-stream'; }
  if (mismatch === 'generation') _loadSessionGeneration = 2;
  _purgeStaleInflightEntries();
  assert.strictEqual(INFLIGHT.A, undefined, mismatch + ' mismatch must restore ordinary stale cleanup');
  assert.deepStrictEqual(__clearedInflight, ['A']);
  assert.strictEqual(_activeSessionSceneRestorePendingFor('A', 'stream-A'), null);
  __raf[frameIndex]();
  assert.strictEqual(await pending, undefined);
}
console.log('ok');
"""
    _run_script(setup, assertions, _idle_cleanup_functions())


def test_old_same_session_scheduler_completion_cannot_clear_newer_pending_work():
    setup = """
const state = { sid: 'A', stream: 'stream-A', gen: 1 };
global.S = { session: { session_id: state.sid, active_stream_id: state.stream }, activeStreamId: state.stream };
global._loadSessionGeneration = state.gen;
global.document = { visibilityState: 'visible', hidden: false, addEventListener() {}, removeEventListener() {} };
global.__raf = [];
global.__timers = [];
global.requestAnimationFrame = (cb) => { __raf.push(cb); return __raf.length; };
global.cancelAnimationFrame = () => {};
global.setTimeout = (fn) => { __timers.push(fn); return __timers.length; };
global.clearTimeout = () => {};
"""
    assertions = """
const old = _deferActiveSessionSceneRestore('A', 'stream-A', 1, () => 'old');
const newer = _deferActiveSessionSceneRestore('A', 'stream-A', 1, () => 'new');
__raf[0]();
assert.strictEqual(await old, undefined, 'superseded restore should lose ownership');
assert.ok(_activeSessionSceneRestorePendingFor('A', 'stream-A'), 'old completion must not remove the newer same-session token');
__raf[1]();
__raf[2]();
assert.strictEqual(await newer, 'new');
assert.strictEqual(_activeSessionSceneRestorePendingFor('A', 'stream-A'), null);
console.log('ok');
"""
    _run_script(setup, assertions)


def test_old_aba_completion_cannot_clear_newer_generation_pending_work():
    setup = """
const state = { sid: 'A', stream: 'stream-A', gen: 1 };
global.S = { session: { session_id: state.sid, active_stream_id: state.stream }, activeStreamId: state.stream };
global._loadSessionGeneration = state.gen;
global.document = { visibilityState: 'visible', hidden: false, addEventListener() {}, removeEventListener() {} };
global.__raf = [];
global.__timers = [];
global.requestAnimationFrame = (cb) => { __raf.push(cb); return __raf.length; };
global.cancelAnimationFrame = () => {};
global.setTimeout = (fn) => { __timers.push(fn); return __timers.length; };
global.clearTimeout = () => {};
"""
    assertions = """
const old = _deferActiveSessionSceneRestore('A', 'stream-A', 1, () => 'old');
_loadSessionGeneration = 2;
const newer = _deferActiveSessionSceneRestore('A', 'stream-A', 2, () => 'new');
__raf[0]();
assert.strictEqual(await old, undefined);
assert.strictEqual(_activeSessionSceneRestorePendingFor('A', 'stream-A').loadGeneration, 2);
__raf[1]();
__raf[2]();
assert.strictEqual(await newer, 'new');
assert.strictEqual(_activeSessionSceneRestorePendingFor('A', 'stream-A'), null);
console.log('ok');
"""
    _run_script(setup, assertions)


def test_active_load_session_branches_defer_after_transcript_render_while_preserving_immediate_work_effects():
    body = _function_body(SESSIONS_JS, "loadSession")
    idx = body.rfind("if(INFLIGHT[sid]){")
    assert idx != -1, "INFLIGHT branch not found"
    inflight_block = body[idx : idx + 9000]
    defer_pos = inflight_block.find("_deferActiveSessionSceneRestoreAndAttach(")
    assert defer_pos != -1, "active in-memory INFLIGHT branch must defer restore/attach"
    sync_topbar_pos = inflight_block.find("syncTopbar();")
    render_messages_pos = inflight_block.find("renderMessages(")
    busy_pos = inflight_block.find("setBusy(true)")
    composer_status_pos = inflight_block.find("setComposerStatus('')")
    start_approval_pos = inflight_block.find("startApprovalPolling(sid)")
    start_clarify_pos = inflight_block.find("startClarifyPolling(sid)")
    defer_workspace_pos = inflight_block.find("_deferWorkspaceRefreshForSession(sid)")
    assert sync_topbar_pos != -1, "inflight branch should call syncTopbar() before deferring restore"
    assert sync_topbar_pos < defer_pos
    assert render_messages_pos != -1, "inflight branch should render messages before deferring restore"
    assert render_messages_pos < defer_pos
    assert busy_pos != -1, "inflight branch should set busy before deferring restore"
    assert busy_pos < defer_pos
    assert composer_status_pos != -1, "inflight branch should clear composer status before deferring restore"
    assert composer_status_pos < defer_pos
    assert start_approval_pos != -1, "inflight branch should restart approval polling before deferring restore"
    assert start_approval_pos < defer_pos
    assert start_clarify_pos != -1, "inflight branch should restart clarify polling before deferring restore"
    assert start_clarify_pos < defer_pos
    assert defer_workspace_pos != -1, "inflight branch should defer workspace refresh before deferring restore"
    assert defer_workspace_pos < defer_pos
    assert "scheduledActiveInflight" not in inflight_block, "loadSession active INFLIGHT branch should not snapshot restored snapshot state for reinsertion"
    assert "restoreScheduledActiveInflight" not in inflight_block, "loadSession active INFLIGHT branch should not reinsert a retained snapshot INFLIGHT"

    idle_idx = body.find("if(activeStreamId){")
    assert idle_idx != -1, "discovered active-stream branch not found"
    idle_block = body[idle_idx : idle_idx + 4200]
    idle_defer = idle_block.find("_deferActiveSessionSceneRestoreAndAttach(")
    assert idle_defer != -1, "active discovered branch must defer restore/attach"
    idle_sync_topbar_pos = idle_block.find("syncTopbar();")
    idle_render_messages_pos = idle_block.find("renderMessages(")
    idle_busy_pos = idle_block.find("S.busy=true")
    idle_set_status_pos = idle_block.find("setStatus('')")
    idle_composer_status_pos = idle_block.find("setComposerStatus('')")
    idle_update_queue_pos = idle_block.find("updateQueueBadge(sid)")
    idle_start_approval_pos = idle_block.find("startApprovalPolling(sid)")
    idle_start_clarify_pos = idle_block.find("startClarifyPolling(sid)")
    idle_defer_workspace_pos = idle_block.find("_deferWorkspaceRefreshForSession(sid)")
    assert idle_sync_topbar_pos != -1, "active discovered branch should call syncTopbar() before deferring restore"
    assert idle_sync_topbar_pos < idle_defer
    assert idle_render_messages_pos != -1, "active discovered branch should render messages before deferring restore"
    assert idle_render_messages_pos < idle_defer
    assert idle_busy_pos != -1, "active discovered branch should set S.busy=true before deferring restore"
    assert idle_busy_pos < idle_defer
    assert idle_set_status_pos != -1, "active discovered branch should clear status before deferring restore"
    assert idle_set_status_pos < idle_defer
    assert idle_composer_status_pos != -1, "active discovered branch should clear composer status before deferring restore"
    assert idle_composer_status_pos < idle_defer
    assert idle_update_queue_pos != -1, "active discovered branch should update queue badge before deferring restore"
    assert idle_update_queue_pos < idle_defer
    assert idle_start_approval_pos != -1, "active discovered branch should restart approval polling before deferring restore"
    assert idle_start_approval_pos < idle_defer
    assert idle_start_clarify_pos != -1, "active discovered branch should restart clarify polling before deferring restore"
    assert idle_start_clarify_pos < idle_defer
    assert idle_defer_workspace_pos != -1, "active discovered branch should defer workspace refresh before deferring restore"
    assert idle_defer_workspace_pos < idle_defer

    assert inflight_block.find("restoreLiveSurfaceForActiveInflight,", defer_pos) > defer_pos
    assert inflight_block.find("attachLiveSceneForActiveSession,", defer_pos) > defer_pos
    assert idle_block.find("restoreLiveSurfaceForIdleInflight,", idle_defer) > idle_defer
    assert idle_block.find("attachLiveSceneForIdleSession,", idle_defer) > idle_defer
