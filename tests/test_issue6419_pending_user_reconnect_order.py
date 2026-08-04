# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0

"""#6419: mid-stream reconnect must keep pending user message before live response."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
NODE = shutil.which("node")
UI_JS = (ROOT / "static" / "ui.js").read_text(encoding="utf-8")
SESSIONS_JS = (ROOT / "static" / "sessions.js").read_text(encoding="utf-8")


def _function_source(src: str, name: str) -> str:
    start = src.find(f"function {name}(")
    assert start != -1, f"{name} not found"
    brace = src.find("{", start)
    depth = 0
    for i, ch in enumerate(src[brace:]):
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return src[start : brace + i + 1]
    raise AssertionError(f"{name} body unterminated")


def _function_body(src: str, name: str) -> str:
    source = _function_source(src, name)
    return source[source.find("{") :]


# ─── Shared helper contract ────────────────────────────────────────────────

def test_merge_pending_session_message_is_a_global_helper():
    """The fix must expose _mergePendingSessionMessage as a single, top-level
    identity-aware helper rather than duplicated insertion logic nested inside
    individual recovery paths."""
    import re

    # Exactly one definition in sessions.js.
    defs = [m.start() for m in re.finditer(r"\bfunction _mergePendingSessionMessage\b", SESSIONS_JS)]
    assert len(defs) == 1, (
        f"_mergePendingSessionMessage should be defined exactly once; found {len(defs)}. "
        "The fix must consolidate helper logic at one chokepoint, not duplicate it."
    )
    # The sole definition must be at a top-level scope (zero indentation), not nested.
    line_start = SESSIONS_JS.rfind("\n", 0, defs[0]) + 1
    indent = len(SESSIONS_JS[line_start:defs[0]]) - len(SESSIONS_JS[line_start:defs[0]].lstrip())
    assert indent == 0, (
        f"_mergePendingSessionMessage must be at module top level (indent=0); found indent={indent}."
    )


def test_merge_helper_inserts_pending_user_before_live_assistant():
    """Given a live assistant row, the pending user row must appear before it."""
    body = _function_source(SESSIONS_JS, "_mergePendingSessionMessage")
    assert "getPendingSessionMessage" in body, "helper derives the candidate row"
    assert "findIndex(m=>m&&m.role==='assistant'&&m._live)" in body, (
        "helper looks for the live assistant boundary"
    )
    assert "messages.splice(liveAssistantIdx,0,pendingMsg)" in body, (
        "helper inserts pending user before the live assistant"
    )
    assert "messages.push(pendingMsg)" in body, (
        "helper falls back to append only when there is no live assistant"
    )
    assert "_hasCurrentTailUserDuplicate" in body, (
        "helper deduplicates against the current tail user row"
    )


def test_refreshSession_uses_shared_helper():
    """refreshSession() must no longer append pending_user_message at the end
    unconditionally; it must route through _mergePendingSessionMessage so that a
    live assistant tail during recovery keeps the user prompt before it."""
    body = _function_body(UI_JS, "refreshSession")
    assert "_mergePendingSessionMessage" in body, (
        "refreshSession uses the shared identity-aware merge helper"
    )
    assert body.find("_mergePendingSessionMessage") < body.find("renderMessages"), (
        "merge must happen before the transcript is re-rendered"
    )
    # The old unconditional push must be gone.
    assert "if(pendingMsg) S.messages.push(pendingMsg)" not in body, (
        "refreshSession must not unconditionally push the pending message"
    )


# ─── loadSession reattach path (was already correct before #6419) ─────────────────

def test_loadSession_inflight_reattach_merges_pending_user_before_render():
    """Regression for the #2341 contract: loadSession INFLIGHT branch must call
    the shared helper and render afterwards."""
    start = SESSIONS_JS.find("if(INFLIGHT[sid]){")
    assert start != -1, "loadSession INFLIGHT branch not found"
    end = SESSIONS_JS.find("}else{", start)
    assert end != -1, "loadSession INFLIGHT branch end not found"
    block = SESSIONS_JS[start:end]

    merge_pos = block.find("_mergePendingSessionMessage")
    render_pos = block.find("renderMessages(")
    assert merge_pos != -1, "INFLIGHT branch must merge pending user message"
    assert render_pos != -1, "INFLIGHT branch must render messages"
    assert merge_pos < render_pos, (
        "pending user row must be merged before renderMessages() rebuilds the transcript"
    )


def _run_node(script: str) -> dict:
    completed = subprocess.run(
        [NODE, "-e", script],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr or completed.stdout
    return json.loads(completed.stdout)


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_merge_helper_repairs_malformed_order_and_is_idempotent():
    """A reconnect can already contain [live assistant, pending user]. The
    canonical helper must move that exact user row before the live boundary,
    preserve its metadata/attachments, and remain ordered on repeated probes."""
    helpers = "\n".join(
        [
            _function_source(SESSIONS_JS, "_messageComparableText"),
            _function_source(SESSIONS_JS, "_stripAttachedFilesMarker"),
            _function_source(SESSIONS_JS, "_stripForcedSkillEnvelope"),
            _function_source(SESSIONS_JS, "_normalizeUserTranscriptText"),
            _function_source(SESSIONS_JS, "_sameTranscriptMessage"),
            _function_source(SESSIONS_JS, "_currentTailUserMessage"),
            _function_source(SESSIONS_JS, "_hasCurrentTailUserDuplicate"),
            _function_source(SESSIONS_JS, "_mergePendingSessionMessage"),
        ]
    )
    script = f"""
{helpers}
function getPendingSessionMessage(session, messages){{
  const text=String(session.pending_user_message||'').trim();
  if(!text) return null;
  return {{role:'user',content:text,_pending:true,_ts:session.pending_started_at}};
}}
const historical={{role:'user',content:'same prompt',_ts:1}};
const settled={{role:'assistant',content:'old answer',_ts:2}};
const live={{role:'assistant',content:'working',_live:true,_ts:4}};
const misplaced={{
  role:'user',content:'same prompt',_pending:true,_ts:3,
  attachments:[{{path:'proof.txt'}}],_source:'resume'
}};
const messages=[historical,settled,live,misplaced];
const session={{pending_user_message:'same prompt',pending_started_at:3}};
const first=_mergePendingSessionMessage(session,messages);
const afterFirst=messages.map(m=>({{role:m.role,content:m.content,live:!!m._live,ts:m._ts,attachments:m.attachments,source:m._source}}));
const second=_mergePendingSessionMessage(session,messages);
process.stdout.write(JSON.stringify({{first,second,afterFirst,final:messages}}));
"""
    result = _run_node(script)

    assert result["first"] is True
    assert result["second"] is False
    assert [row["role"] for row in result["afterFirst"]] == [
        "user",
        "assistant",
        "user",
        "assistant",
    ]
    repaired = result["afterFirst"][2]
    assert repaired["ts"] == 3
    assert repaired["attachments"] == [{"path": "proof.txt"}]
    assert repaired["source"] == "resume"
    assert sum(1 for row in result["final"] if row.get("_ts") == 3) == 1


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_refresh_session_uses_canonical_helper_before_render():
    """The actual refresh path must project [user, live assistant] and render
    once when the canonical helper is available."""
    helpers = "\n".join(
        [
            _function_source(SESSIONS_JS, "_messageComparableText"),
            _function_source(SESSIONS_JS, "_stripAttachedFilesMarker"),
            _function_source(SESSIONS_JS, "_stripForcedSkillEnvelope"),
            _function_source(SESSIONS_JS, "_normalizeUserTranscriptText"),
            _function_source(SESSIONS_JS, "_sameTranscriptMessage"),
            _function_source(SESSIONS_JS, "_currentTailUserMessage"),
            _function_source(SESSIONS_JS, "_hasCurrentTailUserDuplicate"),
            _function_source(SESSIONS_JS, "_mergePendingSessionMessage"),
        ]
    )
    refresh = "async " + _function_source(UI_JS, "refreshSession")
    script = f"""
{helpers}
let rendered=0;
let status='';
const S={{session:{{session_id:'sid-1'}},messages:[]}};
const window={{_restartingForUpdate:false}};
function dismissReconnect(){{}}
function getPendingSessionMessage(session, messages){{
  const text=String(session.pending_user_message||'').trim();
  if(!text) return null;
  return {{role:'user',content:text,_pending:true,_ts:session.pending_started_at}};
}}
async function api(){{return {{session:{{
  session_id:'sid-1',
  active_stream_id:'stream-1',
  pending_user_message:'prompt',
  pending_started_at:3,
  messages:[{{role:'assistant',content:'working',_live:true,_ts:4}}]
}}}};}}
function syncTopbar(){{}}
function _renderMessagesWithScrollSnapshot(){{rendered+=1;}}
function showToast(){{}}
function setStatus(value){{status=value;}}
{refresh}
refreshSession().then(()=>process.stdout.write(JSON.stringify({{rendered,status,roles:S.messages.map(m=>m.role)}})));
"""
    result = _run_node(script)

    assert result["rendered"] == 1
    assert result["status"] == ""
    assert result["roles"] == ["user", "assistant"]


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_refresh_session_fails_closed_when_canonical_helper_is_unavailable():
    """Partial bundles must not reintroduce the old append path. If the canonical
    helper is unavailable, refreshSession reports failure and never renders an
    incorrectly ordered transcript."""
    refresh = "async " + _function_source(UI_JS, "refreshSession")
    script = f"""
let rendered=0;
let status='';
const S={{session:{{session_id:'sid-1'}},messages:[]}};
const window={{_restartingForUpdate:false}};
function dismissReconnect(){{}}
async function api(){{return {{session:{{
  session_id:'sid-1',
  active_stream_id:'stream-1',
  pending_user_message:'prompt',
  messages:[{{role:'assistant',content:'working',_live:true}}]
}}}};}}
function syncTopbar(){{}}
function _renderMessagesWithScrollSnapshot(){{rendered+=1;}}
function showToast(){{}}
function setStatus(value){{status=value;}}
{refresh}
refreshSession().then(()=>process.stdout.write(JSON.stringify({{rendered,status,roles:S.messages.map(m=>m.role)}})));
"""
    result = _run_node(script)

    assert result["rendered"] == 0
    assert result["roles"] == ["assistant"]
    assert "Pending-session merge helper unavailable" in result["status"]
