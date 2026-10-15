"""Behavior regression for #5720 live reasoning presentation ownership."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
NODE = shutil.which("node")


def _run_reasoning_scene(
    *,
    activity_mode: str = "transparent_stream",
    fail_first_anchor_render: bool = False,
    fail_anchor_render_on: int | None = None,
    multi_segment_fallback: bool = False,
    stale_active_stream: bool = False,
    stale_live_turn: bool = False,
    show_thinking: bool = True,
    reasoning_burst: bool = False,
    switch_before_frame: bool = False,
    reasoning_boundary: str = "",
    lifecycle_case: str = "",
) -> dict:
    assert NODE, "node is required for the #5720 browser-chain regression"
    env = os.environ.copy()
    env.setdefault("ISSUE5720_UI_JS", str(ROOT / "static" / "ui.js"))
    env.setdefault("ISSUE5720_MESSAGES_JS", str(ROOT / "static" / "messages.js"))
    env.setdefault("ISSUE5720_SESSIONS_JS", str(ROOT / "static" / "sessions.js"))
    env.setdefault(
        "ISSUE5720_ANCHORS_JS",
        str(ROOT / "static" / "assistant_turn_anchors.js"),
    )
    env["ISSUE5720_ACTIVITY_MODE"] = activity_mode
    if fail_first_anchor_render:
        env["ISSUE5720_FAIL_FIRST_ANCHOR_RENDER"] = "1"
    if fail_anchor_render_on is not None:
        env["ISSUE5720_FAIL_ANCHOR_RENDER_ON"] = str(fail_anchor_render_on)
    if multi_segment_fallback:
        env["ISSUE5720_MULTI_SEGMENT_FALLBACK"] = "1"
    if stale_active_stream:
        env["ISSUE5720_STALE_ACTIVE_STREAM"] = "1"
    if stale_live_turn:
        env["ISSUE5720_STALE_LIVE_TURN"] = "1"
    if not show_thinking:
        env["ISSUE5720_SHOW_THINKING"] = "0"
    if reasoning_burst:
        env["ISSUE5720_REASONING_BURST"] = "1"
    if switch_before_frame:
        env["ISSUE5720_SWITCH_BEFORE_FRAME"] = "1"
    if reasoning_boundary:
        env["ISSUE5720_REASONING_BOUNDARY"] = reasoning_boundary
    if lifecycle_case:
        env["ISSUE5720_LIFECYCLE_CASE"] = lifecycle_case
    result = subprocess.run(
        [NODE, "-e", _NODE_SCENE],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_deepseek_reasoning_deltas_keep_one_live_row_owner():
    result = _run_reasoning_scene()

    assert result["first_text"] == "Plan"
    assert result["second_text"] == "Plan step"
    assert result["same_node"] is True
    assert result["live_reasoning_rows"] == 1
    assert result["render_passes"] == [1, 2]
    assert result["anchor_reasoning_events"] == 1
    assert result["anchor_reasoning_text"] == "Plan step"
    assert result["inflight_reasoning_text"] == "Plan step"


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_reasoning_uses_visible_fallback_when_anchor_declines_first_paint():
    result = _run_reasoning_scene(fail_first_anchor_render=True)

    assert result["first_text"] is None
    assert result["first_fallback_text"] == "Plan"
    assert result["second_text"] == "Plan step"
    assert result["live_reasoning_rows"] == 1
    assert result["fallback_rows_after_recovery"] == 0
    assert result["anchor_reasoning_events"] == 1
    assert result["anchor_reasoning_text"] == "Plan step"
    assert result["inflight_reasoning_text"] == "Plan step"


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_stale_stream_reasoning_cannot_claim_the_active_turn():
    result = _run_reasoning_scene(stale_active_stream=True)

    assert result["anchor_render_attempts"] == 0
    assert result["live_reasoning_rows"] == 0
    assert result["fallback_rows_after_recovery"] == 0
    assert result["anchor_reasoning_events"] == 0
    assert result["inflight_reasoning_text"] == ""


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_reasoning_fallback_respects_show_thinking_false():
    result = _run_reasoning_scene(
        fail_first_anchor_render=True,
        show_thinking=False,
    )

    assert result["anchor_render_attempts"] == 0
    assert result["live_reasoning_rows"] == 0
    assert result["fallback_rows_after_recovery"] == 0
    assert result["anchor_reasoning_events"] == 0


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_reasoning_rebuilds_when_another_sessions_live_turn_blocks_anchor():
    result = _run_reasoning_scene(stale_live_turn=True)

    assert result["first_text"] == "Plan"
    assert result["second_text"] == "Plan step"
    assert result["live_reasoning_rows"] == 1
    assert result["fallback_rows_after_recovery"] == 0
    assert result["anchor_reasoning_events"] == 1
    assert result["anchor_reasoning_text"] == "Plan step"
    assert result["inflight_reasoning_text"] == "Plan step"


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_reasoning_fallback_rebuilds_from_another_sessions_live_turn():
    result = _run_reasoning_scene(
        fail_first_anchor_render=True,
        stale_live_turn=True,
    )

    assert result["first_text"] is None
    assert result["first_fallback_text"] == "Plan"
    assert result["second_text"] == "Plan step"
    assert result["live_reasoning_rows"] == 1
    assert result["fallback_rows_after_recovery"] == 0
    assert result["anchor_reasoning_events"] == 1
    assert result["anchor_reasoning_text"] == "Plan step"
    assert result["inflight_reasoning_text"] == "Plan step"


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
@pytest.mark.parametrize("activity_mode", ["transparent_stream", "compact_worklog"])
def test_later_deferred_anchor_paint_updates_existing_reasoning_owner(activity_mode):
    result = _run_reasoning_scene(
        activity_mode=activity_mode,
        fail_anchor_render_on=2,
    )

    assert result["first_text"] == "Plan"
    assert result["second_text"] == "Plan step"
    assert result["same_node"] is True
    assert result["live_reasoning_rows"] == 1
    assert result["fallback_rows_after_recovery"] == 0
    assert result["visible_reasoning_owners"] == 1
    assert result["anchor_render_attempts"] == 2
    assert result["anchor_reasoning_events"] == 1
    assert result["anchor_reasoning_text"] == "Plan step"
    assert result["inflight_reasoning_text"] == "Plan step"


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
@pytest.mark.parametrize("activity_mode", ["transparent_stream", "compact_worklog"])
def test_later_deferred_anchor_paint_updates_exact_reasoning_owner(activity_mode):
    result = _run_reasoning_scene(
        activity_mode=activity_mode,
        multi_segment_fallback=True,
    )
    exact = result["multi_segment_exact_fallback"]

    assert exact["first_before"] == "Plan step"
    assert exact["first_after"] == "Plan step"
    assert exact["second_before"] == "Second draft"
    assert exact["second_after"] == "Second updated"
    assert exact["legacy_after"] == "Legacy unkeyed"
    assert exact["anchor_run_id"] == "run-5720"
    assert exact["second_row_id"] == "run-5720:2"
    assert exact["second_local_id"] == "live-reasoning:stream-1:2"
    assert exact["live_reasoning_rows"] == 2
    assert exact["fallback_rows"] == 0


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_later_deferred_anchor_paint_remains_hidden_in_final_answer_only_mode():
    result = _run_reasoning_scene(
        activity_mode="hide_all_activity",
        fail_anchor_render_on=2,
    )

    assert result["first_text"] is None
    assert result["second_text"] is None
    assert result["live_reasoning_rows"] == 0
    assert result["fallback_rows_after_recovery"] == 0
    assert result["visible_reasoning_owners"] == 0
    assert result["anchor_render_attempts"] == 2
    assert result["anchor_reasoning_events"] == 1
    assert result["anchor_reasoning_text"] == "Plan step"


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_reasoning_replay_burst_coalesces_paint_without_losing_text():
    result = _run_reasoning_scene(reasoning_burst=True)
    assert result["renders_before_frame"] == 0
    assert result["renders_after_frame"] == 1
    assert result["inflight_chars"] == 226_748
    assert result["anchor_chars"] == 226_748


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_reasoning_frame_from_previous_session_cannot_paint_current_pane():
    result = _run_reasoning_scene(reasoning_burst=True, switch_before_frame=True)
    assert result["renders_after_frame"] == 0
    assert result["inflight_chars"] == 226_748
    assert result["anchor_chars"] == 226_748


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
@pytest.mark.parametrize("boundary", ["tool", "done", "cancel", "apperror", "interim"])
def test_reasoning_boundary_flushes_tail_before_pending_frame(boundary):
    result = _run_reasoning_scene(reasoning_burst=True, reasoning_boundary=boundary)
    assert result["renders_before_frame"] == 0
    assert result["anchor_chars"] == 226_748
    assert result["inflight_chars"] == 226_748
    assert result["renders_after_frame"] == result["renders_at_boundary"]
    if boundary in ("done", "cancel", "apperror"):
        assert result["settled"] is True
        assert result["inflight_cleared"] is True


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_echo_before_frame_does_not_persist_a_thinking_placeholder():
    result = _run_reasoning_scene(reasoning_burst=True, lifecycle_case="echo")
    assert result["texts"] == []
    assert result["retained_reasoning"] == ""
    assert result["settled"] is True


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
@pytest.mark.parametrize("transport", ["reattach", "reconnect"])
def test_closed_attachment_frame_cannot_overwrite_new_reasoning(transport):
    result = _run_reasoning_scene(reasoning_burst=True, lifecycle_case=transport)
    assert result["texts"] == ["OLD reasoning NEW tail"]
    assert result["old_source_closed"] is True
    assert result["renders_after_old_frame"] == result["renders_before_old_frame"]


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_sse_error_with_first_reasoning_pending_keeps_one_reasoning_event():
    result = _run_reasoning_scene(reasoning_burst=True, lifecycle_case="sse_error")
    assert result["texts"] == ["OLD reasoning"]
    assert result["settled"] is True


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
@pytest.mark.parametrize("snapshot", ["absent", "invalid"])
def test_done_without_usable_snapshot_settles_the_current_stream_owner(snapshot):
    result = _run_reasoning_scene(
        reasoning_burst=True,
        lifecycle_case=f"done_missing_session_{snapshot}",
    )

    assert result["messages"][-1]["content"] == "Current streamed answer"
    assert result["messages"][-1]["reasoning"] == "Current streamed reasoning"
    assert result["messages"][-1]["_turnUsage"]["output_tokens"] == 2
    assert result["messages"][-1]["_anchor_stream_id"] == "stream-1"
    assert result["prior_reasoning"] == "Historical reasoning"
    assert result["prior_turn_usage"] is None
    assert result["tool_owner_index"] == len(result["messages"]) - 1
    assert result["tool_owner_id"] == "current-tool"
    assert result["settled"] is True
    assert result["active_stream"] is None
    assert result["inflight_cleared"] is True


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
@pytest.mark.parametrize("snapshot", ["absent", "invalid"])
def test_reasoning_only_done_without_usable_snapshot_does_not_reuse_prior_assistant(snapshot):
    result = _run_reasoning_scene(
        reasoning_burst=True,
        lifecycle_case=f"done_missing_session_reasoning_only_{snapshot}",
    )

    assert result["messages"][-1]["content"] == ""
    assert result["messages"][-1]["reasoning"] == "Current streamed reasoning"
    assert result["messages"][-1]["_turnUsage"]["output_tokens"] == 2
    assert result["prior_reasoning"] == "Historical reasoning"
    assert result["prior_turn_usage"] is None
    assert result["tool_owner_index"] == len(result["messages"]) - 1
    assert result["tool_owner_id"] == "current-tool"
    assert result["settled"] is True
    assert result["active_stream"] is None
    assert result["inflight_cleared"] is True


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
@pytest.mark.parametrize("snapshot", ["absent", "invalid"])
def test_regeneration_refreshes_authority_after_missing_terminal_snapshot(snapshot):
    result = _run_reasoning_scene(
        reasoning_burst=True,
        lifecycle_case=f"done_regeneration_{snapshot}",
    )

    assert result["messages"][-1]["content"] == "Current streamed answer"
    assert result["messages"][-1]["reasoning"] == "Current streamed reasoning"
    assert result["tool_owner_id"] == "current-tool"
    assert result["api_requests"] == ["/api/session?session_id=sid-1&messages=1&resolve_model=0&msg_limit=all"], result
    assert result["operations"] == ["authoritative-get", ["start", "sid-1", "saved-revision"]], result
    assert result["fallback_revision"] is None
    assert result["post_refresh_tool_owner_id"] == "current-tool"


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
@pytest.mark.parametrize("response_kind", ["absent", "malformed", "stale"])
def test_regeneration_without_usable_authoritative_refresh_keeps_recovered_answer(response_kind):
    result = _run_reasoning_scene(
        reasoning_burst=True,
        lifecycle_case=f"done_regeneration_failed_{response_kind}",
    )

    assert result["start_calls"] == []
    assert result["messages"][-1]["content"] == "Current streamed answer"
    assert result["messages"][-1]["reasoning"] == "Current streamed reasoning"
    assert result["tool_owner_id"] == "current-tool"
    assert result["regen_error"] == ""
    assert result["fallback_revision"] is None
    assert result["revision"] is None


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_regeneration_refresh_does_not_apply_after_session_switch():
    result = _run_reasoning_scene(
        reasoning_burst=True,
        lifecycle_case="done_regeneration_switch",
    )

    assert result["start_calls"] == []
    assert result["session_id"] == "sid-other"
    assert result["revision"] == "other-revision"
    assert result["messages"] == [{"role": "assistant", "content": "Other session"}]


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_regeneration_refresh_does_not_replace_same_session_successor_turn():
    result = _run_reasoning_scene(
        reasoning_burst=True,
        lifecycle_case="done_regeneration_late_owner",
    )

    assert result["start_calls"] == []
    assert result["session_id"] == "sid-1"
    assert result["messages"][-1]["content"] == "Newer streamed answer"
    assert result["active_stream"] == "stream-2"
    assert result["busy"] is True
    assert result["inflight_stream"] == "stream-2"


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_valid_terminal_snapshot_keeps_authority_without_untruncated_full_fetch():
    result = _run_reasoning_scene(
        reasoning_burst=True,
        lifecycle_case="done_regeneration_valid",
    )

    assert result["api_requests"] == []
    assert result["start_calls"] == [["sid-1", "embedded-revision"]]
    assert result["revision"] == "embedded-revision"
    assert result["session_totals"] == {
        "input_tokens": 40,
        "output_tokens": 30,
        "estimated_cost": 4,
        "cache_read_tokens": 70,
        "cache_write_tokens": 80,
    }


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
@pytest.mark.parametrize("snapshot", ["absent", "invalid"])
def test_consecutive_done_usage_badges_use_each_turn_delta(snapshot):
    result = _run_reasoning_scene(
        reasoning_burst=True,
        lifecycle_case=f"done_usage_two_turns_{snapshot}",
    )

    assert result["first_usage"] == {
        "input_tokens": 3,
        "output_tokens": 3,
        "estimated_cost": 0.25,
        "cache_read_tokens": 4,
        "cache_write_tokens": 4,
    }
    assert result["second_usage"] == {
        "input_tokens": 3,
        "output_tokens": 2,
        "estimated_cost": 0.4,
        "cache_read_tokens": 3,
        "cache_write_tokens": 5,
    }, result
    assert result["first_usage_after_second"] == result["first_usage"]
    assert result["session_totals"] == {
        "input_tokens": 8,
        "output_tokens": 6,
        "estimated_cost": 0.9,
        "cache_read_tokens": 10,
        "cache_write_tokens": 13,
    }


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_partial_zero_done_usage_updates_only_present_cumulative_totals():
    result = _run_reasoning_scene(
        reasoning_burst=True,
        lifecycle_case="done_usage_partial_zero",
    )

    assert result["session_totals"] == {
        "input_tokens": 0,
        "output_tokens": 1,
        "estimated_cost": 0,
        "cache_read_tokens": 0,
        "cache_write_tokens": 4,
    }


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
@pytest.mark.parametrize("activity_mode", ["transparent_stream", "compact_worklog"])
def test_transport_recovery_paints_pending_token_before_retry(activity_mode):
    result = _run_reasoning_scene(
        activity_mode=activity_mode,
        reasoning_burst=True,
        lifecycle_case="token_reconnect",
    )
    assert "LAST WORDS BEFORE DROP" not in result["before_error"]
    assert "LAST WORDS BEFORE DROP" in result["during_reconnect"]
    assert result["pending_frames"] == 0
    assert result["old_source_closed"] is True
    assert result["inflight_text"] == "LAST WORDS BEFORE DROP NEW connection"
    assert "LAST WORDS BEFORE DROP NEW connection" in result["after_reconnect"]


_NODE_SCENE = r"""
const fs = require('fs');
const uiSrc = fs.readFileSync(process.env.ISSUE5720_UI_JS, 'utf8');
const messagesSrc = fs.readFileSync(process.env.ISSUE5720_MESSAGES_JS, 'utf8');
const sessionsSrc = fs.readFileSync(process.env.ISSUE5720_SESSIONS_JS, 'utf8');
const anchorsSrc = fs.readFileSync(process.env.ISSUE5720_ANCHORS_JS, 'utf8');

function extractFunc(src, name){
  let start = src.indexOf('function ' + name);
  if(start >= 6 && src.slice(start-6,start)==='async ') start -= 6;
  if(start < 0){
    if(name==='isFinalAnswerOnlyMode'||name==='isHideAllActivityMode'){
      return `function ${name}(){ return false; }`;
    }
    if(name==='_anchorSceneRowTimestampSeconds'){
      return `function ${name}(){ return null; }`;
    }
    throw new Error(name + ' not found');
  }
  const params = src.indexOf('(', start);
  let depth = 0, close = -1;
  for(let i=params; i<src.length; i++){
    if(src[i] === '(') depth++;
    else if(src[i] === ')' && --depth === 0){ close = i; break; }
  }
  const brace = src.indexOf('{', close);
  depth = 0;
  for(let i=brace; i<src.length; i++){
    if(src[i] === '{') depth++;
    else if(src[i] === '}' && --depth === 0) return src.slice(start, i + 1);
  }
  throw new Error(name + ' did not close');
}

class FakeElement {
  constructor(tag='div'){
    this.tagName=String(tag).toUpperCase();
    this.children=[];
    this.parentNode=null;
    this.attributes=Object.create(null);
    this.dataset=Object.create(null);
    this.style=Object.create(null);
    this.hidden=false;
    this.id='';
    this._text='';
    this._classes=new Set();
    const self=this;
    this.classList={
      add(...names){ names.forEach(name=>self._classes.add(name)); },
      remove(...names){ names.forEach(name=>self._classes.delete(name)); },
      contains(name){ return self._classes.has(name); },
      toggle(name, force){
        if(force===true){ self._classes.add(name); return true; }
        if(force===false){ self._classes.delete(name); return false; }
        if(self._classes.has(name)){ self._classes.delete(name); return false; }
        self._classes.add(name); return true;
      },
    };
  }
  get parentElement(){ return this.parentNode; }
  get nextSibling(){
    if(!this.parentNode) return null;
    const siblings=this.parentNode.children;
    return siblings[siblings.indexOf(this)+1]||null;
  }
  get className(){ return Array.from(this._classes).join(' '); }
  set className(value){ this._classes=new Set(String(value).split(/\s+/).filter(Boolean)); }
  get textContent(){
    return this.children.length ? this.children.map(child=>child.textContent).join('') : this._text;
  }
  set textContent(value){ this._text=String(value??''); this.children=[]; }
  get innerHTML(){ return this.textContent; }
  set innerHTML(value){ this.textContent=value; }
  setAttribute(name, value){
    const key=String(name), val=String(value);
    this.attributes[key]=val;
    if(key==='id') this.id=val;
    if(key==='class') this.className=val;
    if(key.startsWith('data-')){
      const dataKey=key.slice(5).replace(/-([a-z])/g,(_,c)=>c.toUpperCase());
      this.dataset[dataKey]=val;
    }
  }
  getAttribute(name){
    return Object.prototype.hasOwnProperty.call(this.attributes,name)?this.attributes[name]:null;
  }
  getAttributeNames(){ return Object.keys(this.attributes); }
  removeAttribute(name){
    delete this.attributes[name];
    if(name==='id') this.id='';
    if(name.startsWith('data-')){
      const dataKey=name.slice(5).replace(/-([a-z])/g,(_,c)=>c.toUpperCase());
      delete this.dataset[dataKey];
    }
  }
  appendChild(child){
    if(child.tagName==='#FRAGMENT'){
      child.children.slice().forEach(node=>this.appendChild(node));
      child.children=[];
      return child;
    }
    if(child.parentNode) child.remove();
    child.parentNode=this;
    this.children.push(child);
    return child;
  }
  insertBefore(child, ref){
    if(child.parentNode) child.remove();
    child.parentNode=this;
    const idx=this.children.indexOf(ref);
    if(idx<0) this.children.push(child); else this.children.splice(idx,0,child);
    return child;
  }
  remove(){
    if(!this.parentNode) return;
    const siblings=this.parentNode.children;
    const idx=siblings.indexOf(this);
    if(idx>=0) siblings.splice(idx,1);
    this.parentNode=null;
  }
  matches(selector){ return matchesSelector(this,selector); }
  querySelector(selector){ return this.querySelectorAll(selector)[0]||null; }
  querySelectorAll(selector){
    const out=[];
    const walk=node=>node.children.forEach(child=>{
      if(matchesSelector(child,selector)) out.push(child);
      walk(child);
    });
    walk(this);
    return out;
  }
  closest(selector){
    let node=this;
    while(node){ if(matchesSelector(node,selector)) return node; node=node.parentNode; }
    return null;
  }
}

function matchesSelector(el, selector){
  return String(selector).split(',').some(part=>matchesChain(el,part.trim()));
}
function matchesChain(el, selector){
  const parts=selector.split(/\s+/).filter(Boolean);
  if(!parts.length||!matchesSimple(el,parts[parts.length-1])) return false;
  let node=el.parentNode;
  for(let i=parts.length-2;i>=0;i--){
    while(node&&!matchesSimple(node,parts[i])) node=node.parentNode;
    if(!node) return false;
    node=node.parentNode;
  }
  return true;
}
function matchesSimple(el, selector){
  selector=selector.replace(/^:scope\s*>\s*/,'');
  const nots=[];
  selector=selector.replace(/:not\(([^)]+)\)/g,(_,inner)=>{nots.push(inner);return '';});
  if(nots.some(inner=>matchesSimple(el,inner))) return false;
  const tag=selector.match(/^[A-Za-z][A-Za-z0-9-]*/);
  if(tag&&el.tagName!==tag[0].toUpperCase()) return false;
  const id=selector.match(/#([A-Za-z0-9_-]+)/);
  if(id&&el.id!==id[1]) return false;
  for(const match of selector.matchAll(/\.([A-Za-z0-9_-]+)/g)){
    if(!el.classList.contains(match[1])) return false;
  }
  for(const match of selector.matchAll(/\[([^\]=]+)(?:=["']?([^\]"']*)["']?)?\]/g)){
    const value=el.getAttribute(match[1]);
    if(value===null) return false;
    if(match[2]!==undefined&&String(value)!==String(match[2])) return false;
  }
  return true;
}

global.window={
  _chatActivityDisplayMode:process.env.ISSUE5720_ACTIVITY_MODE||'transparent_stream',
  _showThinking:process.env.ISSUE5720_SHOW_THINKING!=='0',
  _simplifiedToolCalling:true,
};
global.document={
  baseURI:'http://test.local/',
  hidden:false,
  createElement:tag=>new FakeElement(tag),
  createTextNode:text=>{const node=new FakeElement('#text');node.textContent=text;return node;},
  createDocumentFragment:()=>new FakeElement('#fragment'),
  querySelector:()=>null,
};
global.location={href:'http://test.local/'};
global.CSS={escape:value=>String(value)};
const deferredFrames=process.env.ISSUE5720_REASONING_BURST==='1';
const frames=new Map();
const timers=new Map();
let nextFrame=1, clock=1000;
global.performance={now:()=>deferredFrames?clock:(clock+=100)};
global.requestAnimationFrame=fn=>{
  const id=nextFrame++;
  if(deferredFrames) frames.set(id,fn); else fn();
  return id;
};
global.cancelAnimationFrame=id=>frames.delete(id);
global.setTimeout=(fn,ms)=>{
  const id=nextFrame++;
  if(deferredFrames&&ms<=100) frames.set(id,fn);
  else if(ms<600000) timers.set(id,fn);
  return id;
};
global.clearTimeout=id=>{frames.delete(id);timers.delete(id);};

const emptyState=new FakeElement('div');
const msgInner=new FakeElement('div');
const messages=new FakeElement('div');
const turn=new FakeElement('div');
turn.id='liveAssistantTurn';
turn.className='assistant-turn';
turn.dataset.sessionId=process.env.ISSUE5720_STALE_LIVE_TURN==='1'?'sid-old':'sid-1';
msgInner.appendChild(turn);
const byId={emptyState,msgInner,messages,liveAssistantTurn:turn};
global.$=id=>byId[id]||null;
global._createAssistantTurn=()=>turn;
global._assistantTurnBlocks=()=>turn;
global._captureMessageScrollSnapshot=()=>({scrollHeight:1000});
global._prepareLiveAnchorScrollRebuildGuard=()=>({readerAwayFromBottom:false,release:null});
global._restoreMessageScrollSnapshotSameFrame=()=>{};
global.scrollIfPinned=()=>{};
global._moveLiveRunStatusToTurnEnd=()=>{};
global._messageUserUnpinned=false;
global._oldestIdx=0;
global._messagesTruncated=false;
global._syncTransparentEventControls=()=>{};
global._syncToolCallGroupSummary=()=>{};
global._captureWorklogDetailDisclosureState=()=>null;
global._restoreWorklogDetailDisclosureState=()=>{};
global._startActivityElapsedTimer=()=>{};
global._dedupeLiveProcessedWorklogAnchors=()=>{};
global._toolWorklogListEl=group=>group&&group.querySelector&&group.querySelector('.tool-worklog-list')||group;
global.ensureActivityGroup=(blocks,opts)=>{
  opts=opts||{};
  const key=String(opts.activityKey||'');
  let group=key&&blocks.querySelector&&blocks.querySelector(`.tool-worklog-group[data-tool-worklog-key="${CSS.escape(key)}"]`);
  if(group) return group;
  group=new FakeElement('div');
  group.className='tool-worklog-group tool-call-group';
  group.setAttribute('data-tool-worklog-group','1');
  group.setAttribute('data-live-tool-call-group','1');
  group.setAttribute('data-tool-worklog-key',key);
  const list=new FakeElement('div');
  list.className='tool-worklog-list';
  group.appendChild(list);
  blocks.appendChild(group);
  return group;
};
global.ensureLiveWorklogContainer=(blocks,opts)=>ensureActivityGroup(blocks,opts);
global._thinkingActivityNode=text=>{
  const row=new FakeElement('div');
  row.className='agent-activity-thinking transparent-thinking-event';
  const card=new FakeElement('div');
  card.className='thinking-card open';
  const header=new FakeElement('div');
  header.className='thinking-card-header';
  const preview=new FakeElement('span');
  preview.className='transparent-event-thinking-preview';
  preview.textContent=text;
  const body=new FakeElement('div');
  body.className='thinking-card-body';
  const pre=new FakeElement('pre');
  pre.textContent=text;
  header.appendChild(preview);
  body.appendChild(pre);
  card.appendChild(header);
  card.appendChild(body);
  row.appendChild(card);
  return row;
};
global._decorateTransparentEventRow=(row,opts)=>{
  row.classList.add('transparent-event-row');
  row.setAttribute('data-transparent-event-row','1');
  row.setAttribute('data-event-type',String(opts&&opts.type||'activity'));
  const preview=row.querySelector('.transparent-event-thinking-preview');
  if(preview) preview.textContent=String(opts&&opts.preview||opts&&opts.text||'');
  return row;
};
global._rehydrateTransparentLiveRow=()=>{};
global._sanitizeThinkingDisplayText=value=>String(value||'').trim();
global._firstValidTimestampSeconds=()=>null;

eval(anchorsSrc);
for(const name of [
  'chatActivityMode','isTransparentStream','isFinalAnswerOnlyMode','isCompactWorklogMode','isSimplifiedToolCalling',
  '_anchorSceneIsSettledSuccessfulCompression','_anchorSceneRowsForRendering',
  '_anchorSceneToolRowLogicalKey',
  '_anchorSceneRowTimestampSeconds','_anchorSceneTransparentNodeForRow',
  '_transparentLiveRowKey','_transparentLiveRowsCompatible',
  '_transparentLiveRowAttributePairs','_transparentLiveRowInteractiveState',
  '_refreshTransparentThinkingLiveRow','_refreshTransparentLiveRow',
  '_thinkingMarkup','_renderThinkingInto',
  '_resetMismatchedLiveAssistantTurnForSession',
  '_liveAnchorReasoningRowForFallback','_updateLiveAnchorReasoningRowForFallback',
  '_anchorSceneNodeForRow','_anchorSceneWorklogGroup','_renderAnchorSceneRowsIntoWorklog',
  'isLiveAnchorActivitySceneOwner','_projectLiveAnchorActivitySceneForStream',
  '_restoreLiveAnchorScrollSnapshotAfterRebuild',
  '_renderLiveAnchorActivitySceneTransparent','renderLiveAnchorActivityScene',
  '_renderLiveAnchorActivitySceneForStream','appendThinking','updateThinking',
]) eval(extractFunc(uiSrc,name));

let transparentRenderPasses=0;
const realTransparentRender=_renderLiveAnchorActivitySceneTransparent;
_renderLiveAnchorActivitySceneTransparent=function(...args){
  transparentRenderPasses+=1;
  return realTransparentRender(...args);
};
const failFirstAnchorRender=process.env.ISSUE5720_FAIL_FIRST_ANCHOR_RENDER==='1';
const failAnchorRenderOn=Number(process.env.ISSUE5720_FAIL_ANCHOR_RENDER_ON||'0');
let anchorRenderAttempts=0;
window._renderLiveAnchorActivitySceneForStream=function(...args){
  const lifecycleCase=String(process.env.ISSUE5720_LIFECYCLE_CASE||'');
  if(lifecycleCase.startsWith('done_missing_session_')||lifecycleCase.startsWith('done_regeneration_')||lifecycleCase.startsWith('done_usage_')) return true;
  anchorRenderAttempts+=1;
  if(failFirstAnchorRender&&anchorRenderAttempts===1) return false;
  if(failAnchorRenderOn&&anchorRenderAttempts===failAnchorRenderOn) return false;
  return _renderLiveAnchorActivitySceneForStream(...args);
};
window.isLiveAnchorActivitySceneOwner=isLiveAnchorActivitySceneOwner;

const S=global.S={
  session:{session_id:'sid-1',pending_started_at:1},
  messages:[{role:'user',content:'question'}],
  activeStreamId:'stream-1',
  toolCalls:[],
};
const INFLIGHT=global.INFLIGHT={};
const LIVE_STREAMS=global.LIVE_STREAMS={};
const _STREAM_WAS_HIDDEN=global._STREAM_WAS_HIDDEN={};
const _STREAM_NOTIFICATION_BACKGROUND=global._STREAM_NOTIFICATION_BACKGROUND={};
const _desktopBackgroundedForNotifications=false;
global._bindStreamHiddenTracker=()=>{};
global.closeOtherLiveStreams=()=>{};
global._resumeSessionStreamAfterLiveChat=()=>{};
global.resetTurnWorkspaceMutations=()=>{};
global._resetStreamScrollFollow=()=>{};
global._suspendSessionStreamForLiveChat=()=>{};
global.ensureLiveWorklogShell=()=>null;
global._extractInlineThinkingFromContent=(content,reasoning)=>({content:String(content||''),displayText:String(content||''),reasoning:String(reasoning||'')});
global._maybeNotifyPersistentStateSaved=()=>{};
global.enhanceMarkdownTables=()=>{};

class FakeEventSource {
  static instances=[];
  static OPEN=1;
  static CONNECTING=0;
  constructor(){ this.listeners=Object.create(null);this.readyState=1;FakeEventSource.instances.push(this); }
  addEventListener(name,fn){ (this.listeners[name]||(this.listeners[name]=[])).push(fn); }
  emit(name,data){ return Promise.all((this.listeners[name]||[]).map(fn=>fn({data:JSON.stringify(data),lastEventId:''}))); }
  close(){ this.readyState=2; }
}
global.EventSource=FakeEventSource;

const attachStart=messagesSrc.indexOf('function attachLiveStream(');
const attachEnd=messagesSrc.indexOf('\nfunction transcript(){',attachStart);
if(attachStart<0||attachEnd<0) throw new Error('attachLiveStream source boundary not found');
eval(extractFunc(messagesSrc,'_dispatchExtensionTurnLifecycle'));
eval(extractFunc(messagesSrc,'_applyEmbeddedTerminalSession'));
eval(extractFunc(messagesSrc,'closeLiveStream'));
eval(messagesSrc.slice(attachStart,attachEnd));
attachLiveStream('sid-1','stream-1');
const source=FakeEventSource.instances[0];
if(!source) throw new Error('attachLiveStream did not create EventSource');
if(process.env.ISSUE5720_STALE_ACTIVE_STREAM==='1') S.activeStreamId='stream-new';
if(deferredFrames){
  let settled=false;
  global.renderMessages=()=>{settled=!S.activeStreamId;};
  for(const name of ['renderSessionList','finalizeThinkingCard','appendLiveToolCard','clearLiveToolCards','setBusy','setComposerStatus','syncTopbar','loadDir','playNotificationSound','sendBrowserNotification','_markSessionViewed','clearInflightState','clearInflightStateStorage','saveInflightState','snapshotLiveTurnHtmlForSession','scrollToBottom','_clearStreamHidden','_clearStreamNotificationBackground','_syncCtxIndicator','removeThinking']) global[name]=()=>{};
  global._activityStatusNode=()=>new FakeElement('div');
  global._shouldForceCompletionNotification=()=>false;
  global._isDocumentVisibleAndFocused=()=>true;
  eval(extractFunc(messagesSrc,'_isSessionCurrentPane'));
  eval(extractFunc(messagesSrc,'_isSessionActivelyViewed'));
  global._deferStreamErrorIfOffline=()=>false;
  for(const name of ['clearInflight','_clearApprovalPendingForSession','_clearClarifyPendingForSession','stopApprovalPolling','hideApprovalCard','stopClarifyPolling','hideClarifyCard']) global[name]=()=>{};
  global._approvalSessionId=null;
  global._clarifySessionId=null;
  global._completionNotificationPreviewText=()=>'';
  global._carryForwardEphemeralTurnFields=(_,messages)=>messages;
  global._filterRecoveryControlMessages=messages=>messages;
  global._pendingTitleUpdates=new Map();
  global._pendingTitleExpectedCurrent=new Map();
  global._queueDrainSid=null;
  global.localStorage={setItem:()=>{}};
  global.api=async path=>path.includes('/stream/status')?{active:true}:null;
  function reasoningEvents(){
    return window._liveAnchorRegistries.get('stream-1').anchor.activity_events.filter(e=>e.source_event_type==='reasoning');
  }
  function drainFrames(){
    while(frames.size){
      const [id,fn]=frames.entries().next().value;frames.delete(id);clock+=100;fn();
    }
  }
  function installRegenerationDependencies(){
    global._messagesTruncated=false;
    global._oldestIdx=0;
    global._loadingOlder=false;
    global._loadingSessionId=null;
    global._messagesGeneration=0;
    global._bumpMessagesGeneration=()=>++global._messagesGeneration;
    global._advanceVisibleBaselineForServerKnownRows=()=>{};
    global._syncToolCallsForLoadedMessages=eval('('+extractFunc(sessionsSrc,'_syncToolCallsForLoadedMessages')+')');
    global._ensureAllMessagesLoaded=eval('('+extractFunc(sessionsSrc,'_ensureAllMessagesLoaded')+')');
    global.regenerateResponse=eval('('+extractFunc(uiSrc,'regenerateResponse')+')');
    global._adoptRegenerationRevision=eval('('+extractFunc(sessionsSrc,'_adoptRegenerationRevision')+')');
    global._EPHEMERAL_TURN_FIELDS=['_turnUsage','_turnDuration','_turnTps','_gatewayRouting','_statusCard','_anchor_stream_id','_anchor_activity_scene'];
    global._messageIdentityKey=eval('('+extractFunc(messagesSrc,'_messageIdentityKey')+')');
    global._isHistoricalAnchorActivityScene=eval('('+extractFunc(messagesSrc,'_isHistoricalAnchorActivityScene')+')');
    global._carryForwardEphemeralTurnFields=eval('('+extractFunc(messagesSrc,'_carryForwardEphemeralTurnFields')+')');
    window._carryForwardEphemeralTurnFields=global._carryForwardEphemeralTurnFields;
    global.t=()=>'';
    global.setStatus=()=>{};
  }
  function finalSession(reasoning=''){
    return {session_id:'sid-1',messages:[{role:'user',content:'question'},{role:'assistant',content:'Final answer',reasoning}],active_stream_id:null,message_count:2,_messages_offset:0,_messages_truncated:false};
  }
  const lifecycle=process.env.ISSUE5720_LIFECYCLE_CASE;
  if(lifecycle){
    (async()=>{
      if(lifecycle==='token_reconnect'){
        global.renderMd=text=>String(text);
        await source.emit('token',{text:'LAST WORDS BEFORE DROP'});
        const beforeError=turn.textContent;
        await source.emit('error',{});
        const duringReconnect=turn.textContent;
        const pendingFrames=frames.size;
        const [id,probe]=Array.from(timers).at(-1);
        timers.delete(id);await probe();
        await FakeEventSource.instances.at(-1).emit('token',{text:' NEW connection'});
        drainFrames();
        process.stdout.write(JSON.stringify({before_error:beforeError,during_reconnect:duringReconnect,
          pending_frames:pendingFrames,old_source_closed:source.readyState===2,
          inflight_text:INFLIGHT['sid-1'].lastAssistantText,after_reconnect:turn.textContent}));
        return;
      }
      const initial=lifecycle==='echo'?'Visible echo answer with enough text':'OLD reasoning';
      if(!lifecycle.startsWith('done_missing_session_')&&!lifecycle.startsWith('done_regeneration_')&&!lifecycle.startsWith('done_usage_')) await source.emit('reasoning',{text:initial});
      const oldFrames=Array.from(frames.values());
      if(lifecycle==='reattach'||lifecycle==='reconnect') frames.clear();
      if(lifecycle==='echo'){
        await source.emit('interim_assistant',{text:initial,already_streamed:true,reasoning_echo:true});
        await source.emit('done',{session:finalSession(),usage:{}});
      }else if(lifecycle==='reattach'){
        S.session={session_id:'sid-2'};
        closeLiveStream('sid-1');
        S.session={session_id:'sid-1'};S.activeStreamId='stream-1';
        attachLiveStream('sid-1','stream-1',[],{reconnecting:true});
        await Promise.resolve();
        await FakeEventSource.instances.at(-1).emit('reasoning',{text:' NEW tail'});
        drainFrames();
      }else if(lifecycle==='reconnect'){
        await source.emit('error',{});
        const [id,probe]=Array.from(timers).at(-1);
        timers.delete(id);await probe();
        for(let i=0;i<10;i++) await Promise.resolve();
        await FakeEventSource.instances.at(-1).emit('reasoning',{text:' NEW tail'});
        drainFrames();
      }else if(lifecycle==='sse_error'){
        global.api=async()=>null;
        await source.emit('error',{});
        for(let i=0;i<50&&!settled;i++){
          if(timers.size){
            const [id,fn]=timers.entries().next().value;timers.delete(id);await fn();
          }
          for(let j=0;j<10;j++) await Promise.resolve();
        }
      }else if(lifecycle.startsWith('done_missing_session_')||lifecycle.startsWith('done_regeneration_')||lifecycle.startsWith('done_usage_')){
        S.session={session_id:'sid-1',active_stream_id:'stream-1',pending_started_at:1,message_count:3,
          input_tokens:2,output_tokens:1,estimated_cost:0.25,cache_read_tokens:3,cache_write_tokens:4,
          regeneration_revision:'previous-revision'};
        S.messages=[
          {role:'user',content:'Historical prompt',_ts:1},
          {role:'assistant',content:'Historical answer',reasoning:'Historical reasoning',_ts:2},
          {role:'user',content:'Current prompt',_ts:3,_pending:true},
        ];
        await source.emit('reasoning',{text:'Current streamed reasoning'});
        await source.emit('tool',{name:'read_file',tid:'current-tool',args:{path:'notes.txt'}});
        await source.emit('tool_complete',{name:'read_file',tid:'current-tool',snippet:'contents'});
        if(!lifecycle.includes('_reasoning_only_')) await source.emit('token',{text:'Current streamed answer'});
        let terminal={usage:{input_tokens:4,output_tokens:3,estimated_cost:0.5,cache_read_tokens:7,cache_write_tokens:8,duration_seconds:1,tps:2}};
        if(lifecycle.endsWith('_invalid')) terminal.session={session_id:'sid-1',messages:'invalid',regeneration_revision:'unrelated-revision'};
        if(lifecycle.startsWith('done_usage_two_turns_')){
          terminal.usage={input_tokens:5,output_tokens:4,estimated_cost:0.5,cache_read_tokens:7,cache_write_tokens:8,duration_seconds:1,tps:2};
        }else if(lifecycle==='done_usage_partial_zero'){
          terminal.usage={input_tokens:0,estimated_cost:0,cache_read_tokens:0};
        }else if(lifecycle==='done_regeneration_valid'){
          terminal={
            usage:{input_tokens:4,output_tokens:3,estimated_cost:0.5,cache_read_tokens:7,cache_write_tokens:8,duration_seconds:1,tps:2},
            session:{session_id:'sid-1',messages:[
              {role:'user',content:'Historical prompt',_ts:1},
              {role:'assistant',content:'Historical answer',reasoning:'Historical reasoning',_ts:2},
              {role:'user',content:'Current prompt',_ts:3},
              {role:'assistant',content:'Current streamed answer',reasoning:'Current streamed reasoning',_ts:4},
            ],message_count:4,_messages_offset:0,_messages_truncated:false,
              regeneration_revision:'embedded-revision',input_tokens:40,output_tokens:30,estimated_cost:4,
              cache_read_tokens:70,cache_write_tokens:80,tool_calls:[{tid:'current-tool',name:'read_file',assistant_msg_idx:3}]},
          };
        }
        if(lifecycle.startsWith('done_regeneration_')){
          global._adoptRegenerationRevision=eval('('+extractFunc(sessionsSrc,'_adoptRegenerationRevision')+')');
        }
        await source.emit('done',terminal);
        drainFrames();
        for(let i=0;i<50&&!settled;i++){
          if(timers.size){const [id,fn]=timers.entries().next().value;timers.delete(id);await fn();}
          for(let j=0;j<10;j++) await Promise.resolve();
          drainFrames();
        }
        const current=S.messages.at(-1)||{};
        const previous=S.messages.find(m=>m&&m.content==='Historical answer')||{};
        const liveTool=S.toolCalls.find(tc=>tc&&tc.tid==='current-tool')||{};
        let regenResult={};
        if(lifecycle.startsWith('done_regeneration_')){
          installRegenerationDependencies();
          const apiRequests=[];
          const operations=[];
          const startCalls=[];
          const terminalRows=S.messages;
          const fallbackRevision=S.session?(S.session.regeneration_revision??null):null;
          const canonicalRows=terminalRows.map(m=>{
            const row={...m};delete row._pending;delete row._live;
            for(const field of ['_turnUsage','_turnDuration','_turnTps','_gatewayRouting','_statusCard','_anchor_stream_id','_anchor_activity_scene']) delete row[field];
            return row;
          });
          const fullHistory={session:{session_id:'sid-1',messages:canonicalRows,message_count:terminalRows.length,
            regeneration_revision:'saved-revision',tool_calls:[{tid:'current-tool',name:'read_file',assistant_msg_idx:terminalRows.length-1}]}};
          let resolveRefresh=null;
          global.api=path=>{
            if(!path.includes('msg_limit=all')) return Promise.resolve(null);
            apiRequests.push(path);
            operations.push('authoritative-get');
            if(lifecycle==='done_regeneration_failed_absent') return Promise.resolve(null);
            if(lifecycle==='done_regeneration_failed_malformed') return Promise.resolve({session:{session_id:'sid-1',messages:'invalid',regeneration_revision:'bad-revision'}});
            if(lifecycle==='done_regeneration_failed_stale') return Promise.resolve({session:{session_id:'sid-1',messages:terminalRows.slice(0,-1).map(m=>({...m})),
              message_count:terminalRows.length-1,regeneration_revision:'stale-saved-revision'}});
            if(lifecycle==='done_regeneration_switch'||lifecycle==='done_regeneration_late_owner') return new Promise(resolve=>{resolveRefresh=resolve;});
            return Promise.resolve(fullHistory);
          };
          global.startRegeneration=async(sid,revision)=>{
            startCalls.push([sid,revision]);
            operations.push(['start',sid,revision]);
          };
          const row=new FakeElement('div');row.dataset.msgIdx=String(S.messages.length-1);
          const button={closest:selector=>selector==='[data-msg-idx]'?row:null};
          let regenError='';
          if(lifecycle==='done_regeneration_switch'){
            const pending=global.regenerateResponse(button);
            for(let i=0;i<10&&!resolveRefresh;i++) await Promise.resolve();
            S.session={session_id:'sid-other',regeneration_revision:'other-revision'};
            S.messages=[{role:'assistant',content:'Other session'}];
            if(resolveRefresh) resolveRefresh({session:{session_id:'sid-1',messages:terminalRows.map(m=>({...m})),message_count:terminalRows.length,
              regeneration_revision:'saved-revision'}});
            try{await pending;}catch(error){regenError=String(error&&error.message||error);}
          }else if(lifecycle==='done_regeneration_late_owner'){
            const pending=global.regenerateResponse(button);
            for(let i=0;i<10&&!resolveRefresh;i++) await Promise.resolve();
            if(!resolveRefresh) throw new Error('forced refresh did not reach deferred response');
            S.messages=[...S.messages,
              {role:'user',content:'Newer prompt',_ts:5,_pending:true},
              {role:'assistant',content:'Newer streamed answer',_ts:6,_live:true}];
            S.busy=true;
            S.activeStreamId='stream-2';
            S.session.active_stream_id='stream-2';
            INFLIGHT['sid-1']={streamId:'stream-2'};
            resolveRefresh(fullHistory);
            try{await pending;}catch(error){regenError=String(error&&error.message||error);}
          }else{
            try{await global.regenerateResponse(button);}catch(error){regenError=String(error&&error.message||error);}
          }
          const refreshedTool=S.toolCalls.find(tc=>tc&&tc.tid==='current-tool')||{};
          regenResult={api_requests:apiRequests,operations,start_calls:startCalls,regen_error:regenError,
            fallback_revision:fallbackRevision,
            revision:S.session?(S.session.regeneration_revision??null):null,session_id:S.session&&S.session.session_id,
            post_refresh_tool_owner_id:refreshedTool.tid||'',post_refresh_tool_owner_index:refreshedTool.assistant_msg_idx??null,
            messages:S.messages,busy:S.busy,active_stream:S.activeStreamId,
            inflight_stream:INFLIGHT['sid-1']&&INFLIGHT['sid-1'].streamId||null};
        }
        if(lifecycle.startsWith('done_usage_two_turns_')){
          const firstAssistant=S.messages.find(m=>m&&m.content==='Current streamed answer')||{};
          settled=false;
          S.session.active_stream_id='stream-2';
          S.session.message_count=S.messages.length+1;
          S.messages.push({role:'user',content:'Next prompt',_ts:5,_pending:true});
          S.activeStreamId='stream-2';
          attachLiveStream('sid-1','stream-2');
          const nextSource=FakeEventSource.instances.at(-1);
          await nextSource.emit('token',{text:'Second streamed answer'});
          global._EPHEMERAL_TURN_FIELDS=['_turnUsage','_turnDuration','_turnTps','_gatewayRouting','_statusCard','_anchor_stream_id','_anchor_activity_scene'];
          global._messageIdentityKey=eval('('+extractFunc(messagesSrc,'_messageIdentityKey')+')');
          global._isHistoricalAnchorActivityScene=eval('('+extractFunc(messagesSrc,'_isHistoricalAnchorActivityScene')+')');
          global._carryForwardEphemeralTurnFields=eval('('+extractFunc(messagesSrc,'_carryForwardEphemeralTurnFields')+')');
          window._carryForwardEphemeralTurnFields=global._carryForwardEphemeralTurnFields;
          const secondMessages=S.messages.map(m=>{
            const row={...m};delete row._pending;delete row._live;
            for(const field of ['_turnUsage','_turnDuration','_turnTps','_gatewayRouting','_statusCard','_anchor_stream_id','_anchor_activity_scene']) delete row[field];
            return row;
          });
          secondMessages.push({role:'assistant',content:'Second streamed answer',_ts:6});
          const secondUsage={input_tokens:8,output_tokens:6,estimated_cost:0.9,cache_read_tokens:10,cache_write_tokens:13,duration_seconds:2,tps:3};
          await nextSource.emit('done',{usage:secondUsage,session:{session_id:'sid-1',messages:secondMessages,
            message_count:secondMessages.length,_messages_offset:0,_messages_truncated:false,
            regeneration_revision:'second-revision',input_tokens:8,output_tokens:6,estimated_cost:0.9,
            cache_read_tokens:10,cache_write_tokens:13,tool_calls:[{tid:'current-tool',name:'read_file',assistant_msg_idx:3}]}});
          drainFrames();
          for(let i=0;i<50&&!settled;i++){
            if(timers.size){const [id,fn]=timers.entries().next().value;timers.delete(id);await fn();}
            for(let j=0;j<10;j++) await Promise.resolve();
            drainFrames();
          }
          const firstAfter=S.messages.find(m=>m&&m.content==='Current streamed answer')||{};
          const secondAssistant=S.messages.find(m=>m&&m.content==='Second streamed answer')||{};
          process.stdout.write(JSON.stringify({first_usage:firstAssistant._turnUsage||null,
            first_usage_after_second:firstAfter._turnUsage||null,second_usage:secondAssistant._turnUsage||null,
            session_totals:{input_tokens:S.session.input_tokens,output_tokens:S.session.output_tokens,
              estimated_cost:S.session.estimated_cost,cache_read_tokens:S.session.cache_read_tokens,cache_write_tokens:S.session.cache_write_tokens}}));
          return;
        }
        if(lifecycle==='done_usage_partial_zero'){
          process.stdout.write(JSON.stringify({session_totals:{input_tokens:S.session.input_tokens,output_tokens:S.session.output_tokens,
            estimated_cost:S.session.estimated_cost,cache_read_tokens:S.session.cache_read_tokens,cache_write_tokens:S.session.cache_write_tokens}}));
          return;
        }
        process.stdout.write(JSON.stringify({messages:S.messages,prior_reasoning:previous.reasoning||'',
          prior_turn_usage:previous._turnUsage||null,tool_owner_index:liveTool.assistant_msg_idx??null,
          tool_owner_id:liveTool.tid||'',settled,active_stream:S.activeStreamId,
          inflight_cleared:!INFLIGHT['sid-1'],current_reasoning:current.reasoning||'',
          ...regenResult,session_totals:S.session&&{input_tokens:S.session.input_tokens,output_tokens:S.session.output_tokens,
            estimated_cost:S.session.estimated_cost,cache_read_tokens:S.session.cache_read_tokens,cache_write_tokens:S.session.cache_write_tokens}}));
        return;
      }
      const rendersBeforeOldFrame=anchorRenderAttempts;
      if(lifecycle==='reattach'||lifecycle==='reconnect') oldFrames.forEach(fn=>fn());
      drainFrames();
      process.stdout.write(JSON.stringify({
        texts:reasoningEvents().map(e=>e.payload.text),settled,
        retained_reasoning:S.messages.at(-1).reasoning||'',
        old_source_closed:source.readyState===2,
        renders_before_old_frame:rendersBeforeOldFrame,
        renders_after_old_frame:anchorRenderAttempts,
      }));
    })().catch(error=>{console.error(error);process.exitCode=1;});
  }else{
  const text='R'.repeat(226748);
  for(let i=0;i<text.length;i+=2000) source.emit('reasoning',{text:text.slice(i,i+2000)});
  const rendersBeforeFrame=anchorRenderAttempts;
  if(process.env.ISSUE5720_SWITCH_BEFORE_FRAME==='1') S.session={session_id:'sid-2'};
  if(process.env.ISSUE5720_REASONING_BOUNDARY==='interim'){
    source.emit('interim_assistant',{text:'Visible answer',already_streamed:true});
  }else if(process.env.ISSUE5720_REASONING_BOUNDARY==='tool'){
    source.emit('tool',{name:'terminal',tid:'qa-tool',args:{command:'pwd'}});
  }else if(process.env.ISSUE5720_REASONING_BOUNDARY){
    source.emit(process.env.ISSUE5720_REASONING_BOUNDARY,{session:finalSession(text),session_id:'sid-1',usage:{}});
  }
  const rendersAtBoundary=anchorRenderAttempts;
  const inflightChars=INFLIGHT['sid-1']?INFLIGHT['sid-1'].lastReasoningText.length:S.messages.at(-1).reasoning.length;
  drainFrames();
  const registry=window._liveAnchorRegistries.get('stream-1');
  const event=registry.anchor.activity_events.find(e=>e.source_event_type==='reasoning');
  process.stdout.write(JSON.stringify({
    renders_before_frame:rendersBeforeFrame,
    renders_after_frame:anchorRenderAttempts,
    renders_at_boundary:rendersAtBoundary,
    settled,inflight_cleared:!INFLIGHT['sid-1'],
    inflight_chars:inflightChars,
    anchor_chars:event&&event.payload.text.length||0,
  }));
  process.exit(0);
  }
}else{

function anchorReasoningRows(){
  return turn.querySelectorAll(
    '[data-anchor-scene-row="1"][data-anchor-source-event-type="reasoning"],'+
    '[data-anchor-scene-row="1"][data-anchor-row-role="thinking"]'
  );
}
function fallbackReasoningRows(){
  return turn.querySelectorAll('.agent-activity-thinking[data-live-thinking="1"]')
    .filter(row=>row.getAttribute('data-anchor-scene-row')!=='1');
}
function reasoningText(row){
  if(!row) return null;
  const pre=row.querySelector&&row.querySelector('pre');
  return pre?pre.textContent:row.textContent;
}
function applyProductionAnchorEvent(sourceEventType, data){
  const registry=window._liveAnchorRegistries&&window._liveAnchorRegistries.get('stream-1');
  if(!registry) throw new Error('missing live anchor registry');
  window.HermesAssistantTurnAnchors.applyAssistantTurnAnchorSourceEvent(
    registry,
    {
      source_event_type:sourceEventType,
      run_id:'run-5720',
      stream_id:'stream-1',
      ...data,
    },
    {
      session_id:'sid-1',
      stream_id:'stream-1',
      run_id:'run-5720',
    }
  );
  return registry;
}
function appendProductionAnchorReasoningRow(localId, text, seq){
  applyProductionAnchorEvent('reasoning',{
    local_id:localId,
    seq,
    text,
    activitySegmentSeq:seq,
    activityBurstId:0,
  });
  window._renderLiveAnchorActivitySceneForStream('stream-1','sid-1',{
    mode:process.env.ISSUE5720_ACTIVITY_MODE||'transparent_stream',
  });
  const row=anchorReasoningRows().find(candidate=>
    candidate.getAttribute('data-anchor-row-id')===`run-5720:${seq}`
  );
  if(!row) throw new Error('production anchor reasoning row not rendered');
  return row;
}
function appendUnkeyedLegacyReasonRow(text){
  const row=new FakeElement('div');
  row.className='wl-reason';
  row.setAttribute('data-worklog-anchor-reason','1');
  row.textContent=text;
  if(isTransparentStream()){
    turn.appendChild(row);
  }else{
    const group=ensureActivityGroup(turn,{activityKey:'live:stream-1'});
    const list=_toolWorklogListEl(group);
    (list||group||turn).appendChild(row);
  }
  return row;
}

source.emit('reasoning',{text:'Plan '});
const first=anchorReasoningRows()[0]||null;
const firstText=reasoningText(first);
const firstFallback=fallbackReasoningRows()[0]||null;
const firstFallbackText=reasoningText(firstFallback);
const passAfterFirst=transparentRenderPasses;
source.emit('reasoning',{text:'step'});
const second=anchorReasoningRows()[0]||null;
const secondText=reasoningText(second);
const anchorRowsAfterRecovery=anchorReasoningRows();
const fallbackRowsAfterRecovery=fallbackReasoningRows();
const registry=window._liveAnchorRegistries&&window._liveAnchorRegistries.get('stream-1');
const anchorReasoningEvents=registry&&registry.anchor&&Array.isArray(registry.anchor.activity_events)
  ? registry.anchor.activity_events.filter(event=>event&&event.source_event_type==='reasoning')
  : [];
let multiSegmentExactFallback=null;
if(process.env.ISSUE5720_MULTI_SEGMENT_FALLBACK==='1'){
  const firstReasoningRow=anchorReasoningRows()[0]||null;
  const firstBefore=reasoningText(firstReasoningRow);
  const secondId='live-reasoning:stream-1:2';
  const registry=applyProductionAnchorEvent('interim_assistant',{
    seq:1,
    text:'',
  });
  const secondReasoningRow=appendProductionAnchorReasoningRow(secondId,'Second draft',2);
  const secondBefore=reasoningText(secondReasoningRow);
  const legacyRow=appendUnkeyedLegacyReasonRow('Legacy unkeyed');
  appendThinking('Second updated',{
    anchorRenderFallback:true,
    sessionId:'sid-1',
    streamId:'stream-1',
    anchorReasoningLocalId:secondId,
    segmentSeq:2,
    burstId:0,
  });
  multiSegmentExactFallback={
    anchor_run_id:registry&&registry.anchor&&registry.anchor.identity&&registry.anchor.identity.run_id,
    first_row_id:firstReasoningRow&&firstReasoningRow.getAttribute('data-anchor-row-id'),
    first_before:firstBefore,
    first_after:reasoningText(firstReasoningRow),
    second_row_id:secondReasoningRow.getAttribute('data-anchor-row-id'),
    second_local_id:secondReasoningRow.getAttribute('data-anchor-local-id'),
    second_before:secondBefore,
    second_after:reasoningText(secondReasoningRow),
    legacy_after:reasoningText(legacyRow),
    live_reasoning_rows:anchorReasoningRows().length,
    fallback_rows:fallbackReasoningRows().length,
  };
}

process.stdout.write(JSON.stringify({
  first_text:firstText,
  first_fallback_text:firstFallbackText,
  second_text:secondText,
  same_node:first===second,
  live_reasoning_rows:anchorRowsAfterRecovery.length,
  fallback_rows_after_recovery:fallbackRowsAfterRecovery.length,
  visible_reasoning_owners:anchorRowsAfterRecovery.length+fallbackRowsAfterRecovery.length,
  render_passes:[passAfterFirst,transparentRenderPasses],
  anchor_render_attempts:anchorRenderAttempts,
  anchor_reasoning_events:anchorReasoningEvents.length,
  anchor_reasoning_text:anchorReasoningEvents.length
    ? String(anchorReasoningEvents[anchorReasoningEvents.length-1].payload&&anchorReasoningEvents[anchorReasoningEvents.length-1].payload.text||'')
    : null,
  inflight_reasoning_text:String(INFLIGHT['sid-1']&&INFLIGHT['sid-1'].lastReasoningText||''),
  multi_segment_exact_fallback:multiSegmentExactFallback,
}));
}
"""
