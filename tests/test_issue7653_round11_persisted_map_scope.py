"""#7653 re-gate round 11 — the persisted verdict map stops leaking across occurrences.

The 10/10 re-gate left finding 3 open with an exact fix::

    static/sessions.js:3789 and :3799 — Flat tid-wide failures contaminate
    later successful reuse. Reload-then-reuse, unowned summaries, and
    older-page prepend reproduce [true,true]; the base preserves
    [true,false]. Exact fix: require a verified owning assistant index for
    every verdict, skip unowned entries, rebuild the map at settlement, and
    merge paging summaries in combined-transcript coordinates.

``_syncToolCallsForLoadedMessages`` builds ``S._settledToolIsErrorByTid``
from the settled summary. Three defects let a failure of ONE occurrence
paint a DIFFERENT occurrence of the same reused id:

1. an entry with no ``assistant_msg_idx`` still writes a FLAT ``true``
   (``:3789``), so an unowned summary contaminates every later occurrence
   of that tid — including a successful reuse in a later turn;
2. the map is only rebuilt when the whole function runs, so a settlement
   that arrives through a path which skips the rebuild keeps the previous
   (stale) verdicts;
3. paging (``_loadOlderMessages``) merges an OLDER page's summary on top
   of a map built for the newer transcript, with no combined-transcript
   coordinates, so the two transcripts' owner indices are compared as if
   they were the same array.

These tests drive the REAL ``_syncToolCallsForLoadedMessages`` out of
``static/sessions.js`` in node (the round-10 paradigm) and pin:

- an unowned reused-id entry writes NO flat verdict;
- a reused-id verdict is scoped to its owning assistant index;
- the map is rebuilt from scratch on every invocation (a settlement that
  drops a failure clears it);
- paging merges the older page's verdicts in combined-transcript coordinates
  (the server rebases the response; the prepend path merges the RETAINED
  summaries with their owners shifted by the page length), so a newer
  successful occurrence is not repainted by an older page's failure.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
NODE = shutil.which("node")


def _read(relpath: str) -> str:
    return (REPO_ROOT / relpath).read_text(encoding="utf-8")


def _function_block(src: str, header: str) -> str:
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


def _run_node(script: str, payload: dict) -> dict:
    assert NODE, "node not on PATH"
    harness = (
        "var PAYLOAD = JSON.parse(require('fs').readFileSync(0, 'utf8') || '{}');\n"
        + script
    )
    result = subprocess.run(
        [NODE, "-e", harness],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, f"node failed:\n{result.stderr[:800]}"
    return json.loads(result.stdout)


def _producer_body() -> str:
    # #7653 round 14: the map builder is a separate module-level function the
    # sync producer calls — extract it too or the harness throws
    # ReferenceError.
    return (
        _function_block(_read("static/sessions.js"), "function _buildSettledToolIsErrorMap(")
        + "\n"
        + _function_block(_read("static/sessions.js"), "function _syncToolCallsForLoadedMessages(")
        + "\nconst _mkS=()=>({busy:false,activeStreamId:null,session:null,"
          "toolCalls:[],_settledToolIsErrorByTid:null});"
    )


def _build_map(session_tool_calls, messages=None, owner_offset=0):
    """Run the REAL producer and return the verdict map it built."""
    driver = _producer_body() + """
const S=_mkS();
_syncToolCallsForLoadedMessages(PAYLOAD.messages||[], PAYLOAD.sessionToolCalls, 'R11', PAYLOAD.ownerOffset||0);
process.stdout.write(JSON.stringify(S._settledToolIsErrorByTid||null));
"""
    return _run_node(
        driver,
        {
            "sessionToolCalls": session_tool_calls,
            "messages": messages or [],
            "ownerOffset": owner_offset,
        },
    )


# The settled summary for a reused id: turn 1's call_0 succeeded (owner 1),
# turn 2's call_0 failed (owner 4).
_REUSED = [
    {"name": "terminal", "tid": "call_0", "is_error": False,
     "assistant_msg_idx": 1, "snippet": "ok"},
    {"name": "terminal", "tid": "call_0", "is_error": True,
     "assistant_msg_idx": 4, "snippet": "boom"},
]


# ---------------------------------------------------------------------------
# 1. An unowned reused-id entry must not write a flat verdict.
# ---------------------------------------------------------------------------


def test_unowned_reused_id_entry_writes_no_flat_verdict():
    """A reused-id failure with no owner must not paint the tid wide.

    The reviewer's "unowned summaries" case: the tid appears more than once
    in the summary (so it is a REUSED id), one occurrence failed, and the
    failing entry carries no ``assistant_msg_idx`` — nothing proves which
    row it belongs to. Writing a flat ``true`` then repaints every other
    occurrence of that tid, including a successful reuse in a later turn.
    """
    out = _build_map([
        {"name": "terminal", "tid": "call_0", "is_error": False,
         "assistant_msg_idx": 1, "snippet": "ok"},
        {"name": "terminal", "tid": "call_0", "is_error": True,
         "snippet": "boom"},  # reused id, no assistant_msg_idx
    ])
    assert out is None or out.get("call_0") is not True, (
        "an unowned reused-id failure must not become a flat tid-wide verdict"
    )


def test_unique_id_failure_is_owner_scoped():
    """#7653 round 14 contract change: a unique failed id is owner-scoped now,
    not flat. The 10-19 review ordered no flat entries, singletons included —
    a flat ``true`` cannot distinguish two occurrences of a reused id, which
    is the reload-then-reuse / older-page contamination. The render consumers
    resolve the owner through the group/payload fallback chain, so the card
    still renders red (see test_reused_id_failure_is_scoped_to_its_owner and
    the round-14 end-to-end consumer tests)."""
    out = _build_map([
        {"name": "terminal", "tid": "call_a", "is_error": True,
         "assistant_msg_idx": 2, "snippet": "boom"},
    ])
    entry = out.get("call_a") if out else None
    assert entry is not None and entry is not True, (
        "a unique failed id must be owner-scoped, never flat"
    )
    assert isinstance(entry, dict) and entry.get("is_error") is True, entry
    assert entry.get("assistant_msg_idx") == 2, entry


def test_reused_id_failure_is_scoped_to_its_owner():
    """Control: an owned reused-id failure stays scoped, not flat."""
    out = _build_map(_REUSED)
    assert out is not None, "the reused-id failure must be recorded"
    assert out.get("call_0") is not True, (
        "a reused id must never collapse to a flat tid-wide verdict"
    )
    entry = out.get("call_0")
    assert isinstance(entry, dict), f"the entry must be owner-scoped: {entry!r}"
    assert entry.get("is_error") is True
    owners = set(entry.get("occurrences") or {})
    owners.add(entry.get("assistant_msg_idx"))
    assert 4 in owners, f"the failed owner must be recorded: {entry!r}"
    assert 1 not in owners, (
        "the SUCCESSFUL owner must not be marked failed: the later-reuse "
        "contamination the reviewer reproduced"
    )


# ---------------------------------------------------------------------------
# 2. The map is rebuilt at every settlement (no stale verdicts).
# ---------------------------------------------------------------------------


def test_a_settlement_that_drops_the_failure_clears_the_verdict():
    """Reload-then-reuse: the second load's summary has no failure.

    The map is per-session state. If a settlement path skips the rebuild,
    the previous load's failure survives and repaints the new turn's
    successful reuse — the reviewer's ``[true,true]``.
    """
    driver = _producer_body() + """
const S=_mkS();
_syncToolCallsForLoadedMessages(PAYLOAD.messages||[], PAYLOAD.first, 'R11');
const afterFirst=JSON.parse(JSON.stringify(S._settledToolIsErrorByTid||null));
_syncToolCallsForLoadedMessages(PAYLOAD.messages||[], PAYLOAD.second, 'R11');
process.stdout.write(JSON.stringify({afterFirst:afterFirst,
  afterSecond:S._settledToolIsErrorByTid||null}));
"""
    out = _run_node(driver, {
        "messages": [],
        "first": _REUSED,
        # The next settlement of the SAME session: the failing turn is gone
        # from the summary (a reuse that succeeded), so no verdict remains.
        "second": [
            {"name": "terminal", "tid": "call_0", "is_error": False,
             "assistant_msg_idx": 7, "snippet": "ok"},
        ],
    })
    assert out["afterFirst"] is not None, "the first settlement recorded the failure"
    second = out["afterSecond"]
    assert second is None or second.get("call_0") is None or (
        isinstance(second.get("call_0"), dict)
        and not (second["call_0"].get("is_error") is True)
    ), f"the stale failure survived the second settlement: {second!r}"


# ---------------------------------------------------------------------------
# 3. Paging merges in combined-transcript coordinates.
# ---------------------------------------------------------------------------


def test_paging_merge_offsets_the_older_page_owners():
    """The paging verdict map must use combined-transcript coordinates.

    The server rebases ``assistant_msg_idx`` into the returned window, so
    response summaries need no shift in either paging strategy; a prepend
    must merge the RETAINED transcript's verdicts with their owners shifted
    by the page length. The two tests below pin the call site and the
    producer's re-basing behaviour directly; this one keeps the
    region-level anchor so the paging path itself cannot disappear.
    """
    src = _read("static/sessions.js")
    assert "_loadOlderMessages" in src, "the paging path was not found"
    region_start = src.index("function _loadOlderMessages(")
    region = src[region_start:region_start + 20000]
    assert "_syncToolCallsForLoadedMessages(" in region, (
        "the paging merge must rebuild the verdict map in combined-transcript "
        "coordinates (the reviewer's finding 3)"
    )


def test_the_paging_call_passes_the_page_length_and_the_prepend_merge_flag():
    """The paging call site must hand the producer the page length and the
    prepend-only retained-merge gate."""
    src = _read("static/sessions.js")
    region_start = src.index("function _loadOlderMessages(")
    region = src[region_start:region_start + 20000]
    assert "_syncToolCallsForLoadedMessages(" in region, (
        "the paging path must rebuild the verdict map"
    )
    call_start = region.index("_syncToolCallsForLoadedMessages(", region.index("S.messages = nextMessages"))
    call = region[call_start:call_start + 300]
    assert "olderMsgs.length" in call, (
        "the paging rebuild must pass the page length as the retained-owner "
        f"shift; got: {call[:200]!r}"
    )
    assert "!tailMatches" in call, (
        "the retained-verdict merge must be gated on the PREPEND path only — "
        "the cumulative window response already covers every summary"
    )


def test_the_producer_keeps_response_owner_indices_unchanged():
    """Behavioural half: the producer does NOT re-shift a paged summary.

    The server already rebases ``assistant_msg_idx`` into the returned window
    (``_tool_calls_for_message_window``, api/routes.py), so the round-11
    ``ownerOffset`` that shifted every response owner double-shifted it: the
    failed owner 4 with a 3-row offset moved onto 7 (and the reviewer's
    cumulative repro moved failed owner 1 onto successful owner 4). The
    response index must stay exactly as delivered; ``ownerOffset`` now
    applies only to the RETAINED transcript's merged verdicts.
    """
    out = _build_map(
        [
            {"name": "terminal", "tid": "call_0", "is_error": False,
             "assistant_msg_idx": 1, "snippet": "ok"},
            {"name": "terminal", "tid": "call_0", "is_error": True,
             "assistant_msg_idx": 4, "snippet": "boom"},
        ],
        owner_offset=3,
    )
    assert out is not None, "the paged failure must be recorded"
    entry = out.get("call_0")
    assert isinstance(entry, dict), f"the entry must be owner-scoped: {entry!r}"
    owners = set(entry.get("occurrences") or {})
    owners.add(entry.get("assistant_msg_idx"))
    assert 4 in owners, (
        "the response summary's owner is already window-relative and must not "
        f"be re-shifted: {entry!r}"
    )
    assert 7 not in owners, (
        "the round-11 offset moved the failed owner 4 onto owner 7 of the "
        "combined array"
    )
