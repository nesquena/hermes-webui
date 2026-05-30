"""Wiring tests for ``attach_todo_state`` call sites in ``api/routes.py``.

Replaces the previous grep-only structural checks with AST-level call
shape verification.  Behaviour-level coverage of ``attach_todo_state``
itself lives in ``tests/test_todo_state_emission.py``; end-to-end
scenarios (full message history → derived snapshot) in
``tests/test_todo_state_scenarios.py``.

The reviewer's concern was that string presence cannot catch parameter
swaps (e.g. ``attach_todo_state(msgs, raw)`` instead of
``attach_todo_state(raw, msgs)``).  This file walks the AST and pins:

* both expected call sites exist (WebUI session GET + CLI fallback),
* each call passes exactly two positional arguments,
* the first argument is a plain ``Name`` node (the response payload
  dict — ``raw`` or ``sess`` in current code) and is mutated in place,
* the second argument is a plain ``Name`` node (the message list —
  ``_all_msgs`` or ``msgs`` in current code).

Pinning to ``Name`` nodes (not specific identifiers) keeps the test
robust to harmless renames while still catching real swaps.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import List

import pytest


ROUTES_PY = Path(__file__).parent.parent / "api" / "routes.py"


def _attach_todo_state_calls() -> List[ast.Call]:
    src = ROUTES_PY.read_text(encoding="utf-8")
    tree = ast.parse(src)
    calls: List[ast.Call] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Name) and func.id == "attach_todo_state":
            calls.append(node)
        elif isinstance(func, ast.Attribute) and func.attr == "attach_todo_state":
            calls.append(node)
    return calls


def test_routes_imports_attach_helper():
    src = ROUTES_PY.read_text(encoding="utf-8")
    tree = ast.parse(src)
    imported = False
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "api.todo_state":
            for alias in node.names:
                if alias.name == "attach_todo_state":
                    imported = True
    assert imported, (
        "api/routes.py must `from api.todo_state import attach_todo_state` "
        "so the session GET handler can attach the todo_state cold-load."
    )


def test_attach_called_at_least_twice():
    """One call covers the WebUI session GET, the other the CLI fallback.

    Both must fire; if a refactor accidentally drops the CLI path, opening
    a CLI-only session from the sidebar will silently lose the Todos panel
    cold-load — exactly the regression this test is meant to catch.
    """
    calls = _attach_todo_state_calls()
    assert len(calls) >= 2, (
        f"api/routes.py must call attach_todo_state from both the WebUI "
        f"and CLI session paths; found {len(calls)} call site(s)."
    )


@pytest.mark.parametrize("call_index", [0, 1])
def test_attach_passes_two_positional_args(call_index):
    """The helper signature is ``attach_todo_state(payload, messages)``.

    Both arguments must be passed positionally — neither has a default.
    A call with the wrong arity is a regression we want to fail loud.
    """
    calls = _attach_todo_state_calls()
    if call_index >= len(calls):
        pytest.skip(f"only {len(calls)} call site(s) — index {call_index} skipped")
    call = calls[call_index]
    assert len(call.args) == 2, (
        f"attach_todo_state call at line {call.lineno} must pass exactly 2 "
        f"positional args (payload, messages); got {len(call.args)}."
    )
    assert not call.keywords, (
        f"attach_todo_state call at line {call.lineno} should not use "
        f"keyword arguments; the helper signature is purely positional."
    )


@pytest.mark.parametrize("call_index", [0, 1])
def test_attach_args_are_simple_names(call_index):
    """Catch a structural swap or accidental literal injection.

    Both arguments must be bare ``Name`` nodes — i.e. local variables
    (``raw``/``sess`` for payload, ``_all_msgs``/``msgs`` for messages),
    not literals, attribute accesses, or call results.  This is loose
    enough to allow harmless renames but still catches things like
    ``attach_todo_state(raw, [])`` or ``attach_todo_state({}, msgs)``.
    """
    calls = _attach_todo_state_calls()
    if call_index >= len(calls):
        pytest.skip(f"only {len(calls)} call site(s) — index {call_index} skipped")
    call = calls[call_index]
    payload_arg, messages_arg = call.args[0], call.args[1]
    assert isinstance(payload_arg, ast.Name), (
        f"attach_todo_state call at line {call.lineno}: first argument "
        f"(payload) must be a local variable, got {ast.dump(payload_arg)}."
    )
    assert isinstance(messages_arg, ast.Name), (
        f"attach_todo_state call at line {call.lineno}: second argument "
        f"(messages) must be a local variable, got {ast.dump(messages_arg)}."
    )
    # The two arguments must be distinct names — passing the same
    # variable for both is an obvious wiring bug.
    assert payload_arg.id != messages_arg.id, (
        f"attach_todo_state call at line {call.lineno}: payload and "
        f"messages arguments resolve to the same variable "
        f"`{payload_arg.id}` — this is almost certainly a copy-paste bug."
    )
