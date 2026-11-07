"""#7653 re-gate round 12 — paging owner coordinates and recovery metadata.

The 14/10 re-gate left four findings open; this file covers the two with
explicit fixes (finding 1 and finding 4).

## Finding 1 — paging moves verdicts onto the wrong owner

The server rebases ``assistant_msg_idx`` into the RETURNED window for both
paging strategies (``_tool_calls_for_message_window``, ``api/routes.py``), so
the round-11 ``ownerOffset`` added at the paging call site (``olderMsgs.length``
for every index) double-shifted: cumulative paging moved a failed owner 1 onto
successful owner 4, and prepend paging moved it onto 7. The fix keeps response
summary indices unchanged; prepends merge the RETAINED transcript's verdicts
with only their owners shifted by the page length.

## Finding 4 — terminal recovery loses the current tool's metadata

``_mergeSettledToolCallsWithLiveMetadata`` fell through to the NAME fallback
whenever the occurrence-exact search failed. Stream-born live rows
(``upsertLiveToolCall``) carry no ``assistant_msg_idx``, so even a DISTINCT id
failed the search and an older same-name live row was consumed first — the
current row ended up with the older row's ``duration``/``started_at`` (or none).
The fix reserves live metadata for explicit-id matches before the name
fallback; the verdict transfer stays occurrence-scoped.

Both halves drive the REAL production functions in node, same harness as the
round-10/11 files.
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


def _owners_of(entry):
    owners = set(entry.get("occurrences") or {})
    owners.add(entry.get("assistant_msg_idx"))
    return owners


# ---------------------------------------------------------------------------
# Finding 1 — sessions.js verdict-map paging.
# ---------------------------------------------------------------------------


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


def _build_map(session_tool_calls, owner_offset=0, preseed=None, merge_prev=False):
    """Run the REAL ``_syncToolCallsForLoadedMessages`` and return its map.

    ``preseed`` simulates the RETAINED transcript's verdict map (the state
    ``S._settledToolIsErrorByTid`` holds before a paging replace): a first
    call without the merge flag rebuilds it from ``preseed``. The second call
    then applies the paging replace with ``owner_offset`` and, when
    ``merge_prev``, the prepend retained-merge.
    """
    driver = _producer_body() + """
const S=_mkS();
let first=null;
if(PAYLOAD.preseed){
  _syncToolCallsForLoadedMessages([], PAYLOAD.preseed, 'R12');
  first=JSON.parse(JSON.stringify(S._settledToolIsErrorByTid||null));
}
_syncToolCallsForLoadedMessages(PAYLOAD.messages||[], PAYLOAD.sessionToolCalls, 'R12',
  PAYLOAD.ownerOffset||0, !!PAYLOAD.mergePrev);
process.stdout.write(JSON.stringify({first:first, final:S._settledToolIsErrorByTid||null}));
"""
    return _run_node(
        driver,
        {
            "sessionToolCalls": session_tool_calls,
            "messages": [],
            "ownerOffset": owner_offset,
            "preseed": preseed or [],
            "mergePrev": merge_prev,
        },
    )


def test_cumulative_paging_keeps_response_owner_indices_unchanged():
    """Cumulative paging: the server-rebased index must not be re-shifted.

    The reviewer's cumulative repro: the window summary carries the failed
    ``call_0`` at combined owner 1 and the successful reuse at owner 4. The
    round-11 call added ``olderMsgs.length`` (3 here) to every response
    index, moving the failure onto the successful owner 4.
    """
    out = _build_map(
        [{"name": "terminal", "tid": "call_0", "is_error": False,
          "assistant_msg_idx": 4, "snippet": "ok"},
         {"name": "terminal", "tid": "call_0", "is_error": True,
          "assistant_msg_idx": 1, "snippet": "boom"}],
        owner_offset=3,
    )
    entry = out["final"].get("call_0")
    assert isinstance(entry, dict), f"the failure must be recorded: {entry!r}"
    assert 1 in _owners_of(entry), (
        "the cumulative response summary's owner is already window-relative; "
        f"the paging offset must not re-shift it: {entry!r}"
    )
    assert 4 not in _owners_of(entry), (
        "the round-11 offset moved the failed owner onto the successful owner 4"
    )


def test_prepend_paging_keeps_the_older_page_owners_in_page_coordinates():
    """Prepend paging: the older page's own indices are already combined.

    The older page sits at the FRONT of the combined array, so a page-relative
    failed owner 1 is combined owner 1. The reviewer's prepend repro: the
    round-11 offset (6 here) moved the failure onto the retained transcript's
    successful owner 7.
    """
    out = _build_map(
        [{"name": "terminal", "tid": "call_0", "is_error": False,
          "assistant_msg_idx": 7, "snippet": "ok"},
         {"name": "terminal", "tid": "call_0", "is_error": True,
          "assistant_msg_idx": 1, "snippet": "boom"}],
        owner_offset=6,
        merge_prev=True,
    )
    entry = out["final"].get("call_0")
    assert isinstance(entry, dict), f"the failure must be recorded: {entry!r}"
    assert 1 in _owners_of(entry), (
        "the prepended page's owner index needs no shift (page at front): "
        f"{entry!r}"
    )
    assert 7 not in _owners_of(entry), (
        "the round-11 offset moved the older page's failure onto the retained "
        "successful owner 7"
    )


def test_prepend_paging_merges_the_retained_verdicts_with_shifted_owners():
    """Prepend paging: the retained transcript's verdicts must survive.

    ``responseSession.tool_calls`` for a ``msg_before`` page contains ONLY the
    older page's summaries, so a prepend that wholesale-replaces the map drops
    the current transcript's verdicts. The retained failure (a reused id whose
    failed occurrence is at owner 1 in the old array) must be merged back,
    shifted by the page length (6) onto owner 7 of the combined array.
    """
    out = _build_map(
        [],
        owner_offset=6,
        preseed=[{"name": "terminal", "tid": "call_0", "is_error": False,
                  "assistant_msg_idx": 4, "snippet": "ok"},
                 {"name": "terminal", "tid": "call_0", "is_error": True,
                  "assistant_msg_idx": 1, "snippet": "boom"}],
        merge_prev=True,
    )
    entry = out["final"].get("call_0")
    assert isinstance(entry, dict), (
        "the retained failure must survive the prepend's map replace: "
        f"{out['final']!r}"
    )
    assert 7 in _owners_of(entry), (
        "the retained summary's owner must be shifted by the page length: "
        f"{entry!r}"
    )


def test_prepend_paging_keeps_a_unique_retained_owner_scoped_verdict():
    """#7653 round 14 contract change: a retained unique-id failure survives
    the prepend merge OWNER-SCOPED (no flat entries anywhere). The retained
    rows moved down the combined array by the page length, so the owner
    shifts by the page length; the older page does not mention the tid."""
    out = _build_map(
        [],
        owner_offset=6,
        preseed=[{"name": "terminal", "tid": "call_a", "is_error": True,
                  "assistant_msg_idx": 1, "snippet": "boom"}],
        merge_prev=True,
    )
    entry = out["final"].get("call_a")
    assert entry is not True and isinstance(entry, dict), (
        "the retained failure must stay owner-scoped, never flat: "
        f"{out['final']!r}"
    )
    owners = set(entry.get("occurrences") or {})
    owners.add(entry.get("assistant_msg_idx"))
    assert 1 + 6 in owners, (
        f"the retained failure must survive, shifted by the page length: {entry!r}"
    )


# ---------------------------------------------------------------------------
# Finding 4 — messages.js recovery metadata reservation.
# ---------------------------------------------------------------------------


def _merge_slice(raw_calls, live):
    """Run the REAL ``_mergeSettledToolCallsWithLiveMetadata``."""
    fn = _function_block(
        _read("static/messages.js"),
        "function _mergeSettledToolCallsWithLiveMetadata(",
    )
    script = f"""
const S = {{ toolCalls: PAYLOAD.live }};
{fn}
process.stdout.write(JSON.stringify(_mergeSettledToolCallsWithLiveMetadata(PAYLOAD.rawCalls)));
"""
    return _run_node(script, {"rawCalls": raw_calls, "live": live})


# The reviewer's recovery shape: two DISTINCT ids, both terminal ``bash``
# calls. The live rows are stream-born (no ``assistant_msg_idx``) and sit in
# S.toolCalls newest-first; the settled summaries carry their owners.
_RECOVERY_RAW = [
    {"name": "bash", "tid": "call_1", "assistant_msg_idx": 3},
    {"name": "bash", "tid": "call_2", "assistant_msg_idx": 5},
]
_RECOVERY_LIVE = [
    {"name": "bash", "tid": "call_2", "duration": 8.0, "started_at": 2000},
    {"name": "bash", "tid": "call_1", "duration": 5.0, "started_at": 1000},
]


def test_recovery_reserves_live_metadata_for_the_explicit_id():
    """The current tool keeps its own ``duration``/``started_at``.

    Occurrence-exact matching fails for stream-born live rows (no
    ``assistant_msg_idx``), so the round-11 code fell to the NAME fallback and
    handed the current ``call_2`` the OLDER ``call_1`` live row's metadata
    (and vice versa) — the reviewer's "terminal recovery loses a current
    tool's duration and started_at", reproduced even with distinct ids.
    """
    out = _merge_slice(_RECOVERY_RAW, _RECOVERY_LIVE)
    by_tid = {r.get("tid"): r for r in out}
    assert by_tid["call_2"].get("duration") == 8.0, (
        "the current call must keep its own live duration, not the older "
        f"same-name row's: {by_tid['call_2']!r}"
    )
    assert by_tid["call_2"].get("started_at") == 2000
    assert by_tid["call_1"].get("duration") == 5.0, (
        "the older call keeps its own metadata: {by_tid['call_1']!r}"
    )
    assert by_tid["call_1"].get("started_at") == 1000


def test_recovery_metadata_reservation_does_not_loosen_verdict_scoping():
    """Control: the loosened metadata match must not drive ``is_error``.

    The metadata reservation picks the explicit id's own unused live row when
    occurrence-exact fails, but the one-way verdict upgrade stays gated on the
    occurrence-exact ``idMatchEntry`` — a name/metadata match must never
    repaint a row failed.
    """
    out = _merge_slice(
        [{"name": "bash", "tid": "call_2", "assistant_msg_idx": 5}],
        [{"name": "bash", "tid": "call_2", "is_error": True, "duration": 8.0}],
    )
    assert out[0].get("is_error") is not True, (
        "an unowned (indexless-live) id match transfers metadata only, never "
        "a verdict"
    )


def test_recovery_occurrence_exact_match_still_upgrades_the_verdict():
    """Control: an occurrence-exact match keeps the rounds 3-8 upgrade."""
    out = _merge_slice(
        [{"name": "bash", "tid": "call_2", "assistant_msg_idx": 5}],
        [{"name": "bash", "tid": "call_2", "assistant_msg_idx": 5,
          "is_error": True, "duration": 8.0}],
    )
    assert out[0].get("is_error") is True
