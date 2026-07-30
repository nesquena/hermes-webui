from __future__ import annotations

import ast
from pathlib import Path


STREAMING_PY = Path(__file__).parent.parent / "api" / "streaming.py"


def _emit_todo_state_calls() -> list[ast.Call]:
    tree = ast.parse(STREAMING_PY.read_text(encoding="utf-8"))
    calls: list[ast.Call] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "emit_todo_state":
            calls.append(node)
    return calls


def test_streaming_imports_todo_state_emitter():
    tree = ast.parse(STREAMING_PY.read_text(encoding="utf-8"))

    assert any(
        isinstance(node, ast.ImportFrom)
        and node.module == "api.todo_state"
        and any(alias.name == "emit_todo_state" for alias in node.names)
        for node in ast.walk(tree)
    )


def test_streaming_emits_todo_state_on_both_tool_callback_shapes():
    calls = _emit_todo_state_calls()

    assert len(calls) >= 2
    for call in calls[:2]:
        kw_names = {kw.arg for kw in call.keywords}
        assert {"name", "function_result", "session_id", "stream_id"}.issubset(kw_names)


def test_streaming_prefers_full_tool_result_when_available():
    src = STREAMING_PY.read_text(encoding="utf-8")

    assert "cb_kwargs.get('result')" in src
    assert "else preview" in src
    assert "function_result=function_result" in src
