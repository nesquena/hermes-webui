import shutil
import subprocess
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).parent.parent


def test_ensure_messages_loaded_hydrates_session_todo_state_sidecar():
    src = (REPO_ROOT / "static" / "sessions.js").read_text(encoding="utf-8")

    assert "if(data.session.todo_state !== undefined)" in src
    assert "S.session.todo_state = data.session.todo_state" in src
    assert "delete S.session.todo_state" in src
    assert "_hydrateTodosFromSession(S.session)" in src
    assert "scheduleTodosRefresh()" in src


def test_load_todos_renders_single_source_of_truth_before_legacy_scan():
    src = (REPO_ROOT / "static" / "panels.js").read_text(encoding="utf-8")
    start = src.find("function loadTodos()")
    end = src.find("function _legacyTodosFromMessages()")

    assert start != -1
    assert end != -1
    load_todos = src[start:end]

    assert "if (S.todoStateMeta)" in load_todos
    assert "todos = Array.isArray(S.todos) ? S.todos : [];" in load_todos
    assert "todos = _legacyTodosFromMessages();" in load_todos
    assert load_todos.find("todos = Array.isArray(S.todos) ? S.todos : [];") < load_todos.find("todos = _legacyTodosFromMessages();")


def test_legacy_todos_fallback_still_uses_raw_session_messages():
    src = (REPO_ROOT / "static" / "panels.js").read_text(encoding="utf-8")

    assert "function _legacyTodosFromMessages()" in src
    assert "const sourceMessages = (S.session && Array.isArray(S.session.messages) && S.session.messages.length) ? S.session.messages : S.messages;" in src


def test_workspace_todos_tab_prefers_live_sse_snapshot_before_cold_load_sidecar():
    src = (REPO_ROOT / "static" / "workspace.js").read_text(encoding="utf-8")
    start = src.find("function _loadWorkspacePanelTodos()")
    end = src.find("const ARTIFACT_IGNORE_RE", start)

    assert start != -1
    assert end != -1
    helper = src[start:end]

    assert "if(S && Array.isArray(S.todos)){" in helper
    assert "todos = S.todos;" in helper
    assert "S.session.todo_state" in helper
    assert helper.find("todos = S.todos;") < helper.find("S.session.todo_state"), (
        "Workspace Todos must prefer the live S.todos snapshot so opening the tab "
        "after todo_state SSE updates does not render the stale cold-load sidecar"
    )


def test_todo_panels_delegate_rendering_to_shared_helpers():
    ui = (REPO_ROOT / "static" / "ui.js").read_text(encoding="utf-8")
    panels = (REPO_ROOT / "static" / "panels.js").read_text(encoding="utf-8")
    workspace = (REPO_ROOT / "static" / "workspace.js").read_text(encoding="utf-8")

    assert "const TODO_STATUS_RENDERING=Object.freeze({" in ui
    assert "function renderTodoStatusIcon(status,size=14)" in ui
    assert "function renderTodoRow(todo,options={})" in ui
    assert "function renderTodoRows(todos,options={})" in ui
    assert "function renderTodoEmptyState(options={})" in ui

    left_start = panels.find("function loadTodos()")
    left_end = panels.find("function _legacyTodosFromMessages()", left_start)
    workspace_start = workspace.find("function _loadWorkspacePanelTodos()")
    workspace_end = workspace.find("const ARTIFACT_IGNORE_RE", workspace_start)

    assert left_start != -1 and left_end != -1
    assert workspace_start != -1 and workspace_end != -1
    left_block = panels[left_start:left_end]
    workspace_block = workspace[workspace_start:workspace_end]

    assert "renderTodoEmptyState()" in left_block
    assert "renderTodoRows(todos, {metadata:true})" in left_block
    assert "renderTodoEmptyState({centered:true})" in workspace_block
    assert "renderTodoRows(todos, {metadata:true})" in workspace_block


def test_workspace_todos_no_longer_defines_local_icon_or_empty_text_mapping():
    src = (REPO_ROOT / "static" / "workspace.js").read_text(encoding="utf-8")
    start = src.find("function _loadWorkspacePanelTodos()")
    end = src.find("const ARTIFACT_IGNORE_RE", start)

    assert start != -1 and end != -1
    helper = src[start:end]

    assert "No active tasks" not in helper
    assert "const statusIcon" not in helper
    assert "<svg" not in helper
    assert "renderTodoEmptyState({centered:true})" in helper


def test_workspace_files_and_artifacts_paths_stay_outside_todo_render_change():
    src = (REPO_ROOT / "static" / "workspace.js").read_text(encoding="utf-8")
    todos_start = src.find("function _loadWorkspacePanelTodos()")
    artifacts_start = src.find("const ARTIFACT_IGNORE_RE")

    assert todos_start != -1
    assert artifacts_start != -1
    assert "renderSessionArtifacts()" in src[:todos_start]
    assert "const ARTIFACT_MUTATION_TOOLS = new Set" in src[artifacts_start:]
    assert "function _normalizeArtifactPath(path)" in src[artifacts_start:]


@pytest.mark.skipif(shutil.which("node") is None, reason="node is required for shared todo renderer behavior test")
def test_shared_todo_renderer_outputs_consistent_status_markup(tmp_path):
    ui = (REPO_ROOT / "static" / "ui.js").read_text(encoding="utf-8")
    start = ui.find("const TODO_STATUS_RENDERING=Object.freeze({")
    end = ui.find("function _todosPanelIsActive()", start)

    assert start != -1
    assert end != -1
    helper = ui[start:end]
    script = f"""
const helper = {helper!r};
const liCalls = [];
function li(name, size) {{
  liCalls.push([name, size]);
  return `<svg data-icon="${{name}}" data-size="${{size}}"></svg>`;
}}
function esc(value) {{
  return String(value ?? '').replace(/[&<>"']/g, c => ({{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}}[c]));
}}
function t(key) {{ return key === 'todos_no_active' ? 'No active task list in this session.' : key; }}
const api = new Function('li', 'esc', 't', helper + '; return {{TODO_STATUS_RENDERING, todoStatusKey, renderTodoRow, renderTodoRows, renderTodoEmptyState}};')(li, esc, t);
function assert(cond, msg) {{ if(!cond) throw new Error(msg); }}
const expected = {{
  pending: ['square', 'var(--muted)'],
  in_progress: ['loader', 'var(--blue)'],
  completed: ['check', 'rgba(100,200,100,.8)'],
  cancelled: ['x', 'rgba(200,100,100,.5)'],
}};
for (const [status, [icon, color]] of Object.entries(expected)) {{
  assert(api.TODO_STATUS_RENDERING[status].icon === icon, status + ' icon mismatch');
  assert(api.TODO_STATUS_RENDERING[status].color === color, status + ' color mismatch');
  const row = api.renderTodoRow({{id: status + '-id', content: status + ' content', status}}, {{metadata: true}});
  assert(row.includes(`data-icon="${{icon}}"`), status + ' row icon mismatch');
  assert(row.includes(color), status + ' row color mismatch');
  assert(row.includes(`${{status}}-id · ${{status}}`), status + ' metadata mismatch');
}}
assert(api.todoStatusKey('unknown') === 'pending', 'unknown statuses fall back to pending');
assert(api.renderTodoRows([{{id:'a', content:'A', status:'pending'}}, {{id:'b', text:'B', status:'completed'}}], {{metadata:true}}).includes('b · completed'), 'shared rows include metadata');
assert(api.renderTodoEmptyState({{centered:true}}).includes('No active task list in this session.'), 'empty state uses i18n text');
"""
    script_path = tmp_path / "shared_todo_renderer_test.js"
    script_path.write_text(script, encoding="utf-8")
    result = subprocess.run(
        ["node", str(script_path)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr or result.stdout
