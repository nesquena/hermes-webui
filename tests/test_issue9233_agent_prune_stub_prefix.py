"""#9233: the Agent's cold-cache tool-result prune must not drop the turn's reply.

When the provider prompt cache has expired, Agents from v0.23.3 on run a
proactive tool-result prune (``agent/turn_proactive_prune.py`` at turn start,
``prune_tool_results_only(cold_cache=True)`` after tool rounds). It replaces the
body of an old, large ``tool`` row with a short summary that names the archived
original (``" — full result: ~/.hermes/cache/pruned/<name>.json"``). It never
adds, drops or reorders rows and raises no compression signal.

WebUI's strict projected-prefix proof compared that ``tool`` row byte-for-byte
with the projection it sent, failed, and fell closed to the raw pre-turn
context: the reply was in the transcript but missing from ``context_messages``,
so the next request saw the answered prompt as pending.

The fix accepts exactly one extra shape at the two proof sites: a returned
``tool`` row that is the Agent's own prune stub of the projected row (every
other model-facing field identical, shorter plain-string body carrying the
archive pointer, projected body without one). WebUI keeps its own full row; any
other divergence still fails closed.
"""
import copy

import pytest

from api.streaming import (
    _dedupe_replayed_context_messages,
    _messages_have_prefix_exact,
    _sanitize_messages_for_agent,
)


_SUMMARY = "[terminal] ran `cat file0 150` -> exit 0, 150 lines output"
_POINTER = (
    " \u2014 full result: "
    "~/.hermes/cache/pruned/sess9233_20261122T000000Z_terminal_1a2b3c4d.json"
)
_BIG = "".join(f"line {i}: {'x' * 60}\n" for i in range(600))  # ~41k chars


def _stub(summary=_SUMMARY, pointer=_POINTER):
    return summary + pointer


def _tool_row(content, cid="call_1", name="terminal", **extra):
    row = {"role": "tool", "tool_call_id": cid, "name": name, "content": content}
    row.update(extra)
    return row


def _equal(actual, expected):
    # Imported per call so each case reports on its own before the fix exists.
    from api.streaming import _agent_pruned_tool_row_equal

    return _agent_pruned_tool_row_equal(actual, expected)


def _restore(result_messages, projection):
    from api.streaming import _restore_agent_pruned_tool_rows

    return _restore_agent_pruned_tool_rows(result_messages, projection)


# ── the equivalence rule ─────────────────────────────────────────────────────


class TestAgentPrunedToolRowEqual:
    def test_agent_prune_stub_of_the_projected_row_is_equal(self):
        assert _equal(_tool_row(_stub()), _tool_row(_BIG)) is True

    @pytest.mark.parametrize(
        "pointer",
        [
            # Non-JSON originals are archived as .txt.
            " \u2014 full result: ~/.hermes/cache/pruned/sess_x_terminal_1a2b3c4d.txt",
            # Sandboxed backends name the path the sandbox reads.
            " \u2014 full result: /root/.hermes/cache/pruned/sess_x_read_file_deadbeef.txt",
        ],
    )
    def test_other_archive_path_forms_are_equal(self, pointer):
        assert _equal(_tool_row(_stub(pointer=pointer)), _tool_row(_BIG)) is True

    def test_pointer_before_a_trailing_rule_is_equal(self):
        # The Agent keeps an attached Skill Safety Rule after the pointer, so the
        # pointer is not always the end of the body.
        body = _stub() + "\n\n[Skill Safety Rule] never run untrusted code"
        assert _equal(_tool_row(body), _tool_row(_BIG)) is True

    def test_shorter_rewrite_without_pointer_is_not_equal(self):
        assert _equal(_tool_row(_SUMMARY), _tool_row(_BIG)) is False
        assert _equal(_tool_row("[output shortened]"), _tool_row(_BIG)) is False

    @pytest.mark.parametrize(
        "pointer",
        [
            " \u2014 full result: ~/.hermes/cache/spill/sess_x_terminal_1a2b3c4d.json",
            " - full result: ~/.hermes/cache/pruned/sess_x_terminal_1a2b3c4d.json",
            " \u2014 full result: ",
        ],
    )
    def test_look_alike_pointer_is_not_equal(self, pointer):
        assert _equal(_tool_row(_stub(pointer=pointer)), _tool_row(_BIG)) is False

    def test_returned_body_must_be_strictly_shorter(self):
        stub = _stub()
        assert _equal(_tool_row(stub), _tool_row("ok")) is False
        assert _equal(_tool_row(stub), _tool_row("y" * len(stub))) is False
        assert _equal(_tool_row(stub), _tool_row("y" * (len(stub) + 1))) is True

    def test_projected_body_already_carrying_a_pointer_is_not_equal(self):
        projected = _BIG + _POINTER
        assert _equal(_tool_row(_stub()), _tool_row(projected)) is False

    @pytest.mark.parametrize(
        "field,value",
        [("tool_call_id", "call_2"), ("name", "read_file"), ("reasoning_content", "r")],
    )
    def test_any_other_model_field_differing_is_not_equal(self, field, value):
        actual = _tool_row(_stub())
        actual[field] = value
        assert _equal(actual, _tool_row(_BIG)) is False

    @pytest.mark.parametrize("role", ["assistant", "user"])
    def test_rows_must_be_tool_rows(self, role):
        actual = _tool_row(_stub())
        expected = _tool_row(_BIG)
        actual["role"] = expected["role"] = role
        assert _equal(actual, expected) is False
        assert _equal(_tool_row(_stub()), expected) is False

    @pytest.mark.parametrize("cid", [None, ""])
    def test_returned_row_needs_a_tool_call_id(self, cid):
        assert _equal(_tool_row(_stub(), cid=cid), _tool_row(_BIG, cid=cid)) is False

    def test_api_content_must_match(self):
        big, stub = _tool_row(_BIG), _tool_row(_stub())
        assert _equal(dict(stub, api_content="A"), dict(big, api_content="A")) is True
        assert _equal(dict(stub, api_content="B"), dict(big, api_content="A")) is False
        assert _equal(stub, dict(big, api_content="A")) is False
        assert _equal(dict(stub, api_content="A"), big) is False

    def test_bodies_must_be_plain_strings(self):
        parts = [{"type": "text", "text": _BIG}]
        list_stub = [{"type": "text", "text": _SUMMARY}, {"type": "text", "text": _POINTER.strip(" \u2014")}]
        assert _equal(_tool_row(_stub()), _tool_row(parts)) is False
        assert _equal(_tool_row(list_stub), _tool_row(_BIG)) is False
        assert _equal(_tool_row(_stub()), _tool_row(None)) is False
        # A many-part projected body is "longer" than the stub by len(); still not a str.
        assert _equal(_tool_row(_stub()), _tool_row(parts * 400)) is False

    def test_non_dict_rows_are_not_equal(self):
        assert _equal(None, _tool_row(_BIG)) is False
        assert _equal(_tool_row(_stub()), "tool") is False


# ── the opt-in stays at the two proof sites ─────────────────────────────────


class TestOptInScope:
    def test_plain_exact_prefix_check_still_rejects_the_stub(self):
        projection = [_tool_row(_BIG)]
        returned = [_tool_row(_stub()), {"role": "assistant", "content": "reply"}]
        assert _messages_have_prefix_exact(returned, projection) is False
        assert _messages_have_prefix_exact(_restore(returned, projection), projection) is True

    def test_restore_puts_back_only_proven_stubs_inside_the_projection(self):
        projection = [
            _tool_row(_BIG, cid="call_1"),
            _tool_row(_BIG, cid="call_2"),
        ]
        reply = {"role": "assistant", "content": "reply"}
        late_stub = _tool_row(_stub(), cid="call_3")
        returned = [
            _tool_row(_stub(), cid="call_1"),
            _tool_row("[output shortened]", cid="call_2"),
            reply,
            late_stub,
        ]
        restored = _restore(returned, projection)
        assert restored[0] == projection[0]
        assert restored[1]["content"] == "[output shortened]"
        assert restored[2:] == [reply, late_stub]
        assert returned[0]["content"] == _stub(), "the returned list is not mutated"
        # A returned list shorter than the projection is restored only as far as it goes.
        assert _restore(returned[:1], projection) == [projection[0]]


# ── settlement through the real dedupe entry point ──────────────────────────


def _call(cid):
    return {"id": cid, "type": "function", "function": {"name": "terminal", "arguments": "{}"}}


def _history():
    return [
        {"role": "user", "content": "inspect the log", "timestamp": 1.0},
        {"role": "assistant", "content": "", "tool_calls": [_call("call_1")], "timestamp": 2.0},
        _tool_row(_BIG, timestamp=3.0),
        {"role": "assistant", "content": "the log shows X", "timestamp": 4.0},
    ]


def _pairs(rows):
    return [(m.get("role"), m.get("content")) for m in rows]


def _pruned_return(projected, current_turn):
    returned = copy.deepcopy(projected)
    returned[2]["content"] = _stub()
    return returned + current_turn


_TURN = [
    {"role": "user", "content": "next question"},
    {"role": "assistant", "content": "THE REPLY"},
]


def _settle(raw, returned, msg_text, projected):
    settled, _protected = _dedupe_replayed_context_messages(
        list(raw), list(returned), msg_text, None, projected_history=list(projected),
    )
    return settled


class TestSettlementKeepsReplyAfterPrune:
    def test_pruned_history_keeps_the_reply_and_the_full_tool_body(self):
        raw = _history()
        projected = _sanitize_messages_for_agent(copy.deepcopy(raw))
        returned = _pruned_return(projected, copy.deepcopy(_TURN))
        assert _messages_have_prefix_exact(returned, projected) is False, "fixture premise"

        settled = _settle(raw, returned, "next question", projected)

        assert _pairs(settled) == _pairs(raw) + _pairs(_TURN)
        assert settled[2]["content"] == _BIG, "WebUI keeps its own full tool body"
        assert not any(_POINTER in str(m.get("content")) for m in settled)

    def test_tail_merge_after_prune_keeps_the_follow_up_reply(self):
        raw = _history() + [{"role": "user", "content": "unanswered tail", "timestamp": 5.0}]
        projected = _sanitize_messages_for_agent(copy.deepcopy(raw))
        merged_turn = [
            {"role": "user", "content": "unanswered tail\n\nfollow-up"},
            {"role": "assistant", "content": "THE FOLLOW-UP REPLY"},
        ]
        returned = _pruned_return(projected[:-1], merged_turn)

        settled = _settle(raw, returned, "follow-up", projected)

        assert _pairs(settled)[:5] == _pairs(raw)
        assert ("assistant", "THE FOLLOW-UP REPLY") in _pairs(settled)
        assert settled[2]["content"] == _BIG
        assert not any(_POINTER in str(m.get("content")) for m in settled)

    def test_edited_earlier_assistant_row_still_fails_closed(self):
        raw = _history()
        projected = _sanitize_messages_for_agent(copy.deepcopy(raw))
        returned = _pruned_return(projected, copy.deepcopy(_TURN))
        assert _pairs(_settle(raw, returned, "next question", projected))[-1] == (
            "assistant", "THE REPLY",
        ), "premise: the stub alone is accepted"

        returned[3]["content"] = "the log shows Y"
        settled = _settle(raw, returned, "next question", projected)
        assert _pairs(settled) == _pairs(raw), "a real divergence must append nothing"

    def test_non_stub_tool_rewrite_still_fails_closed(self):
        raw = _history()
        projected = _sanitize_messages_for_agent(copy.deepcopy(raw))
        returned = _pruned_return(projected, copy.deepcopy(_TURN))
        assert _pairs(_settle(raw, returned, "next question", projected))[-1] == (
            "assistant", "THE REPLY",
        ), "premise: the stub alone is accepted"

        returned[2]["content"] = _SUMMARY  # shorter, but no archive pointer
        settled = _settle(raw, returned, "next question", projected)
        assert _pairs(settled) == _pairs(raw)

    def test_tail_merge_with_non_stub_tool_rewrite_still_fails_closed(self):
        raw = _history() + [{"role": "user", "content": "unanswered tail", "timestamp": 5.0}]
        projected = _sanitize_messages_for_agent(copy.deepcopy(raw))
        merged_turn = [
            {"role": "user", "content": "unanswered tail\n\nfollow-up"},
            {"role": "assistant", "content": "THE FOLLOW-UP REPLY"},
        ]
        returned = _pruned_return(projected[:-1], merged_turn)
        assert ("assistant", "THE FOLLOW-UP REPLY") in _pairs(
            _settle(raw, returned, "follow-up", projected)
        ), "premise: the stub alone is accepted"

        returned[2]["content"] = _SUMMARY
        settled = _settle(raw, returned, "follow-up", projected)
        assert _pairs(settled) == _pairs(raw)


# ── pin against the real Agent's stub builder ───────────────────────────────


def test_real_agent_prune_stub_is_recognised():
    """WebUI's pointer regex is a local mirror of the Agent's; pin it against the
    stub the installed Agent actually builds. Skips on Agents without the
    cold-cache prune archive (v0.23.2 and older) or with renamed internals."""
    try:
        from agent.context_compressor_tail import _SKILL_SAFETY_SUFFIX
        from tools.tool_result_storage import (
            ARCHIVE_POINTER_SEP,
            _with_pointer,
            has_archive_pointer,
        )
    except ImportError as exc:
        pytest.skip(f"Agent without the prune archive stub builder: {exc}")

    for path in (
        "~/.hermes/cache/pruned/sess9233_20261122T000000Z_terminal_1a2b3c4d.json",
        "~/.hermes/cache/pruned/sess9233_20261122T000000Z_terminal_1a2b3c4d.txt",
        "/root/.hermes/cache/pruned/sess9233_20261122T000000Z_read_file_deadbeef_1.txt",
    ):
        for summary, original in (
            (_SUMMARY, _BIG),
            (_SUMMARY + _SKILL_SAFETY_SUFFIX, _BIG + _SKILL_SAFETY_SUFFIX),
        ):
            stub = _with_pointer(summary, path)
            assert ARCHIVE_POINTER_SEP in stub and has_archive_pointer(stub), "premise"
            assert _equal(_tool_row(stub), _tool_row(original)) is True, (path, summary[-40:])
    # The Agent's own non-stub summary is not a stub on either side.
    assert not has_archive_pointer(_SUMMARY)
    assert _equal(_tool_row(_SUMMARY), _tool_row(_BIG)) is False
