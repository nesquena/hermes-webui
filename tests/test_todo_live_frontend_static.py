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
    assert "scheduleTodosRefresh()" in block


@pytest.mark.skipif(shutil.which("node") is None, reason="node is required for frontend scheduler behavior test")
def test_schedule_todos_refresh_fans_out_without_breaking_sidebar_path(tmp_path):
    script = r'''
const fs = require('fs');
const vm = require('vm');
const src = fs.readFileSync(process.argv[2], 'utf8');
const start = src.indexOf('let _todosLastRenderedHash=null;');
const end = src.indexOf('function _resetTodosRenderCache()', start);
if (start < 0 || end < 0) throw new Error('todo scheduler block not found');
const block = src.slice(start, end) + '\nthis.scheduleTodosRefresh = scheduleTodosRefresh;';
function run(panelActive) {
  const panel = {classList:{contains:(name)=> name === 'active' && panelActive}};
  const calls = {load:0, workspace:0, raf:0};
  const context = {
    document:{getElementById:(id)=> id === 'panelTodos' ? panel : null},
    requestAnimationFrame:(cb)=>{ calls.raf++; cb(); return calls.raf; },
    loadTodos:()=>{ calls.load++; },
    _refreshWorkspacePanelTodos:()=>{ calls.workspace++; },
  };
  vm.createContext(context);
  vm.runInContext(block, context);
  context.scheduleTodosRefresh();
  return calls;
}
function assert(cond, msg){ if(!cond) throw new Error(msg); }
let active = run(true);
assert(active.raf === 1, 'scheduler must use RAF coalescing');
assert(active.load === 1, 'active sidebar Todos must still call loadTodos');
assert(active.workspace === 1, 'scheduler must fan out to workspace refresh helper');
let inactive = run(false);
assert(inactive.load === 0, 'inactive sidebar Todos must not call loadTodos from RAF path');
assert(inactive.workspace === 1, 'workspace helper remains responsible for workspace visibility gating');
'''
    script_path = tmp_path / "todo_scheduler_test.js"
    script_path.write_text(script, encoding="utf-8")
    result = subprocess.run(
        ["node", str(script_path), str(REPO_ROOT / "static" / "ui.js")],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr or result.stdout


@pytest.mark.skipif(shutil.which("node") is None, reason="node is required for workspace todo gate behavior test")
def test_workspace_todos_refresh_gate_requires_enabled_visible_todos_tab(tmp_path):
    script = r'''
const fs = require('fs');
const vm = require('vm');
const src = fs.readFileSync(process.argv[2], 'utf8');
const start = src.indexOf('function _workspaceTodosTabIsActive()');
const end = src.indexOf("if(typeof document !== 'undefined')", start);
if (start < 0 || end < 0) throw new Error('workspace todos gate block not found');
const block = src.slice(start, end) + '\nthis._refreshWorkspacePanelTodos = _refreshWorkspacePanelTodos;';
function run({enabled=true, activeTab='todos', tabHidden=false, panelHidden=false}) {
  const elements = {
    workspaceTodosTab:{hidden:tabHidden},
    workspaceTodosPanel:{hidden:panelHidden},
  };
  const calls = {load:0};
  const context = {
    window:{_workspaceTodosTab:enabled},
    document:{
      querySelector:(sel)=> sel === '.rightpanel' ? {dataset:{activeTab}} : null,
      getElementById:(id)=> elements[id] || null,
    },
    _loadWorkspacePanelTodos:()=>{ calls.load++; },
  };
  vm.createContext(context);
  vm.runInContext(block, context);
  context._refreshWorkspacePanelTodos();
  return calls.load;
}
function assert(cond, msg){ if(!cond) throw new Error(msg); }
assert(run({}) === 1, 'visible enabled workspace Todos tab must refresh');
assert(run({activeTab:'files'}) === 0, 'Files tab must not refresh workspace Todos');
assert(run({activeTab:'artifacts'}) === 0, 'Artifacts tab must not refresh workspace Todos');
assert(run({enabled:false}) === 0, 'default-off workspace Todos setting must not refresh');
assert(run({tabHidden:true}) === 0, 'hidden workspace Todos tab must not refresh');
assert(run({panelHidden:true}) === 0, 'hidden workspace Todos panel must not refresh');
'''
    script_path = tmp_path / "workspace_todos_gate_test.js"
    script_path.write_text(script, encoding="utf-8")
    result = subprocess.run(
        ["node", str(script_path), str(REPO_ROOT / "static" / "workspace.js")],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr or result.stdout


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
