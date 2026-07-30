from __future__ import annotations

import ast
from pathlib import Path


ROUTES_PY = Path(__file__).parent.parent / "api" / "routes.py"


def _attach_todo_state_calls() -> list[ast.Call]:
    tree = ast.parse(ROUTES_PY.read_text(encoding="utf-8"))
    calls: list[ast.Call] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Name) and func.id == "attach_todo_state":
            calls.append(node)
    return calls


def test_routes_imports_attach_todo_state():
    tree = ast.parse(ROUTES_PY.read_text(encoding="utf-8"))

    assert any(
        isinstance(node, ast.ImportFrom)
        and node.module == "api.todo_state"
        and any(alias.name == "attach_todo_state" for alias in node.names)
        for node in ast.walk(tree)
    )


def test_routes_attach_todo_state_from_webui_and_cli_session_paths():
    calls = _attach_todo_state_calls()

    assert len(calls) >= 2
    arg_names = []
    for call in calls[:2]:
        assert len(call.args) == 2
        assert not call.keywords
        assert isinstance(call.args[0], ast.Name)
        assert isinstance(call.args[1], ast.Name)
        assert call.args[0].id != call.args[1].id
        arg_names.append((call.args[0].id, call.args[1].id))

    assert ("raw", "_all_msgs") in arg_names
    assert ("sess", "msgs") in arg_names
