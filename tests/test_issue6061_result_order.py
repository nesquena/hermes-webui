"""Ordering contract for persisted tool-call summaries (#6061 review, P2).

The persisted summaries are assigned directly to ``s.tool_calls`` during
settlement and the saved-card consumers preserve encounter order inside the same
assistant bucket, so the list handed to them must follow the order of the
tool-result rows in the transcript -- never the order in which a repair managed
to resolve each row.

The repair that gives a row its own live-ledger entry by tool id must therefore
reserve the id/positional matches first and emit afterwards; an earlier row the
ledger cannot name keeps its position ahead of a later row it can name.
"""
import json
import pathlib
import sys

REPO_ROOT = pathlib.Path(__file__).parent.parent.resolve()
sys.path.insert(0, str(REPO_ROOT))

from api.streaming import _extract_tool_calls_from_messages


def _step(tid, name, msg_args=None):
    return {
        "role": "assistant",
        "content": "",
        "tool_calls": [{
            "id": tid,
            "function": {"name": name, "arguments": json.dumps(msg_args or {})},
        }],
    }


def _result(text, tid=None):
    row = {"role": "tool", "content": text}
    if tid:
        row["tool_call_id"] = tid
    return row


def _order(calls):
    return [(c.get("tid"), c.get("name"), c.get("snippet")) for c in calls]


def test_earlier_unmatched_result_stays_ahead_of_a_later_known_id_row():
    """The reviewer's minimal example: Z first (unknown), B second (ledger knows B)."""
    messages = [
        {"role": "user", "content": "q"},
        _step("A", "terminal"),
        _result("first-result", "Z"),
        _result("second-result", "B"),
    ]
    live = [
        {"name": "terminal", "args": {"cmd": "ls"}, "tid": "A"},
        {"name": "write_file", "args": {"path": "x"}, "tid": "B"},
    ]
    calls = _extract_tool_calls_from_messages(messages, live_tool_calls=live)

    assert [c["snippet"] for c in calls] == ["first-result", "second-result"], _order(calls)
    assert [c["name"] for c in calls] == ["terminal", "write_file"], _order(calls)
    assert [c["assistant_msg_idx"] for c in calls] == [1, 1], _order(calls)


def test_announced_later_row_does_not_jump_ahead_of_an_earlier_result():
    """Same class when the later row was also announced in the assistant message."""
    messages = [
        {"role": "user", "content": "q"},
        _step("A", "terminal"),
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "B", "function": {"name": "write_file", "arguments": "{}"}},
        ]},
        _result("first-result", "Z"),
        _result("second-result", "B"),
    ]
    live = [
        {"name": "terminal", "args": {"cmd": "ls"}, "tid": "A"},
        {"name": "write_file", "args": {"path": "x"}, "tid": "B"},
    ]
    calls = _extract_tool_calls_from_messages(messages, live_tool_calls=live)

    assert [c["snippet"] for c in calls] == ["first-result", "second-result"], _order(calls)
    assert [c["tid"] for c in calls] == ["Z", "B"], _order(calls)


def test_row_order_holds_across_two_assistant_buckets():
    """Row order wins while each row keeps its own assistant bucket."""
    messages = [
        {"role": "user", "content": "q"},
        _step("A", "terminal"),
        _result("first-result", "A"),
        _step("C", "read_file"),
        _result("second-result"),
        _result("third-result", "D"),
        {"role": "assistant", "content": "done"},
    ]
    live = [
        {"name": "terminal", "args": {"command": "pwd"}, "tid": "A"},
        {"name": "read_file", "args": {"path": "b"}, "tid": "C"},
        {"name": "write_file", "args": {"path": "d"}, "tid": "D"},
    ]
    calls = _extract_tool_calls_from_messages(messages, live_tool_calls=live)

    assert [c["snippet"] for c in calls] == [
        "first-result", "second-result", "third-result"
    ], _order(calls)
    assert [c["name"] for c in calls] == ["terminal", "read_file", "write_file"], _order(calls)
    assert [c["assistant_msg_idx"] for c in calls] == [1, 3, 3], _order(calls)


def test_known_id_ownership_survives_the_ordering_repair():
    """Control: a row whose id the ledger knows keeps its own name/args."""
    messages = [
        {"role": "user", "content": "q"},
        _step("A", "terminal"),
        _result("written", "B"),
        _result("ls ok", "A"),
        {"role": "assistant", "content": "done"},
    ]
    live = [
        {"name": "terminal", "args": {"cmd": "ls"}, "tid": "A"},
        {"name": "write_file", "args": {"path": "y"}, "tid": "B"},
    ]
    calls = _extract_tool_calls_from_messages(messages, live_tool_calls=live)

    assert [c["tid"] for c in calls] == ["B", "A"], _order(calls)
    by_tid = {c["tid"]: c for c in calls}
    assert by_tid["B"]["name"] == "write_file", by_tid["B"]
    assert by_tid["B"]["args"] == {"path": "y"}, by_tid["B"]
    assert by_tid["A"]["name"] == "terminal", by_tid["A"]
    assert by_tid["A"]["snippet"] == "ls ok", by_tid["A"]


def test_rows_without_ids_still_pair_positionally():
    """Control: a legacy transcript with no tool ids keeps positional pairing."""
    messages = [
        {"role": "user", "content": "q"},
        {"role": "assistant", "content": "starting"},
        _result("first-result"),
        _result("second-result"),
    ]
    live = [
        {"name": "terminal", "args": {"command": "pwd"}},
        {"name": "write_file", "args": {"path": "no-ids"}},
    ]
    calls = _extract_tool_calls_from_messages(messages, live_tool_calls=live)

    assert [c["snippet"] for c in calls] == ["first-result", "second-result"], _order(calls)
    assert [c["name"] for c in calls] == ["terminal", "write_file"], _order(calls)


def test_unknown_id_row_is_neither_dropped_nor_moved():
    """Control: a row the ledger cannot name keeps its result row and its place."""
    messages = [
        {"role": "user", "content": "q"},
        _step("A", "terminal"),
        _result("first-result", "Z"),
        _result("second-result", "A"),
    ]
    live = [
        {"name": "terminal", "args": {}, "tid": "A"},
        {"name": "write_file", "args": {"path": "z"}, "tid": "B"},
    ]
    calls = _extract_tool_calls_from_messages(messages, live_tool_calls=live)

    assert [c["snippet"] for c in calls] == ["first-result", "second-result"], _order(calls)
    assert [c["name"] for c in calls] == ["write_file", "terminal"], _order(calls)
    assert [c["assistant_msg_idx"] for c in calls] == [1, 1], _order(calls)
