"""#8038 — the rest of what the legacy sanitizer drops, on the Gateway runs path.

#8035 gave the Gateway ``conversation_history`` the legacy path's filter for
error markers and empty partials. Two more kinds of row still reached the
Gateway, and one call site still spelled that filter out by hand:

1. a reasoning-only assistant row went out as ``{"role": "assistant",
   "content": ""}``. The Gateway writeback builds exactly that row when a run's
   reply text is empty and reasoning was streamed;
2. a ``_recovered`` user row was always sent. The legacy sanitizer drops it
   unless it separates two assistant turns;
3. ``_api_safe_message_positions`` repeated the two ``_error`` / empty
   ``_partial`` checks inline.

The rows have the shapes the app writes: the writeback's assistant row
(``content`` empty, ``reasoning`` beside it) and the recovered prompt of
``api/streaming.py`` (``_recovered: True`` on a user row).
"""
from __future__ import annotations

import pytest

from tests.test_issue8034_gateway_runs_history_filter import (
    ANSWER,
    ERROR_ROW,
    FOLLOW_UP,
    USER,
    _gateway_history,
    _pairs,
)

# What the Gateway writeback stores for a run that streamed reasoning and no text.
REASONING_ONLY = {
    "role": "assistant",
    "content": "",
    "timestamp": 20,
    "reasoning": "the run ended before any answer text",
}
REASONING_CONTENT_ONLY = {
    "role": "assistant",
    "content": "",
    "timestamp": 21,
    "reasoning_content": "provider-side reasoning, no answer",
}
ANSWER_WITH_REASONING = {
    "role": "assistant",
    "content": "an answer that was thought about",
    "timestamp": 22,
    "reasoning": "some thinking",
}
RECOVERED = {"role": "user", "content": "a prompt that was recovered", "timestamp": 23, "_recovered": True}
SECOND_ANSWER = {"role": "assistant", "content": "second answer", "timestamp": 24}


def _legacy_pairs(rows):
    from api.streaming import _sanitize_messages_for_api

    return [
        (row["role"], row["content"])
        for row in _sanitize_messages_for_api(rows)
        if row.get("role") in {"user", "assistant"}
    ]


@pytest.mark.parametrize(
    "row", [REASONING_ONLY, REASONING_CONTENT_ONLY], ids=["reasoning", "reasoning_content"]
)
def test_a_reasoning_only_row_is_not_sent_to_the_gateway(row):
    history = _gateway_history([USER, row, FOLLOW_UP], tag="reasoning-only")

    assert _pairs(history) == [("user", "first question"), ("user", "second question")]
    assert all(str(entry["content"]).strip() for entry in history)


def test_an_answer_that_carries_reasoning_is_still_sent():
    history = _gateway_history([USER, ANSWER_WITH_REASONING, FOLLOW_UP], tag="answer-reasoning")

    assert _pairs(history) == [
        ("user", "first question"),
        ("assistant", "an answer that was thought about"),
        ("user", "second question"),
    ]


def test_a_recovered_prompt_nobody_answered_is_not_replayed():
    """A user turn follows it: it is a stale prompt."""
    history = _gateway_history([USER, ANSWER, RECOVERED, FOLLOW_UP], tag="recovered-stale")

    assert _pairs(history) == [
        ("user", "first question"),
        ("assistant", "first answer"),
        ("user", "second question"),
    ]


def test_a_recovered_prompt_after_a_user_turn_is_dropped():
    """Keeping it would put two user turns side by side."""
    history = _gateway_history([USER, RECOVERED, SECOND_ANSWER], tag="recovered-after-user")

    assert _pairs(history) == [("user", "first question"), ("assistant", "second answer")]


def test_a_recovered_prompt_between_two_answers_is_kept():
    """Dropping it would fuse two assistant turns."""
    history = _gateway_history([USER, ANSWER, RECOVERED, SECOND_ANSWER], tag="recovered-kept")

    assert _pairs(history) == [
        ("user", "first question"),
        ("assistant", "first answer"),
        ("user", "a prompt that was recovered"),
        ("assistant", "second answer"),
    ]
    assert all(set(entry) == {"role", "content"} for entry in history)


def test_the_neighbours_are_the_rows_that_are_actually_sent():
    """An error row between a user turn and the recovered prompt is not a
    neighbour: it is dropped first, so the prompt sits after a user turn."""
    after_user = _gateway_history([USER, ERROR_ROW, RECOVERED, SECOND_ANSWER], tag="recovered-kept-seq-a")
    after_answer = _gateway_history(
        [USER, ANSWER, REASONING_ONLY, RECOVERED, SECOND_ANSWER], tag="recovered-kept-seq-b"
    )

    assert _pairs(after_user) == [("user", "first question"), ("assistant", "second answer")]
    assert _pairs(after_answer) == [
        ("user", "first question"),
        ("assistant", "first answer"),
        ("user", "a prompt that was recovered"),
        ("assistant", "second answer"),
    ]


def test_the_previous_neighbour_is_the_last_row_kept():
    """Two recovered prompts in a row between two answers: the first is a stale
    prompt (a user turn follows it) and goes; the second then sits between the
    two answers and stays. Judged against the row before it rather than the
    last row kept, the second would go too and the answers would touch."""
    earlier = dict(RECOVERED, content="an earlier recovered prompt", timestamp=19)
    rows = [USER, ANSWER, earlier, RECOVERED, SECOND_ANSWER]

    assert _pairs(_gateway_history(rows, tag="recovered-twice")) == [
        ("user", "first question"),
        ("assistant", "first answer"),
        ("user", "a prompt that was recovered"),
        ("assistant", "second answer"),
    ]
    assert _legacy_pairs(rows) == _pairs(_gateway_history(rows, tag="recovered-twice-parity"))


def test_only_a_user_row_is_a_recovered_prompt():
    """The marker on an assistant row changes nothing, as on the legacy path."""
    marked_answer = dict(ANSWER, _recovered=True)
    rows = [USER, marked_answer, FOLLOW_UP]

    assert _pairs(_gateway_history(rows, tag="recovered-assistant")) == [
        ("user", "first question"),
        ("assistant", "first answer"),
        ("user", "second question"),
    ]
    assert _legacy_pairs(rows) == _pairs(_gateway_history(rows, tag="recovered-assistant-parity"))


def test_a_tool_result_is_not_a_neighbour_on_the_gateway():
    """The Gateway history carries no tool rows, so a recovered prompt after a
    tool result separates two assistant turns of what is sent, and stays. The
    legacy path sends the tool row, has a tool result before the prompt, and
    drops it: the one place the two decide differently, each on its own rows."""
    tool_turn = {
        "role": "assistant",
        "content": "let me look that up",
        "tool_calls": [
            {"id": "call_1", "type": "function", "function": {"name": "terminal", "arguments": "{}"}}
        ],
    }
    tool_result = {"role": "tool", "tool_call_id": "call_1", "content": "ok"}
    rows = [USER, tool_turn, tool_result, RECOVERED, SECOND_ANSWER]

    assert _pairs(_gateway_history(rows, tag="tool-neighbour")) == [
        ("user", "first question"),
        ("assistant", "let me look that up"),
        ("user", "a prompt that was recovered"),
        ("assistant", "second answer"),
    ]
    assert _legacy_pairs(rows) == [
        ("user", "first question"),
        ("assistant", "let me look that up"),
        ("assistant", "second answer"),
    ]


@pytest.mark.parametrize(
    "rows",
    [
        [USER, REASONING_ONLY, FOLLOW_UP],
        [USER, ANSWER, RECOVERED, FOLLOW_UP],
        [USER, RECOVERED, SECOND_ANSWER],
        [USER, ANSWER, RECOVERED, SECOND_ANSWER],
        [USER, ERROR_ROW, RECOVERED, SECOND_ANSWER],
        [USER, ANSWER, REASONING_ONLY, RECOVERED, SECOND_ANSWER, RECOVERED],
        [RECOVERED, USER, ANSWER_WITH_REASONING, REASONING_CONTENT_ONLY, FOLLOW_UP],
    ],
    ids=[
        "reasoning-only",
        "stale-recovered",
        "recovered-after-user",
        "recovered-between-answers",
        "error-then-recovered",
        "recovered-last",
        "recovered-first",
    ],
)
def test_user_and_assistant_histories_fare_the_same_on_both_backends(rows):
    """For a history of user and assistant rows only, the Gateway is sent the
    turns the legacy sanitizer keeps. Tool rows are outside this: the Gateway
    history carries none."""
    assert _pairs(_gateway_history(rows, tag="parity-8038")) == _legacy_pairs(rows)


def test_every_call_site_asks_the_one_predicate(monkeypatch):
    """A row only a stand-in predicate refuses is dropped by the sanitizer, by
    the positions helper and by the Gateway builder alike."""
    import api.streaming as streaming

    marked = {"role": "assistant", "content": "only the stand-in refuses this", "_stand_in": True}
    rows = [USER, marked, FOLLOW_UP]
    monkeypatch.setattr(
        streaming,
        "_is_non_replayable_history_row",
        lambda msg: isinstance(msg, dict) and bool(msg.get("_stand_in")),
    )

    expected = [("user", "first question"), ("user", "second question")]
    assert [(row["role"], row["content"]) for row in streaming._sanitize_messages_for_api(rows)] == expected
    assert [
        (row["role"], row["content"]) for _index, row in streaming._api_safe_message_positions(rows)
    ] == expected
    assert _pairs(_gateway_history(rows, tag="one-predicate")) == expected
