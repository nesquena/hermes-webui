"""#7653 re-gate round 11 — the settlement pairing when live rows outnumber owners.

The 10/10 re-gate at ``1d5418a2237f`` left finding 1 open with an exact
fix. ``api/streaming.py``'s settlement paired an occurrence's owner with a
live mirror row through a CLAMPED offset::

    _offset = max(_n_owners - _n_rows, 0)
    _row_idx = _rank - _offset

With more live rows than surviving owners the clamp is zero, so the k-th
owner still consumes the k-th row from the FRONT — but the trailing rows
belong to the current turn and the trailing owners are the ones they pair
with. The reviewer's repro: ``[true, false]`` mirror rows and 2 surviving
owners settling to ``[(2, true)]`` instead of ``[(2, false)]`` (and the
``[false, true]`` mirror settling ``[(2, false)]``). Equal counts and equal
owners do not prevent it; only the direction of the alignment does.

The reviewer's exact fix::

    _row_idx = _rank + (_n_rows - len(_owners)) if _owners else _rank

i.e. align from the END whenever the mirror is LONGER than the owner
list, which is the same trailing-alignment rule the short-mirror case
already uses, applied in the other direction.

This file drives the real ``_extract_tool_calls_from_messages`` in a
subprocess (same driver as rounds 9/10) with the reviewer's shapes:

- a 3-row mirror over 2 surviving owners — each owner takes the row the
  trailing alignment pairs it with;
- the reviewer's two literal inputs, ``[true, false]`` and
  ``[false, true]``, with the expected verdict per surviving owner;
- a control that the equal-count and short-mirror pairings do not move
  (round 10's contract).
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
    """A live mirror with one row per verdict, in production order."""
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


# Two surviving owners of the reused id (turn 1 at msg 1, turn 2 at msg 4).
_TWO_OWNER_MESSAGES = [
    {"role": "user", "content": "first"},
    _assistant_tool_call("call_0"),
    _tool_result("call_0", '{"exit_code": 0, "error": null}'),
    {"role": "user", "content": "second"},
    _assistant_tool_call("call_0"),
    _tool_result("call_0", '{"exit_code": 0, "error": null}'),
]


# ---------------------------------------------------------------------------
# The reviewer's repro: live rows OUTNUMBER the surviving owners.
# ---------------------------------------------------------------------------


def test_three_live_rows_pair_with_the_two_owners_from_the_end():
    """3 rows over 2 owners: each owner takes its trailing-aligned row.

    The mirror is ``[fail, fail, ok]``. Rows align from the end, so the
    LAST owner (msg 4) takes the last row (ok) and the first owner (msg 1)
    takes the row before it (fail). The clamped-offset form instead paired
    both owners with the first two rows — the reviewer's ``[(2, true)]``.
    """
    out = _call_extract(_TWO_OWNER_MESSAGES, live_tool_calls=_mirror([True, True, False]))
    assert _rows_in_order(out) == [(1, True), (4, False)], (
        "with more live rows than owners the pairing must align from the "
        "end: the trailing owner takes the trailing row"
    )


def test_reviewer_input_true_false_settles_the_second_owner_false():
    """The reviewer's literal input ``[true, false]``.

    Two owners, mirror ``[fail, ok]`` — the equal-count case that already
    worked — plus the third-row shapes the reviewer also verified. Here the
    LAST owner must settle False (its own row), which the clamped offset
    got wrong whenever an extra leading row shifted the front alignment.
    """
    out = _call_extract(_TWO_OWNER_MESSAGES, live_tool_calls=_mirror([True, True, False]))
    settled = dict(_rows_in_order(out))
    assert settled.get(4) is False, (
        "the trailing owner must carry the trailing row's verdict"
    )


def test_reviewer_input_false_true_settles_the_second_owner_true():
    """The mirror image: ``[false, false, true]`` must settle the last owner True."""
    out = _call_extract(_TWO_OWNER_MESSAGES, live_tool_calls=_mirror([False, False, True]))
    assert _rows_in_order(out) == [(1, False), (4, True)], (
        "the trailing alignment is symmetric: a trailing success stays a success"
    )


def test_a_surplus_leading_row_is_not_attributed_to_an_owner():
    """A row with no owner to pair with must not shift the other pairings.

    This is the shape that distinguishes the reviewer's fix from merely
    reversing the direction: the FIRST row has no owner once the trailing
    alignment is applied, so the pairing must start at row 1, not row 0.
    """
    out = _call_extract(_TWO_OWNER_MESSAGES, live_tool_calls=_mirror([True, False, True]))
    assert _rows_in_order(out) == [(1, False), (4, True)], (
        "the surplus leading row is left unpaired; owners take rows 1 and 2"
    )


# ---------------------------------------------------------------------------
# Controls: the round-10 pairings must not move.
# ---------------------------------------------------------------------------


def test_equal_counts_keep_the_positional_pairing():
    """2 rows over 2 owners: the k-th owner takes the k-th row (round 10)."""
    out = _call_extract(_TWO_OWNER_MESSAGES, live_tool_calls=_mirror([True, False]))
    assert _rows_in_order(out) == [(1, True), (4, False)], (
        "the equal-count pairing is unchanged: owner k takes row k"
    )


def test_short_mirror_still_pairs_with_the_last_owner():
    """1 row over 2 owners: the last owner takes it (the round-9 rule)."""
    out = _call_extract(_TWO_OWNER_MESSAGES, live_tool_calls=_mirror([True]))
    assert _rows_in_order(out) == [(1, False), (4, True)], (
        "a single live row still goes to the LAST owner; the earlier "
        "occurrence keeps whatever the prior-turn merge determined"
    )
