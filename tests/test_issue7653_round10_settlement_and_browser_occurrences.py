"""#7653 round 10 regression — the two CONSUMERS stop collapsing occurrences.

The 10/06 re-gate at ``b051498ea`` (the re-arm commit) left two SILENT
findings open. Both are on the consumer side of the re-armed live mirror,
and both pair ONE occurrence of a reused tool id with a verdict that
belongs to a DIFFERENT occurrence:

**Finding 1 (SILENT) — server settlement.**
``_live_tool_calls_by_tid`` (``api/streaming.py``) indexed the mirror with
``setdefault``, so the first row won per tid. The round-9 rule then bound
that single verdict to the LAST owner, which is its k=1 case. Fed this
head's TWO-row mirror (``call_0`` fails, ``call_0`` succeeds),
``_extract_tool_calls_from_messages`` returned ``[(0, False), (2, True)]``
— the reverse of the correct verdicts and the same result the pre-fix
single-row mirror produced. Master's settlement carries no ``is_error`` at
all, so the reversed verdict was a live regression against master (master
never paints the success red).

Fix: return a LIST per tid in mirror order and pair the k-th assistant
owner of a reused tid with the k-th live row. When the mirror is shorter
than the owner list, align from the end and leave the earlier occurrences
unknown so the prior-turn merge applies.

**Finding 2 (SILENT) — the browser live settle.**
``liveMetadataByTid`` (``static/ui.js``) was first-wins too, and the
one-way upgrade copied the first row's ``is_error`` onto both occurrences:
the live-settle path yields ``[[0,true],[2,true]]``.
``_mergeSettledToolCallsWithLiveMetadata`` (``static/messages.js``) built
the same first-wins map.

Fix: keep an array per tid and take the next entry not already used
(``usedLiveToolMetadata`` / ``used``); skip the transfer when ownership is
ambiguous rather than guessing.

Per the reviewer's request this file adds:
- a settlement test asserting ``[(0, True), (2, False)]`` from the
  two-row mirror;
- a browser-slice test asserting ``[[0,true],[2,false]]`` on the
  live-settle path (``copyLiveToolMetadata`` + the real
  ``_mergeSettledToolCallsWithLiveMetadata``);
- a control that the single-row mirror keeps the round-9 pairing (the
  last owner takes the only live row).

Both new tests fail on this head before the fix and pass after it.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
NODE = shutil.which("node")

# ---------------------------------------------------------------------------
# SERVER: _extract_tool_calls_from_messages driven in a subprocess, same as
# the round-6..9 fixtures (api.streaming pulls heavy deps).
# ---------------------------------------------------------------------------

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
    """Collapse the settled rows to (assistant_msg_idx, is_error) in order."""
    return [(row["assistant_msg_idx"], row["is_error"]) for row in out]


def _assistant_tool_call(tid: str, name: str = "terminal") -> dict:
    """An assistant row emitting ``tid`` (Anthropic content form, the same
    normalisation path the round-9/10 tests use)."""
    return {
        "role": "assistant",
        "content": [
            {"type": "tool_use", "id": tid, "name": name, "input": {"command": "ls"}},
        ],
    }


def _tool_result(tid: str, raw: str) -> dict:
    return {"role": "tool", "tool_call_id": tid, "content": raw}


# The re-arm head's mirror: two occurrences of the SAME id, in production
# order. Occurrence 0 fails, occurrence 1 succeeds.
_TWO_ROW_MIRROR = [
    {"name": "terminal", "tid": "call_0", "done": True, "is_error": True,
     "snippet": "boom", "args": {"command": "pwd"}},
    {"name": "terminal", "tid": "call_0", "done": True, "is_error": False,
     "snippet": "ok", "args": {"command": "ls"}},
]

# The two assistant owners that emit the reused id, in the same order as
# the mirror rows (turn 1 then turn 2), plus a trailing tool result each.
_TWO_OCCURRENCE_MESSAGES = [
    {"role": "user", "content": "first"},
    _assistant_tool_call("call_0"),
    _tool_result("call_0", '{"exit_code": 2, "error": "boom"}'),
    {"role": "user", "content": "second"},
    _assistant_tool_call("call_0"),
    _tool_result("call_0", '{"exit_code": 0, "error": null}'),
]


# ---------------------------------------------------------------------------
# Finding 1 — settlement pairs the k-th owner with the k-th live row
# ---------------------------------------------------------------------------


def test_two_row_mirror_settles_each_occurrence_with_its_own_verdict():
    """Bug repro (finding 1): the two-row live mirror must settle to the
    reviewer's expected ``[(0, True), (2, False)]``.

    On this head before the fix the mirror was collapsed with
    ``setdefault``, so BOTH occurrences took the FIRST row's verdict and
    the pairs came out ``[(0, False), (2, True)]`` — reversed. Master
    carries no ``is_error`` at all (``[(0, None), (2, None)]``), so the
    reversed order was a regression against master too.
    """
    out = _call_extract(
        _TWO_OCCURRENCE_MESSAGES,
        live_tool_calls=_TWO_ROW_MIRROR,
    )
    assert len(out) == 2, f"both occurrences must settle: {out}"
    assert _rows_in_order(out) == [(1, True), (4, False)], (
        "each occurrence must carry its OWN live verdict: the failing "
        "call_0 stays failed and the succeeding call_0 stays successful"
    )


def test_two_row_mirror_verdicts_are_not_collapsed_by_tid():
    """Shape assertion for the same fix: the index now returns a LIST per
    tid in mirror order, so the pairing is positional and the two
    occurrences cannot be settled from the same row.

    Pinned directly on the helper to keep the invariant visible if the
    settlement loop's pairing is ever refactored.
    """
    driver = (
        "import sys, json;\n"
        "sys.path.insert(0, %r);\n"
        "from api.streaming import _live_tool_calls_by_tid;\n"
        "data = json.loads(sys.stdin.read());\n"
        "idx = _live_tool_calls_by_tid(data['live_tool_calls']);\n"
        "out = {k: [tc.get('is_error') for tc in v] for k, v in idx.items()};\n"
        "sys.stdout.write(json.dumps(out));\n"
    ) % str(REPO_ROOT)
    proc = subprocess.run(
        [sys.executable, "-c", driver],
        input=json.dumps({"live_tool_calls": _TWO_ROW_MIRROR}),
        capture_output=True,
        text=True,
        timeout=90,
    )
    assert proc.returncode == 0, f"driver failed:\n{proc.stderr[:500]}"
    assert json.loads(proc.stdout) == {"call_0": [True, False]}, (
        "the mirror index must keep every row of a reused id in order, "
        "not collapse to the first one"
    )


def test_single_row_mirror_still_pairs_with_the_last_owner():
    """Control (round 9 must not move): a one-row mirror still hands its
    verdict to the LAST owner of a reused tid — the k=1 case of the new
    pairing, and the behaviour round 9 pinned.
    """
    out = _call_extract(
        [
            {"role": "user", "content": "first"},
            _assistant_tool_call("call_0"),
            _tool_result("call_0", '{"exit_code": 0, "error": null}'),
            {"role": "user", "content": "second"},
            _assistant_tool_call("call_0"),
            _tool_result("call_0", '{"exit_code": 2, "error": "boom"}'),
        ],
        live_tool_calls=[
            {"name": "terminal", "tid": "call_0", "done": True,
             "is_error": True, "snippet": "boom"},
        ],
    )
    assert _rows_in_order(out) == [(1, False), (4, True)], (
        "the last owner must still take the single live row (round-9 rule)"
    )


def test_short_mirror_leaves_earlier_owners_unknown_for_prior_merge():
    """The spec's short-mirror rule: with fewer live rows than owners,
    alignment is from the END, so the earliest occurrence stays unknown and
    the prior-turn merge below can supply its verdict."""
    prior = [
        {"name": "terminal", "tid": "call_0", "assistant_msg_idx": 1,
         "is_error": True, "snippet": "boom"},
    ]
    out = _call_extract(
        _TWO_OCCURRENCE_MESSAGES,
        live_tool_calls=[
            {"name": "terminal", "tid": "call_0", "done": True,
             "is_error": False, "snippet": "ok"},
        ],
        prior_tool_calls=prior,
    )
    # The single live row pairs with the LAST owner (4), which is already
    # unknown-free; the earlier owner (1) must keep the prior failure
    # rather than silently defaulting to success.
    assert _rows_in_order(out) == [(1, True), (4, False)], (
        "the short mirror must not collapse the earlier occurrence onto "
        "the current turn's live row"
    )


# ---------------------------------------------------------------------------
# Finding 2 — the browser live settle assigns one live row per occurrence
# ---------------------------------------------------------------------------


def _function_block(src: str, header: str) -> str:
    """Lift a top-level ``{ ... }`` block starting at ``header`` verbatim."""
    start = src.find(header)
    assert start != -1, f"{header!r} not found"
    brace = src.find("{", start)
    assert brace != -1, f"no body for {header!r}"
    depth = 0
    for idx in range(brace, len(src)):
        if src[idx] == "{":
            depth += 1
        elif src[idx] == "}":
            depth -= 1
            if depth == 0:
                return src[start:idx + 1]
    raise AssertionError(f"{header!r} did not close")


def _ui_block() -> str:
    """Lift the production ``liveToolMetadata`` declaration, the per-tid
    map BUILD and ``copyLiveToolMetadata`` out of ``renderMessages`` in
    static/ui.js, verbatim."""
    src = (REPO_ROOT / "static" / "ui.js").read_text(encoding="utf-8")
    decl = "const liveToolMetadata="
    start = src.find(decl)
    assert start != -1, "liveToolMetadata declaration not found in ui.js"
    line_start = src.rfind("\n", 0, start) + 1
    closure = _function_block(src, "const copyLiveToolMetadata=")
    return src[line_start:].split(closure, 1)[0] + closure


def _live_settle_driver(rows: list[dict], live: list[dict]) -> list[dict]:
    """Run the REAL ``copyLiveToolMetadata`` slice over the re-armed
    two-row mirror, mirroring the live-settle path in renderMessages."""
    assert NODE, "node not on PATH"
    script = f"""
var PAYLOAD = JSON.parse(require('fs').readFileSync(0, 'utf8') || '{{}}');
const S = {{ _settledLiveToolMetadata: PAYLOAD.live }};
{_ui_block()}
const rows = PAYLOAD.rows.map(function(r) {{
  return copyLiveToolMetadata(Object.assign({{}}, r), r.name, r.tid || '');
}});
process.stdout.write(JSON.stringify(rows));
"""
    result = subprocess.run(
        [NODE, "-e", script],
        input=json.dumps({"rows": rows, "live": live}),
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, f"node failed:\n{result.stderr}"
    return json.loads(result.stdout)


def test_browser_live_settle_keeps_each_occurrence_verdict():
    """Bug repro (finding 2): the live-settle path must render the
    re-armed two-row mirror as ``[[0,true],[2,false]]``.

    On this head before the fix ``liveMetadataByTid`` was first-wins, so
    both rows were handed the FIRST live entry and the one-way upgrade
    painted both failed: ``[[0,true],[2,true]]``.
    """
    rows = [
        {"name": "terminal", "tid": "call_0", "done": True},
        {"name": "terminal", "tid": "call_0", "done": True},
    ]
    # The re-armed mirror, in production order: fail then succeed.
    live = [
        {"name": "terminal", "tid": "call_0", "done": True, "is_error": True,
         "snippet": "boom"},
        {"name": "terminal", "tid": "call_0", "done": True, "is_error": False,
         "snippet": "ok"},
    ]
    out = _live_settle_driver(rows, live)
    assert out[0].get("is_error") is True, (
        "the failing call_0 occurrence must render failed"
    )
    assert out[1].get("is_error") is not True, (
        "the succeeding call_0 occurrence must NOT inherit the first "
        "occurrence's failure (bug: first-wins per-tid map + one-way "
        "upgrade painted both cards red)"
    )


def test_browser_live_settle_ambiguous_ownership_is_not_guessed():
    """The spec's skip-on-ambiguity rule: more rendered occurrences than
    the mirror has rows means the third occurrence's owner cannot be
    identified, so no verdict is transferred (rather than re-pairing a
    consumed row and guessing)."""
    rows = [
        {"name": "terminal", "tid": "call_0", "done": True},
        {"name": "terminal", "tid": "call_0", "done": True},
        {"name": "terminal", "tid": "call_0", "done": True},
    ]
    live = [
        {"name": "terminal", "tid": "call_0", "done": True, "is_error": True,
         "snippet": "boom"},
        {"name": "terminal", "tid": "call_0", "done": True, "is_error": False,
         "snippet": "ok"},
    ]
    out = _live_settle_driver(rows, live)
    assert out[0].get("is_error") is True
    assert out[1].get("is_error") is not True
    # The third occurrence has no unused live row: nothing is transferred,
    # so it stays green instead of inheriting an already-used verdict.
    assert out[2].get("is_error") is not True, (
        "an ambiguous extra occurrence must not re-pair a consumed live row"
    )


def test_browser_live_settle_unique_id_still_upgrades():
    """Control: a genuinely unique failed id still renders red through the
    same path — rounds 3-8 behaviour must not regress."""
    rows = [{"name": "terminal", "tid": "call_a", "done": True}]
    live = [
        {"name": "terminal", "tid": "call_a", "done": True, "is_error": True,
         "snippet": "boom"},
    ]
    out = _live_settle_driver(rows, live)
    assert out[0].get("is_error") is True


def _messages_merge_slice(rows: list[dict], live: list[dict]) -> list[dict]:
    """Run the REAL ``_mergeSettledToolCallsWithLiveMetadata``
    (static/messages.js) over the re-armed two-row mirror."""
    assert NODE, "node not on PATH"
    fn = _function_block(
        (REPO_ROOT / "static" / "messages.js").read_text(encoding="utf-8"),
        "function _mergeSettledToolCallsWithLiveMetadata(",
    )
    script = f"""
var PAYLOAD = JSON.parse(require('fs').readFileSync(0, 'utf8') || '{{}}');
const S = {{ toolCalls: PAYLOAD.live }};
{fn}
process.stdout.write(JSON.stringify(
  _mergeSettledToolCallsWithLiveMetadata(PAYLOAD.rawCalls)));
"""
    result = subprocess.run(
        [NODE, "-e", script],
        input=json.dumps({"rawCalls": rows, "live": live}),
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, f"node failed:\n{result.stderr}"
    return json.loads(result.stdout)


def test_messages_merge_slice_keeps_each_occurrence_verdict():
    """Bug repro (finding 2, messages.js half): the same first-wins map
    exists in ``_mergeSettledToolCallsWithLiveMetadata``; the re-armed
    two-row mirror must not paint both persisted rows failed."""
    raw = [
        {"name": "terminal", "tid": "call_0"},
        {"name": "terminal", "tid": "call_0"},
    ]
    live = [
        {"name": "terminal", "tid": "call_0", "is_error": True},
        {"name": "terminal", "tid": "call_0", "is_error": False},
    ]
    out = _messages_merge_slice(raw, live)
    assert out[0].get("is_error") is True, "the failing occurrence stays failed"
    assert out[1].get("is_error") is not True, (
        "the succeeding occurrence must not inherit the first row's failure"
    )


def test_messages_merge_slice_unique_id_still_upgrades():
    """Control: the id-map hit still upgrades a unique failed id, and the
    name fallback still restores the presentation keys (round 8)."""
    raw = [
        {"tid": "call_ok", "name": "terminal", "duration": None},
        {"tid": "call_old", "name": "terminal", "duration": None},
    ]
    live = [
        {"tid": "call_ok", "name": "terminal", "is_error": True, "duration": 2.5},
        {"tid": "call_new", "name": "terminal", "is_error": True, "duration": 9},
    ]
    out = _messages_merge_slice(raw, live)
    assert out[0].get("is_error") is True, "the id-map hit must still upgrade"
    assert out[0].get("duration") == 2.5, "the id-map hit restores duration"
    assert not out[1].get("is_error"), "the name-matched row must not inherit"
    assert out[1].get("duration") == 9, (
        "the name fallback must keep restoring duration / started_at"
    )


# ---------------------------------------------------------------------------
# Finding 3 — the re-arm branch writes args into the shared mirror
# ---------------------------------------------------------------------------


def test_rearm_branch_writes_args_into_both_mirrors():
    """Bug repro (finding 3): the shared-mirror row written by the re-arm
    branch must carry ``args`` exactly like the fresh branch, because
    ``cancel_stream`` snapshots it into ``_build_partial_message`` ->
    ``_partial_tool_calls`` (restored by static/ui.js) and the
    partial-marker signature digests ``args``.

    Pinned as a source-shape assertion on both mirror writes inside the
    re-arm branch, since the branch lives ~1500 lines inside
    ``_run_agent_streaming`` and cannot be driven in isolation.
    """
    src = (REPO_ROOT / "api" / "streaming.py").read_text(encoding="utf-8")
    start = src.find("elif _start_verdict == 'rearm':")
    assert start != -1, "re-arm branch not found in api/streaming.py"
    # Take the branch body through the end of the SSE put() it emits.
    put_pos = src.find("put('tool', {", start)
    assert put_pos != -1, "re-arm branch has no tool.started put()"
    body = src[start:src.find("\n", src.find("'tid': tool_call_id,", put_pos))]
    assert body.count("'args': args if isinstance(args, dict) else {},") >= 2, (
        "both the per-stream and the shared mirror row written by the "
        "re-arm branch must carry args (the second occurrence otherwise "
        "restores with EMPTY args on cancel, and the partial-marker "
        "signature diverges from the fresh branch's)"
    )


def test_partial_signature_digests_args_of_rearmed_rows():
    """Control for finding 3: the partial-marker signature really does
    read ``args``, which is why the missing key changed dedupe."""
    src = (REPO_ROOT / "api" / "streaming.py").read_text(encoding="utf-8")
    sig = _function_block(src, "def _partial_message_signature(")
    assert "json.dumps(" in sig and "tool_call.get('args')" in sig, (
        "_partial_message_signature must digest the tool args so two "
        "re-arm-shaped partials can be recognised as the same marker"
    )
