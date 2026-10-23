"""#7653 re-gate round 11 — the browser consumers pair by OCCURRENCE, not by order.

The 10/10 re-gate left finding 2 open with an exact fix. Both browser
consumers resolve a reused tool id per occurrence, but they pair a settled
row with the *first available* live row without establishing WHICH
occurrence that row is:

* ``copyLiveToolMetadata`` (``static/ui.js:19390``), and
* ``_mergeSettledToolCallsWithLiveMetadata`` (``static/messages.js:4597``).

Both keep an array per tid and hand out the next unused entry. That is
order-dependent: whenever the settled rows and the live mirror rows are
not in the same order — a scene-owned row rendered out of transcript
order, a shorter mirror that must align to the TRAILING occurrences, or a
persisted summary whose rows arrive in a different sequence than the
mirror appended them — the pairing crosses occurrences. The reviewer's
repro: a later failed ``call_0`` repaints an older success, so both
consumers produce ``[true, true]`` where the release base preserves
``[false, true]``.

The reviewer's exact fix: match occurrences across the complete
transcript (including scene-owned rows), align shorter mirrors to
trailing occurrences, and transfer verdicts only with attributable
ownership.

The ownership evidence already exists on both sides: a settled row
carries ``assistant_msg_idx`` (the server writes it into every
``s.tool_calls`` entry, and the scene path carries it on
``group``/``payload``), and a live mirror row carries it once the row was
derived from a transcript. So the pairing becomes:

1. when BOTH sides name an ``assistant_msg_idx``, pair on it exactly —
   never on consumption order;
2. otherwise (no index on either side) keep the existing next-unused
   behaviour, which is all an indexless mirror can support;
3. when an index is named but no live row owns it, transfer nothing
   rather than guess.

These tests drive the REAL functions out of ``static/ui.js`` and
``static/messages.js`` in node (same harness as round 10), so they pin the
shipped pairing rather than a restatement of it.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
NODE = shutil.which("node")

# ---------------------------------------------------------------------------
# Harness: run the real functions from the real files.
# ---------------------------------------------------------------------------


def _function_block(src: str, header: str) -> str:
    """Slice one top-level function out of a static/*.js source file."""
    start = src.index(header)
    i = src.index("{", start)
    depth = 0
    while i < len(src):
        ch = src[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return src[start : i + 1]
        i += 1
    raise AssertionError(f"unbalanced braces after {header}")


def _run_node(script: str, payload: dict) -> list:
    assert NODE, "node not on PATH"
    result = subprocess.run(
        [NODE, "-e", script],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, f"node failed:\n{result.stderr[:800]}"
    return json.loads(result.stdout)


def _merge_driver() -> str:
    """The real ``_mergeSettledToolCallsWithLiveMetadata`` from messages.js."""
    fn = _function_block(
        (REPO_ROOT / "static" / "messages.js").read_text(encoding="utf-8"),
        "function _mergeSettledToolCallsWithLiveMetadata(",
    )
    return f"""
var PAYLOAD = JSON.parse(require('fs').readFileSync(0, 'utf8') || '{{}}');
const S = {{ toolCalls: PAYLOAD.live }};
{fn}
process.stdout.write(JSON.stringify(
  _mergeSettledToolCallsWithLiveMetadata(PAYLOAD.rawCalls)));
"""


def _ui_driver() -> str:
    """The real ``copyLiveToolMetadata`` pairing half from ui.js.

    The function closes over ``liveToolMetadata`` / ``S`` / ``resultsByTid``
    and friends, so the harness binds the same names it reads. The
    per-tid index (``liveMetadataByTid``) and the ``used`` set live in the
    enclosing scope right above the function, so the harness slices them
    out of the real file too — an inline restatement would pin a copy, not
    the shipped pairing.
    """
    src = (REPO_ROOT / "static" / "ui.js").read_text(encoding="utf-8")
    index_start = src.index("const liveMetadataByTid=new Map();")
    index_block = src[index_start : src.index("const usedLiveToolMetadata=new Set();", index_start) + len("const usedLiveToolMetadata=new Set();")]
    fn = _function_block(src, "const copyLiveToolMetadata=(next,name,tid)=>{")
    return f"""
var PAYLOAD = JSON.parse(require('fs').readFileSync(0, 'utf8') || '{{}}');
var liveToolMetadata = PAYLOAD.live;
var S = {{}};
var resultsByTid = {{}};
var persistedSnippetByTid = {{}};
var _persistedIsErrorByTid = null;
function _cliPatchSnippetFromArgs(){{ return ''; }}
function _cliToolCardSnippet(a, b){{ return a || b || ''; }}
function _cliToolCardHasDiffSnippet(){{ return false; }}
function _toolArgsSnapshot(a){{ return a || {{}}; }}
function _assistantToolAnchorIdxForMessage(){{ return -1; }}
var derived = [];
{index_block}
{fn}
// Drive the derived-row path with transcript-owned rows that carry their
// owning assistant index, exactly as the fallback loop builds them.
PAYLOAD.rows.forEach(function(row){{
  derived.push(copyLiveToolMetadata(Object.assign({{done:true}}, row), row.name, row.tid));
}});
process.stdout.write(JSON.stringify(derived));
"""


def _rows_out(out):
    return [(r.get("assistant_msg_idx"), r.get("is_error")) for r in out]


# ---------------------------------------------------------------------------
# The reviewer's repro: the settled rows and the mirror are in DIFFERENT
# orders, so order-based pairing crosses occurrences.
# ---------------------------------------------------------------------------

# The persisted/settled rows, in transcript order: an EARLIER successful
# call_0 (owner 1) and a LATER failed call_0 (owner 4).
_SETTLED = [
    {"name": "terminal", "tid": "call_0", "done": True, "assistant_msg_idx": 1},
    {"name": "terminal", "tid": "call_0", "done": True, "assistant_msg_idx": 4},
]

# The live mirror in the order the re-arm branch appended it: the FAILED
# occurrence (owner 4) was classified last, so it sits at the END.
_MIRROR_INDEXED = [
    {"name": "terminal", "tid": "call_0", "done": True, "is_error": False,
     "assistant_msg_idx": 1, "snippet": "ok"},
    {"name": "terminal", "tid": "call_0", "done": True, "is_error": True,
     "assistant_msg_idx": 4, "snippet": "boom"},
]


def test_messages_merge_pairs_by_owner_index_not_consumption_order():
    """messages.js: the failed owner-4 row must not repaint owner-1.

    Before the fix the next-unused pairing handed the settled rows the
    mirror rows in consumption order. Whenever the two sequences differ in
    order the verdicts cross — the reviewer's ``[true, true]`` where the
    base preserves ``[false, true]``.
    """
    out = _run_node(_merge_driver(), {"rawCalls": _SETTLED, "live": _MIRROR_INDEXED})
    by_owner = {r.get("assistant_msg_idx"): r.get("is_error") for r in out}
    assert by_owner.get(1) is not True, (
        "the earlier successful call_0 (owner 1) must stay successful"
    )
    assert by_owner.get(4) is True, (
        "the later failed call_0 (owner 4) must stay failed"
    )


def test_ui_copy_pairs_by_owner_index_not_consumption_order():
    """ui.js: the same crossing on the live-settle path."""
    out = _run_node(_ui_driver(), {"rows": _SETTLED, "live": _MIRROR_INDEXED})
    by_owner = {r.get("assistant_msg_idx"): r.get("is_error") for r in out}
    assert by_owner.get(1) is not True, (
        "the earlier successful call_0 (owner 1) must stay successful"
    )
    assert by_owner.get(4) is True, (
        "the later failed call_0 (owner 4) must stay failed"
    )


def test_messages_merge_reversed_mirror_order_still_pairs_by_owner():
    """The mirror appended in the OPPOSITE order must pair identically.

    This is the shape that distinguishes an owner-indexed pairing from a
    trailing-alignment heuristic: flipping the mirror's order changes
    nothing about which owner owns which verdict.
    """
    out = _run_node(
        _merge_driver(),
        {"rawCalls": _SETTLED, "live": list(reversed(_MIRROR_INDEXED))},
    )
    by_owner = {r.get("assistant_msg_idx"): r.get("is_error") for r in out}
    assert by_owner.get(1) is not True
    assert by_owner.get(4) is True


def test_ui_copy_reversed_mirror_order_still_pairs_by_owner():
    out = _run_node(
        _ui_driver(),
        {"rows": _SETTLED, "live": list(reversed(_MIRROR_INDEXED))},
    )
    by_owner = {r.get("assistant_msg_idx"): r.get("is_error") for r in out}
    assert by_owner.get(1) is not True
    assert by_owner.get(4) is True


# ---------------------------------------------------------------------------
# The reviewer's "align shorter mirrors to trailing occurrences".
# ---------------------------------------------------------------------------


def test_messages_merge_short_mirror_aligns_to_the_trailing_owner():
    """A mirror with one row for two owners pairs with the TRAILING owner.

    The single live row belongs to the current turn, so it must upgrade the
    trailing occurrence only. An indexless mirror keeps the next-unused
    rule (the trailing owner is also the last consumed row), so the
    observable contract is: the trailing owner takes the verdict and the
    earlier owner takes nothing.
    """
    settled = [
        {"name": "terminal", "tid": "call_0", "done": True},
        {"name": "terminal", "tid": "call_0", "done": True},
    ]
    live = [
        {"name": "terminal", "tid": "call_0", "done": True, "is_error": True,
         "snippet": "boom"},
    ]
    out = _run_node(_merge_driver(), {"rawCalls": settled, "live": live})
    assert out[0].get("is_error") is not True, (
        "the earlier occurrence must not inherit the trailing failure"
    )
    assert out[1].get("is_error") is True, (
        "the trailing occurrence takes the single live row's verdict"
    )


# ---------------------------------------------------------------------------
# Controls: the indexless behaviour and the unique-id upgrade must not move.
# ---------------------------------------------------------------------------


def test_messages_merge_indexless_mirror_keeps_next_unused_pairing():
    """No index on either side: the round-10 next-unused pairing stands."""
    settled = [
        {"name": "terminal", "tid": "call_0", "done": True},
        {"name": "terminal", "tid": "call_0", "done": True},
    ]
    live = [
        {"name": "terminal", "tid": "call_0", "done": True, "is_error": True,
         "snippet": "boom"},
        {"name": "terminal", "tid": "call_0", "done": True, "is_error": False,
         "snippet": "ok"},
    ]
    out = _run_node(_merge_driver(), {"rawCalls": settled, "live": live})
    assert out[0].get("is_error") is True
    assert out[1].get("is_error") is not True


def test_messages_merge_unique_id_still_upgrades():
    """A genuinely unique failed id still renders red (rounds 3-8)."""
    out = _run_node(
        _merge_driver(),
        {
            "rawCalls": [{"name": "terminal", "tid": "call_a", "done": True}],
            "live": [
                {"name": "terminal", "tid": "call_a", "done": True,
                 "is_error": True, "snippet": "boom"},
            ],
        },
    )
    assert out[0].get("is_error") is True


def test_messages_merge_named_index_without_a_live_owner_transfers_nothing():
    """An index no live row owns must not borrow a neighbour's verdict."""
    settled = [{"name": "terminal", "tid": "call_0", "done": True,
                "assistant_msg_idx": 9}]
    live = [{"name": "terminal", "tid": "call_0", "done": True,
             "is_error": True, "assistant_msg_idx": 4, "snippet": "boom"}]
    out = _run_node(_merge_driver(), {"rawCalls": settled, "live": live})
    assert out[0].get("is_error") is not True, (
        "ownership must be attributable: an unowned index transfers nothing"
    )
