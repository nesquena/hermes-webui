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
) -> dict:
    assert NODE, "node is required for the #5720 browser-chain regression"
    env = os.environ.copy()
    env.setdefault("ISSUE5720_UI_JS", str(ROOT / "static" / "ui.js"))
    env.setdefault("ISSUE5720_MESSAGES_JS", str(ROOT / "static" / "messages.js"))
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


_NODE_SCENE = r"""
const fs = require('fs');
const uiSrc = fs.readFileSync(process.env.ISSUE5720_UI_JS, 'utf8');
const messagesSrc = fs.readFileSync(process.env.ISSUE5720_MESSAGES_JS, 'utf8');
const anchorsSrc = fs.readFileSync(process.env.ISSUE5720_ANCHORS_JS, 'utf8');

function extractFunc(src, name){
  const start = src.indexOf('function ' + name);
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
global.requestAnimationFrame=fn=>{fn();return 1;};
global.setTimeout=()=>1;
global.clearTimeout=()=>{};

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
  '_anchorSceneRowTimestampSeconds','_anchorSceneTransparentNodeForRow',
  '_transparentLiveRowKey','_transparentLiveRowsCompatible',
  '_transparentLiveRowAttributePairs','_transparentLiveRowInteractiveState',
  '_refreshTransparentThinkingLiveRow','_refreshTransparentLiveRow',
  '_thinkingMarkup','_renderThinkingInto',
  '_resetMismatchedLiveAssistantTurnForSession',
  '_liveAnchorReasoningRowForFallback','_updateLiveAnchorReasoningRowForFallback',
  '_anchorSceneNodeForRow','_anchorSceneWorklogGroup','_renderAnchorSceneRowsIntoWorklog',
  'isLiveAnchorActivitySceneOwner','_projectLiveAnchorActivitySceneForStream',
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
};
const INFLIGHT=global.INFLIGHT={};
const LIVE_STREAMS=global.LIVE_STREAMS={};
const _STREAM_WAS_HIDDEN=global._STREAM_WAS_HIDDEN={};
const _STREAM_NOTIFICATION_BACKGROUND=global._STREAM_NOTIFICATION_BACKGROUND={};
const _desktopBackgroundedForNotifications=false;
global._bindStreamHiddenTracker=()=>{};
global.closeOtherLiveStreams=()=>{};
global.closeLiveStream=()=>{};
global.resetTurnWorkspaceMutations=()=>{};
global._resetStreamScrollFollow=()=>{};
global._suspendSessionStreamForLiveChat=()=>{};
global.ensureLiveWorklogShell=()=>null;
global._extractInlineThinkingFromContent=(content,reasoning)=>({content:String(content||''),reasoning:String(reasoning||'')});

class FakeEventSource {
  static instances=[];
  static OPEN=1;
  static CONNECTING=0;
  constructor(){ this.listeners=Object.create(null);this.readyState=1;FakeEventSource.instances.push(this); }
  addEventListener(name,fn){ (this.listeners[name]||(this.listeners[name]=[])).push(fn); }
  emit(name,data){ for(const fn of this.listeners[name]||[]) fn({data:JSON.stringify(data),lastEventId:''}); }
  close(){ this.readyState=2; }
}
global.EventSource=FakeEventSource;

const attachStart=messagesSrc.indexOf('function attachLiveStream(');
const attachEnd=messagesSrc.indexOf('\nfunction transcript(){',attachStart);
if(attachStart<0||attachEnd<0) throw new Error('attachLiveStream source boundary not found');
eval(messagesSrc.slice(attachStart,attachEnd));
attachLiveStream('sid-1','stream-1');
const source=FakeEventSource.instances[0];
if(!source) throw new Error('attachLiveStream did not create EventSource');
if(process.env.ISSUE5720_STALE_ACTIVE_STREAM==='1') S.activeStreamId='stream-new';

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
"""
