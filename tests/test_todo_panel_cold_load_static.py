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
