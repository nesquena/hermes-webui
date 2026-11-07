"""#7358 round 9 SERVER-side regression — occurrence-keyed verdicts.

The 10/01 re-gate review's SILENT findings 1 and 3 on commit ``a50c8423d2d6``.
Tool-call ids are NOT unique (llama.cpp emits one constant id; other
providers reuse ``call_0`` every turn), so a verdict keyed on the tid
alone leaks onto every other occurrence of the same id on the server:

- **Finding 1** (``api/streaming.py::_extract_tool_calls_from_messages``):
  a live/prior verdict looked up by ``tid`` copies a later turn's failure
  onto an earlier successful occurrence of the same id.
- **Finding 3** (``api/routes.py::merge_duplicate_tool_row``): the
  name+invocation hydration fallback can pair two rows that carry DIFFERENT
  explicit ids and copies the incoming failure onto the existing row
  (old-ok became error).

The fix (per the reviewer): key every verdict on the call OCCURRENCE — in
settlement by (owning ``assistant_msg_idx``, ``tid``), and in hydration by
refusing to transfer ``is_error`` across different explicit ids. The id
normalisation mirrors the Agent's own
``conversation_compression_reply_anchor`` helpers.

This file covers the SERVER half of the two findings:
- bug repro for Finding 1 (settlement) with reused ``call_0``;
- controls proving unique-id settlement and the prior-merge behaviour are
  unchanged (rounds 3-8);
- bug repro + control for Finding 3 (hydration merge gate).
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------------------
# Shared driver for _extract_tool_calls_from_messages (running in a subprocess,
# same as the round-6/7/8 fixtures, because api.streaming pulls heavy deps).
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


def _assistant_tool_call(tid: str, name: str = "terminal", args: dict | None = None) -> dict:
    return {
        "role": "assistant",
        "content": [{"type": "tool_use", "id": tid, "name": name, "input": args or {"command": "ls"}}],
    }


def _tool_result(tid: str, raw: str) -> dict:
    return {"role": "tool", "tool_call_id": tid, "content": raw}


# ---------------------------------------------------------------------------
# Finding 1 — server settlement keys verdicts on the call occurrence
# ---------------------------------------------------------------------------


def test_reused_tid_settlement_keeps_turn_one_success(call_extract):
    """Bug repro (finding 1): two turns both emit ``call_0`` (turn-1
    success, turn-2 failure) and the live mirror holds only the failing
    turn-2 entry. The verdict must land on the LAST occurrence of the
    reused id (turn 2) and must NOT bleed onto turn 1."""
    messages = [
        _user("first"),
        _assistant_tool_call("call_0"),
        _tool_result("call_0", '{"exit_code": 0, "error": null}'),
        _user("second"),
        _assistant_tool_call("call_0"),
        _tool_result("call_0", '{"exit_code": 2, "error": "boom"}'),
    ]
    live = [
        {"name": "terminal", "tid": "call_0", "done": True, "is_error": True, "snippet": "boom"},
    ]
    out = call_extract(messages, live_tool_calls=live)
    by_idx = {(row["assistant_msg_idx"], row["tid"]): row["is_error"] for row in out}
    # The failing (last) occurrence is red.
    assert by_idx[(4, "call_0")] is True
    # The earlier successful occurrence must NOT inherit the failure.
    assert by_idx[(1, "call_0")] is False


def test_unique_tid_settlement_is_unchanged(call_extract):
    """Control: unique ids still settle with their own live verdict —
    rounds 3-8 behaviour must not move (single occurrence per tid)."""
    messages = [
        _user("first"),
        _assistant_tool_call("call_a"),
        _tool_result("call_a", '{"exit_code": 1, "error": "nope"}'),
        _user("second"),
        _assistant_tool_call("call_b"),
        _tool_result("call_b", '{"exit_code": 0, "error": null}'),
    ]
    live = [
        {"name": "terminal", "tid": "call_a", "done": True, "is_error": True, "snippet": "nope"},
        {"name": "terminal", "tid": "call_b", "done": True, "is_error": False, "snippet": "ok"},
    ]
    out = call_extract(messages, live_tool_calls=live)
    by_idx = {(row["assistant_msg_idx"], row["tid"]): row["is_error"] for row in out}
    assert by_idx[(1, "call_a")] is True
    assert by_idx[(4, "call_b")] is False


def test_single_turn_live_defaults_and_prior_merge_unchanged(call_extract):
    """Control: single-occurrence live/prior behaviour from rounds 6-8 —
    live wins when present, prior applies when live is missing, default is
    False when neither has the call."""
    messages = [
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "t1", "type": "function", "function": {"name": "terminal", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "t1", "content": "boom"},
    ]
    # Live present + False -> False wins over prior True (round 6).
    out = call_extract(
        messages,
        live_tool_calls=[{"name": "terminal", "tid": "t1", "is_error": False, "done": True}],
        prior_tool_calls=[{"name": "terminal", "tid": "t1", "is_error": True, "snippet": "boom"}],
    )
    assert out[0]["is_error"] is False
    # Live missing + prior True -> True wins (round 6 merge).
    out = call_extract(
        messages,
        live_tool_calls=[],
        prior_tool_calls=[{"name": "terminal", "tid": "t1", "is_error": True, "snippet": "boom"}],
    )
    assert out[0]["is_error"] is True
    # Neither -> default False (round 3-5 base behaviour).
    out = call_extract(messages, live_tool_calls=[], prior_tool_calls=[])
    assert out[0]["is_error"] is False


def test_reused_tid_prior_merge_does_not_leak_across_occurrences(call_extract):
    """Bug repro (finding 1, prior half): a reused tid in the prior summary
    is ambiguous and must not be attributed to a fresh occurrence of the same
    id — default to False instead of inheriting a prior failure."""
    messages = [
        _user("second"),
        _assistant_tool_call("call_0"),
        _tool_result("call_0", '{"exit_code": 2, "error": "boom"}'),
    ]
    live = []
    prior = [
        {"name": "terminal", "tid": "call_0", "is_error": True,
         "assistant_msg_idx": 1, "snippet": "old"},
        {"name": "terminal", "tid": "call_0", "is_error": False,
         "assistant_msg_idx": 42, "snippet": "newer-other"},
    ]
    out = call_extract(messages, live_tool_calls=live, prior_tool_calls=prior)
    assert len(out) == 1
    assert out[0]["is_error"] is False


def test_reused_tid_prior_owner_mismatch_is_refused(call_extract):
    """Bug repro (control): a prior row naming a DIFFERENT owning assistant
    than the current occurrence is a different call that merely reuses the id
    — its verdict must only apply to ITS occurrence, and the newer occurrence
    defaults to False instead of inheriting the older one's True."""
    messages = [
        _user("first"),
        _assistant_tool_call("call_0"),
        _tool_result("call_0", '{"exit_code": 0, "error": null}'),
        _user("second"),
        _assistant_tool_call("call_0"),
        _tool_result("call_0", '{"exit_code": 2, "error": "boom"}'),
    ]
    # The current (turn-2) live mirror has lost the call; prior only carries
    # turn-1's call_0 (True). It must be applied to its own occurrence and
    # refused for turn-2.
    live = []
    prior = [{"name": "terminal", "tid": "call_0", "is_error": True,
              "assistant_msg_idx": 1, "snippet": "old"}]
    out = call_extract(messages, live_tool_calls=live, prior_tool_calls=prior)
    by_idx = {(row["assistant_msg_idx"], row["tid"]): row["is_error"] for row in out}
    assert by_idx[(1, "call_0")] is True, "the owner-matched occurrence keeps its own prior verdict"
    assert by_idx[(4, "call_0")] is False, "the newer occurrence must not inherit the older one's prior True"


# ---------------------------------------------------------------------------
# Finding 3 — hydration: merge_duplicate_tool_row must not copy is_error
# ---------------------------------------------------------------------------


def _module_function_block(src: str, name: str) -> str:
    """Lift a top-level ``def <name>(...)`` verbatim up to the next top-level def."""
    start = src.find(f"def {name}(")
    assert start != -1, f"{name} not found"
    lines = src[start:].split("\n")
    out = [lines[0]]
    for line in lines[1:]:
        if line and not line[0].isspace() and not line.startswith(")"):
            break
        out.append(line)
    return "\n".join(out).rstrip()


def _nested_function_block(src: str, header: str) -> str:
    """Lift a nested (indented) def block and dedent it to top-level."""
    start = src.find(header)
    assert start != -1, f"{header!r} not found"
    # ``start`` points at the leading ``def``; rewind to the line start so the
    # enclosing indentation is captured (otherwise the dedent below is off).
    line_start = src.rfind("\n", 0, start) + 1
    base_indent = start - line_start
    lines = src[line_start:].split("\n")
    out = [lines[0]]
    for line in lines[1:]:
        if line.strip() and (len(line) - len(line.lstrip(" "))) <= base_indent:
            break
        out.append(line)
    block = "\n".join(out).rstrip()
    return "\n".join(ln[base_indent:] if ln.startswith(" " * base_indent) else ln for ln in block.split("\n"))


def _merge_driver_factory():
    """Build a callable(existing, incoming) -> merged dict that runs the REAL
    ``merge_duplicate_tool_row`` from api/routes.py (lifted, dedented) against
    a provided row pair in a subprocess."""
    src = (REPO_ROOT / "api" / "routes.py").read_text(encoding="utf-8")
    for name in (
        "_anchor_scene_tool_row_id",
        "_anchor_scene_tool_rows_have_different_explicit_ids",
        "_anchor_scene_is_bounded_tool_body_preview",
    ):
        assert f"def {name}(" in src, f"{name} missing from routes.py"
    lifted = "\n\n".join(
        [
            _module_function_block(src, "_anchor_scene_tool_row_id"),
            _module_function_block(src, "_anchor_scene_tool_rows_have_different_explicit_ids"),
            _module_function_block(src, "_anchor_scene_is_bounded_tool_body_preview"),
            _nested_function_block(src, "def merge_duplicate_tool_row("),
        ]
    )
    driver = (
        "import json, sys, copy;\n"
        + lifted
        + "\n\ndef main(data):\n"
        + "    return merge_duplicate_tool_row(data['existing'], data['incoming']);\n"
        + "data = json.loads(sys.stdin.read());\n"
        + "sys.stdout.write(json.dumps(main(data)));\n"
    )

    def _merge(existing: dict, incoming: dict) -> dict:
        proc = subprocess.run(
            [sys.executable, "-c", driver],
            input=json.dumps({"existing": existing, "incoming": incoming}),
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert proc.returncode == 0, f"merge driver failed:\n{proc.stderr[:600]}"
        return json.loads(proc.stdout)

    return _merge


merge_driver = pytest.fixture(scope="module")(_merge_driver_factory)


def test_hydration_different_ids_keep_error_not_transferred(merge_driver):
    """Bug repro (finding 3): two rows with DIFFERENT explicit ids matched by
    name+invocation must not exchange the error verdict — the presentation
    keys still merge, but ``is_error`` stays off the existing row."""
    existing = {
        "role": "tool",
        "tool_call_id": "call_old",
        "tool": {"id": "call_old", "name": "terminal", "is_error": False,
                 "snippet": "[exit 0]", "command": "pwd"},
        "payload": {"name": "terminal", "is_error": False, "snippet": "[exit 0]"},
    }
    incoming = {
        "role": "tool",
        "tool_call_id": "call_new",
        "tool": {"id": "call_new", "name": "terminal", "is_error": True,
                 "snippet": "[exit 2]", "command": "pwd", "duration": "1.2s"},
        "payload": {"name": "terminal", "is_error": True, "snippet": "[exit 2]"},
    }
    merged = merge_driver(existing, incoming)
    assert merged["tool"].get("is_error") is not True, (
        "a different-id incoming failure must not be copied onto the existing row"
    )
    assert merged.get("status") != "error"
    assert merged["tool"].get("duration") == "1.2s", (
        "the name+invocation fallback must still copy presentation keys"
    )


def test_hydration_same_id_still_upgrades_error(merge_driver):
    """Control (finding 3): when the two rows share the id the error upgrade
    still runs (round-3 fixing behaviour must not regress)."""
    existing = {
        "role": "tool",
        "tool_call_id": "call_1",
        "tool": {"id": "call_1", "name": "terminal", "is_error": False, "snippet": "[exit 0]"},
        "payload": {"name": "terminal", "is_error": False, "snippet": "[exit 0]"},
    }
    incoming = {
        "role": "tool",
        "tool_call_id": "call_1",
        "tool": {"id": "call_1", "name": "terminal", "is_error": True, "snippet": "[exit 3]"},
        "payload": {"name": "terminal", "is_error": True, "snippet": "[exit 3]"},
    }
    merged = merge_driver(existing, incoming)
    assert merged["tool"].get("is_error") is True
    assert merged.get("status") == "error"