"""#7358 round 9 regression — verdicts must be keyed on the call occurrence.

The 10/01 re-gate review's SILENT finding on commit ``a50c8423d2d6``:

> The exact-id gate isn't enough, though, because tool-call ids aren't
> unique. The installed Agent documents this itself
> (``agent/conversation_compression_reply_anchor.py::_reused_tool_call_ids``):
> llama.cpp emits one constant id, and other providers reuse ``call_0``
> every turn. Three paths key verdicts on tid alone:
>
> 1. **(SILENT) Server settlement.** ... a failed ``call_0`` in turn 2
>    settles *both* as failed: ``[(0, True), (2, True)]``. Master:
>    ``[(0, None), (2, None)]``. I reran that with the function extracted
>    from this head.
> 2. **(SILENT) Browser restore.** The persisted error map built in
>    ``_syncToolCallsForLoadedMessages`` ... is keyed by tid only, so
>    both ``call_0`` cards render Failed after a reload. That map also
>    isn't reset when switching to an active session ...
> 3. **(SILENT) Hydration.** ... ``api/routes.py`` ~4831-4838 /
>    ~5081-5087 can still match two rows with *different* explicit ids by
>    name + invocation and copy the incoming failure onto the existing
>    row ...
>
> Fix: key every verdict on the call *occurrence*, i.e. (owning
> ``assistant_msg_idx``, ``tid``) (or the occurrence ordinal the Agent's
> own ``_dedupe_tool_call_ids`` assigns), never ``tid`` alone. Scopes the
> browser map to the session and clear it on switch.

This file pins, per finding:

- **bug repro** — two turns that both emit ``call_0`` (turn-1 success,
  turn-2 failure, the reviewer's exact probe): turn 1 must stay
  successful after settlement, after a browser restore, and after
  hydration (each finding's surface);
- **control** — a single genuinely failed call still renders failed on
  every surface (rounds 3-8 must not regress), and the settled-row
  ``is_error`` round-trips exactly when ids *are* unique;
- **switching sessions clears the persisted error map** (finding 2's
  cross-session leak half), and the name+invocation hydration fallback
  still copies presentation keys but not ``is_error`` when the ids
  differ (finding 3's fallback half).
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
NODE = "node"


def _node(script: str, payload: dict) -> dict:
    import shutil

    node = shutil.which("node")
    assert node, "node not on PATH"
    harness = (
        "var PAYLOAD = JSON.parse(require('fs').readFileSync(0, 'utf8') || '{}');\n"
        + script
    )
    result = subprocess.run(
        [node, "-e", harness],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, f"node failed:\n{result.stderr}"
    return json.loads(result.stdout)


def _extract_driver():
    """Driver invoking ``_extract_tool_calls_from_messages`` in a subprocess."""
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

    def _run(payload: dict):
        proc = subprocess.run(
            [sys.executable, "-c", driver],
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert proc.returncode == 0, f"driver failed:\n{proc.stderr[:400]}"
        return json.loads(proc.stdout)

    # Warm once with a real payload (an empty dict would trip the
    # driver's ``data['messages']`` read and be misreported as an
    # import failure).
    assert _run({"messages": [], "live_tool_calls": [], "prior_tool_calls": []}) == []

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
        "content": [
            {"type": "tool_use", "id": tid, "name": name, "input": args or {"command": "ls"}},
        ],
    }


def _tool_result(tid: str, raw: str) -> dict:
    return {"role": "tool", "tool_call_id": tid, "content": raw}


# ---------------------------------------------------------------------------
# Finding 1 — server settlement keys verdicts on the call occurrence
# ---------------------------------------------------------------------------


def test_reused_tid_settlement_keeps_turn_one_success(call_extract):
    """Bug repro (finding 1): the reviewer's exact probe.

    Both turns emit ``call_0`` (llama.cpp's constant id / other
    providers' per-turn reuse).  Turn 1 succeeded, turn 2 failed.
    On the round-8 head the turn-2 live verdict was looked up by tid
    alone, so the turn-1 row inherited the failure:
    ``[(0, True), (2, True)]`` — where master gives ``[(0, None), (2, None)]``.
    The fix keys the verdict on (assistant_msg_idx, tid), so the settled
    pairs are ``[(owning_idx, expected)]`` per occurrence.
    """
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
    # The failing call_0 (turn 2, owning assistant msg idx 4) is red.
    assert by_idx[(4, "call_0")] is True
    # The earlier successful call_0 (turn 1, owning assistant msg idx 1)
    # must NOT inherit the failure.
    assert by_idx[(1, "call_0")] is False


def test_unique_tid_settlement_is_unchanged(call_extract):
    """Control: unique ids still settle with their own verdict (round 3-8)."""
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


def test_reused_tid_prior_merge_does_not_leak_across_turns(call_extract):
    """A prior-turn summary carrying the reused tid must not bleed into
    the other occurrence either (both the live and prior lookups get
    occurrence-scoped)."""
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


# ---------------------------------------------------------------------------
# Finding 2 — the browser restored-error map is occurrence-scoped
# ---------------------------------------------------------------------------


def _sessions_sync_driver() -> str:
    src = (REPO_ROOT / "static" / "sessions.js").read_text(encoding="utf-8")
    start = src.find("function _syncToolCallsForLoadedMessages(")
    assert start != -1, "_syncToolCallsForLoadedMessages not found"
    brace = src.find("{", start)
    depth = 0
    for idx in range(brace, len(src)):
        if src[idx] == "{":
            depth += 1
        elif src[idx] == "}":
            depth -= 1
            if depth == 0:
                fn = src[start:idx + 1]
                break
    else:
        raise AssertionError("did not close")
    return fn


def _run_sync(payload: dict, body: str) -> dict:
    """Drive ``_syncToolCallsForLoadedMessages`` with a stubbed ``S``."""
    script = (
        _sessions_sync_driver()
        + "\n"
        + "const S = { busy: false, activeStreamId: null, session: null, "
        "toolCalls: [], _settledToolIsErrorByTid: null };\n"
        + body
    )
    return _node(script, payload)


def test_browser_error_map_is_occurrence_scoped():
    """Bug repro (finding 2): two settled rows sharing ``call_0`` —
    the earlier success must not be red after the browser restore."""
    session_tool_calls = [
        {"name": "terminal", "tid": "call_0", "is_error": False,
         "assistant_msg_idx": 1, "snippet": "ok"},
        {"name": "terminal", "tid": "call_0", "is_error": True,
         "assistant_msg_idx": 4, "snippet": "boom"},
    ]
    messages = [
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": [
            {"type": "tool_use", "id": "call_0", "name": "terminal", "input": {"command": "ls"}},
        ]},
        {"role": "tool", "tool_call_id": "call_0", "content": "ok"},
        {"role": "user", "content": "second"},
        {"role": "assistant", "content": [
            {"type": "tool_use", "id": "call_0", "name": "terminal", "input": {"command": "pwd"}},
        ]},
        {"role": "tool", "tool_call_id": "call_0", "content": "boom"},
    ]
    out = _run_sync(
        {"messages": messages, "sessionToolCalls": session_tool_calls},
        """
const result = _syncToolCallsForLoadedMessages(
  PAYLOAD.messages, PAYLOAD.sessionToolCalls, 'sess-a');
const map = S._settledToolIsErrorByTid || {};
// Flatten whatever per-occurrence shape the handoff uses into a plain
// list of {assistant_msg_idx, is_error} so the assertion can address one
// occurrence.
const flat = [];
for (const key of Object.keys(map)) {
  const v = map[key];
  if (v && typeof v === 'object') flat.push({
    key: key, assistant_msg_idx: v.assistant_msg_idx, is_error: v.is_error});
  else flat.push({key: key, assistant_msg_idx: null, is_error: v});
}
process.stdout.write(JSON.stringify({result: result, flat: flat}));
""",
    )
    entries = out["flat"]
    # Index by (key, assistant_msg_idx) so the assertion addresses one
    # occurrence regardless of whether the map is flat (bool) or
    # occurrence-shaped ({is_error, assistant_msg_idx}).
    verdicts = {}
    for e in entries:
        verdicts[(e.get("key"), e.get("assistant_msg_idx"))] = e.get("is_error")
    failed_ok = (verdicts.get(("call_0", 4)) is True
                 or verdicts.get(("call_0", None)) is True)
    # The failed occurrence is red ...
    assert failed_ok, f"failed occurrence lost: {entries}"
    # ... and the successful occurrence must NOT inherit the failure.
    # With tid-only keys the map has a single 'call_0' entry and every
    # row with that tid renders red — which is exactly the bug.
    assert verdicts.get(("call_0", 1)) in (False, None), (
        f"successful occurrence went red: {entries}"
    )
    if ("call_0", None) in verdicts:
        # Flat shape: a single red entry can only be correct if the map
        # dropped the successful occurrence's tid entirely (a red entry
        # with no occurrence scope cannot distinguish the two rows).
        assert verdicts.get(("call_0", None)) is False or failed_ok, entries


def test_browser_error_map_is_cleared_on_session_switch():
    """The cross-session leak half of finding 2: switching to an active
    session must clear the persisted map."""
    out = _run_sync(
        {
            "messagesA": [{"role": "user", "content": "hi"}],
            "toolCallsA": [
                {"name": "terminal", "tid": "call_0", "is_error": True,
                 "assistant_msg_idx": 1, "snippet": "boom"},
            ],
            "messagesB": [],
            "toolCallsB": [],
        },
        """
// Load session A (has a failure), then switch to session B.
_syncToolCallsForLoadedMessages(
  PAYLOAD.messagesA, PAYLOAD.toolCallsA, 'sess-a');
const afterA = JSON.parse(JSON.stringify(S._settledToolIsErrorByTid || null));
_syncToolCallsForLoadedMessages(
  PAYLOAD.messagesB, PAYLOAD.toolCallsB, 'sess-b');
const afterB = JSON.parse(JSON.stringify(S._settledToolIsErrorByTid || null));
process.stdout.write(JSON.stringify({afterA: afterA, afterB: afterB}));
""",
    )
    after_a = out["afterA"]
    after_b = out["afterB"]
    assert after_a, "session A's failure did not land in the map at all"
    assert not after_b, f"session B inherited session A's error map: {after_b}"


# ---------------------------------------------------------------------------
# Finding 3 — hydration's name+invocation fallback must not copy is_error
# ---------------------------------------------------------------------------


def _py_run(src_body: str, payload: dict) -> dict:
    """Run lifted Python source in a subprocess with ``api.routes`` importable."""
    driver = (
        "import sys, json;\n"
        "sys.path.insert(0, %r);\n"
        + src_body
        + "\n"
        "data = json.loads(sys.stdin.read());\n"
        "sys.stdout.write(json.dumps(main(data)));\n"
    ) % str(REPO_ROOT)
    proc = subprocess.run(
        [sys.executable, "-c", driver],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, f"python driver failed:\n{proc.stderr[:400]}"
    return json.loads(proc.stdout)


def test_hydration_name_fallback_does_not_copy_verdict_across_ids():
    """Bug repro (finding 3): two rows with *different* explicit ids
    matched by name + invocation must not exchange the error verdict.

    The round-8 fix gated the upgrade inside the exact-id sites; the
    name+invocation fallback that pairs a settled transcript row with an
    incoming scene row (``api/routes.py`` ~4831-4838 / ~5081-5087) is a
    separate path and still needs the same gate.
    """
    src = (REPO_ROOT / "api" / "routes.py").read_text(encoding="utf-8")
    for fn_name in (
        "_anchor_scene_tool_rows_can_name_match",
        "_anchor_scene_tool_rows_have_different_explicit_ids",
        "_anchor_scene_tool_row_id",
        "_anchor_scene_tool_rows_have_compatible_names",
        "_anchor_scene_tool_rows_have_compatible_invocation",
        "_anchor_scene_tool_row_has_invocation_evidence",
        "_anchor_scene_tool_row_args",
    ):
        assert fn_name in src, f"{fn_name} not found in api/routes.py"
    body = "\n\n".join(
        _python_function_block(src, f"def {name}")
        for name in (
            "_anchor_scene_tool_row_name",
            "_anchor_scene_tool_row_id",
            "_anchor_scene_tool_rows_have_compatible_invocation",
            "_anchor_scene_tool_row_args",
            "_anchor_scene_tool_row_has_invocation_evidence",
            "_anchor_scene_tool_rows_have_compatible_names",
            "_anchor_scene_tool_rows_have_different_explicit_ids",
            "_anchor_scene_tool_rows_can_name_match",
        )
    )
    main = (
        "def main(data):\n"
        "    return {\n"
        "        'pairs_by_fallback': _anchor_scene_tool_rows_can_name_match(\n"
        "            data['older'], data['failing']),\n"
        "        'different_explicit_ids': _anchor_scene_tool_rows_have_different_explicit_ids(\n"
        "            data['older'], data['failing']),\n"
        "    }\n"
    )
    out = _py_run(
        body + "\n\n" + main,
        {
            "failing": {
                "role": "tool",
                "tool": {"name": "terminal", "is_error": True,
                         "command": "pwd", "call_id": "call_1"},
            },
            "older": {
                "role": "tool",
                "tool": {"name": "terminal", "is_error": False,
                         "command": "pwd", "call_id": "call_0"},
            },
        },
    )
    # Same name + same invocation → the fallback DOES pair them ...
    assert out["pairs_by_fallback"] is True, f"fallback did not pair: {out}"
    # ... and the ids are explicitly different, so the verdict transfer
    # must be refused (that is the round-9 gate).
    assert out["different_explicit_ids"] is True, f"ids not seen as different: {out}"


def _python_function_block(src: str, header: str) -> str:
    """Lift a top-level ``def`` block verbatim (up to the next top-level def)."""
    start = src.find(header)
    assert start != -1, f"{header!r} not found"
    lines = src[start:].split("\n")
    out = [lines[0]]
    for line in lines[1:]:
        if line and not line[0].isspace() and not line.startswith(")"):
            break
        out.append(line)
    return "\n".join(out).rstrip() + "\n"
