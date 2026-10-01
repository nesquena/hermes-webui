"""#7358 round 10 SERVER-side regression — call_id-first id order + per-occurrence prior merge.

The 10/01 re-gate review's two SILENT findings on the round-9 server fix
(``api/streaming.py::_extract_tool_calls_from_messages``):

**Finding 1 (SILENT)** — the derived tool id used ``id`` before ``call_id``,
the reverse of the Agent's pairing order (``_tc_get(tc, "call_id") or
_tc_get(tc, "id")`` in ``agent/context_compressor.py`` /
``agent/message_sanitization.py``). When one ``tool_calls`` row carries both
fields with DIFFERENT values (Codex/Responses style), the WebUI keyed the
pending map on ``id`` while the tool result references ``call_id`` — the
result misses, the row falls into the positional live-fallback, and the live
failure lands on the first unresolved (possibly historical) call instead of
the call that actually failed.

**Finding 2 (SILENT)** — the round-9 ``prior_reused`` blanket refusal threw
away the prior verdict of the occurrence it DID match. With turn-1 ``call_0``
success (owner (1, call_0)) + turn-2 ``call_0`` failure (owner (4, call_0)),
the prior pair [(1, false), (4, true)] was rewritten [(1, false), (4, false)]
— a historical failure flipped back to success. Round 10 merges prior
verdicts per occurrence ``(assistant_msg_idx, tid)`` instead; prior rows
without an owner index are skipped conservatively, and several prior rows for
the same occurrence merge with ``any is_error == True`` (only upgrade, never
downgrade).

This file covers the server half of both findings:
- bug repro for Finding 1 (id/call_id split, live verdict must bind to call_id);
- bug repro + control for Finding 2 (reused call_0, prior failure preserved);
- controls proving current-turn live direct-hit behaviour is unchanged.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------------------
# Shared driver for _extract_tool_calls_from_messages (subprocess, same as the
# round-6/7/8/9 fixtures, because api.streaming pulls heavy deps).
# ---------------------------------------------------------------------------


def _extract_driver():
    driver = (
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

    def _run(payload: dict) -> dict:
        proc = subprocess.run(
            [sys.executable, "-c", driver],
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert proc.returncode == 0, f"driver failed:\n{proc.stderr[:400]}"
        return json.loads(proc.stdout)

    def run(messages, live_tool_calls=None, prior_tool_calls=None):
        return _run({
            "messages": messages,
            "live_tool_calls": live_tool_calls or [],
            "prior_tool_calls": prior_tool_calls or [],
        })

    return run


call_extract = pytest.fixture(scope="module")(_extract_driver)


def _user(text: str) -> dict:
    return {"role": "user", "content": text}


def _tool_call_row(tid: str, call_id: str | None = None) -> dict:
    row = {"id": tid, "type": "function",
           "function": {"name": "terminal", "arguments": '{"command": "ls"}'}}
    if call_id is not None:
        row["call_id"] = call_id
    return row


# ---------------------------------------------------------------------------
# Finding 1 — id/call_id split: live verdict must bind to the call_id
# ---------------------------------------------------------------------------


def test_id_call_id_split_live_verdict_binds_to_call_id(call_extract):
    """Bug repro (finding 1): turn-1's tool_call carries BOTH ``id`` and
    ``call_id`` with different values (Codex/Responses style); its tool
    result references the ``call_id``. Turn-2 (current) has a plain call and
    the live mirror holds only turn-2's FAILING entry.

    OLD: pending keyed on ``id`` (``call_A1``) -> the ``call_B1`` result
    misses -> turn-1's row stays unresolved -> the positional live-fallback
    binds it to turn-2's live failure (verdict lands on the wrong call).
    NEW: pending keyed on ``call_id`` (``call_B1``) -> the result hits and
    turn-1 keeps its success; the live failure stays on turn-2's call.
    """
    messages = [
        _user("first"),
        {"role": "assistant", "content": "", "tool_calls": [_tool_call_row("call_A1", call_id="call_B1")]},
        {"role": "tool", "tool_call_id": "call_B1", "content": '{"exit_code": 0, "error": null}'},
        _user("second"),
        {"role": "assistant", "content": "", "tool_calls": [_tool_call_row("call_T")]},
        {"role": "tool", "tool_call_id": "call_T", "content": '{"exit_code": 2, "error": "boom"}'},
    ]
    live = [
        {"name": "terminal", "tid": "call_T", "done": True, "is_error": True, "snippet": "boom"},
    ]
    out = call_extract(messages, live_tool_calls=live)
    assert len(out) == 2, "both calls must settle as resolved rows"
    by_idx = {(row["assistant_msg_idx"], row["tid"]): row["is_error"] for row in out}
    # Turn-1's call is keyed on its call_id and keeps its success.
    assert by_idx[(1, "call_B1")] is False, "turn-1 success must not inherit turn-2's live failure"
    # Turn-2's live failure lands on the current call only.
    assert by_idx[(4, "call_T")] is True


def test_id_call_id_split_single_turn_live_hit_is_unchanged(call_extract):
    """Control for finding 1: a single current-turn call with a split
    id/call_id whose live mirror entry (keyed on the call_id) is present —
    the live verdict must bind directly, via the MAIN path, with the
    call_id as the row's tid (both old and new code end True, but only the
    fix resolves the pairing through the pending map)."""
    messages = [
        {"role": "assistant", "content": "", "tool_calls": [_tool_call_row("call_A", call_id="call_B")]},
        {"role": "tool", "tool_call_id": "call_B", "content": "boom"},
    ]
    out = call_extract(
        messages,
        live_tool_calls=[{"name": "terminal", "tid": "call_B", "done": True, "is_error": True, "snippet": "boom"}],
    )
    assert len(out) == 1
    assert out[0]["tid"] == "call_B", "the settled row carries the call_id (the pairing key)"
    assert out[0]["is_error"] is True


# ---------------------------------------------------------------------------
# Finding 2 — prior verdicts merge per occurrence (assistant_msg_idx, tid)
# ---------------------------------------------------------------------------


def test_reused_tid_prior_failure_survives_next_settlement(call_extract):
    """Bug repro (finding 2): turn-1 ``call_0`` success (owner (1, call_0)),
    turn-2 ``call_0`` failure (owner (4, call_0)). Turn-3 settles with the
    live mirror holding only turn-3's unique call and the prior summary
    carrying BOTH ``call_0`` rows.

    OLD: tid reused in the prior summary -> blanket refusal -> both call_0
    occurrences default False -> [(1, false), (4, true)] rewritten
    [(1, false), (4, false)] (historical failure downgraded).
    NEW: prior merged per (assistant_msg_idx, tid) -> each occurrence keeps
    its own verdict.
    """
    messages = [
        _user("first"),
        {"role": "assistant", "content": "", "tool_calls": [_tool_call_row("call_0")]},
        {"role": "tool", "tool_call_id": "call_0", "content": '{"exit_code": 0, "error": null}'},
        _user("second"),
        {"role": "assistant", "content": "", "tool_calls": [_tool_call_row("call_0")]},
        {"role": "tool", "tool_call_id": "call_0", "content": '{"exit_code": 2, "error": "boom"}'},
        _user("third"),
        {"role": "assistant", "content": "", "tool_calls": [_tool_call_row("call_x")]},
        {"role": "tool", "tool_call_id": "call_x", "content": '{"exit_code": 0, "error": null}'},
    ]
    live = [
        {"name": "terminal", "tid": "call_x", "done": True, "is_error": False, "snippet": "ok"},
    ]
    prior = [
        {"name": "terminal", "tid": "call_0", "assistant_msg_idx": 1, "is_error": False, "snippet": "ok"},
        {"name": "terminal", "tid": "call_0", "assistant_msg_idx": 4, "is_error": True, "snippet": "boom"},
    ]
    out = call_extract(messages, live_tool_calls=live, prior_tool_calls=prior)
    by_idx = {(row["assistant_msg_idx"], row["tid"]): row["is_error"] for row in out}
    assert by_idx[(1, "call_0")] is False, "turn-1 success preserved"
    assert by_idx[(4, "call_0")] is True, "turn-2 failure must NOT be downgraded to success"
    assert by_idx[(7, "call_x")] is False, "turn-3 live verdict applied"


def test_same_occurrence_duplicate_prior_rows_only_upgrade(call_extract):
    """Round-10 conservative merge rule: several prior rows for the SAME
    occurrence — any ``is_error=True`` wins (only upgrade, never downgrade
    a recorded failure)."""
    messages = [
        {"role": "assistant", "content": "", "tool_calls": [_tool_call_row("call_0")]},
        {"role": "tool", "tool_call_id": "call_0", "content": "boom"},
    ]
    prior = [
        {"name": "terminal", "tid": "call_0", "assistant_msg_idx": 0, "is_error": False},
        {"name": "terminal", "tid": "call_0", "assistant_msg_idx": 0, "is_error": True},
    ]
    out = call_extract(messages, live_tool_calls=[], prior_tool_calls=prior)
    assert len(out) == 1
    assert out[0]["is_error"] is True, "any True prior row for the occurrence wins"


def test_prior_without_owner_idx_is_not_attributed(call_extract):
    """Round-10: a prior row missing ``assistant_msg_idx`` can only be
    scoped by tid, and only when that tid names a SINGLE occurrence in this
    history — the round-6 two-turn path (a prior verdict surviving the live
    mirror losing the call) must keep working."""
    messages = [
        {"role": "assistant", "content": "", "tool_calls": [_tool_call_row("t1")]},
        {"role": "tool", "tool_call_id": "t1", "content": "boom"},
    ]
    out = call_extract(
        messages,
        live_tool_calls=[],
        prior_tool_calls=[{"name": "terminal", "tid": "t1", "is_error": True, "snippet": "boom"}],
    )
    assert len(out) == 1
    assert out[0]["is_error"] is True, (
        "unattributable prior row still merges by tid for a single-occurrence id"
    )


def test_prior_without_owner_idx_not_attributed_when_tid_reused(call_extract):
    """Round-10: the same owner-less prior row for a REUSED tid would leak
    onto the wrong occurrence (round-9 finding 1), so with a reused tid it
    is dropped conservatively — the round-6 single-occurrence path above is
    unaffected."""
    messages = [
        _user("first"),
        {"role": "assistant", "content": "", "tool_calls": [_tool_call_row("call_0")]},
        {"role": "tool", "tool_call_id": "call_0", "content": "ok"},
        _user("second"),
        {"role": "assistant", "content": "", "tool_calls": [_tool_call_row("call_0")]},
        {"role": "tool", "tool_call_id": "call_0", "content": "boom"},
    ]
    out = call_extract(
        messages,
        live_tool_calls=[],
        prior_tool_calls=[{"name": "terminal", "tid": "call_0", "is_error": True, "snippet": "boom"}],
    )
    by_idx = {(row["assistant_msg_idx"], row["tid"]): row["is_error"] for row in out}
    assert by_idx == {(1, "call_0"): False, (4, "call_0"): False}, (
        "an owner-less prior verdict for a reused tid must not leak onto either occurrence"
    )


# ---------------------------------------------------------------------------
# Control (c) — current-turn live direct-hit behaviour is unchanged
# ---------------------------------------------------------------------------


def test_current_turn_live_direct_hit_unchanged(call_extract):
    """Control: a current-turn call whose live mirror entry is present and
    classified must take the live verdict directly, even when a prior
    summary carries the same occurrence — rounds 6-8 semantics must not
    move (live wins over prior)."""
    messages = [
        {"role": "assistant", "content": "", "tool_calls": [_tool_call_row("t1")]},
        {"role": "tool", "tool_call_id": "t1", "content": "boom"},
    ]
    out = call_extract(
        messages,
        live_tool_calls=[{"name": "terminal", "tid": "t1", "done": True, "is_error": True, "snippet": "boom"}],
        prior_tool_calls=[{"name": "terminal", "tid": "t1", "assistant_msg_idx": 0, "is_error": False, "snippet": "ok"}],
    )
    assert len(out) == 1
    assert out[0]["is_error"] is True, "live classification still wins over prior"
