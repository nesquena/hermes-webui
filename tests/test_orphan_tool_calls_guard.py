"""Regression tests for _strip_orphan_tool_calls (TOOL_USE_RESULT_MISMATCH 400).

An assistant message can end up carrying tool_call ids that have no tool_result
immediately after. Strict upstreams (Bedrock) then reject every request in that
session with HTTP 400 TOOL_USE_RESULT_MISMATCH, and because each failure
appends another poisoned row the session never recovers on its own.
"""
import json

import pytest

from api.streaming import _strip_orphan_tool_calls


def _call(cid):
    return {"id": cid, "type": "function", "function": {"name": "t", "arguments": "{}"}}


def _assistant(cids, content=""):
    return {"role": "assistant", "content": content, "tool_calls": [_call(c) for c in cids]}


def _tool(cid, content="ok"):
    return {"role": "tool", "tool_call_id": cid, "content": content}


class TestPairedCallsSurvive:
    def test_single_paired_call_untouched(self):
        msgs = [_assistant(["a1"]), _tool("a1")]
        before = json.dumps(msgs)
        _strip_orphan_tool_calls(msgs)
        assert json.dumps(msgs) == before

    def test_multiple_paired_calls_in_one_block(self):
        msgs = [_assistant(["a1", "a2", "a3"]), _tool("a1"), _tool("a2"), _tool("a3")]
        _strip_orphan_tool_calls(msgs)
        assert [c["id"] for c in msgs[0]["tool_calls"]] == ["a1", "a2", "a3"]

    def test_consecutive_turns_stay_intact(self):
        msgs = [
            _assistant(["a1"]), _tool("a1"),
            {"role": "assistant", "content": "mid"},
            _assistant(["b1", "b2"]), _tool("b1"), _tool("b2"),
        ]
        before = json.dumps(msgs)
        _strip_orphan_tool_calls(msgs)
        assert json.dumps(msgs) == before


class TestOrphansRemoved:
    def test_text_message_with_fully_orphan_calls_loses_field(self):
        """A final text answer left carrying tool_call ids from earlier turns."""
        poison = [f"toolu_bdrk_{i}" for i in range(6)]
        msgs = [_assistant(poison, content="summary text"), {"role": "user", "content": "next"}]
        _strip_orphan_tool_calls(msgs)
        assert "tool_calls" not in msgs[0]
        assert msgs[0]["content"] == "summary text", "content must be preserved"

    def test_partial_orphans_keep_only_paired(self):
        msgs = [_assistant(["good", "orphan"]), _tool("good")]
        _strip_orphan_tool_calls(msgs)
        assert [c["id"] for c in msgs[0]["tool_calls"]] == ["good"]

    def test_duplicate_assistant_between_call_and_result(self):
        """A duplicate row splits an otherwise valid pair apart.

        Upstream requires the tool_result *immediately* after, so the first
        assistant's call is an orphan even though a tool row exists later.
        """
        msgs = [_assistant(["x1"]), _assistant(["x1"], content="dupe"), _tool("x1")]
        _strip_orphan_tool_calls(msgs)
        assert "tool_calls" not in msgs[0], "row separated from its result is an orphan"
        assert [c["id"] for c in msgs[1]["tool_calls"]] == ["x1"], "adjacent row keeps its call"

    def test_tool_result_after_user_turn_does_not_count(self):
        msgs = [_assistant(["a1"]), {"role": "user", "content": "hi"}, _tool("a1")]
        _strip_orphan_tool_calls(msgs)
        assert "tool_calls" not in msgs[0]

    def test_trailing_assistant_call_is_exempt(self):
        """The LAST row is an in-flight turn: its result just hasn't landed yet.

        Seen when a live turn is cut short by a WebUI restart. Only a
        row followed by something else can be structurally orphaned.
        """
        msgs = [{"role": "user", "content": "go"}, _assistant(["pending"])]
        _strip_orphan_tool_calls(msgs)
        assert [c["id"] for c in msgs[1]["tool_calls"]] == ["pending"]

    def test_orphan_before_trailing_row_still_stripped(self):
        """Tail exemption must not shield rows earlier in the array."""
        msgs = [_assistant(["ghost"], content="text"), _assistant(["pending"])]
        _strip_orphan_tool_calls(msgs)
        assert "tool_calls" not in msgs[0], "non-trailing orphan must go"
        assert [c["id"] for c in msgs[1]["tool_calls"]] == ["pending"], "tail stays"

    def test_json_string_tool_calls_are_parsed(self):
        msgs = [
            {"role": "assistant", "content": "x", "tool_calls": json.dumps([_call("s1")])},
            {"role": "user", "content": "next"},
        ]
        _strip_orphan_tool_calls(msgs)
        assert "tool_calls" not in msgs[0]


class TestIdempotenceAndSafety:
    def test_running_twice_is_stable(self):
        msgs = [_assistant(["a1", "orphan"]), _tool("a1")]
        _strip_orphan_tool_calls(msgs)
        once = json.dumps(msgs)
        _strip_orphan_tool_calls(msgs)
        assert json.dumps(msgs) == once

    def test_repaired_output_passes_upstream_contract(self):
        """Post-repair invariant: no assistant call id lacks an adjacent result."""
        msgs = [
            _assistant(["a1", "ghost"]), _tool("a1"),
            _assistant(["b1"], content="text"),
            {"role": "user", "content": "u"},
            _assistant(["c1"]), _tool("c1"),
        ]
        _strip_orphan_tool_calls(msgs)
        for i, m in enumerate(msgs):
            calls = m.get("tool_calls") or []
            covered, j = set(), i + 1
            while j < len(msgs) and msgs[j].get("role") == "tool":
                covered.add(msgs[j].get("tool_call_id"))
                j += 1
            unpaired = {c["id"] for c in calls} - covered
            assert not unpaired, f"index {i} still orphaned: {unpaired}"

    @pytest.mark.parametrize("bad", [None, [], "not-a-list", 42])
    def test_malformed_input_does_not_raise(self, bad):
        assert _strip_orphan_tool_calls(bad) is bad or _strip_orphan_tool_calls(bad) == bad

    def test_non_dict_rows_are_skipped(self):
        msgs = ["junk", None, _assistant(["a1"]), _tool("a1")]
        _strip_orphan_tool_calls(msgs)
        assert [c["id"] for c in msgs[2]["tool_calls"]] == ["a1"]

    def test_unparseable_tool_calls_string_left_alone(self):
        msgs = [{"role": "assistant", "content": "x", "tool_calls": "{{{not json"}]
        _strip_orphan_tool_calls(msgs)
        assert msgs[0]["tool_calls"] == "{{{not json"

    def test_empty_tool_calls_list_untouched(self):
        msgs = [{"role": "assistant", "content": "x", "tool_calls": []}]
        _strip_orphan_tool_calls(msgs)
        assert msgs[0]["tool_calls"] == []
