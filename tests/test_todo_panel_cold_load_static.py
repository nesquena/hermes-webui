from pathlib import Path


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
    end = src.find("function _escHtml", start)

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


def test_workspace_todos_render_uses_separate_hash_cache():
    src = (REPO_ROOT / "static" / "workspace.js").read_text(encoding="utf-8")
    start = src.find("function _loadWorkspacePanelTodos()")
    end = src.find("function _escHtml", start)

    assert "let _workspaceTodosLastRenderedHash = null;" in src
    assert "function _resetWorkspaceTodosRenderCache()" in src
    assert start != -1
    assert end != -1
    helper = src[start:end]

    assert "_workspaceTodosLastRenderedHash === '__empty__'" in helper
    assert "_workspaceTodosHash(todos)" in helper
    assert "if(hash === _workspaceTodosLastRenderedHash) return;" in helper
