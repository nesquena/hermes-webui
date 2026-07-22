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


def _extract_functions() -> str:
    owner_body = _function_body(SESSIONS_JS, "_isActiveSessionSceneRestoreOwner")
    defer_body = _function_body(SESSIONS_JS, "_deferActiveSessionSceneRestore")
    return (
        "function _isActiveSessionSceneRestoreOwner(sid, activeStreamId, loadGeneration)"
        "{" + owner_body + "}\n"
        "function _deferActiveSessionSceneRestore(sid, activeStreamId, loadGeneration, restoreFn)"
        "{" + defer_body + "}\n"
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


def _run_script(setup: str, assertions: str) -> None:
    body = _extract_functions()
    constants = "var _ACTIVE_SESSION_SCENE_RESTORE_HIDDEN_TIMEOUT_MS = 250;\n"
    wrapped_assertions = f";(async () => {{\n{assertions}\n}})();"
    script = "const assert = require('assert');\n" + constants + "\n" + body + "\n" + setup + "\n" + wrapped_assertions
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
global.setTimeout = (fn) => {
  timeoutCalls.push(fn);
  return 999;
};
global.clearTimeout = () => {};
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
assert.deepStrictEqual(timeoutCalls, []);

const frame1 = rAFQueue.shift();
frame1();
assert.strictEqual(rAFQueue.length, 1);
assert.deepStrictEqual(__calls, []);

const frame2 = rAFQueue.shift();
frame2();

return p.then((value) => {
  assert.strictEqual(value, 'ok');
  assert.deepStrictEqual(__calls, ['restored']);
  assert.strictEqual(rAFQueue.length, 0);
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


def test_active_load_session_branches_defer_after_transcript_render_while_preserving_immediate_work_effects():
    body = _function_body(SESSIONS_JS, "loadSession")
    idx = body.rfind("if(INFLIGHT[sid]){")
    assert idx != -1, "INFLIGHT branch not found"
    inflight_block = body[idx : idx + 9000]
    defer_pos = inflight_block.find("_deferActiveSessionSceneRestore(")
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
    idle_block = body[idle_idx : idle_idx + 2400]
    idle_defer = idle_block.find("_deferActiveSessionSceneRestore(")
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

    active_inflight_re = re.search(
        r"_deferActiveSessionSceneRestore\([^\n]*=>\s*\{[\s\S]*?restoreLiveSurfaceForActiveInflight\(\)[\s\S]*?attachLiveSceneForActiveSession\(\)",
        inflight_block,
    )
    assert active_inflight_re is not None, "inflight deferred callback must restore scene before attach"

    active_discovered_re = re.search(
        r"_deferActiveSessionSceneRestore\([^\n]*=>\s*\{[\s\S]*?restoreLiveSurfaceForIdleInflight\(\)[\s\S]*?attachLiveSceneForIdleSession\(\)",
        idle_block,
    )
    assert active_discovered_re is not None, "discovered active-branch callback must restore scene before attach"
