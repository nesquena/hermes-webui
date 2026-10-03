"""Live thinking card tail-follow regression coverage.

The thinking card body must keep scrolling as reasoning text streams in
(latch-follow), hold a reader who scrolled up, resume when they return to the
bottom, and keep that follow/hold intent across the Worklog detail rebuilds
(capture/restore atBottom) that run while a stream is live.

Tail-follow is explicit to LIVE thinking rows: a settled creation write must
never seed the follow latch (an unchanged settled card at the top stays at the
top across a rebuild), and tool-card details keep master's absolute-offset
restore (no atBottom snapshot, no new-bottom re-pin).

Runs the real functions from static/ui.js in node against a small DOM fake,
mirroring the harness shape of test_issue5720_reasoning_owner.py.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
NODE = shutil.which("node")


_NODE_PROGRAM = r"""
const fs = require('fs');
const uiSrc = fs.readFileSync(process.env.THINKFOLLOW_UI_JS, 'utf8');

function extractFunc(src, name){
  const start = src.indexOf('function ' + name);
  if(start < 0) throw new Error(name + ' not found');
  const brace = src.indexOf('{', start);
  let depth = 1, i = brace + 1;
  while(i < src.length && depth > 0){
    const c = src[i];
    if(c === '{') depth++;
    else if(c === '}') depth--;
    i++;
  }
  if(depth !== 0) throw new Error(name + ' did not close');
  return src.slice(start, i);
}

// ── Minimal DOM fake (same shape as test_issue5720_reasoning_owner.py) ──────
class FakeElement {
  constructor(tag='div'){
    this.tagName=String(tag).toUpperCase();
    this.children=[];
    this.parentNode=null;
    this.attributes=Object.create(null);
    this.dataset=Object.create(null);
    this.id='';
    this._text='';
    this._classes=new Set();
    this.scrollTop=0;
    this.scrollHeight=0;
    this.clientHeight=0;
    this._listeners=Object.create(null);
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
  removeAttribute(name){ delete this.attributes[name]; }
  appendChild(child){
    if(child.parentNode) child.remove();
    child.parentNode=this;
    this.children.push(child);
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
  addEventListener(name, fn){
    (this._listeners[name]||(this._listeners[name]=[])).push(fn);
  }
  // Simulate a native scroll event (user drag or programmatic settle).
  fireScroll(){ (this._listeners['scroll']||[]).forEach(fn=>fn({type:'scroll',target:this})); }
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
  for(const match of selector.matchAll(/\[([^=\]]+)(?:=["']?([^\]"']*)["']?)?\]/g)){
    const value=el.getAttribute(match[1]);
    if(value===null) return false;
    if(match[2]!==undefined&&String(value)!==String(match[2])) return false;
  }
  return true;
}

// ── Harness globals ─────────────────────────────────────────────────────────
global.window={};
global.document={ createElement:tag=>new FakeElement(tag) };
global.S={ session:{session_id:'sid-1'} };
global._sanitizeThinkingDisplayText=value=>String(value||'').trim();
global._thinkingMarkup=()=>'';

const stick=uiSrc.match(/const _THINKING_TAIL_STICK_PX=(\d+)/);
if(!stick) throw new Error('_THINKING_TAIL_STICK_PX not found in ui.js');
globalThis._THINKING_TAIL_STICK_PX=Number(stick[1]);
const sel=uiSrc.match(/const _worklogDetailDisclosureSelector='([^']+)'/);
if(!sel) throw new Error('_worklogDetailDisclosureSelector not found in ui.js');
globalThis._worklogDetailDisclosureSelector=sel[1];

for(const name of [
  '_bindThinkingTailFollow','_worklogDetailBodyAtTail','_thinkingRowIsLive',
  '_worklogDetailTextKey','_worklogDetailBaseKey',
  '_worklogDetailDisclosureIsOpen','_worklogDetailScrollableBody',
  '_setWorklogDetailDisclosureOpen','_worklogDetailDisclosureKeyForElement',
  '_captureWorklogDetailDisclosureState','_restoreWorklogDetailDisclosureState',
  '_renderThinkingInto','_refreshTransparentThinkingLiveRow',
]) eval(extractFunc(uiSrc,name));

// ── Builders ────────────────────────────────────────────────────────────────
function makeThinkingRow(text, live=true){
  const row=new FakeElement('div');
  row.className='agent-activity-thinking';
  row.setAttribute('data-thinking-key','k1');
  if(live) row.setAttribute('data-live-thinking-row','1');
  const card=new FakeElement('div');
  card.className='thinking-card open';
  const body=new FakeElement('div');
  body.className='thinking-card-body';
  body.clientHeight=200;
  const pre=new FakeElement('pre');
  pre.textContent=text;
  body.appendChild(pre);
  card.appendChild(body);
  row.appendChild(card);
  return {row,card,body,pre};
}

function makeTransparentPair(existingText, nextText){
  const existing=makeThinkingRow(existingText);
  existing.row.className='transparent-event-row transparent-thinking-event';
  existing.row.setAttribute('data-event-type','thinking');
  existing.row.setAttribute('data-live-thinking','1');
  const node=makeThinkingRow(nextText);
  node.row.className='transparent-event-row transparent-thinking-event';
  node.row.setAttribute('data-event-type','thinking');
  node.row.setAttribute('data-live-thinking','1');
  return {existing:existing.row, existingBody:existing.body, node:node.row};
}

function makeToolRow(){
  const row=new FakeElement('div');
  row.className='tool-card-row';
  row.setAttribute('data-tool-call-id','t1');
  const card=new FakeElement('div');
  card.className='tool-card open';
  const body=new FakeElement('div');
  body.className='tool-card-detail';
  body.clientHeight=200;
  card.appendChild(body);
  row.appendChild(card);
  return {row,card,body};
}

const out={};

// ── S1: fresh streaming body follows its tail ───────────────────────────────
{
  const {row,body,pre}=makeThinkingRow('');
  body.scrollHeight=200;
  _renderThinkingInto(row,'line one');
  out.s1_write1_scroll=body.scrollTop;
  out.s1_latch_after_first=body._thinkingTailFollow===true;
  body.scrollHeight=800;
  _renderThinkingInto(row,'line one\nline two');
  out.s1_write2_scroll=body.scrollTop;
  body.scrollHeight=1000;
  _renderThinkingInto(row,'line one\nline two\nline three');
  out.s1_write3_scroll=body.scrollTop;

  // ── S2: reader scrolls up mid-stream -> held in place ─────────────────────
  body.scrollTop=300;
  body.fireScroll();
  out.s2_held=body._thinkingTailFollow===false;
  body.scrollHeight=1200;
  _renderThinkingInto(row,'line one\nline two\nline three\nline four');
  out.s2_scroll=body.scrollTop;

  // ── S3: reader returns to the bottom -> follows again ────────────────────
  body.scrollTop=1000;
  body.fireScroll();
  out.s3_refollowed=body._thinkingTailFollow===true;
  body.scrollHeight=1300;
  _renderThinkingInto(row,'line one\nline two\nline three\nline four\nline five');
  out.s3_scroll=body.scrollTop;
}

// ── S4: a settled creation write is not tail-follow state (review #7988) ────
{
  const {row,body}=makeThinkingRow('fixed text', false);
  body.scrollHeight=900;
  _renderThinkingInto(row,'fixed text');
  out.s4_scroll=body.scrollTop;
  out.s4_latch_seeded=body._thinkingTailFollow!==undefined;
  // Capture + rebuild with growth: the settled top-of-card reader must not be
  // jumped to the new bottom.
  const state=_captureWorklogDetailDisclosureState(row);
  const entry=state.get('thinking::k1#0');
  out.s4_snap_has_atbottom=!!(entry&&('atBottom' in entry));
  const rebuilt=makeThinkingRow('fixed text', false);
  rebuilt.body.scrollHeight=1400;
  _restoreWorklogDetailDisclosureState(rebuilt.row,state);
  out.s4_rebuild_scroll=rebuilt.body.scrollTop;
  out.s4_rebuild_latch=rebuilt.body._thinkingTailFollow!==undefined;
}

// ── S5: transparent-stream refresh follows / holds like the worklog path ────
{
  const {existing,existingBody,node}=makeTransparentPair('hi','hi there friend');
  existingBody.scrollHeight=700;
  const ret=_refreshTransparentThinkingLiveRow(existing,node);
  out.s5_return=ret===true;
  out.s5_scroll=existingBody.scrollTop;
  existingBody.scrollTop=150;
  existingBody.fireScroll();
  existingBody.scrollHeight=900;
  _refreshTransparentThinkingLiveRow(existing,node);
  // same text -> no write; grow the text to trigger a real write
  const {node:node2}=makeTransparentPair('hi','hi there friend, more words');
  existingBody.scrollHeight=900;
  _refreshTransparentThinkingLiveRow(existing,node2);
  out.s5b_scroll=existingBody.scrollTop;
}

// ── S6: tail reader survives a rebuild with content growth ──────────────────
{
  const {row,body}=makeThinkingRow('thinking');
  body.scrollHeight=800;
  body.scrollTop=600;
  _bindThinkingTailFollow(body);
  body.fireScroll();
  const state=_captureWorklogDetailDisclosureState(row);
  const entry=state.get('thinking::k1#0');
  out.s6_capture=entry?{open:entry.open,scrollTop:entry.scrollTop,atBottom:entry.atBottom}:null;
  const rebuilt=makeThinkingRow('thinking grown while rebuilding');
  rebuilt.body.scrollHeight=1200;
  _restoreWorklogDetailDisclosureState(rebuilt.row,state);
  out.s6_restored_scroll=rebuilt.body.scrollTop;
  out.s6_latch=rebuilt.body._thinkingTailFollow===true;
}

// ── S7: holding reader survives a rebuild and stays held ────────────────────
{
  const {row,body}=makeThinkingRow('thinking');
  body.scrollHeight=800;
  body.scrollTop=300;
  _bindThinkingTailFollow(body);
  body.fireScroll();
  const state=_captureWorklogDetailDisclosureState(row);
  const entry=state.get('thinking::k1#0');
  out.s7_capture_atBottom=entry?entry.atBottom:null;
  out.s7_capture_scrollTop=entry?entry.scrollTop:null;
  const rebuilt=makeThinkingRow('thinking');
  rebuilt.body.scrollHeight=1200;
  _restoreWorklogDetailDisclosureState(rebuilt.row,state);
  out.s7_restored_scroll=rebuilt.body.scrollTop;
  out.s7_held=rebuilt.body._thinkingTailFollow===false;
  rebuilt.body.scrollHeight=1300;
  _renderThinkingInto(rebuilt.row,'thinking grew again');
  out.s7_after_write_scroll=rebuilt.body.scrollTop;
  // A SECOND rebuild cycle must still hold: the rebuilt body's creation write
  // seeds follow=true, and restore must override it with the captured hold or
  // the reader is yanked to the bottom one tick after scrolling up.
  const state2=_captureWorklogDetailDisclosureState(rebuilt.row);
  const entry2=state2.get('thinking::k1#0');
  out.s7_second_capture_atBottom=entry2?entry2.atBottom:null;
  const rebuilt2=makeThinkingRow('thinking');
  rebuilt2.body.scrollHeight=1400;
  _restoreWorklogDetailDisclosureState(rebuilt2.row,state2);
  out.s7_second_restored_scroll=rebuilt2.body.scrollTop;
  out.s7_second_held=rebuilt2.body._thinkingTailFollow===false;
  rebuilt2.body.scrollHeight=1500;
  _renderThinkingInto(rebuilt2.row,'thinking grew yet again');
  out.s7_second_after_write_scroll=rebuilt2.body.scrollTop;
}

// ── S8: unbound settled body carries no tail-follow state across restore ────
{
  const {row,body}=makeThinkingRow('settled thinking', false);
  body.scrollHeight=800;
  const state=_captureWorklogDetailDisclosureState(row);
  const entry=state.get('thinking::k1#0');
  out.s8_capture_has_atbottom=!!(entry&&('atBottom' in entry));
  const rebuilt=makeThinkingRow('settled thinking', false);
  rebuilt.body.scrollHeight=1200;
  _restoreWorklogDetailDisclosureState(rebuilt.row,state);
  out.s8_restored_scroll=rebuilt.body.scrollTop;
  out.s8_latch=rebuilt.body._thinkingTailFollow===undefined?'none':String(rebuilt.body._thinkingTailFollow);
}

// ── S9: an open tool-card detail keeps its absolute offset across rebuilds ──
{
  const a=makeToolRow();
  a.body.scrollHeight=800;
  a.body.scrollTop=300;
  const state=_captureWorklogDetailDisclosureState(a.row);
  const entry=Array.from(state.values())[0];
  out.s9_capture_has_atbottom=!!(entry&&('atBottom' in entry));
  out.s9_capture_scrollTop=entry?entry.scrollTop:null;
  const b=makeToolRow();
  b.body.scrollHeight=1200;
  _restoreWorklogDetailDisclosureState(b.row,state);
  out.s9_restored_scroll=b.body.scrollTop;
  out.s9_latch=b.body._thinkingTailFollow===undefined?'none':String(b.body._thinkingTailFollow);
  // Even at its tail a tool detail replays its absolute offset (master
  // behavior) — it must never be re-pinned to a rebuilt body's new bottom.
  const c=makeToolRow();
  c.body.scrollHeight=800;
  c.body.scrollTop=600;
  const state2=_captureWorklogDetailDisclosureState(c.row);
  const d=makeToolRow();
  d.body.scrollHeight=1200;
  _restoreWorklogDetailDisclosureState(d.row,state2);
  out.s9_tail_replayed_scroll=d.body.scrollTop;
}

console.log(JSON.stringify(out));
"""


def _run_scenarios() -> dict:
    assert NODE, "node is required for the thinking tail-follow regression"
    env = os.environ.copy()
    env["THINKFOLLOW_UI_JS"] = str(ROOT / "static" / "ui.js")
    result = subprocess.run(
        [NODE, "-e", _NODE_PROGRAM],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_streaming_thinking_body_follows_its_tail():
    out = _run_scenarios()

    # Fresh body not yet scrollable: stays at 0 but latches follow.
    assert out["s1_write1_scroll"] == 0
    assert out["s1_latch_after_first"] is True
    # Growth past the cap: pinned to the new bottom on every write.
    assert out["s1_write2_scroll"] == 600
    assert out["s1_write3_scroll"] == 800


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_reader_who_scrolled_up_is_held_until_back_at_the_tail():
    out = _run_scenarios()

    assert out["s2_held"] is True
    assert out["s2_scroll"] == 300
    assert out["s3_refollowed"] is True
    assert out["s3_scroll"] == 1100


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_creation_write_does_not_pin_a_settled_card():
    out = _run_scenarios()

    # An unchanged settled write never seeds tail-follow state...
    assert out["s4_scroll"] == 0
    assert out["s4_latch_seeded"] is False
    # ...so the capture carries no atBottom and a taller rebuild leaves the
    # top-of-card reader at the top instead of jumping them to the new bottom.
    assert out["s4_snap_has_atbottom"] is False
    assert out["s4_rebuild_scroll"] == 0
    assert out["s4_rebuild_latch"] is False


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_transparent_stream_refresh_follows_and_holds():
    out = _run_scenarios()

    assert out["s5_return"] is True
    assert out["s5_scroll"] == 500
    assert out["s5b_scroll"] == 150


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_tail_reader_is_re_pinned_to_the_new_bottom_after_a_rebuild():
    out = _run_scenarios()

    assert out["s6_capture"] == {"open": True, "scrollTop": 600, "atBottom": True}
    assert out["s6_restored_scroll"] == 1000
    assert out["s6_latch"] is True


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_holding_reader_keeps_place_and_hold_across_a_rebuild():
    out = _run_scenarios()

    assert out["s7_capture_atBottom"] is False
    assert out["s7_capture_scrollTop"] == 300
    assert out["s7_restored_scroll"] == 300
    assert out["s7_held"] is True
    assert out["s7_after_write_scroll"] == 300
    # Second rebuild cycle: the hold must survive repeated rebuilds, not just
    # the first one (restore overrides the rebuilt body's seeded latch).
    assert out["s7_second_capture_atBottom"] is False
    assert out["s7_second_restored_scroll"] == 300
    assert out["s7_second_held"] is True
    assert out["s7_second_after_write_scroll"] == 300


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_unbound_settled_body_is_not_pinned_by_restore():
    out = _run_scenarios()

    assert out["s8_capture_has_atbottom"] is False
    assert out["s8_restored_scroll"] == 0
    assert out["s8_latch"] == "none"


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_tool_detail_keeps_absolute_offset_across_rebuilds():
    out = _run_scenarios()

    # Tool-card details are not tail-follow bodies: no atBottom in the
    # snapshot, no follow latch, and the prior absolute offset is preserved
    # even when the detail was at its tail and the rebuild grew the content.
    assert out["s9_capture_has_atbottom"] is False
    assert out["s9_capture_scrollTop"] == 300
    assert out["s9_restored_scroll"] == 300
    assert out["s9_latch"] == "none"
    assert out["s9_tail_replayed_scroll"] == 600
