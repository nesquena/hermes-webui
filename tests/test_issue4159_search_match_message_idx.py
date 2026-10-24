"""Regression for #4159 (salvage of PR #4224, hardened in #5106): clicking a
sidebar session-search result that matched on message CONTENT opens that
session AND scrolls to the matched message and flashes it.

Hardening properties pinned here (one block each):

1. Backend: content hits carry ``match_message_idx``, the RAW index of the hit
   in the FULL ``sess.messages`` list (hidden internal rows still count toward
   the index, because the /api/session payload keeps them; they just never
   match). Title hits carry no index.
2. Frontend: the full-session index is translated to the LOCAL rendered index.
   A truncated (paged) session is force-loaded through the uncapped
   ``_ensureAllMessagesLoaded()`` (``msg_limit=all``), never a bigger tail
   window, and a stale DOM row with the same local id never stands in for the
   target.
3. Assistant-message hits jump too (not only user rows).
4. Session-switch race: if the user switched (or started switching) sessions
   before the jump fires, nothing happens.
5. Virtualization: the jump is delegated to the transcript's
   virtualization-aware ``jumpToTurnQuestion`` (ui.js), which materializes a
   target that is outside the virtual window instead of assuming a DOM row.

Plus backward compatibility: a missing/invalid index just opens the session.

The frontend tests run the REAL functions extracted from static/*.js in node
against small deterministic stubs.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import urlparse

import pytest

_REPO = Path(__file__).resolve().parent.parent
_OUTLINE_JS = _REPO / "static" / "outline.js"
_SESSIONS_JS = _REPO / "static" / "sessions.js"
_UI_JS = _REPO / "static" / "ui.js"
NODE = shutil.which("node")
needs_node = pytest.mark.skipif(NODE is None, reason="node required")


# --------------------------------------------------------------------------- #
# 1. Backend: match_message_idx is surfaced on content hits
# --------------------------------------------------------------------------- #
def _run_search(query, *, session_messages, sessions_meta=None):
    import api.routes as routes

    meta = sessions_meta or [
        {"session_id": "s1", "title": "Untitled", "profile": "default"}
    ]
    session = SimpleNamespace(session_id="s1", messages=session_messages)
    captured = {}

    def fake_j(handler, payload, status=200, extra_headers=None):
        captured["status"] = status
        captured["payload"] = payload

    # The content scan reads through get_session_for_scan (the LRU-transparent
    # accessor); patching get_session would leave the result set empty.
    with patch("api.routes.all_sessions", return_value=list(meta)), patch(
        "api.routes.get_session_for_scan", return_value=session
    ), patch("api.profiles.get_active_profile_name", return_value="default"), patch(
        "api.routes.j", side_effect=fake_j
    ):
        routes._handle_sessions_search(SimpleNamespace(), urlparse(query))
    return captured


def _hidden_row(text="ASYNC DELEGATION needle wakeup"):
    return {"role": "user", "content": text, "_source": "delegation_wakeup"}


def test_content_match_includes_message_index():
    msgs = [
        {"role": "user", "content": "first message"},
        {"role": "assistant", "content": "second message - no hit"},
        {"role": "user", "content": "NEEDLE in the third message"},
        {"role": "assistant", "content": "fourth message"},
    ]
    captured = _run_search(
        "/api/sessions/search?q=needle&content=1&depth=10", session_messages=msgs
    )
    assert captured["status"] == 200
    results = captured["payload"]["sessions"]
    assert len(results) == 1
    hit = results[0]
    assert hit["match_type"] == "content"
    assert hit["match_message_idx"] == 2


def test_content_match_returns_first_hit_index_not_last():
    msgs = [
        {"role": "user", "content": "alpha NEEDLE first"},
        {"role": "user", "content": "beta NEEDLE second"},
    ]
    captured = _run_search(
        "/api/sessions/search?q=needle&content=1&depth=10", session_messages=msgs
    )
    assert captured["payload"]["sessions"][0]["match_message_idx"] == 0


def test_assistant_content_match_reports_its_index():
    msgs = [
        {"role": "user", "content": "question"},
        {"role": "assistant", "content": "the NEEDLE is in the reply"},
    ]
    captured = _run_search(
        "/api/sessions/search?q=needle&content=1&depth=10", session_messages=msgs
    )
    assert captured["payload"]["sessions"][0]["match_message_idx"] == 1


def test_hidden_rows_count_toward_full_index_but_never_match_or_use_depth():
    """The index is into the FULL sess.messages list. Hidden delegation rows are
    in the /api/session payload (the renderer just skips them), so they must
    shift the index; they still neither match nor consume the depth budget."""
    msgs = [
        _hidden_row(),  # contains the needle but must not match
        _hidden_row("another hidden row"),
        {"role": "user", "content": "plain question"},
        {"role": "assistant", "content": "reply with the NEEDLE"},
    ]
    captured = _run_search(
        "/api/sessions/search?q=needle&content=1&depth=2", session_messages=msgs
    )
    hit = captured["payload"]["sessions"][0]
    assert hit["match_message_idx"] == 3, (
        "raw index into sess.messages (hidden rows included), not an index "
        "into the filtered scan pool"
    )


def test_title_match_does_not_include_message_index():
    meta = [{"session_id": "s1", "title": "needle in the title", "profile": "default"}]
    captured = _run_search(
        "/api/sessions/search?q=needle&content=1",
        session_messages=[{"role": "user", "content": "no hit here"}],
        sessions_meta=meta,
    )
    hit = captured["payload"]["sessions"][0]
    assert hit["match_type"] == "title"
    assert "match_message_idx" not in hit


def test_no_match_returns_empty_results():
    captured = _run_search(
        "/api/sessions/search?q=needle&content=1",
        session_messages=[{"role": "user", "content": "no hit here"}],
    )
    assert captured["payload"]["count"] == 0


# --------------------------------------------------------------------------- #
# Node harness for outline.js's _jumpToFullSessionMessage
# --------------------------------------------------------------------------- #
_EXTRACT = r"""
const fs=require('fs');
function extractFrom(source, name){
  const start=source.search(new RegExp('(?:async\\s+)?function\\s+'+name+'\\s*\\('));
  if(start<0) throw Error('missing '+name);
  let i=source.indexOf('{',start),depth=1; i++;
  while(depth&&i<source.length){if(source[i]==='{')depth++;if(source[i]==='}')depth--;i++;}
  return source.slice(start,i);
}
const outlineSrc=fs.readFileSync(process.argv[1],'utf8');
const uiSrc=fs.readFileSync(process.argv[2],'utf8');
const scenario=JSON.parse(process.argv[3]);
"""

_JUMP_DRIVER = _EXTRACT + r"""
const events=[], frames=[];
global.window=global;
// Full transcript: user/assistant pairs; the NEEDLE sits at full index 2 (user)
// and 3 (assistant).
const FULL=[];
for(let i=0;i<10;i++){
  FULL.push({role:i%2===0?'user':'assistant',content:(i===2||i===3)?'NEEDLE '+i:'msg '+i});
}
const TAIL_OFFSET=6;
global.S={session:{session_id:'a'},busy:!!scenario.busy,activeStreamId:null};
global._loadingSessionId=scenario.loadingOther?'b':null;
if(scenario.truncated){
  S.messages=FULL.slice(TAIL_OFFSET);
  global._oldestIdx=TAIL_OFFSET;
  global._messagesTruncated=true;
}else{
  S.messages=FULL.slice();
  global._oldestIdx=0;
  global._messagesTruncated=false;
}
// A compression continuation's display stitches its parent's archived rows in
// front of its own, so the server's index (into its OWN messages) is shifted.
if(scenario.stitchPrefix){
  const pre=[];
  for(let i=0;i<scenario.stitchPrefix;i++) pre.push({role:i%2===0?'user':'assistant',content:'archived '+i});
  S.messages=pre.concat(S.messages);
}
global._messageRenderWindowSize=30;
global._currentMessageRenderWindowSize=()=>_messageRenderWindowSize;
global._messageRenderableMessageCount=()=>S.messages.length;
// DOM: rows stamped like ui.js does (data-msg-idx local, data-session-msg-idx
// absolute = _oldestIdx + local, user rows also get id msg-user-<local>).
let rows=[];
function stamp(base){
  rows=S.messages.map((m,i)=>({id:m.role==='user'?'msg-user-'+i:'',dataset:{msgIdx:String(i),sessionMsgIdx:String(base+i)},m}));
}
stamp(scenario.staleBase!=null?scenario.staleBase:_oldestIdx);
const container={
  querySelectorAll(sel){
    const mm=/data-msg-idx="(\d+)"/.exec(sel);
    return mm?rows.filter(r=>r.dataset.msgIdx===mm[1]):[];
  },
};
global.document={getElementById:id=>id==='messages'?container:(rows.find(r=>r.id===id)||null)};
global.requestAnimationFrame=f=>{frames.push(f);return frames.length;};
global._cancelBottomSettle=()=>events.push('cancel-settle');
global._beginMessageJumpScroll=()=>events.push('begin-jump');
global.renderMessages=()=>{events.push('render');stamp(_oldestIdx);};
global.api=async()=>{events.push('direct-api');return null;};
let ensureOpts=null;
global._ensureAllMessagesLoaded=async(opts)=>{
  events.push('ensure-all');
  ensureOpts=opts===undefined?null:opts;
  if(scenario.switchDuringLoad){
    // The user opened session b meanwhile (fully loaded, offset 0): the shared
    // loader bails, and a's index must not be applied to b's transcript.
    S.session={session_id:'b'};S.messages=FULL.slice();_oldestIdx=0;_messagesTruncated=false;
    return false;
  }
  if(scenario.staleDuringLoad){
    // The user sent a turn while the request was out: with rejectIfStale the
    // shared loader leaves the live transcript alone and reports false.
    S.messages.push({role:'user',content:'NEW PROMPT'});
    return false;
  }
  if(!_messagesTruncated) return true;
  S.messages=FULL.slice();_oldestIdx=0;_messagesTruncated=false;
  return true;
};
let delegated=null;
global.jumpToTurnQuestion=async(q,a)=>{
  const row=document.getElementById('msg-user-'+q)||rows.find(r=>r.dataset.msgIdx===String(q))||null;
  delegated={q,a,rowSessionIdx:row?Number(row.dataset.sessionMsgIdx):null,rowContent:row?row.m.content:null};
  events.push('delegate');
};
for(const name of ['_messageSessionIndexBase','_messageRawIdxForSessionIndex']) eval(extractFrom(uiSrc,name));
for(const name of ['_currentSid','_expandOutlineRenderWindow','_searchHitMessageText','_jumpToFullSessionMessage']) eval(extractFrom(outlineSrc,name));
(async()=>{
  let idx=scenario.idx;
  if(scenario.idxKind==='nan') idx=NaN;
  const p=_jumpToFullSessionMessage(idx, scenario.targetSid===undefined?'a':scenario.targetSid, scenario.query);
  for(let i=0;i<20;i++){
    await new Promise(r=>setImmediate(r));
    if(scenario.switchDuringFrame&&frames.length) S.session={session_id:'b'};
    while(frames.length) frames.shift()();
  }
  const ok=await p;
  console.log(JSON.stringify({ok,events,delegated,oldestIdx:_oldestIdx,ensureOpts,messageCount:S.messages.length}));
})().catch(e=>{console.error(e);process.exitCode=1;});
"""


def _run_jump(**scenario):
    result = subprocess.run(
        [NODE, "-e", _JUMP_DRIVER, str(_OUTLINE_JS), str(_UI_JS), json.dumps(scenario)],
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


# --------------------------------------------------------------------------- #
# 2. Full -> local translation; uncapped full-history load; stale DOM
# --------------------------------------------------------------------------- #
@needs_node
def test_untruncated_session_jumps_without_loading_or_rerendering():
    r = _run_jump(idx=2)
    assert r["ok"] is True
    assert r["events"] == ["delegate"]
    assert r["delegated"]["q"] == 2
    assert r["delegated"]["rowSessionIdx"] == 2
    assert r["delegated"]["rowContent"] == "NEEDLE 2"


@needs_node
def test_truncated_session_force_loads_full_history_then_jumps_to_full_index():
    """Full index 2 is BEFORE the loaded tail (offset 6). Treating it as a local
    index would hit full message 8; the helper must load full history through
    the shared uncapped loader, re-render, and only then resolve."""
    r = _run_jump(idx=2, truncated=True)
    assert r["ok"] is True
    assert "direct-api" not in r["events"], "must reuse _ensureAllMessagesLoaded"
    assert r["events"].count("ensure-all") == 1
    assert r["events"].index("ensure-all") < r["events"].index("render") < r["events"].index("delegate")
    assert r["oldestIdx"] == 0
    assert r["delegated"]["q"] == 2
    assert r["delegated"]["rowSessionIdx"] == 2
    assert r["delegated"]["rowContent"] == "NEEDLE 2"
    assert r["ensureOpts"] == {"rejectIfStale": True}, "the automatic load must opt in to the freshness guard"


@needs_node
def test_stale_full_history_reply_stops_the_jump_and_keeps_the_live_transcript():
    """The user sends a turn while the full-history request is out. The loader
    rejects the stale reply; the jump must stop there: no re-render, no
    delegation, and the live (still paged) transcript keeps the new prompt."""
    r = _run_jump(idx=2, truncated=True, staleDuringLoad=True, query="needle")
    assert r["ok"] is False
    assert "render" not in r["events"]
    assert r["delegated"] is None
    assert r["oldestIdx"] == 6
    assert r["messageCount"] == 5  # 4-row tail + the new prompt


@needs_node
def test_truncated_hit_inside_tail_resolves_to_the_same_absolute_message():
    r = _run_jump(idx=8, truncated=True)
    assert r["delegated"]["rowSessionIdx"] == 8
    assert r["delegated"]["rowContent"] == "msg 8"


@needs_node
def test_streaming_truncated_session_translates_via_oldest_idx_without_loading():
    """While a turn streams the full reload is skipped; the full index is still
    translated through _oldestIdx (8 - 6 = local 2), and a hit outside the
    loaded tail is a no-op instead of a wrong row."""
    inside = _run_jump(idx=8, truncated=True, busy=True)
    assert "ensure-all" not in inside["events"]
    assert inside["delegated"]["q"] == 2
    assert inside["delegated"]["rowSessionIdx"] == 8
    outside = _run_jump(idx=2, truncated=True, busy=True)
    assert outside["ok"] is False
    assert outside["delegated"] is None


@needs_node
def test_stale_row_with_same_local_id_is_rerendered_before_jumping():
    """Full history loaded elsewhere (outline panel) without a re-render leaves
    rows stamped for the old tail offset: local id 2 shows absolute 8. The jump
    must re-render first so the delegate resolves the real target."""
    r = _run_jump(idx=2, staleBase=6)
    assert r["events"].index("render") < r["events"].index("delegate")
    assert r["delegated"]["rowSessionIdx"] == 2
    assert r["delegated"]["rowContent"] == "NEEDLE 2"


def test_full_history_loader_is_uncapped():
    """The helper relies on _ensureAllMessagesLoaded being a TRUE full load
    (msg_limit=all) that rebases _oldestIdx to 0 — not a msg_limit=9999 tail."""
    src = _SESSIONS_JS.read_text(encoding="utf-8")
    start = src.index("async function _ensureAllMessagesLoaded(")
    body = src[start:start + 6000]
    assert "msg_limit=all" in body
    assert "_oldestIdx = 0;" in body
    outline = _OUTLINE_JS.read_text(encoding="utf-8")
    start = outline.index("async function _jumpToFullSessionMessage(")
    helper = outline[start:outline.index("\n}\n", start)]
    assert "msg_limit" not in helper.replace("(msg_limit=all)", "")
    assert "'msg-user-' + fullIdx" not in helper


# The REAL shared loader from sessions.js, with a held api() reply so the test
# can change the transcript while the full-history request is out.
_LOADER_DRIVER = r"""
const fs=require('fs');
function extractFrom(source, name){
  const start=source.search(new RegExp('(?:async\\s+)?function\\s+'+name+'\\s*\\('));
  if(start<0) throw Error('missing '+name);
  let i=source.indexOf('{',start),depth=1; i++;
  while(depth&&i<source.length){if(source[i]==='{')depth++;if(source[i]==='}')depth--;i++;}
  return source.slice(start,i);
}
const src=fs.readFileSync(process.argv[1],'utf8');
const scenario=JSON.parse(process.argv[2]);
global.window=global;
const FULL=[];
for(let i=0;i<10;i++) FULL.push({role:i%2===0?'user':'assistant',content:'msg '+i});
global.S={session:{session_id:'a',message_count:10},busy:false,activeStreamId:null,messages:FULL.slice(6)};
global._messagesTruncated=!scenario.untruncated;
global._oldestIdx=6;
global._loadingOlder=false;
global._loadingSessionId=null;
global._messagesGeneration=0;
global._bumpMessagesGeneration=()=>{_messagesGeneration++;};
let synced=0;
global._syncToolCallsForLoadedMessages=()=>{synced++;};
global._advanceVisibleBaselineForServerKnownRows=()=>{};
let release=null, calls=0;
global.api=()=>{calls++;return new Promise(r=>{release=()=>r({session:{session_id:'a',messages:FULL.slice(),
  tool_calls:[],message_count:10,visible_message_count:10}});});};
for(const name of ['_transcriptFreshnessSnapshot','_transcriptIsFresh','_ensureAllMessagesLoaded']) eval(extractFrom(src,name));
(async()=>{
  const p=_ensureAllMessagesLoaded(scenario.opts);
  await new Promise(r=>setImmediate(r));
  switch(scenario.during){
    case 'send': S.messages.push({role:'user',content:'NEW PROMPT'});S.busy=true;break;
    case 'sendFinished': S.messages.push({role:'user',content:'NEW PROMPT'});
      S.messages.push({role:'assistant',content:'NEW ANSWER'});break;
    case 'streamStarted': S.activeStreamId='stream-1';break;
    case 'replaced': S.messages=S.messages.map(m=>Object.assign({},m));break;
    case 'switch': S.session={session_id:'b'};break;
  }
  if(release) release();
  const result=await p;
  console.log(JSON.stringify({result,calls,contents:S.messages.map(m=>m.content),oldestIdx:_oldestIdx,
    truncated:_messagesTruncated,synced,loadingOlder:_loadingOlder}));
})().catch(e=>{console.error(e);process.exitCode=1;});
"""


def _run_loader(**scenario):
    result = subprocess.run(
        [NODE, "-e", _LOADER_DRIVER, str(_SESSIONS_JS), json.dumps(scenario)],
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


@needs_node
@pytest.mark.parametrize(
    "during,tail",
    [
        ("send", ["msg 6", "msg 7", "msg 8", "msg 9", "NEW PROMPT"]),
        ("sendFinished", ["msg 6", "msg 7", "msg 8", "msg 9", "NEW PROMPT", "NEW ANSWER"]),
        ("streamStarted", ["msg 6", "msg 7", "msg 8", "msg 9"]),
        ("replaced", ["msg 6", "msg 7", "msg 8", "msg 9"]),
    ],
)
def test_loader_rejects_a_reply_that_is_stale_by_the_time_it_lands(during, tail):
    """With rejectIfStale, a full-history reply that lands after the transcript
    changed (a send in flight, a turn that already finished, a stream that
    started, a reload that replaced the array) touches nothing: messages, tool
    calls, paging and counts all stay as the user sees them."""
    r = _run_loader(opts={"rejectIfStale": True}, during=during)
    assert r["result"] is False
    assert r["contents"] == tail
    assert r["oldestIdx"] == 6
    assert r["truncated"] is True
    assert r["synced"] == 0
    assert r["loadingOlder"] is False, "the mutex must be released on the reject path"


@needs_node
@pytest.mark.parametrize("opts", [None, {"rejectIfStale": True}])
def test_loader_applies_a_fresh_reply_and_reports_it(opts):
    r = _run_loader(opts=opts, during=None)
    assert r["result"] is True
    assert r["calls"] == 1
    assert r["contents"] == [f"msg {i}" for i in range(10)]
    assert r["oldestIdx"] == 0
    assert r["truncated"] is False
    assert r["synced"] == 1


@needs_node
def test_loader_reports_false_on_session_switch_and_true_when_already_full():
    switched = _run_loader(opts={"rejectIfStale": True}, during="switch")
    assert switched["result"] is False
    assert switched["oldestIdx"] == 6 and switched["synced"] == 0
    full = _run_loader(opts={"rejectIfStale": True}, untruncated=True)
    assert full["result"] is True
    assert full["calls"] == 0


# --------------------------------------------------------------------------- #
# 3. Assistant-message hits jump too
# --------------------------------------------------------------------------- #
@needs_node
def test_assistant_hit_delegates_with_assistant_segment_index():
    r = _run_jump(idx=3, truncated=True)
    assert r["ok"] is True
    assert r["delegated"]["q"] == 3
    assert r["delegated"]["a"] == 3, "assistant hits resolve the assistant segment"


@needs_node
def test_user_hit_passes_no_assistant_index_so_the_row_is_highlighted():
    r = _run_jump(idx=2)
    assert r["delegated"]["a"] == -1


# --------------------------------------------------------------------------- #
# 4. Session-switch race guard
# --------------------------------------------------------------------------- #
@needs_node
def test_switched_before_the_jump_fires_does_nothing():
    r = _run_jump(idx=2, targetSid="other")
    assert r["ok"] is False
    assert r["events"] == []


@needs_node
def test_switched_while_full_history_loads_does_nothing():
    r = _run_jump(idx=2, truncated=True, switchDuringLoad=True)
    assert r["ok"] is False
    assert "render" not in r["events"]
    assert r["delegated"] is None


@needs_node
def test_switched_during_post_render_frame_does_nothing():
    r = _run_jump(idx=2, truncated=True, switchDuringFrame=True)
    assert "render" in r["events"]
    assert r["delegated"] is None


@needs_node
def test_another_session_loading_does_nothing():
    r = _run_jump(idx=2, loadingOther=True)
    assert r["ok"] is False
    assert r["events"] == []


# --------------------------------------------------------------------------- #
# Backward compatibility: missing / invalid index
# --------------------------------------------------------------------------- #
@needs_node
@pytest.mark.parametrize(
    "scenario",
    [
        {"idx": -1},
        {"idx": 1.5},
        {"idx": "2"},
        {"idx": None},
        {"idxKind": "nan", "idx": 0},
        {"idx": 99},
    ],
    ids=["negative", "fractional", "string", "null", "nan", "out-of-range"],
)
def test_invalid_index_never_jumps(scenario):
    r = _run_jump(**scenario)
    assert r["ok"] is False
    assert r["delegated"] is None
    assert "render" not in r["events"]


# --------------------------------------------------------------------------- #
# 5. Virtualization-aware jump (real jumpToTurnQuestion from ui.js)
# --------------------------------------------------------------------------- #
_VIRTUAL_DRIVER = _EXTRACT + r"""
const events=[], frames=[];
global.window=global;
const N=200, ROW=100, VIEW=500;
const FULL=[];
for(let i=0;i<N;i++) FULL.push({role:i%2===0?'user':'assistant',content:i===scenario.idx?'NEEDLE':'msg '+i});
global.S={session:{session_id:'a'},messages:FULL.slice(),busy:false,activeStreamId:null};
global._oldestIdx=0;global._messagesTruncated=false;global._loadingSessionId=null;
global._programmaticScroll=false;global._programmaticScrollSetAt=0;
global._messageVirtualWindowKey='x';
global._messageRenderWindowSize=30;
global.performance={now:()=>0};
const container={scrollTop:(N-5)*ROW,scrollHeight:N*ROW,clientHeight:VIEW};
// Virtualized DOM: only rows inside [scrollTop-ROW, scrollTop+VIEW+ROW] exist.
let rows=[];
function mk(i){
  const m=S.messages[i];
  const r={id:m.role==='user'?'msg-user-'+i:'',dataset:{msgIdx:String(i),sessionMsgIdx:String(_oldestIdx+i)},
    classList:{remove(){},add(c){events.push('highlight:'+i);}},offsetWidth:1,
    getClientRects(){return [{}];},
    scrollIntoView(o){events.push('scroll:'+i+':'+o.block);}};
  return r;
}
function renderWindow(){
  rows=[];
  const lo=Math.max(0,Math.floor(container.scrollTop/ROW)-1), hi=Math.min(N-1,Math.ceil((container.scrollTop+VIEW)/ROW)+1);
  for(let i=lo;i<=hi;i++) rows.push(mk(i));
}
renderWindow();
container.querySelectorAll=sel=>{const mm=/data-msg-idx="(\d+)"/.exec(sel);return mm?rows.filter(r=>r.dataset.msgIdx===mm[1]):[];};
global.document={getElementById:id=>id==='messages'?container:(rows.find(r=>r.id===id)||null)};
global.$=id=>document.getElementById(id);
global.requestAnimationFrame=f=>{frames.push(f);return frames.length;};
global.renderMessages=()=>{events.push('render@'+container.scrollTop);renderWindow();};
global._cancelBottomSettle=()=>{};
global._beginMessageJumpScroll=()=>{};
global._deferClearProgrammaticScroll=()=>{};
global._messageHiddenBeforeCount=()=>0;
global._currentMessageRenderWindowSize=()=>_messageRenderWindowSize;
global._messageRenderableMessageCount=()=>N;
global._getVisibleMessagesWithIdx=()=>S.messages.map((m,rawIdx)=>({m,rawIdx}));
global._messageVirtualScrollTopForVisibleIdx=(vis,idx,c)=>Math.max(0,idx*ROW-Math.round(c.clientHeight*0.35));
global.window.setTimeout=(f)=>0;
global._ensureAllMessagesLoaded=async()=>{events.push('ensure-all');};
for(const name of ['_messageSessionIndexBase','_messageRawIdxForSessionIndex','_messageVisibleIndexForRawIdx','_userMessageDomId','_highlightQuestionRow','_messageJumpScrollBehavior','jumpToTurnQuestion']) eval(extractFrom(uiSrc,name));
for(const name of ['_currentSid','_expandOutlineRenderWindow','_searchHitMessageText','_jumpToFullSessionMessage']) eval(extractFrom(outlineSrc,name));
(async()=>{
  const targetInDomBefore=rows.some(r=>r.dataset.msgIdx===String(scenario.idx));
  const p=_jumpToFullSessionMessage(scenario.idx,'a');
  for(let i=0;i<20;i++){await new Promise(r=>setImmediate(r));while(frames.length) frames.shift()();}
  const ok=await p;
  console.log(JSON.stringify({ok,events,targetInDomBefore}));
})().catch(e=>{console.error(e);process.exitCode=1;});
"""


def _run_virtual(**scenario):
    result = subprocess.run(
        [NODE, "-e", _VIRTUAL_DRIVER, str(_OUTLINE_JS), str(_UI_JS), json.dumps(scenario)],
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


@needs_node
def test_virtualized_user_target_outside_dom_is_materialized_and_highlighted():
    r = _run_virtual(idx=10)
    assert r["targetInDomBefore"] is False
    assert r["ok"] is True
    renders = [e for e in r["events"] if e.startswith("render@")]
    assert renders, "the virtual window must be moved to the target and re-rendered"
    assert "scroll:10:center" in r["events"]
    assert "highlight:10" in r["events"]


@needs_node
def test_virtualized_assistant_target_outside_dom_is_materialized():
    r = _run_virtual(idx=11)
    assert r["targetInDomBefore"] is False
    assert any(e.startswith("render@") for e in r["events"])
    assert "scroll:11:start" in r["events"]


# --------------------------------------------------------------------------- #
# sessions.js: the search-result click dispatches the jump
# --------------------------------------------------------------------------- #
_CLICK_DRIVER = r"""
const fs=require('fs');
const source=fs.readFileSync(process.argv[1],'utf8');
const scenario=JSON.parse(process.argv[2]);
function extract(name){
  const start=source.search(new RegExp('function\\s+'+name+'\\s*\\('));
  if(start<0) throw Error('missing '+name);
  let i=source.indexOf('{',start),depth=1; i++;
  while(depth&&i<source.length){if(source[i]==='{')depth++;if(source[i]==='}')depth--;i++;}
  return source.slice(start,i);
}
global.window=global;
const timers=[], calls=[];
window.setTimeout=(f)=>{timers.push(f);return timers.length;};
if(scenario.helper==='ok') window._jumpToFullSessionMessage=(idx,sid)=>{calls.push([idx,sid]);return Promise.resolve(true);};
if(scenario.helper==='throws') window._jumpToFullSessionMessage=()=>{calls.push('threw');throw new Error('boom');};
if(scenario.helper==='rejects') window._jumpToFullSessionMessage=()=>{calls.push('rejected');return Promise.reject(new Error('boom'));};
eval(extract('_jumpToSessionSearchHit'));
(async()=>{
  const scheduled=_jumpToSessionSearchHit(scenario.session);
  while(timers.length) timers.shift()();
  await new Promise(r=>setImmediate(r));
  console.log(JSON.stringify({scheduled,calls}));
})().catch(e=>{console.error(e);process.exitCode=1;});
"""


def _run_click(session, helper="ok"):
    result = subprocess.run(
        [NODE, "-e", _CLICK_DRIVER, str(_SESSIONS_JS), json.dumps({"session": session, "helper": helper})],
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


@needs_node
def test_content_hit_click_dispatches_full_session_jump_with_clicked_sid():
    r = _run_click({"session_id": "s1", "match_type": "content", "match_message_idx": 4})
    assert r == {"scheduled": True, "calls": [[4, "s1"]]}


@needs_node
@pytest.mark.parametrize(
    "session",
    [
        {"session_id": "s1", "match_type": "title"},
        {"session_id": "s1"},
        {"session_id": "s1", "match_type": "content"},
        {"session_id": "s1", "match_type": "content", "match_message_idx": None},
        {"session_id": "s1", "match_type": "content", "match_message_idx": "4"},
        {"session_id": "s1", "match_type": "content", "match_message_idx": -1},
        {"session_id": "s1", "match_type": "content", "match_message_idx": 2.5},
        {"match_type": "content", "match_message_idx": 4},
    ],
    ids=["title-hit", "plain-row", "no-idx", "null-idx", "string-idx", "negative", "fractional", "no-sid"],
)
def test_non_content_or_invalid_hit_only_opens_the_session(session):
    assert _run_click(session) == {"scheduled": False, "calls": []}


@needs_node
@pytest.mark.parametrize("helper", ["missing", "throws", "rejects"])
def test_jump_failures_never_break_the_open(helper):
    r = _run_click({"session_id": "s1", "match_type": "content", "match_message_idx": 1}, helper=helper)
    assert r["scheduled"] is (helper != "missing")


def test_row_open_dispatches_jump_after_the_session_opens():
    src = _SESSIONS_JS.read_text(encoding="utf-8")
    start = src.index("const _openRowSession=async()=>{")
    body = src[start:src.index("};", start)]
    assert body.index("await _openSidebarSession(s);") < body.index("_jumpToSessionSearchHit(s,_hitQuery);")
    # The query is captured before the open, so the jump can confirm its target.
    assert body.index("const _hitQuery=") < body.index("await _openSidebarSession(s);")


def test_cross_script_wiring_goes_through_window():
    """_jumpToFullSessionMessage lives inside outline.js's IIFE, so sessions.js
    can only reach it through the window export (a bare call is a silent no-op,
    the bug that sank the original #4224)."""
    outline = _OUTLINE_JS.read_text(encoding="utf-8")
    assert "window._jumpToFullSessionMessage = _jumpToFullSessionMessage" in outline
    sessions = _SESSIONS_JS.read_text(encoding="utf-8")
    assert "window._jumpToFullSessionMessage(idx,sid,query)" in sessions
    bare = re.findall(r"(?<![.\w])_jumpTo(?:FullSession)?Message\s*\(", sessions)
    assert not bare, "sessions.js must call the outline jump helpers via window"


# --------------------------------------------------------------------------- #
# Query confirmation: merged/stitched transcripts can shift indices (RM, #5106)
# --------------------------------------------------------------------------- #
@needs_node
def test_query_confirms_the_indexed_target():
    r = _run_jump(idx=2, query="needle")
    assert r["ok"] is True
    assert r["delegated"]["q"] == 2 and r["delegated"]["rowContent"] == "NEEDLE 2"


@needs_node
def test_stitched_view_falls_back_to_first_message_containing_the_query():
    """The server indexes the continuation's own messages (NEEDLE at 2) but the
    transcript shows 2 archived parent rows first: index 2 is 'msg 0'. The jump
    must not highlight it; it goes to the first message containing the query."""
    r = _run_jump(idx=2, query="Needle", stitchPrefix=2)
    assert r["ok"] is True
    assert r["delegated"]["q"] == 4
    assert r["delegated"]["rowContent"] == "NEEDLE 2"
    assert r["delegated"]["rowSessionIdx"] == 4


@needs_node
def test_query_absent_from_the_loaded_transcript_does_not_jump():
    r = _run_jump(idx=2, query="not-in-this-transcript")
    assert r["ok"] is False
    assert r["delegated"] is None


@needs_node
def test_no_query_keeps_the_index_jump():
    r = _run_jump(idx=2)
    assert r["ok"] is True and r["delegated"]["q"] == 2


# -- Far jumps land instantly so the highlight is visible ---------------------------------------------------------
# A smooth scroll from the bottom of a 240-message session to message 0 ran ~1.5 s over 86k px, and the highlight
# (1.6 s pulse, peak at ~0.56 s) had faded before the row arrived. Far targets and reduced-motion users now jump
# instantly; near targets keep the smooth scroll.

_BEHAVIOR_DRIVER = r"""
const fs = require('fs');
const src = fs.readFileSync(process.argv[1], 'utf8');
const start = src.indexOf('function _messageJumpScrollBehavior(');
let i = src.indexOf('{', start), depth = 1; i++;
while (depth > 0) { if (src[i] === '{') depth++; else if (src[i] === '}') depth--; i++; }
const sc = JSON.parse(process.argv[2]);
global.window = { matchMedia: (q) => ({ matches: !!sc.reduced && q.includes('reduce') }) };
eval(src.slice(start, i));
const container = { clientHeight: 800, getBoundingClientRect: () => ({ top: 100 }) };
const el = { getBoundingClientRect: () => ({ top: 100 + sc.offset }) };
process.stdout.write(_messageJumpScrollBehavior(container, el));
"""


def _jump_behavior(offset, reduced=False):
    r = subprocess.run([NODE, "-e", _BEHAVIOR_DRIVER, str(_UI_JS), json.dumps({"offset": offset, "reduced": reduced})],
                       capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    return r.stdout


@needs_node
@pytest.mark.parametrize("offset,expected", [(300, "smooth"), (-1500, "smooth"), (1601, "auto"), (-86000, "auto")])
def test_far_jump_targets_land_instantly_and_near_ones_scroll_smoothly(offset, expected):
    assert _jump_behavior(offset) == expected


@needs_node
def test_reduced_motion_always_jumps_instantly():
    assert _jump_behavior(300, reduced=True) == "auto"


def test_question_and_answer_jumps_use_the_distance_aware_behavior():
    ui = _UI_JS.read_text(encoding="utf-8")
    assert "seg.scrollIntoView({block:'start',behavior:_messageJumpScrollBehavior(container,seg)})" in ui
    assert "row.scrollIntoView({block:'center',behavior:_messageJumpScrollBehavior(container,row)})" in ui
