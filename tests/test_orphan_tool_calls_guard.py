"""Regression tests for _strip_orphan_tool_calls (TOOL_USE_RESULT_MISMATCH 400).

An assistant message can end up carrying tool_call ids that have no tool_result
immediately after. Strict upstreams (Bedrock) then reject every request in that
session with HTTP 400 TOOL_USE_RESULT_MISMATCH, and because each failure
appends another poisoned row the session never recovers on its own.
"""
import json
from types import SimpleNamespace

import pytest

from api.streaming import (
    _sanitize_messages_for_api,
    _settle_result_messages,
    _strip_orphan_tool_calls,
)


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
    def test_repair_returns_new_rows_without_mutating_input_rows(self):
        """Context repair must not delete metadata from the display transcript."""
        msgs = [_assistant(["orphan"], content="visible answer"), {"role": "user", "content": "next"}]
        before = json.dumps(msgs, sort_keys=True, ensure_ascii=False, separators=(",", ":"))

        repaired = _strip_orphan_tool_calls(msgs)

        assert repaired is not msgs
        assert json.dumps(msgs, sort_keys=True, ensure_ascii=False, separators=(",", ":")) == before
        assert "tool_calls" not in repaired[0]
        assert repaired[0] is not msgs[0], "changed rows must be cloned"
        assert repaired[1] is msgs[1], "unchanged rows need not be cloned"

    def test_repaired_nested_metadata_is_not_aliased(self):
        """Copy-on-write must also sever nested tool-call dictionaries."""
        msgs = [_assistant(["good", "orphan"]), _tool("good")]
        repaired = _strip_orphan_tool_calls(msgs)

        repaired[0]["tool_calls"][0]["function"]["name"] = "changed-in-context"

        assert msgs[0]["tool_calls"][0]["function"]["name"] == "t"

    def test_text_message_with_fully_orphan_calls_loses_field(self):
        """A final text answer left carrying foreign ids is repaired in the output."""
        poison = [f"toolu_bdrk_{i}" for i in range(6)]
        msgs = [_assistant(poison, content="summary text"), {"role": "user", "content": "next"}]
        repaired = _strip_orphan_tool_calls(msgs)
        assert "tool_calls" not in repaired[0]
        assert repaired[0]["content"] == "summary text", "content must be preserved"
        assert [call["id"] for call in msgs[0]["tool_calls"]] == poison

    def test_partial_orphans_keep_only_paired(self):
        msgs = [_assistant(["good", "orphan"]), _tool("good")]
        repaired = _strip_orphan_tool_calls(msgs)
        assert [c["id"] for c in repaired[0]["tool_calls"]] == ["good"]
        assert [c["id"] for c in msgs[0]["tool_calls"]] == ["good", "orphan"]

    def test_duplicate_assistant_between_call_and_result(self):
        """A duplicate row splits an otherwise valid pair apart.

        Upstream requires the tool_result *immediately* after, so the first
        assistant's call is an orphan even though a tool row exists later.
        """
        msgs = [_assistant(["x1"]), _assistant(["x1"], content="dupe"), _tool("x1")]
        repaired = _strip_orphan_tool_calls(msgs)
        assert "tool_calls" not in repaired[0], "row separated from its result is an orphan"
        assert [c["id"] for c in repaired[1]["tool_calls"]] == ["x1"], "adjacent row keeps its call"
        assert [c["id"] for c in msgs[0]["tool_calls"]] == ["x1"]

    def test_tool_result_after_user_turn_does_not_count(self):
        msgs = [_assistant(["a1"]), {"role": "user", "content": "hi"}, _tool("a1")]
        repaired = _strip_orphan_tool_calls(msgs)
        assert "tool_calls" not in repaired[0]
        assert [c["id"] for c in msgs[0]["tool_calls"]] == ["a1"]

    def test_trailing_settled_assistant_call_is_repaired(self):
        """A completed-writeback helper cannot infer in-flight state from position."""
        msgs = [{"role": "user", "content": "go"}, _assistant(["orphan"])]
        repaired = _strip_orphan_tool_calls(msgs)
        assert "tool_calls" not in repaired[1]
        assert [c["id"] for c in msgs[1]["tool_calls"]] == ["orphan"]

    def test_trailing_settled_assistant_call_cannot_reach_next_provider_request(self):
        """An orphan is repaired outbound without rewriting persisted context."""
        previous = [{"role": "user", "content": "go", "timestamp": 1.0}]
        result = previous + [_assistant(["orphan"], content="final text")]
        session = SimpleNamespace(
            messages=list(previous),
            context_messages=list(previous),
            truncation_watermark=None,
        )

        _settle_result_messages(
            session,
            previous,
            previous,
            result,
            "go",
            "webui",
            None,
        )

        assert "tool_calls" in result[-1]
        assert "tool_calls" in session.context_messages[-1]
        next_request = _sanitize_messages_for_api(session.context_messages)
        assert all(not msg.get("tool_calls") for msg in next_request)
        assert [call["id"] for call in result[-1]["tool_calls"]] == ["orphan"]

    def test_anthropic_call_id_pair_survives_outbound_repair(self):
        msgs = [
            {"role": "assistant", "content": "", "tool_calls": [
                {"call_id": "anthropic-1", "type": "function", "function": {"name": "t", "arguments": "{}"}}
            ]},
            {"role": "tool", "tool_call_id": "anthropic-1", "content": "ok"},
        ]
        repaired = _sanitize_messages_for_api(msgs)
        assert repaired[0]["tool_calls"][0]["call_id"] == "anthropic-1"

    def test_non_adjacent_result_cannot_rescue_earlier_orphan(self):
        msgs = [
            {"role": "assistant", "content": "", "tool_calls": [_call("foreign")]},
            {"role": "assistant", "content": "intervening"},
            {"role": "tool", "tool_call_id": "foreign", "content": "late result"},
        ]
        repaired = _sanitize_messages_for_api(msgs)
        assert repaired == [{"role": "assistant", "content": "intervening"}]

    def test_orphan_before_trailing_row_still_stripped(self):
        msgs = [_assistant(["ghost"], content="text"), _assistant(["second-ghost"])]
        repaired = _strip_orphan_tool_calls(msgs)
        assert "tool_calls" not in repaired[0]
        assert "tool_calls" not in repaired[1]

    def test_json_string_tool_calls_are_parsed(self):
        msgs = [
            {"role": "assistant", "content": "x", "tool_calls": json.dumps([_call("s1")])},
            {"role": "user", "content": "next"},
        ]
        repaired = _strip_orphan_tool_calls(msgs)
        assert "tool_calls" not in repaired[0]
        assert isinstance(msgs[0]["tool_calls"], str)


class TestIdempotenceAndSafety:
    def test_running_twice_is_stable(self):
        msgs = [_assistant(["a1", "orphan"]), _tool("a1")]
        once = _strip_orphan_tool_calls(msgs)
        twice = _strip_orphan_tool_calls(once)
        assert json.dumps(twice) == json.dumps(once)

    def test_repaired_output_passes_upstream_contract(self):
        """Post-repair invariant: no assistant call id lacks an adjacent result."""
        msgs = [
            _assistant(["a1", "ghost"]), _tool("a1"),
            _assistant(["b1"], content="text"),
            {"role": "user", "content": "u"},
            _assistant(["c1"]), _tool("c1"),
        ]
        repaired = _strip_orphan_tool_calls(msgs)
        for i, m in enumerate(repaired):
            calls = m.get("tool_calls") or []
            covered, j = set(), i + 1
            while j < len(repaired) and repaired[j].get("role") == "tool":
                covered.add(repaired[j].get("tool_call_id"))
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


class TestRecoveredUserTransparency:
    """#1543 stale-stream recovery inserts a transient ``_recovered`` user row
    between an assistant's ``tool_calls`` and the first ``tool`` result. A
    later pass in ``_sanitize_messages_for_api`` / ``_api_safe_message_positions``
    strips that row, so the adjacency scan must treat it as transparent —
    otherwise a valid, completed tool pair is misclassified as an orphan and
    silently lost on the exact recovery path this PR family hardens.
    """

    def test_recovered_user_before_first_tool_result_does_not_split_pair(self):
        msgs = [
            _assistant(["a", "b"]),
            {"role": "user", "content": "stale", "_recovered": True},
            _tool("a"),
            _tool("b"),
            {"role": "assistant", "content": "done"},
        ]
        before = json.dumps(msgs)
        repaired = _strip_orphan_tool_calls(msgs)
        assert [c["id"] for c in repaired[0]["tool_calls"]] == ["a", "b"]
        assert json.dumps(msgs) == before, "input rows must not be mutated"

    def test_recovered_user_between_two_tool_results_keeps_both_calls(self):
        msgs = [
            _assistant(["a", "b"]),
            _tool("a"),
            {"role": "user", "content": "stale", "_recovered": True},
            _tool("b"),
            {"role": "assistant", "content": "done"},
        ]
        repaired = _strip_orphan_tool_calls(msgs)
        assert [c["id"] for c in repaired[0]["tool_calls"]] == ["a", "b"]

    def test_recovered_user_without_following_tool_still_orphans_the_call(self):
        """Transparency must not rescue a call that genuinely has no result."""
        msgs = [
            _assistant(["a", "ghost"]),
            _tool("a"),
            {"role": "user", "content": "stale", "_recovered": True},
            # no tool("ghost") follows → ghost is still a real orphan
            {"role": "user", "content": "next turn"},
        ]
        repaired = _strip_orphan_tool_calls(msgs)
        assert [c["id"] for c in repaired[0]["tool_calls"]] == ["a"]

    def test_sanitize_for_api_keeps_pair_split_by_recovered_user(self):
        """End-to-end: the sanitizer path must not silently drop a valid pair."""
        msgs = [
            _assistant(["a", "b"]),
            _tool("a"),
            {"role": "user", "content": "stale", "_recovered": True},
            _tool("b"),
            {"role": "assistant", "content": "done"},
        ]
        sanitized = _sanitize_messages_for_api(msgs)
        # First surviving row is the assistant carrying the full pair.
        assert sanitized[0]["role"] == "assistant"
        assert [c["id"] for c in sanitized[0]["tool_calls"]] == ["a", "b"]

    def test_api_safe_message_positions_keeps_pair_split_by_recovered_user(self):
        """_api_safe_message_positions delegates to _strip_orphan_tool_calls;
        the same transparency rule must apply on this projection path.
        """
        from api.streaming import _api_safe_message_positions
        msgs = [
            _assistant(["a", "b"]),
            _tool("a"),
            {"role": "user", "content": "stale", "_recovered": True},
            _tool("b"),
            {"role": "assistant", "content": "done"},
        ]
        out = _api_safe_message_positions(msgs)
        # First surviving row is the assistant carrying the full pair, and
        # both tool results survive the projection.
        assert out[0][1]["role"] == "assistant"
        assert [c["id"] for c in out[0][1]["tool_calls"]] == ["a", "b"]
        surviving_tool_ids = [
            row[1].get("tool_call_id") for row in out if row[1].get("role") == "tool"
        ]
        assert surviving_tool_ids == ["a", "b"]
