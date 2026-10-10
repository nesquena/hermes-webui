"""A chronological insert must never land inside a multi-result tool block.

``_insert_state_message_chronologically`` skips past an
``assistant(tool_calls)`` -> tool-result block so a late state.db row cannot be
placed between a tool call and its result — splitting that pair makes the next
send fail provider validation.

The original guard only recognised the block when ``messages[idx - 1]`` was the
owning assistant. That holds for a single-result turn, but a multi-tool turn
emits several adjacent tool rows, so when the insertion point landed on the
SECOND result its left neighbour was another ``tool`` row and the guard did not
fire. The row was inserted mid-block:

    assistant(tool_calls), tool(t1), <inserted user>, tool(t2)

These tests pin the backward scan that finds the owning assistant across any
number of preceding contiguous tool rows.
"""

import pytest

from api.models import _insert_state_message_chronologically


def _msg(role, ts, content="x", **extra):
    row = {"role": role, "timestamp": ts, "content": content}
    row.update(extra)
    return row


def _tool_result(call_id, ts):
    return {
        "role": "tool",
        "timestamp": ts,
        "tool_call_id": call_id,
        "content": f"result-{call_id}",
    }


def _block(n_results, first_result_ts=210, step=10):
    """An assistant turn with ``n_results`` tool calls and their results."""
    assistant = _msg(
        "assistant",
        200,
        "",
        tool_calls=[
            {"id": f"t{i + 1}", "function": {"name": f"fn{i + 1}"}}
            for i in range(n_results)
        ],
    )
    results = [
        _tool_result(f"t{i + 1}", first_result_ts + i * step) for i in range(n_results)
    ]
    return assistant, results


def _assert_block_intact(messages, assistant, results):
    start = messages.index(assistant)
    positions = [messages.index(r) for r in results]
    expected = [start + 1 + i for i in range(len(results))]
    assert positions == expected, (
        "tool block was split: "
        f"{[m['role'] for m in messages]} (assistant at {start}, results at {positions})"
    )


@pytest.mark.parametrize("late_ts", [205, 210, 215, 220, 225, 235])
@pytest.mark.parametrize("n_results", [2, 3, 4])
def test_late_row_never_splits_a_multi_result_tool_block(n_results, late_ts):
    """No insertion point may land between an assistant's tool results."""
    assistant, results = _block(n_results)
    messages = [_msg("user", 100), assistant, *results, _msg("assistant", 500)]
    late = _msg("user", late_ts, "late-prompt")

    _insert_state_message_chronologically(messages, late)

    _assert_block_intact(messages, assistant, results)
    assert late in messages, "the late row should still be placed somewhere"


def test_single_result_block_still_protected():
    """The original single-result case must keep working."""
    assistant, results = _block(1)
    messages = [_msg("user", 100), assistant, *results, _msg("assistant", 500)]
    late = _msg("user", 215, "late-prompt")

    _insert_state_message_chronologically(messages, late)

    _assert_block_intact(messages, assistant, results)


def test_orphan_tool_rows_do_not_trap_the_backward_scan():
    """Tool rows with no owning assistant must not make the scan run away.

    If the backward walk finds no ``assistant(tool_calls)`` owner it must leave
    the insertion point alone rather than skipping the whole run.
    """
    orphans = [_tool_result("t1", 210), _tool_result("t2", 220)]
    messages = [_msg("user", 100), *orphans, _msg("assistant", 500)]
    late = _msg("user", 215, "late-prompt")

    _insert_state_message_chronologically(messages, late)

    assert late in messages
    # The orphan rows are still present and still in their original order.
    assert [m for m in messages if m.get("role") == "tool"] == orphans


def test_late_row_after_the_block_is_unaffected():
    """A row that belongs after the whole block lands after it, not inside."""
    assistant, results = _block(3)
    messages = [_msg("user", 100), assistant, *results, _msg("assistant", 500)]
    late = _msg("user", 260, "late-prompt")

    _insert_state_message_chronologically(messages, late)

    _assert_block_intact(messages, assistant, results)
    assert messages.index(late) > messages.index(results[-1])
