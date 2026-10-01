"""#7358 round 10 regression — cold-reload owner fallback + per-session map reset.

The 10/02 re-gate review's two SILENT browser findings:

> **Finding 3.** ``_enrichSettledToolRowBodyFromLive`` (static/messages.js) and
> ``copyLiveToolMetadata`` (static/ui.js) read the row's owning assistant
> message index from the *top level* (``row.assistant_msg_idx`` /
> ``next.assistant_msg_idx``), but a scene row built by ``_anchorSceneRowBase``
> stores the owner on ``row.group.assistant_msg_idx`` and
> ``row.payload.assistant_msg_idx`` — there is no top-level field. So a
> cold-reloaded reused-id failure (round 9's ``{assistant_msg_idx, is_error}``
> map entry) never matched its row and the card reverted to "Completed".
>
> Fix: resolve the owner through the same fallback chain in both consumers —
> top level -> ``group.assistant_msg_idx`` -> ``payload.assistant_msg_idx``.

> **Finding 4.** ``S._settledToolIsErrorByTid`` is per-session state, but the
> New Chat path (``newSession``) clears ``S.toolCalls`` /
> ``clearLiveToolCards()`` without resetting the map — and New Chat replaces
> ``S.session`` without going through ``loadSession()``, so
> ``_syncToolCallsForLoadedMessages`` (the only place the map is rebuilt) never
> runs for it. The previous session's reused-id failure verdict leaks into the
> brand-new session. The INFLIGHT restore branch has the same gap: it skips
> ``_syncToolCallsForLoadedMessages`` entirely.
>
> Fix: reset ``S._settledToolIsErrorByTid = null`` in the New Chat cleanup
> sequence and in the INFLIGHT restore branch. ``loadSession``'s normal path
> keeps its rebuild-on-every-invocation semantics (no change).

This file follows the round-9 node-sandbox paradigm: lift a function block
verbatim out of the source and run it under ``node -e`` with a PAYLOAD global
on stdin.
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


def _run_node(script: str, payload: dict) -> dict:
    """Run a Node script with the payload on stdin; return parsed stdout."""
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
    assert result.returncode == 0, f"node failed:\n{result.stderr}"
    return json.loads(result.stdout)


def _sessions_producer() -> str:
    return _function_block(
        _read("static/sessions.js"), "function _syncToolCallsForLoadedMessages("
    )


def _producer_body() -> str:
    # A fresh S with every field the producer reads/mutates.
    return (
        _sessions_producer()
        + "\nconst _mkS=()=>({busy:false,activeStreamId:null,session:null,"
          "toolCalls:[],_settledToolIsErrorByTid:null});"
    )


# The reviewer's exact probe: turn 1 ``call_0`` succeeded, turn 2 ``call_0``
# failed, both surfaced in the server's settled summary (round 9's shape).
REUSED_PROBE = [
    {"name": "terminal", "tid": "call_0", "is_error": False,
     "assistant_msg_idx": 1, "snippet": "ok"},
    {"name": "terminal", "tid": "call_0", "is_error": True,
     "assistant_msg_idx": 4, "snippet": "boom"},
]


# ── Finding 3a — messages.js consumer resolves the owner via group/payload ──


def _run_messages_enrich(row: dict, live: dict) -> dict:
    """Drive ``_enrichSettledToolRowBodyFromLive`` with the round-9 producer
    feeding ``S._settledToolIsErrorByTid``."""
    enrich = _function_block(
        _read("static/messages.js"), "function _enrichSettledToolRowBodyFromLive("
    )
    driver = _producer_body() + f"""
function _anchorSceneStringPayload(v) {{ return (v===undefined||v===null)?null:String(v); }}
function _anchorSceneToolArgs(live) {{ return (live && live.args && typeof live.args==='object') ? live.args : null; }}
const S=_mkS();
_syncToolCallsForLoadedMessages(PAYLOAD.messages, PAYLOAD.sessionToolCalls, 'R10');
{enrich}
const r=JSON.parse(JSON.stringify(PAYLOAD.row));
const enriched=_enrichSettledToolRowBodyFromLive(r, PAYLOAD.live);
process.stdout.write(JSON.stringify({{row:r,enriched:enriched}}));
"""
    return _run_node(driver, {
        "messages": [],
        "sessionToolCalls": REUSED_PROBE,
        "live": live,
        "row": row,
    })


def _scene_row(owner_idx, snippet="[exit 2]"):
    """A cold-reloaded scene row in the exact ``_anchorSceneRowBase`` shape:
    the owner lives on ``group.assistant_msg_idx`` / ``payload.assistant_msg_idx``
    and there is NO top-level ``assistant_msg_idx`` (that is the bug)."""
    return {
        "tool_call_id": "call_0",
        "tool": {"id": "call_0", "name": "terminal", "snippet": snippet},
        "payload": {"name": "terminal", "snippet": snippet,
                    "assistant_msg_idx": owner_idx},
        "group": {"assistant_msg_idx": owner_idx},
    }


def test_cold_reload_reused_failure_reads_group_owner() -> None:
    """Finding 3 bug repro (messages.js): the genuinely failed ``call_0`` row
    carries its owner on ``group.assistant_msg_idx``/``payload.assistant_msg_idx``
    only. It must still render red after a cold reload; the round-9 top-level
    read missed it and the card reverted to "Completed"."""
    # Cold reload: no in-memory live mirror that id-matches this row, so the
    # persisted map is the only verdict source.
    out = _run_messages_enrich(
        _scene_row(4, snippet="[exit 2]"),
        {"name": "terminal", "tid": "call_other"},
    )
    assert out["row"]["tool"].get("is_error") is True, (
        "the failed reused-id scene row reverted to Completed: its owner is on "
        "group/payload, not the top level, and the consumer must fall back"
    )
    assert out["row"]["payload"].get("is_error") is True
    assert out["enriched"] is True


def test_cold_reload_reused_failure_reads_payload_owner() -> None:
    """Same repro but the row has only ``payload.assistant_msg_idx`` (no
    ``group`` object at all) — the fallback chain must cover that shape too."""
    row = _scene_row(4)
    row.pop("group")
    out = _run_messages_enrich(row, {"name": "terminal", "tid": "call_other"})
    assert out["row"]["tool"].get("is_error") is True, (
        "payload-only owner shape must still resolve the reused-id failure"
    )
    assert out["row"]["payload"].get("is_error") is True


def test_cold_reload_success_sibling_stays_green_via_group_owner() -> None:
    """Guard (Finding 3/round-9): the earlier successful ``call_0`` scene row
    (owner idx 1) must stay green even though a later ``call_0`` failed — the
    group/payload fallback must not widen the verdict to the whole tid."""
    out = _run_messages_enrich(
        _scene_row(1, snippet="[exit 0]"),
        {"name": "terminal", "tid": "call_other"},
    )
    assert not out["row"]["tool"].get("is_error"), (
        "the successful sibling was repainted red by the reused-id failure "
        "(owner resolution must stay occurrence-scoped)"
    )
    assert not out["row"]["payload"].get("is_error")


def test_cold_reload_top_level_owner_still_wins() -> None:
    """Consistency control: when the row carries a top-level
    ``assistant_msg_idx`` (older row shapes / live-derived rows), it must take
    precedence over group/payload — a conflicting group value must not flip an
    owned failure to green."""
    row = _scene_row(4)
    row["assistant_msg_idx"] = 4
    row["group"] = {"assistant_msg_idx": 99}  # conflicting, must be ignored
    out = _run_messages_enrich(row, {"name": "terminal", "tid": "call_other"})
    assert out["row"]["tool"].get("is_error") is True, (
        "top-level owner must take precedence in the fallback chain"
    )


# ── Finding 3b — ui.js consumer resolves the owner via group/payload ────────


def _run_ui_copy_live(rows: list[dict]) -> dict:
    """Drive ``copyLiveToolMetadata`` (static/ui.js) with the round-9 producer
    feeding ``S._settledToolIsErrorByTid``."""
    fn = _function_block(_read("static/ui.js"), "const copyLiveToolMetadata=")
    driver = _producer_body() + f"""
const S=_mkS();
_syncToolCallsForLoadedMessages(PAYLOAD.messages, PAYLOAD.sessionToolCalls, 'R10');
// Cold-reload render path: no in-memory live mirror, so the persisted map is
// the only verdict source.
const liveToolMetadata = [];
const liveMetadataByTid = new Map();
const usedLiveToolMetadata = new Set();
const _persistedIsErrorByTid = S._settledToolIsErrorByTid;
{fn}
const rows = PAYLOAD.rows.map(r=>copyLiveToolMetadata(
  Object.assign({{}}, r), r.name, r.tid || r.id || r.tool_call_id || r.call_id || ''));
process.stdout.write(JSON.stringify(rows));
"""
    return _run_node(driver, {
        "messages": [],
        "sessionToolCalls": REUSED_PROBE,
        "rows": rows,
    })


def test_ui_copy_live_resolves_reused_failure_via_group_owner() -> None:
    """Finding 3 bug repro (ui.js): the failed ``call_0`` row with owner on
    ``group``/``payload`` only must render red; the successful sibling (owner
    idx 1) must stay green."""
    out = _run_ui_copy_live([
        {"name": "terminal", "tid": "call_0", "done": True,
         "group": {"assistant_msg_idx": 4},
         "payload": {"assistant_msg_idx": 4}},
        {"name": "terminal", "tid": "call_0", "done": True,
         "group": {"assistant_msg_idx": 1},
         "payload": {"assistant_msg_idx": 1}},
    ])
    assert out[0].get("is_error") is True, (
        "ui.js consumer failed to resolve the reused-id failure via the "
        "group/payload owner — card reverted to Completed on cold reload"
    )
    assert out[1].get("is_error") is not True, (
        "the successful sibling was repainted red by the reused-id failure"
    )


def test_ui_copy_live_top_level_owner_still_wins() -> None:
    """Consistency control (ui.js): top-level owner takes precedence."""
    out = _run_ui_copy_live([
        {"name": "terminal", "tid": "call_0", "done": True,
         "assistant_msg_idx": 4,
         "group": {"assistant_msg_idx": 99},
         "payload": {"assistant_msg_idx": 99}},
    ])
    assert out[0].get("is_error") is True, (
        "ui.js top-level owner must take precedence in the fallback chain"
    )


# ── Finding 4 — New Chat / INFLIGHT reset of the per-session map ────────────


def _new_chat_cleanup_region() -> str:
    """Lift the New Chat cleanup statements (``S.toolCalls=[]`` through
    ``clearLiveToolCards()``) verbatim out of ``newSession``."""
    src = _read("static/sessions.js")
    ns_start = src.index("async function newSession(flash, options={}){")
    tc_pos = src.index("S.toolCalls=[];", ns_start)
    clc_pos = src.index("clearLiveToolCards();", tc_pos)
    return src[tc_pos:clc_pos + len("clearLiveToolCards();")]


def test_new_chat_cleanup_resets_settled_error_map() -> None:
    """Finding 4 bug repro: New Chat must drop the previous session's
    persisted error map. Behavior simulation — run the cleanup statements with
    an S carrying a stale verdict and assert the map is gone afterwards."""
    region = _new_chat_cleanup_region()
    driver = f"""
const S = {{
  toolCalls: [{{tid:'call_0',is_error:true}}],
  _settledToolIsErrorByTid: {{call_0: {{assistant_msg_idx: 1, is_error: true}}}},
}};
let _messagesTruncated = true;
let _oldestIdx = 7;
function clearLiveToolCards() {{}}
{region}
process.stdout.write(JSON.stringify({{
  toolCalls: S.toolCalls,
  map: S._settledToolIsErrorByTid,
  truncated: _messagesTruncated,
  oldestIdx: _oldestIdx,
}}));
"""
    out = _run_node(driver, {})
    assert out["toolCalls"] == [], f"toolCalls not cleared: {out}"
    assert out["map"] is None, (
        f"the old session's error map leaked into the new chat: {out['map']}"
    )
    assert out["truncated"] is False and out["oldestIdx"] == 0


def test_inflight_restore_branch_resets_settled_error_map() -> None:
    """Finding 4 source pin: the INFLIGHT restore branch (the second
    ``S.toolCalls=[];`` after the New Chat one, inside loadSession) must also
    reset the map — that branch skips ``_syncToolCallsForLoadedMessages``
    entirely, so without the explicit reset the previous session's verdicts
    leak into the restored session's render."""
    src = _read("static/sessions.js")
    # First occurrence is the New Chat cleanup (pinned by test above); the
    # second is the INFLIGHT restore branch.
    first = src.index("S.toolCalls=[];")
    second = src.index("S.toolCalls=[];", first + 1)
    region = src[second:src.index("clearLiveToolCards()", second) + len("clearLiveToolCards()")]
    assert "S._settledToolIsErrorByTid=null" in region, (
        "the INFLIGHT restore branch must reset S._settledToolIsErrorByTid "
        "before rebuilding the live worklog"
    )
    # Also pin that the reset is not commented out.
    assert "// #7358 round 10" in region, "missing round-10 annotation"


def test_sync_rebuild_semantics_unchanged() -> None:
    """Control: the producer still rebuilds the map from scratch on every
    invocation (round-9 semantics preserved — no early-return leak, no
    accumulation across calls)."""
    out = _run_node(_producer_body() + """
const S=_mkS();
_syncToolCallsForLoadedMessages([], [{'name':'terminal','tid':'call_0','is_error':true,'assistant_msg_idx':1}], 'A');
const _A=S._settledToolIsErrorByTid;
_syncToolCallsForLoadedMessages([], [], 'B');
process.stdout.write(JSON.stringify({A:_A,B:S._settledToolIsErrorByTid}));
""", {})
    assert out["A"] and out["A"].get("call_0") is True, f"A map: {out}"
    assert not out["B"], f"session B inherited session A's error map: {out}"
