from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).parent.parent


def _read_static(path: str) -> str:
    return (REPO_ROOT / path).read_text(encoding="utf-8")


def test_frontend_state_and_inflight_storage_include_todo_snapshot():
    ui = _read_static("static/ui.js")
    messages = _read_static("static/messages.js")

    assert "todos:[],todoStateMeta:null" in ui
    assert "const todos=Array.isArray(state.todos)?state.todos:null" in ui
    assert "todoStateMeta" in ui[ui.find("function _compactInflightState"):ui.find("function _writeInflightStateMap")]
    assert "todos:Array.isArray(inflight.todos)?inflight.todos:S.todos" in messages
    assert "todoStateMeta:inflight.todoStateMeta||S.todoStateMeta||null" in messages


def test_frontend_todo_state_listener_is_registered_and_journaled():
    messages = _read_static("static/messages.js")

    assert "source.addEventListener('todo_state'" in messages
    assert "if(d.session_id&&d.session_id!==activeSid) return;" in messages
    assert "if(!S.session||S.session.session_id!==activeSid) return;" in messages
    assert "if(incomingTs&&currentTs&&incomingTs<currentTs) return;" in messages
    assert "inflight.todos=S.todos" in messages
    assert "'todo_state','approval'" in messages


def test_hydrate_todos_from_session_reconciles_cold_and_inflight_snapshots():
    ui = _read_static("static/ui.js")
    start = ui.find("function _hydrateTodosFromSession(session)")
    end = ui.find("function snapshotLiveTurnHtmlForSession")

    assert start != -1
    assert end != -1
    block = ui[start:end]
    assert "const cold=session&&session.todo_state;" in block
    assert "const streamActive=!!(session&&session.active_stream_id);" in block
    assert "const coldWins=(coldTs===0)?(!streamActive):(coldTs>inflightTs);" in block
    assert "S.todos=cold.todos" in block
    assert "S.todos=inflight.todos" in block
    assert "S.todoStateMeta=null" in block
    assert "_resetTodosRenderCache();" in block


@pytest.mark.skipif(shutil.which("node") is None, reason="node is required for frontend handler behavior test")
def test_todo_state_listener_replaces_snapshot_filters_session_and_rejects_older_ts(tmp_path):
    script = r'''
const fs = require('fs');
const src = fs.readFileSync(process.argv[2], 'utf8');
function extractTodoStateHandler(source) {
  const start = source.indexOf("source.addEventListener('todo_state'");
  if (start < 0) throw new Error('todo_state listener not found');
  const arrow = source.indexOf('=>', start);
  const bodyStart = source.indexOf('{', arrow);
  let depth = 0;
  for (let i = bodyStart; i < source.length; i++) {
    const ch = source[i];
    if (ch === '{') depth++;
    else if (ch === '}') {
      depth--;
      if (depth === 0) return source.slice(bodyStart + 1, i);
    }
  }
  throw new Error('todo_state listener body not closed');
}
const body = extractTodoStateHandler(src);
let S = {session:{session_id:'s1'}, todos:[], todoStateMeta:null};
let INFLIGHT = {s1:{messages:[]}};
let activeSid = 's1';
let persistCalls = 0;
function persistInflightState(){ persistCalls++; }
let refreshCalls = 0;
function scheduleTodosRefresh(){ refreshCalls++; }
const handler = new Function('e','S','INFLIGHT','activeSid','persistInflightState','scheduleTodosRefresh', body);
function fire(payload){ handler({data: JSON.stringify(payload)}, S, INFLIGHT, activeSid, persistInflightState, scheduleTodosRefresh); }
function assert(cond, msg){ if(!cond) throw new Error(msg); }

fire({session_id:'other', todos:[{id:'x', content:'wrong', status:'pending'}], ts:20});
assert(S.todos.length === 0, 'cross-session event must be ignored');
assert(persistCalls === 0 && refreshCalls === 0, 'ignored event must not persist or refresh');

fire({session_id:'s1', todos:[{id:'a', content:'current', status:'in_progress'}], ts:10, version:1});
assert(S.todos[0].id === 'a', 'valid event replaces S.todos');
assert(S.todoStateMeta.ts === 10, 'valid event stores timestamp');
assert(INFLIGHT.s1.todos[0].id === 'a', 'valid event mirrors into INFLIGHT');
assert(INFLIGHT.s1.todoStateMeta.ts === 10, 'valid event mirrors meta into INFLIGHT');
assert(persistCalls === 1 && refreshCalls === 1, 'valid event persists and schedules refresh');

fire({session_id:'s1', todos:[{id:'old', content:'old', status:'pending'}], ts:5, version:1});
assert(S.todos[0].id === 'a', 'older event must not roll back S.todos');
assert(persistCalls === 1 && refreshCalls === 1, 'older event must not persist or refresh');

handler({data:'not json'}, S, INFLIGHT, activeSid, persistInflightState, scheduleTodosRefresh);
assert(S.todos[0].id === 'a', 'malformed event must be swallowed');
'''
    script_path = tmp_path / "todo_listener_test.js"
    script_path.write_text(script, encoding="utf-8")
    result = subprocess.run(
        ["node", str(script_path), str(REPO_ROOT / "static" / "messages.js")],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr or result.stdout
