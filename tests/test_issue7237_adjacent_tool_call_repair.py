"""Regression tests for the adjacent tool-call/result repair (#7237 standalone).

The upstream projection already drops *globally* unanswered tool_calls. What it
cannot see is the shape where a **later, unrelated** tool result makes an
**earlier, adjacent-less** call look answered: the call's id is in the global
``answered_ids`` set, so it survives, and a strict provider rejects the whole
request with a 400 (``tool_use ids were found without tool_result blocks``).

The repair runs BEFORE the global pass and pairs only an *adjacent* result with
its call, so:

* ``assistant(call), tool(result)``           → pair kept
* ``assistant(call), user, tool(result)``     → orphan dropped (user boundary)
* ``assistant(call), assistant(progress), tool(result)`` → pair kept (the Agent
  merges consecutive assistant rows, so an intermediate progress row is
  transparent)
* ``assistant(call1), tool(r1), assistant(call2), tool(r2)`` → both kept
* ``assistant(call), user`` (no result)       → orphan dropped

The repair must never mutate its input (it is a context-only projection).
"""
from api.streaming import (
    _api_safe_message_positions,
    _sanitize_messages_for_api,
    _strip_orphan_tool_calls,
)


def _call(cid):
    return {"id": cid, "type": "function", "function": {"name": "t", "arguments": "{}"}}


def _assistant_with_call(cid, content=""):
    return {"role": "assistant", "content": content, "tool_calls": [_call(cid)]}


def _tool_result(cid, content="ok"):
    return {"role": "tool", "tool_call_id": cid, "content": content}


def test_adjacent_pair_survives():
    rows = [
        {"role": "user", "content": "go"},
        _assistant_with_call("a"),
        _tool_result("a"),
    ]
    out = _strip_orphan_tool_calls(rows)
    assert out[1]["tool_calls"], "an immediately-answered call must survive"


def test_orphan_without_any_result_is_dropped():
    rows = [
        {"role": "user", "content": "go"},
        _assistant_with_call("a"),
        {"role": "user", "content": "next"},
    ]
    out = _strip_orphan_tool_calls(rows)
    assert not out[1].get("tool_calls"), "a call with no result at all is an orphan"


def test_user_boundary_between_call_and_result_is_an_orphan():
    """The result belongs to another turn — the global pass would keep the call."""
    rows = [
        {"role": "user", "content": "go"},
        _assistant_with_call("a"),
        {"role": "user", "content": "interruption"},
        _tool_result("a"),
    ]
    out = _strip_orphan_tool_calls(rows)
    assert not out[1].get("tool_calls"), "a result across a user boundary is not adjacent"


def test_intermediate_assistant_row_is_transparent():
    """The Agent merges consecutive assistant rows, so a progress row between a
    call and its result must not turn a valid pair into an orphan."""
    rows = [
        {"role": "user", "content": "go"},
        _assistant_with_call("a"),
        {"role": "assistant", "content": "working..."},
        _tool_result("a"),
    ]
    out = _strip_orphan_tool_calls(rows)
    assert out[1]["tool_calls"], "an intermediate progress row is transparent"


def test_recovered_user_row_between_pair_is_transparent():
    """#1543 stale-stream recovery materialises a ``_recovered`` user row between
    the call and its result; a later pass drops it, so it must be transparent."""
    rows = [
        {"role": "user", "content": "go"},
        _assistant_with_call("a"),
        {"role": "user", "content": "recovered", "_recovered": True},
        _tool_result("a"),
    ]
    out = _strip_orphan_tool_calls(rows)
    assert out[1]["tool_calls"], "a _recovered user row is transparent"


def test_two_adjacent_pairs_both_survive():
    rows = [
        {"role": "user", "content": "go"},
        _assistant_with_call("a"),
        _tool_result("a"),
        _assistant_with_call("b"),
        _tool_result("b"),
    ]
    out = _strip_orphan_tool_calls(rows)
    assert out[1]["tool_calls"] and out[3]["tool_calls"], "both answered pairs survive"


def test_repair_never_mutates_input():
    import copy

    rows = [
        {"role": "user", "content": "go"},
        _assistant_with_call("a"),
        {"role": "user", "content": "next"},
    ]
    snapshot = copy.deepcopy(rows)
    _strip_orphan_tool_calls(rows)
    assert rows == snapshot, "the repair is context-only and must not mutate input"


def test_all_calls_orphaned_drops_tool_calls_key():
    rows = [
        {"role": "user", "content": "go"},
        _assistant_with_call("a"),
        {"role": "user", "content": "next"},
    ]
    out = _strip_orphan_tool_calls(rows)
    assert "tool_calls" not in out[1], "the key itself is dropped, not set to []"


def test_sanitizer_keeps_pair_across_user_boundary_shape():
    """End-to-end through the real projection: a result belonging to a later
    turn must not keep an earlier unanswered call alive."""
    rows = [
        {"role": "user", "content": "go"},
        _assistant_with_call("a"),
        {"role": "user", "content": "interruption"},
        _tool_result("a"),
    ]
    out = _sanitize_messages_for_api(rows)
    for msg in out:
        assert not msg.get("tool_calls"), "no orphan call may reach the provider"


def test_sanitizer_keeps_legitimate_adjacent_pair():
    rows = [
        {"role": "user", "content": "go"},
        _assistant_with_call("a"),
        _tool_result("a"),
        {"role": "user", "content": "next"},
    ]
    out = _sanitize_messages_for_api(rows)
    kept = [m for m in out if m.get("role") == "assistant" and m.get("tool_calls")]
    assert kept, "a real answered pair must survive the projection"


def test_api_safe_positions_mirrors_the_repair():
    rows = [
        {"role": "user", "content": "go"},
        _assistant_with_call("a"),
        {"role": "user", "content": "interruption"},
        _tool_result("a"),
    ]
    out = _api_safe_message_positions(rows)
    for _idx, msg in out:
        assert not msg.get("tool_calls"), "the positions projection mirrors the repair"
