"""#7358 round 9 browser regression — the persisted error map is occurrence-scoped.

The 10/01 re-gate review's SILENT finding 2:

> **Browser restore.** The persisted error map built in
> ``_syncToolCallsForLoadedMessages`` is keyed by tid only, so both ``call_0``
> cards render Failed after a reload. That map also isn't reset when switching
> to an active session, because the refresh returns early, so it can leak
> across sessions.
>
> Fix: key every verdict on the call *occurrence*, i.e. (owning
> ``assistant_msg_idx``, ``tid``), never ``tid`` alone; scope the browser map
> to the session and clear it on switch.

This is the browser-side half (finding 2). The parent owns
``test_issue7358_round9_occurrence_keyed_verdicts.py``; this file adds node
-driver pins for the ``sessions.js`` producer and the render consumer that
reads ``S._settledToolIsErrorByTid`` (``copyLiveToolMetadata`` in
``static/ui.js``), following the round-8
``test_issue7358_round8_id_only_is_error_upgrade.py`` conventions (lift a
function block verbatim, run it under ``node -e`` with a PAYLOAD global).

New shape of ``S._settledToolIsErrorByTid``::

    S._settledToolIsErrorByTid[tid] === true                          # unique id
    S._settledToolIsErrorByTid[tid] = {assistant_msg_idx, is_error}  # reused id
    # …plus an optional `occurrences` sub-map when several failed calls share
    # the tid (each keyed by assistant_msg_idx → true).

Consumers: a flat ``true`` still applies tid-wide (unique ids, rounds 3-8
behaviour unchanged); a reused-id object is honoured only when the row names
the owning ``assistant_msg_idx`` and it matches the recorded failure — so an
earlier successful ``call_0`` never renders red. The map is rebuilt from
scratch on every ``_syncToolCallsForLoadedMessages`` invocation, including
the active-streaming early return, so a session switch can't leak it.
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


def _flatten_script() -> str:
    return (
        "const _flat=[];\n"
        "for(const _k of Object.keys(S._settledToolIsErrorByTid||{})){\n"
        "  const _v=S._settledToolIsErrorByTid[_k];\n"
        "  if(_v&&typeof _v==='object') _flat.push({key:_k,assistant_msg_idx:_v.assistant_msg_idx,"
        "is_error:_v.is_error,occurrences:_v.occurrences||null});\n"
        "  else _flat.push({key:_k,assistant_msg_idx:null,is_error:_v,occurrences:null});\n"
        "}\n"
        "process.stdout.write(JSON.stringify({map:S._settledToolIsErrorByTid,flat:_flat}));"
    )


# The reviewer's exact probe: turn 1 ``call_0`` succeeded, turn 2 ``call_0``
# failed, both surfaced in the server's settled summary.
REUSED_PROBE = [
    {"name": "terminal", "tid": "call_0", "is_error": False,
     "assistant_msg_idx": 1, "snippet": "ok"},
    {"name": "terminal", "tid": "call_0", "is_error": True,
     "assistant_msg_idx": 4, "snippet": "boom"},
]


def _probe_messages():
    return [
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": [{"type": "tool_use", "id": "call_0", "name": "terminal", "input": {"command": "ls"}}],
         "tool_calls": [{"id": "call_0", "function": {"name": "terminal"}}]},
        {"role": "tool", "tool_call_id": "call_0", "content": "ok"},
        {"role": "user", "content": "second"},
        {"role": "assistant", "content": [{"type": "tool_use", "id": "call_0", "name": "terminal", "input": {"command": "pwd"}}],
         "tool_calls": [{"id": "call_0", "function": {"name": "terminal"}}]},
        {"role": "tool", "tool_call_id": "call_0", "content": "boom"},
    ]


# ── Finding 2a — the producer is occurrence-scoped ─────────────────────────


def _producer_map(payload: dict) -> dict:
    driver = _producer_body() + f"""
const S=_mkS();
_syncToolCallsForLoadedMessages(PAYLOAD.messages, PAYLOAD.sessionToolCalls, PAYLOAD.sessionId||'R9');
{_flatten_script()}
"""
    return _run_node(driver, payload)


def test_reused_tid_producer_is_occurrence_scoped():
    """Bug repro (producer shape): two settled rows share ``call_0``. The
    failure must be recorded only against the owning assistant index (4), and
    the map must NOT expose a tid-wide verdict for the reused id that would
    recolour the earlier successful ``call_0`` on reload."""
    out = _producer_map({"messages": [], "sessionToolCalls": REUSED_PROBE})
    entries = {e["assistant_msg_idx"]: e["is_error"] for e in out["flat"] if e["key"] == "call_0"}
    assert entries.get(4) is True, (
        f"the failed call_0 occurrence was not recorded red: {out['flat']}"
    )
    for e in out["flat"]:
        if e["key"] == "call_0" and e["assistant_msg_idx"] is None:
            raise AssertionError(
                f"reused call_0 has a tid-wide verdict that recolours every card: {out['flat']}"
            )
    assert not (entries.get(1) is True), (
        f"the successful call_0 occurrence is red: {out['flat']}"
    )


def test_browser_copy_live_consumer_resolves_reused_tid_per_occurrence():
    """Bug repro via the real cold-reload render consumer (``copyLiveToolMetadata``,
    static/ui.js): the earlier successful ``call_0`` row must stay green while
    the genuinely failed ``call_0`` row renders red."""
    fn = _function_block(_read("static/ui.js"), "const copyLiveToolMetadata=")
    driver = _producer_body() + f"""
const S=_mkS();
const _res = _syncToolCallsForLoadedMessages(
  PAYLOAD.messages, PAYLOAD.sessionToolCalls, 'R9');
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
    payload = {
        "messages": _probe_messages(),
        "sessionToolCalls": REUSED_PROBE,
        "rows": [
            {"name": "terminal", "tid": "call_0", "assistant_msg_idx": 1, "done": True},
            {"name": "terminal", "tid": "call_0", "assistant_msg_idx": 4, "done": True},
        ],
    }
    out = _run_node(driver, payload)
    assert out[0].get("is_error") is not True, (
        "the earlier successful call_0 was rendered red by the reused tid "
        "(bug: a tid-keyed persisted map paints every card)"
    )
    assert out[1].get("is_error") is True, (
        "the genuinely failed call_0 did not render red"
    )


def test_messages_enrich_consumer_resolves_reused_tid_per_occurrence():
    """The other persisted-map render consumer (``_enrichSettledToolRowBodyFromLive``,
    static/messages.js) must not resurrect the earlier success either: the
    idx-1 row stays green while the idx-4 row renders red."""
    enrich = _function_block(
        _read("static/messages.js"), "function _enrichSettledToolRowBodyFromLive("
    )
    driver = _producer_body() + f"""
function _anchorSceneStringPayload(v) {{ return (v===undefined||v===null)?null:String(v); }}
function _anchorSceneToolArgs(live) {{ return (live && live.args && typeof live.args==='object') ? live.args : null; }}
const S=_mkS();
_syncToolCallsForLoadedMessages(PAYLOAD.messages, PAYLOAD.sessionToolCalls, 'R9');
{enrich}
const apply = (row) => {{
  const r=JSON.parse(JSON.stringify(row));
  const enriched=_enrichSettledToolRowBodyFromLive(r, PAYLOAD.live);
  return {{row:r, enriched:enriched}};
}};
process.stdout.write(JSON.stringify([
  apply(PAYLOAD.successRow),
  apply(PAYLOAD.failedRow),
]));
"""
    payload = {
        "messages": [],
        "sessionToolCalls": REUSED_PROBE,
        "live": {"name": "terminal", "tid": "call_new"},
        "successRow": {
            "tool_call_id": "call_0", "assistant_msg_idx": 1,
            "tool": {"id": "call_0", "name": "terminal", "snippet": "[exit 0]"},
            "payload": {"name": "terminal", "snippet": "[exit 0]"},
        },
        "failedRow": {
            "tool_call_id": "call_0", "assistant_msg_idx": 4,
            "tool": {"id": "call_0", "name": "terminal", "snippet": "[exit 2]"},
            "payload": {"name": "terminal", "snippet": "[exit 2]"},
        },
    }
    out = _run_node(driver, payload)
    assert not out[0]["row"]["tool"].get("is_error"), (
        "the earlier successful tool row was resurrectred red by a reused call_0"
    )
    assert not out[0]["row"]["payload"].get("is_error")
    assert out[1]["row"]["tool"].get("is_error") is True, (
        "the genuinely failed tool row did not render red"
    )
    assert out[1]["row"]["payload"].get("is_error") is True


# ── Control — unique ids still upgrade (rounds 3-8 behaviour unchanged) ────


def test_unique_tid_producer_flat_true_preserved():
    """Control: a genuinely unique failed id still lands as flat ``true`` so
    the historical ``map[tid]===true`` lookups work unchanged."""
    out = _producer_map({
        "messages": [],
        "sessionToolCalls": [{"name": "terminal", "tid": "call_a", "is_error": True, "assistant_msg_idx": 1}],
    })
    assert out["map"].get("call_a") is True, out["flat"]
    assert len(out["flat"]) == 1 and out["flat"][0]["key"] == "call_a"


def test_unique_tid_consumer_still_upgrades():
    """Control: through the real cold-reload consumer, a unique failed id still
    renders red (rounds 3-8 must not regress)."""
    fn = _function_block(_read("static/ui.js"), "const copyLiveToolMetadata=")
    driver = _producer_body() + f"""
const S=_mkS();
_syncToolCallsForLoadedMessages([], PAYLOAD.sessionToolCalls, 'R9');
const liveToolMetadata = [];
const liveMetadataByTid = new Map();
const usedLiveToolMetadata = new Set();
const _persistedIsErrorByTid = S._settledToolIsErrorByTid;
{fn}
const rows = PAYLOAD.rows.map(r=>copyLiveToolMetadata(Object.assign({{}},r), r.name, r.tid));
process.stdout.write(JSON.stringify(rows));
"""
    payload = {
        "sessionToolCalls": [
            {"name": "terminal", "tid": "call_a", "is_error": True, "assistant_msg_idx": 1},
            {"name": "terminal", "tid": "call_b", "is_error": False, "assistant_msg_idx": 4},
        ],
        "rows": [
            {"name": "terminal", "tid": "call_a", "assistant_msg_idx": 1, "done": True},
            {"name": "terminal", "tid": "call_b", "assistant_msg_idx": 4, "done": True},
        ],
    }
    out = _run_node(driver, payload)
    assert out[0].get("is_error") is True  # unique failed id → red
    assert out[1].get("is_error") is not True  # unique succeeded id → green


# ── Finding 2b — the map is session-scoped and cleared on switch ───────────


def test_session_switch_active_stream_clears_map():
    """Bug repro: switching to an *actively streaming* session hits the early
    return in ``_syncToolCallsForLoadedMessages``, which used to leave the
    previous session's verdict map in place (cross-session leak). The map must
    be cleared even on that early-return path."""
    driver = _producer_body() + """
const S=_mkS();
// Session A loaded normally and built a failure map.
_syncToolCallsForLoadedMessages(PAYLOAD.messagesA, PAYLOAD.toolCallsA, 'A');
const _mapA=JSON.parse(JSON.stringify(S._settledToolIsErrorByTid||null));
// Now switch to a DIFFERENT session that is itself actively streaming — the
// refresh bails at the busy/activeStreamId early return.
S.busy=true; S.activeStreamId='stream-B';
_syncToolCallsForLoadedMessages([], [], 'B');
const _mapB=JSON.parse(JSON.stringify(S._settledToolIsErrorByTid||null));
process.stdout.write(JSON.stringify({A:_mapA,B:_mapB}));
"""
    payload = {
        "messagesA": [],
        "toolCallsA": [
            {"name": "terminal", "tid": "call_0", "is_error": True, "assistant_msg_idx": 1, "snippet": "boom"},
        ],
    }
    out = _run_node(driver, payload)
    assert out["A"], f"session A's failure should be recorded: {out}"
    assert not out["B"], f"session B inherited session A's error map: {out}"


def test_session_switch_normal_load_clears_map():
    """Control: an ordinary load of a failing-less session B clears the map."""
    driver = _producer_body() + """
const S=_mkS();
_syncToolCallsForLoadedMessages([], [{'name':'terminal','tid':'call_0','is_error':true,'assistant_msg_idx':1}], 'A');
const _A=JSON.parse(JSON.stringify(S._settledToolIsErrorByTid||null));
_syncToolCallsForLoadedMessages([], [], 'B');
const _B=JSON.parse(JSON.stringify(S._settledToolIsErrorByTid||null));
process.stdout.write(JSON.stringify({A:_A,B:_B}));
"""
    out = _run_node(driver, {})
    assert out["A"], "session A's failure should be recorded"
    assert not out["B"], f"session B inherited session A's error map: {out}"