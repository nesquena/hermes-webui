"""#7653 re-gate round 14 — the persisted verdict map is owner-scoped, never flat.

The 10-19 re-gate (item 2) found the flat-singleton shape still painting
successful reuses red, in both directions:

- **Reload, then reuse.** Reload ``[call_0 fail@1]`` gives map
  ``{call_0: true}`` (flat — a singleton keeps the rounds 3-8 shape). The
  next turn reuses ``call_0`` successfully (owner 4); after ``done`` the map
  is still the stale flat ``true`` (the done/recovery paths never rebuild
  it), so owner 4 renders failed. Expected ``[(1,true),(4,false)]``.
- **Opposite direction.** The retained page's single ``call_0`` failed (flat
  ``true``); prepending an older page whose single ``call_0`` succeeded
  merges the flat ``true`` back in (the response has no failure entry for a
  success), so the older successful card turns red.
- **Older failed singleton.** The retained page holds fail@1 + ok@4;
  prepending an older page whose only ``call_0`` failed makes the response
  write its own flat ``true`` (singleton in that page), which wins and drops
  the retained scoped entries — all three cards render red, including the
  retained success.

The maintainer's fix shape: **write no flat entries; key every verdict by
``(tid, owner)``, singletons included; decide reuse over the combined
transcript; on prepend take the union of shifted retained owners and
response owners; rebuild the map from ``session.tool_calls`` in the done and
recovery paths before ``_mergeSettledToolCallsWithLiveMetadata``.**

These tests drive the REAL ``_buildSettledToolIsErrorMap`` /
``_syncToolCallsForLoadedMessages`` (static/sessions.js) and the REAL
``_enrichSettledToolRowBodyFromLive`` consumer (static/messages.js) in node.
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


def _producer_src() -> str:
    """The real map builder + sync producer, with the minimum S scaffold."""
    return (
        _function_block(_read("static/sessions.js"), "function _buildSettledToolIsErrorMap(")
        + "\n"
        + _function_block(_read("static/sessions.js"), "function _syncToolCallsForLoadedMessages(")
        + "\nconst _mkS=()=>({busy:false,activeStreamId:null,session:null,"
          "toolCalls:[],_settledToolIsErrorByTid:null});"
    )


_CONSUMER_STUBS = """
function _anchorSceneStringPayload(v){ return v; }
function _anchorSceneToolArgs(){ return null; }
"""


def _consumer_src() -> str:
    return (
        _CONSUMER_STUBS
        + _function_block(_read("static/messages.js"), "function _enrichSettledToolRowBodyFromLive(")
    )


def _build_map(session_tool_calls):
    """Run the REAL shared builder and return the verdict map."""
    driver = _producer_src() + """
const map=_buildSettledToolIsErrorMap(PAYLOAD.sessionToolCalls||[]);
process.stdout.write(JSON.stringify(map));
"""
    return _run_node(driver, {"sessionToolCalls": session_tool_calls})


def _sync_map(session_tool_calls, prev_map=None, owner_offset=0):
    """Run the REAL sync producer (optionally the prepend merge) and return the map."""
    driver = _producer_src() + """
const S=_mkS();
if(PAYLOAD.prevMap!==undefined&&PAYLOAD.prevMap!==null) S._settledToolIsErrorByTid=PAYLOAD.prevMap;
_syncToolCallsForLoadedMessages([], PAYLOAD.sessionToolCalls||[], 'R14', PAYLOAD.ownerOffset||0, !!PAYLOAD.prevMap);
process.stdout.write(JSON.stringify(S._settledToolIsErrorByTid||null));
"""
    return _run_node(
        driver,
        {
            "sessionToolCalls": session_tool_calls,
            "prevMap": prev_map,
            "ownerOffset": owner_offset,
        },
    )


def _enrich_row(scoped_map, row_tid, row_owner):
    """Run the REAL consumer with the map installed; return the row's is_error."""
    driver = _consumer_src() + """
const S={busy:false,activeStreamId:null,_settledToolIsErrorByTid:PAYLOAD.map};
const row={role:'tool', tool_call_id:PAYLOAD.rowTid,
           group:{assistant_msg_idx:PAYLOAD.rowOwner},
           tool:{}, payload:{}};
_enrichSettledToolRowBodyFromLive(row, {tid:PAYLOAD.rowTid, name:'terminal'});
process.stdout.write(JSON.stringify({tool:row.tool.is_error===true, payload:row.payload.is_error===true}));
"""
    out = _run_node(
        driver,
        {"map": scoped_map, "rowTid": row_tid, "rowOwner": row_owner},
    )
    return out["tool"] or out["payload"]


def _failed_owners(entry):
    """The owner set a map entry marks failed (occurrence keys are strings)."""
    if entry is True:
        return "FLAT"
    if not isinstance(entry, dict):
        return set()
    owners = {int(k) for k in (entry.get("occurrences") or {})}
    if entry.get("assistant_msg_idx") is not None:
        owners.add(int(entry["assistant_msg_idx"]))
    return owners


# The settled summary after the reload + the reuse turn: turn 1's call_0
# failed (owner 1), turn 2's call_0 succeeded (owner 4).
_SETTLED_AFTER_REUSE = [
    {"name": "terminal", "tid": "call_0", "is_error": True,
     "assistant_msg_idx": 1, "snippet": "boom"},
    {"name": "terminal", "tid": "call_0", "is_error": False,
     "assistant_msg_idx": 4, "snippet": "ok"},
]


# ---------------------------------------------------------------------------
# 1. No flat entries — a singleton failure is scoped to its owner.
# ---------------------------------------------------------------------------


def test_singleton_failure_is_owner_scoped_not_flat():
    """The reload map for ``[call_0 fail@1]`` must not be a flat ``true``.

    A flat entry cannot distinguish the two occurrences of a reused id, so it
    is what let the later successful reuse render red. The entry is scoped to
    owner 1; a row that cannot prove owner 1 renders from its own data.
    """
    out = _build_map([
        {"name": "terminal", "tid": "call_0", "is_error": True,
         "assistant_msg_idx": 1, "snippet": "boom"},
    ])
    assert out is not None, "the failure must still be recorded"
    assert out.get("call_0") is not True, (
        "a singleton failure must not collapse to a flat tid-wide verdict — "
        "that is the reload-then-reuse contamination"
    )
    entry = out.get("call_0")
    assert isinstance(entry, dict) and entry.get("is_error") is True, entry
    assert _failed_owners(entry) == {1}, entry


def test_unowned_failure_writes_no_verdict():
    """A failure with no provable owner writes nothing (fail closed)."""
    out = _build_map([
        {"name": "terminal", "tid": "call_0", "is_error": True, "snippet": "boom"},
    ])
    assert out is None or out.get("call_0") is None, (
        "an unowned failure must not paint the tid wide"
    )


def test_successful_occurrence_writes_no_verdict():
    """Only failures enter the map; a successful occurrence stays absent."""
    out = _build_map(_SETTLED_AFTER_REUSE)
    assert _failed_owners(out.get("call_0")) == {1}, out


# ---------------------------------------------------------------------------
# 2. Reload-then-reuse: the rebuilt map scopes the new occurrence out.
# ---------------------------------------------------------------------------


def test_reload_then_reuse_does_not_paint_the_new_occurrence():
    """Case A end to end: reload ``[call_0 fail@1]``, the turn reuses call_0
    successfully at owner 4, the done path rebuilds the map from the settled
    summary — the new occurrence must render unfailed and the old one failed.
    """
    reload_map = _sync_map([
        {"name": "terminal", "tid": "call_0", "is_error": True,
         "assistant_msg_idx": 1, "snippet": "boom"},
    ])
    # The stale reload map must already be owner-scoped (no flat true).
    assert _failed_owners(reload_map.get("call_0")) == {1}, reload_map

    # The done/recovery rebuild from THIS settlement's summary.
    settled_map = _build_map(_SETTLED_AFTER_REUSE)
    assert _failed_owners(settled_map.get("call_0")) == {1}, settled_map

    # The real consumer: owner 4 (the successful reuse) renders unfailed;
    # owner 1 (the failure) renders failed.
    assert _enrich_row(settled_map, "call_0", 4) is False, (
        "the successful reuse was painted red by the rebuilt map"
    )
    assert _enrich_row(settled_map, "call_0", 1) is True, (
        "the failed occurrence lost its verdict"
    )


def test_done_and_recovery_paths_rebuild_the_map_before_the_merge():
    """Structural pin: both settlement paths rebuild the map from the
    settlement's own ``session.tool_calls`` BEFORE
    ``_mergeSettledToolCallsWithLiveMetadata`` (the reviewer's case A was a
    stale reload map surviving ``done``)."""
    src = _read("static/messages.js")
    builder = "_buildSettledToolIsErrorMap("
    merge = "_mergeSettledToolCallsWithLiveMetadata("
    rebuild_sites = 0
    for idx in range(len(src)):
        found = src.find(builder, idx)
        if found == -1:
            break
        # The builder call must be followed (within the same statement block)
        # by the merge call — i.e. the map is fresh when the merge runs.
        window = src[found:found + 400]
        if merge in window:
            rebuild_sites += 1
        idx = found
    assert rebuild_sites >= 2, (
        "both the done and the recovery path must rebuild the verdict map "
        f"before the live-metadata merge (found {rebuild_sites})"
    )


# ---------------------------------------------------------------------------
# 3. Prepend: an older successful page is not repainted by a retained
#    singleton, and an older failed singleton does not drop a retained
#    success.
# ---------------------------------------------------------------------------


def test_older_successful_page_is_not_repainted_by_a_retained_singleton():
    """Case B: the retained page's single call_0 failed; the prepended older
    page's single call_0 succeeded (no failure entry in the response). The
    merged map must not mark the older owner failed."""
    retained = _sync_map([
        {"name": "terminal", "tid": "call_0", "is_error": True,
         "assistant_msg_idx": 1, "snippet": "boom"},
    ])
    # The older page's summary carries the success (owner 5, inside the
    # 6-message page that sits at the FRONT of the combined array, so its
    # page-relative index is already the combined one) and NO failure.
    merged = _sync_map(
        [
            {"name": "terminal", "tid": "call_0", "is_error": False,
             "assistant_msg_idx": 5, "snippet": "ok"},
        ],
        prev_map=retained,
        owner_offset=6,  # the prepended page is 6 messages long
    )
    owners = _failed_owners(merged.get("call_0"))
    assert owners != "FLAT", merged
    # The retained failure moved down by the page length: 1 + 6 = 7.
    assert 1 + 6 in owners, (
        f"the retained failure must survive the prepend, shifted: {merged}"
    )
    assert 5 not in owners, (
        "the older SUCCESSFUL occurrence was painted red by the retained "
        f"singleton: {merged}"
    )
    # End to end through the real consumer: the older card renders unfailed.
    assert _enrich_row(merged, "call_0", 5) is False, merged


def test_older_failed_singleton_prepend_keeps_the_retained_success():
    """Case C: the retained page holds fail@1 + ok@4; the prepended older
    page's only call_0 failed (owner 0 after the shift). The response's
    scoped entry and the retained union must coexist — the retained success
    (owner 4, shifted to 10) stays unfailed."""
    retained = _sync_map(_SETTLED_AFTER_REUSE)
    merged = _sync_map(
        [
            {"name": "terminal", "tid": "call_0", "is_error": True,
             "assistant_msg_idx": 0, "snippet": "old boom"},
        ],
        prev_map=retained,
        owner_offset=6,
    )
    owners = _failed_owners(merged.get("call_0"))
    assert owners != "FLAT", merged
    assert 0 in owners, f"the older page's failure must be recorded: {merged}"
    assert 1 + 6 in owners, f"the retained failure must survive: {merged}"
    assert 4 + 6 not in owners, (
        "the retained SUCCESS was painted red by the older page's failure: "
        f"{merged}"
    )
    assert _enrich_row(merged, "call_0", 4 + 6) is False, merged
