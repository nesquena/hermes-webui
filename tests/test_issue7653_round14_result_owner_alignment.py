"""#7653 re-gate round 14 — the settlement alignment counts only completed calls.

The 10-19 re-gate (item 5) found the pairing offset shifted by a
declared-but-unfinished trailing call::

    Input: owner 1 call_0 -> exit 3; owner 3 call_0 -> success; owner 5
    declares call_0 and has no result; live [true,false]. Owner 5 is counted
    in _owners, so the offset is -1. PR [(1,false),(3,true)], master
    [(1,None),(3,None)], expected [(1,true),(3,false)].

The occurrence scan counted every assistant row that EMITS the tid, so an
assistant row that only declares a call and never receives its result
occupied a rank in the alignment list. The reviewer's fix: align only
against occurrences that HAVE a result.

This file drives the real ``_extract_tool_calls_from_messages`` (same
subprocess driver as rounds 9-11) with the reviewer's exact shape, plus
controls that the completed-only pairings do not move.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

_EXTRACT_DRIVER = (
    "import sys, json;\n"
    "sys.path.insert(0, %r);\n"
    "from api.streaming import _extract_tool_calls_from_messages;\n"
    "data = json.loads(sys.stdin.read());\n"
    "out = _extract_tool_calls_from_messages(\n"
    "    data['messages'],\n"
    "    live_tool_calls=data['live_tool_calls'],\n"
    "    prior_tool_calls=data.get('prior_tool_calls'),\n"
    ");\n"
    "sys.stdout.write(json.dumps(out));\n"
) % str(REPO_ROOT)


def _call_extract(messages, live_tool_calls=None, prior_tool_calls=None):
    proc = subprocess.run(
        [sys.executable, "-c", _EXTRACT_DRIVER],
        input=json.dumps({
            "messages": messages,
            "live_tool_calls": live_tool_calls or [],
            "prior_tool_calls": prior_tool_calls or [],
        }),
        capture_output=True,
        text=True,
        timeout=90,
    )
    assert proc.returncode == 0, f"driver failed:\n{proc.stderr[:500]}"
    return json.loads(proc.stdout)


def _rows_in_order(out):
    return [(row["assistant_msg_idx"], row["is_error"]) for row in out]


def _assistant_tool_call(tid: str, name: str = "terminal") -> dict:
    return {
        "role": "assistant",
        "content": [
            {"type": "tool_use", "id": tid, "name": name, "input": {"command": "ls"}},
        ],
    }


def _tool_result(tid: str, raw: str) -> dict:
    return {"role": "tool", "tool_call_id": tid, "content": raw}


def _mirror(verdicts):
    return [
        {
            "name": "terminal",
            "tid": "call_0",
            "done": True,
            "is_error": bool(v),
            "snippet": "boom" if v else "ok",
            "args": {"command": "ls"},
        }
        for v in verdicts
    ]


# The reviewer's exact shape: owner 1 exits 3, owner 3 succeeds, owner 5
# declares the call and never receives its result.
_UNFINISHED_TRAILING_MESSAGES = [
    {"role": "user", "content": "first"},
    _assistant_tool_call("call_0"),          # owner 1
    _tool_result("call_0", '{"exit_code": 3, "error": null}'),
    _assistant_tool_call("call_0"),          # owner 3
    _tool_result("call_0", '{"exit_code": 0, "error": null}'),
    _assistant_tool_call("call_0"),          # owner 5 — declared, no result
]


def test_unfinished_trailing_call_does_not_shift_completed_verdicts():
    """The reviewer's item-5 repro: the declared-but-unfinished owner 5 must
    not consume a rank, so owner 1 keeps the exit-3 verdict and owner 3 the
    success."""
    out = _call_extract(_UNFINISHED_TRAILING_MESSAGES, _mirror([True, False]))
    rows = _rows_in_order(out)
    assert rows == [(1, True), (3, False)], (
        "the unfinished trailing call shifted the completed pairings — "
        f"expected [(1,true),(3,false)], got {rows}"
    )


def test_unfinished_trailing_call_with_a_single_completed_owner():
    """Only owner 1 has a result; the mirror's single row belongs to it, not
    to the unfinished declaration after it."""
    messages = [
        {"role": "user", "content": "first"},
        _assistant_tool_call("call_0"),          # owner 1
        _tool_result("call_0", '{"exit_code": 3, "error": null}'),
        _assistant_tool_call("call_0"),          # owner 3 — declared, no result
    ]
    out = _call_extract(messages, _mirror([True]))
    rows = _rows_in_order(out)
    assert rows == [(1, True)], (
        f"the single completed owner must take the single live row: {rows}"
    )


def test_all_owners_completed_alignment_is_unchanged():
    """Control: with every declaring owner holding a result the pairing is
    the round-11 trailing alignment, unchanged."""
    messages = [
        {"role": "user", "content": "first"},
        _assistant_tool_call("call_0"),          # owner 1
        _tool_result("call_0", '{"exit_code": 3, "error": null}'),
        {"role": "user", "content": "second"},
        _assistant_tool_call("call_0"),          # owner 4
        _tool_result("call_0", '{"exit_code": 0, "error": null}'),
    ]
    out = _call_extract(messages, _mirror([True, False]))
    rows = _rows_in_order(out)
    assert rows == [(1, True), (4, False)], rows


def test_result_owners_helper_tracks_only_completed_occurrences():
    """The pre-scan itself: an owner without a result never enters the
    alignment list."""
    driver = (
        "import sys, json;\n"
        "sys.path.insert(0, %r);\n"
        "from api.streaming import _tool_result_owners_by_tid;\n"
        "data = json.loads(sys.stdin.read());\n"
        "sys.stdout.write(json.dumps(_tool_result_owners_by_tid(data['messages'])));\n"
    ) % str(REPO_ROOT)
    proc = subprocess.run(
        [sys.executable, "-c", driver],
        input=json.dumps({"messages": _UNFINISHED_TRAILING_MESSAGES}),
        capture_output=True,
        text=True,
        timeout=90,
    )
    assert proc.returncode == 0, f"driver failed:\n{proc.stderr[:500]}"
    owners = json.loads(proc.stdout)
    assert owners.get("call_0") == [1, 3], (
        f"only the completed owners may be aligned: {owners}"
    )
