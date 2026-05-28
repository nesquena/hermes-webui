"""Wiring tests for the live ``todo_state`` SSE emission in ``api/streaming.py``.

The reviewer's concern with the previous grep-only structural test was that
``substring presence`` cannot catch parameter-passing errors.  This file
replaces those checks with AST-level call-shape verification:

* every ``emit_todo_state(...)`` call site is parsed,
* the required keyword arguments are pinned (``name``, ``function_result``,
  ``session_id``, ``stream_id``),
* the first positional argument is verified to be ``put``  — the queue
  callable the helper expects.

This is parse-only (no HTTP server, no live agent) but still catches
real wiring regressions: dropped kwargs, swapped positional arguments,
typos like ``stream_id=session_id``, etc.

Behavioural coverage of ``emit_todo_state`` itself lives in
``tests/test_todo_state_emission.py``; end-to-end scenarios in
``tests/test_todo_state_scenarios.py``.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import List

import pytest


STREAMING_PY = Path(__file__).parent.parent / "api" / "streaming.py"

# Required keyword arguments for every ``emit_todo_state`` call site.
# These are the names the helper consumes; renaming any of them here
# without renaming the helper signature is the regression we want to
# fail loud on.
REQUIRED_KWARGS = {"name", "function_result", "session_id", "stream_id"}


def _emit_todo_state_calls() -> List[ast.Call]:
    src = STREAMING_PY.read_text(encoding="utf-8")
    tree = ast.parse(src)
    calls: List[ast.Call] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Name) and func.id == "emit_todo_state":
            calls.append(node)
        elif isinstance(func, ast.Attribute) and func.attr == "emit_todo_state":
            calls.append(node)
    return calls


def test_streaming_imports_emit_helper():
    """Sanity check: helper must be imported so call sites resolve."""
    src = STREAMING_PY.read_text(encoding="utf-8")
    tree = ast.parse(src)
    imported = False
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "api.todo_state":
            for alias in node.names:
                if alias.name == "emit_todo_state":
                    imported = True
    assert imported, (
        "api/streaming.py must `from api.todo_state import emit_todo_state` "
        "so the live SSE path can mirror todo tool state to the browser."
    )


def test_emit_todo_state_called_at_least_twice():
    """Both legacy ``tool_progress_callback`` (event_type='tool.completed')
    and modern structured ``on_tool_complete`` paths must emit.  If only one
    fires, agents using the other path silently lose realtime updates.
    """
    calls = _emit_todo_state_calls()
    assert len(calls) >= 2, (
        f"api/streaming.py must call emit_todo_state from both the legacy "
        f"and modern tool callback paths; found {len(calls)} call site(s)."
    )


@pytest.mark.parametrize("call_index", [0, 1])
def test_emit_call_passes_put_as_first_positional(call_index):
    """First positional argument must be the SSE ``put`` callback —
    swapping it for any other identifier would break delivery.
    """
    calls = _emit_todo_state_calls()
    if call_index >= len(calls):
        pytest.skip(f"only {len(calls)} call site(s) — index {call_index} skipped")
    call = calls[call_index]
    assert call.args, (
        f"emit_todo_state call at line {call.lineno} has no positional args; "
        "the SSE `put` callback must be the first positional argument."
    )
    first = call.args[0]
    assert isinstance(first, ast.Name) and first.id == "put", (
        f"emit_todo_state call at line {call.lineno} must pass `put` as the "
        f"first positional argument; got {ast.dump(first)}."
    )


def test_every_emit_call_supplies_all_required_kwargs():
    """Every call site must name every required kwarg.  This is what
    catches a real wiring bug like dropping ``session_id`` or renaming
    ``function_result`` while leaving the helper untouched.
    """
    calls = _emit_todo_state_calls()
    for call in calls:
        present = {kw.arg for kw in call.keywords if kw.arg is not None}
        missing = REQUIRED_KWARGS - present
        assert not missing, (
            f"emit_todo_state call at line {call.lineno} is missing "
            f"required kwargs: {sorted(missing)}.  Required: "
            f"{sorted(REQUIRED_KWARGS)}; present: {sorted(present)}."
        )


def test_session_id_kwarg_uses_session_id_variable():
    """Catch a copy-paste swap like ``session_id=stream_id``.

    We cannot fully verify the runtime value, but we can pin the AST
    shape: in this file ``session_id=`` must read from a ``session_id``
    name, not from any other identifier.  If the surrounding scope ever
    renames the variable, update both this test and the call site.
    """
    calls = _emit_todo_state_calls()
    for call in calls:
        for kw in call.keywords:
            if kw.arg == "session_id":
                assert isinstance(kw.value, ast.Name), (
                    f"emit_todo_state call at line {call.lineno}: "
                    f"session_id= must be a bare `session_id` identifier, "
                    f"got {ast.dump(kw.value)}."
                )
                assert kw.value.id == "session_id", (
                    f"emit_todo_state call at line {call.lineno}: "
                    f"session_id= must read from `session_id`, "
                    f"got `{kw.value.id}` (suspected copy-paste swap)."
                )


def test_stream_id_kwarg_uses_stream_id_variable():
    """Symmetric guard: ``stream_id=`` must read from ``stream_id``."""
    calls = _emit_todo_state_calls()
    for call in calls:
        for kw in call.keywords:
            if kw.arg == "stream_id":
                assert isinstance(kw.value, ast.Name), (
                    f"emit_todo_state call at line {call.lineno}: "
                    f"stream_id= must be a bare `stream_id` identifier, "
                    f"got {ast.dump(kw.value)}."
                )
                assert kw.value.id == "stream_id", (
                    f"emit_todo_state call at line {call.lineno}: "
                    f"stream_id= must read from `stream_id`, "
                    f"got `{kw.value.id}` (suspected copy-paste swap)."
                )
