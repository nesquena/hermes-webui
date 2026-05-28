"""Unit tests for ``api.todo_state`` derivation helpers.

These tests pin the canonical snapshot shape and the fall-through
behaviour for malformed inputs. The streaming and cold-load paths both
depend on these contracts, so any breakage here will surface at the
callsites with clear errors.

Behavioural tests for ``emit_todo_state`` and ``attach_todo_state`` —
the side-effecting wrappers — live in ``tests/test_todo_state_emission.py``.
End-to-end real-world scenario coverage lives in
``tests/test_todo_state_scenarios.py``.
"""

import json

import pytest

from api.todo_state import (
    EVENT_NAME,
    PAYLOAD_KEY,
    VERSION,
    derive_todo_state,
    parse_todo_tool_result,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _todo_payload(todos):
    """Mirror what tools.todo_tool.todo_tool() returns as a JSON string."""
    pending = sum(1 for t in todos if t["status"] == "pending")
    in_progress = sum(1 for t in todos if t["status"] == "in_progress")
    completed = sum(1 for t in todos if t["status"] == "completed")
    cancelled = sum(1 for t in todos if t["status"] == "cancelled")
    return json.dumps({
        "todos": todos,
        "summary": {
            "total": len(todos),
            "pending": pending,
            "in_progress": in_progress,
            "completed": completed,
            "cancelled": cancelled,
        },
    }, ensure_ascii=False)


def _todo_msg(todos):
    return {"role": "tool", "content": _todo_payload(todos)}


# ---------------------------------------------------------------------------
# parse_todo_tool_result
# ---------------------------------------------------------------------------


class TestParseTodoToolResult:
    def test_valid_json_string(self):
        raw = _todo_payload([
            {"id": "1", "content": "first", "status": "pending"},
            {"id": "2", "content": "second", "status": "in_progress"},
        ])
        snap = parse_todo_tool_result(raw)
        assert snap is not None
        assert snap["version"] == VERSION
        assert len(snap["todos"]) == 2
        assert snap["todos"][0]["content"] == "first"
        assert snap["summary"]["total"] == 2
        assert snap["summary"]["in_progress"] == 1

    def test_accepts_pre_parsed_dict(self):
        # Defensive: future callers may deserialize earlier.
        data = {"todos": [{"id": "1", "content": "x", "status": "pending"}],
                "summary": {"total": 1}}
        snap = parse_todo_tool_result(data)
        assert snap is not None
        assert snap["todos"][0]["id"] == "1"

    def test_missing_summary_yields_empty_dict(self):
        raw = json.dumps({"todos": [{"id": "1", "content": "a", "status": "pending"}]})
        snap = parse_todo_tool_result(raw)
        assert snap is not None
        assert snap["summary"] == {}

    def test_summary_not_dict_replaced_with_empty(self):
        raw = json.dumps({
            "todos": [{"id": "1", "content": "a", "status": "pending"}],
            "summary": "broken",
        })
        snap = parse_todo_tool_result(raw)
        assert snap is not None
        assert snap["summary"] == {}

    @pytest.mark.parametrize("bad", [
        None,
        "",
        "not json",
        "{",
        '{"todos": broken',
    ])
    def test_invalid_json_returns_none(self, bad):
        assert parse_todo_tool_result(bad) is None

    @pytest.mark.parametrize("bad", [
        '{}',
        '{"foo": "bar"}',
        '{"todos": "not a list"}',
        '{"todos": null}',
        '"just a string"',
        '42',
    ])
    def test_wrong_shape_returns_none(self, bad):
        assert parse_todo_tool_result(bad) is None

    def test_empty_todos_list_is_valid(self):
        # An explicit "list cleared" snapshot is a valid state to surface.
        raw = json.dumps({"todos": [], "summary": {"total": 0}})
        snap = parse_todo_tool_result(raw)
        assert snap is not None
        assert snap["todos"] == []

    def test_returns_new_dict_each_call(self):
        # Mutating the returned snapshot must never alter another caller's
        # copy. The dict is built fresh by ``_normalize_snapshot``; pin the
        # invariant so a future "return data" optimization does not break
        # callers that mutate (e.g. attaching session_id/ts in emit).
        raw = _todo_payload([{"id": "1", "content": "x", "status": "pending"}])
        a = parse_todo_tool_result(raw)
        b = parse_todo_tool_result(raw)
        assert a is not None and b is not None
        assert a is not b
        a["todos"].append({"id": "ghost", "content": "z", "status": "pending"})
        assert len(b["todos"]) == 1

    def test_unicode_content_preserved(self):
        # tools.todo_tool uses ensure_ascii=False; round-tripping through
        # the parser must keep multibyte content intact (Chinese task names
        # are common in our usage).
        raw = _todo_payload([
            {"id": "1", "content": "审核架构", "status": "in_progress"},
            {"id": "2", "content": "🚀 deploy", "status": "pending"},
        ])
        snap = parse_todo_tool_result(raw)
        assert snap is not None
        assert snap["todos"][0]["content"] == "审核架构"
        assert snap["todos"][1]["content"] == "🚀 deploy"


# ---------------------------------------------------------------------------
# derive_todo_state
# ---------------------------------------------------------------------------


class TestDeriveTodoState:
    def test_empty_messages(self):
        assert derive_todo_state([]) is None
        assert derive_todo_state(None) is None

    def test_no_tool_messages(self):
        msgs = [
            {"role": "user", "content": "plan something"},
            {"role": "assistant", "content": "sure"},
        ]
        assert derive_todo_state(msgs) is None

    def test_finds_latest_when_multiple_writes(self):
        msgs = [
            _todo_msg([{"id": "1", "content": "old", "status": "completed"}]),
            {"role": "assistant", "content": "thinking"},
            _todo_msg([
                {"id": "1", "content": "new", "status": "in_progress"},
                {"id": "2", "content": "next", "status": "pending"},
            ]),
        ]
        state = derive_todo_state(msgs)
        assert state is not None
        assert state["version"] == VERSION
        assert len(state["todos"]) == 2
        assert state["todos"][0]["content"] == "new"
        assert state["todos"][0]["status"] == "in_progress"

    def test_skips_non_todo_tool_messages(self):
        msgs = [
            _todo_msg([{"id": "1", "content": "task", "status": "pending"}]),
            {"role": "tool", "content": '{"result": "ok"}'},
            {"role": "tool", "content": '{"output": "hello"}'},
        ]
        state = derive_todo_state(msgs)
        assert state is not None
        assert state["todos"][0]["content"] == "task"

    def test_malformed_json_skipped(self):
        msgs = [
            _todo_msg([{"id": "1", "content": "good", "status": "pending"}]),
            {"role": "tool", "content": '{"todos": broken json'},
        ]
        # The malformed message is skipped; we fall back to the earlier valid one.
        state = derive_todo_state(msgs)
        assert state is not None
        assert state["todos"][0]["content"] == "good"

    def test_non_string_content_skipped(self):
        # Multimodal tool results carry ``content`` as a list of OpenAI/
        # Anthropic content parts.  The ``todo`` tool always returns a JSON
        # string, so list-shaped content is never a todo write — skipping
        # is correct behavior, not a bug.
        msgs = [
            {"role": "tool", "content": None},
            {"role": "tool", "content": [
                {"type": "text", "text": '{"todos": [...] }'},
                {"type": "image_url", "image_url": {"url": "data:..."}},
            ]},
        ]
        assert derive_todo_state(msgs) is None

    def test_non_dict_message_skipped(self):
        msgs = [
            "not a message",
            None,
            _todo_msg([{"id": "1", "content": "x", "status": "pending"}]),
        ]
        state = derive_todo_state(msgs)
        assert state is not None
        assert state["todos"][0]["id"] == "1"

    def test_fast_path_skips_non_todo_payloads(self):
        # The '"todos"' substring guard avoids json.loads on unrelated payloads.
        # Verified indirectly: a tool message without "todos" in its content
        # must not be consulted even if json.loads would have succeeded.
        msgs = [
            {"role": "tool", "content": '{"unrelated": "value"}'},
        ]
        assert derive_todo_state(msgs) is None

    def test_substring_match_passes_fast_path_but_normalize_rejects(self):
        # The fast-path guard is intentionally loose for cheap filtering.
        # An unrelated tool result that mentions ``"todos"`` in a string
        # field passes the substring gate but ``_normalize_snapshot`` then
        # rejects it because the top-level ``todos`` key is not a list.
        # This pins the layered defense.
        msgs = [
            {"role": "tool", "content": json.dumps({
                "result": "wrote a file that mentions \"todos\" inside",
                "todos": "ignore this — string, not list",
            })},
        ]
        assert derive_todo_state(msgs) is None

    def test_messages_iterator_is_supported(self):
        # Callers may pass any iterable; the helper materializes once.
        def gen():
            yield {"role": "user", "content": "hi"}
            yield _todo_msg([{"id": "1", "content": "task", "status": "pending"}])
        state = derive_todo_state(gen())
        assert state is not None
        assert state["todos"][0]["content"] == "task"

    def test_tuple_input_supported_without_extra_copy(self):
        # ``reversed`` works on tuples natively; pin support so the optimized
        # branch (no shallow copy) stays exercised.
        msgs = (
            {"role": "user", "content": "go"},
            _todo_msg([{"id": "1", "content": "t", "status": "pending"}]),
        )
        state = derive_todo_state(msgs)
        assert state is not None
        assert state["todos"][0]["id"] == "1"

    def test_skips_tool_message_missing_role(self):
        msgs = [
            {"content": _todo_payload([
                {"id": "1", "content": "x", "status": "pending"},
            ])},
            {"role": "tool", "content": _todo_payload([
                {"id": "2", "content": "y", "status": "pending"},
            ])},
        ]
        state = derive_todo_state(msgs)
        # The role-less entry is skipped; the second one is returned.
        assert state is not None
        assert state["todos"][0]["id"] == "2"

    def test_returns_first_valid_when_latest_is_malformed(self):
        # Realistic ordering: assistant call → tool result. If the latest
        # tool message is corrupt (truncated by a transport, partial write),
        # we still surface the prior valid snapshot rather than wiping the
        # panel.  Mirrors the agent's _hydrate_todo_store fallback.
        msgs = [
            _todo_msg([{"id": "1", "content": "good", "status": "pending"}]),
            _todo_msg([{"id": "2", "content": "newer good", "status": "in_progress"}]),
            {"role": "tool", "content": '{"todos": ['},  # truncated
        ]
        state = derive_todo_state(msgs)
        assert state is not None
        assert state["todos"][0]["id"] == "2"

    def test_does_not_mutate_caller_list(self):
        msgs = [_todo_msg([{"id": "1", "content": "x", "status": "pending"}])]
        original = list(msgs)
        derive_todo_state(msgs)
        # Length and identity of the original entries unchanged.
        assert msgs == original

    def test_unicode_content_preserved_in_derive(self):
        msgs = [_todo_msg([
            {"id": "1", "content": "中文任务", "status": "in_progress"},
        ])]
        state = derive_todo_state(msgs)
        assert state is not None
        assert state["todos"][0]["content"] == "中文任务"

    def test_large_history_with_late_todo_is_o1_in_practice(self):
        # 5000-msg history; a single todo write near the end. Reverse
        # iteration must short-circuit on the first hit, so this test is
        # both a correctness pin and a perf canary.  We do not assert wall
        # time — just that we get the right snapshot from a large input
        # with non-trivial content lengths.
        big_filler = {"role": "user", "content": "x" * 100}
        msgs = [big_filler] * 5000
        msgs.append(_todo_msg([
            {"id": "late", "content": "ship", "status": "in_progress"},
        ]))
        state = derive_todo_state(msgs)
        assert state is not None
        assert state["todos"][0]["id"] == "late"

    def test_large_history_with_only_early_todo(self):
        # Worst case: todo at the very start, 5000 unrelated tool messages
        # after. Each non-match still has to go through the substring guard,
        # but the function must still return the correct snapshot.
        msgs = [_todo_msg([
            {"id": "early", "content": "first", "status": "completed"},
        ])]
        msgs += [
            {"role": "tool", "content": '{"output":"%d"}' % i}
            for i in range(5000)
        ]
        state = derive_todo_state(msgs)
        assert state is not None
        assert state["todos"][0]["id"] == "early"


# ---------------------------------------------------------------------------
# Module-level constants
# ---------------------------------------------------------------------------


class TestModuleConstants:
    def test_event_name_and_payload_key_exposed(self):
        # Frontend reads a SSE event named ``todo_state`` and a session GET
        # payload key ``todo_state``.  Both are pinned via module constants
        # so a rename is one-line and grep-able.
        assert EVENT_NAME == "todo_state"
        assert PAYLOAD_KEY == "todo_state"

    def test_version_is_stable_across_helpers(self):
        # Cold-load and live-emit must surface the same VERSION so the
        # frontend can use a single decoder.
        raw = _todo_payload([{"id": "1", "content": "x", "status": "pending"}])
        live = parse_todo_tool_result(raw)
        cold = derive_todo_state([{"role": "tool", "content": raw}])
        assert live is not None and cold is not None
        assert live["version"] == cold["version"] == VERSION
