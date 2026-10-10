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
  removeEventListener(name, fn){
    const list=this._listeners[name]||[];
    const idx=list.indexOf(fn);
    if(idx>=0) list.splice(idx,1);
  }
  // Simulate a native scroll event (user drag or programmatic settle).
  fireScroll(){ (this._listeners['scroll']||[]).forEach(fn=>fn({type:'scroll',target:this})); }
  // Simulate the end of the card open transition. Real cards animate both
  // opacity and max-height; only max-height marks a stable layout, so the
  // property is explicit here (#7988 review).
  fireTransitionEnd(propertyName='max-height'){ (this._listeners['transitionend']||[]).slice().forEach(fn=>fn({type:'transitionend',target:this,propertyName})); }
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
  '_bindThinkingTailFollow','_worklogDetailBodyAtTail','_thinkingRowIsLive','_toggleThinkingCard','_thinkingCardOpened','_setTransparentCardOpen',
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

// ── S10: collapsed cards never bind/pin; opening seeds from position ────────
{
  const {row,card,body}=makeThinkingRow('thinking', true);
  card.classList.remove('open');
  body.clientHeight=0;
  body.scrollTop=0;
  let reads=0;
  Object.defineProperty(body,'scrollHeight',{get(){reads+=1;return this._scrollHeight;},configurable:true});
  body._scrollHeight=200;
  _renderThinkingInto(row,'thinking grew while collapsed');
  body._scrollHeight=800;
  _renderThinkingInto(row,'thinking grew again while collapsed');
  out.s10_scroll_reads=reads;
  out.s10_latch_seeded=body._thinkingTailFollow!==undefined;
  out.s10_scroll=body.scrollTop;
  // Opening at the top seeds a hold once the open animation settles — the
  // next delta must not yank the reader.
  _toggleThinkingCard(card);
  body.fireTransitionEnd();
  out.s10_open=card.classList.contains('open');
  out.s10_latch=body._thinkingTailFollow===false?'held':String(body._thinkingTailFollow);
  body.clientHeight=200;
  body._scrollHeight=1000;
  _renderThinkingInto(row,'thinking grew after open');
  out.s10_after_write_scroll=body.scrollTop;
}

// ── S11: collapse then expand mid-stream keeps following ────────────────────
{
  const {row,card,body}=makeThinkingRow('thinking', true);
  body.scrollHeight=800;
  _renderThinkingInto(row,'thinking');
  _renderThinkingInto(row,'thinking longer');
  out.s11_before_collapse_scroll=body.scrollTop;
  _toggleThinkingCard(card);
  out.s11_collapsed=!card.classList.contains('open');
  body.scrollHeight=1000;
  _renderThinkingInto(row,'thinking grew while collapsed');
  out.s11_collapse_scroll=body.scrollTop;
  _toggleThinkingCard(card);
  body.fireTransitionEnd();
  out.s11_after_expand_scroll=body.scrollTop;
  out.s11_latch=String(body._thinkingTailFollow);
}

// ── S12: the transparent-card open handler seeds follow state too ───────────
{
  // A long live card opened at the top via _setTransparentCardOpen must hold.
  const a=makeThinkingRow('thinking', true);
  a.card.classList.remove('open');
  a.body.clientHeight=0;
  a.body.scrollHeight=1000;
  _setTransparentCardOpen(a.card, true);
  a.body.fireTransitionEnd();
  out.s12_open_latch=a.body._thinkingTailFollow===false?'held':String(a.body._thinkingTailFollow);
  a.body.clientHeight=200;
  _renderThinkingInto(a.row,'thinking grew after transparent open');
  out.s12_after_write_scroll=a.body.scrollTop;
  // A body that was following re-pins at the tail when reopened.
  const b=makeThinkingRow('thinking', true);
  b.body.scrollHeight=800;
  _renderThinkingInto(b.row,'thinking');
  _renderThinkingInto(b.row,'thinking longer');
  _setTransparentCardOpen(b.card, false);
  b.body.scrollHeight=1000;
  _setTransparentCardOpen(b.card, true);
  b.body.fireTransitionEnd();
  out.s12_reopen_scroll=b.body.scrollTop;
  out.s12_reopen_latch=String(b.body._thinkingTailFollow);
}

// ── S13: no delayed pin after the reader scrolls up; no re-arm when open ────
{
  // Re-applying open on an already-open card (the rehydrate path) must not
  // arm a delayed settle at all.
  const a=makeThinkingRow('thinking', true);
  a.body.scrollHeight=800;
  _renderThinkingInto(a.row,'thinking');
  _renderThinkingInto(a.row,'thinking longer');
  const before=(a.body._listeners['transitionend']||[]).length;
  _setTransparentCardOpen(a.card, true);
  out.s13_rearmed=((a.body._listeners['transitionend']||[]).length>before);
  // A scroll-up between arm and settle must win: settle re-checks the latch
  // at fire time instead of pinning from the armed value. Faithful repro: a
  // body that was following, collapsed, reopened, then scrolled up before the
  // open animation settles.
  const c=makeThinkingRow('thinking', true);
  c.body.scrollHeight=800;
  _renderThinkingInto(c.row,'thinking');
  _renderThinkingInto(c.row,'thinking longer');
  _toggleThinkingCard(c.card);
  c.body.scrollHeight=1000;
  _renderThinkingInto(c.row,'thinking grew while collapsed');
  _toggleThinkingCard(c.card);
  c.body.scrollTop=0;
  c.body.fireScroll();
  out.s13_latch_after_scrollup=c.body._thinkingTailFollow===false?'held':String(c.body._thinkingTailFollow);
  c.body.fireTransitionEnd();
  out.s13_scroll_after_settle=c.body.scrollTop;
}

// ── S14: a delta landing INSIDE the open window must not pin ────────────────
{
  // Reviewer repro: open long reasoning, wheel up at scrollTop 0, then a delta
  // arrives before transitionend. The old head jumped 0 -> 992.
  const {row,card,body}=makeThinkingRow('thinking', true);
  card.classList.remove('open');
  body.clientHeight=0;
  body.scrollHeight=1000;
  _toggleThinkingCard(card);
  out.s14_settling=body._thinkingTailSettling===true;
  body.clientHeight=200;
  body.scrollTop=0;
  // Deltas during the settle window: none may move the reader.
  _renderThinkingInto(row,'thinking grew during the open animation');
  out.s14_scroll_mid_animation=body.scrollTop;
  _renderThinkingInto(row,'thinking grew again during the open animation');
  out.s14_scroll_mid_animation_2=body.scrollTop;
  out.s14_latch_mid=String(body._thinkingTailFollow);
  body.fireTransitionEnd();
  out.s14_settling_cleared=body._thinkingTailSettling===false;
  out.s14_scroll_after_settle=body.scrollTop;
}

// ── S15: settle ignores the opacity transition, waits for max-height ────────
{
  // opacity ends ~173ms, max-height ~222ms. Measuring on opacity reads a
  // mid-animation height and strands a card that fits on HOLD.
  const {row,card,body}=makeThinkingRow('thinking', true);
  card.classList.remove('open');
  body.clientHeight=0;
  body.scrollHeight=1000;
  _toggleThinkingCard(card);
  // The opacity transition finishes first — it must NOT settle the card.
  body.fireTransitionEnd('opacity');
  out.s15_still_settling_after_opacity=body._thinkingTailSettling===true;
  out.s15_latch_after_opacity=String(body._thinkingTailFollow);
  // Layout is only stable once max-height ends; now the card fits.
  body.clientHeight=1000;
  body.fireTransitionEnd('max-height');
  out.s15_settled=body._thinkingTailSettling===false;
  out.s15_latch_after_maxheight=String(body._thinkingTailFollow);
  body.clientHeight=200;
  body.scrollHeight=1400;
  _renderThinkingInto(row,'thinking grew after a proper settle');
  out.s15_follows_after_settle=body.scrollTop;
}

// ── S16: quick re-toggle does not strand a live card on hold ───────────────
{
  // Open, close inside the open animation, reopen. The close transition's
  // transitionend used to measure a collapsed body and seed HOLD forever.
  const {row,card,body}=makeThinkingRow('thinking', true);
  card.classList.remove('open');
  body.clientHeight=0;
  body.scrollHeight=400;
  _toggleThinkingCard(card);
  _toggleThinkingCard(card);
  out.s16_closed=!card.classList.contains('open');
  // The stale settle from the first open fires against a collapsed body.
  body.fireTransitionEnd();
  out.s16_latch_after_stale_settle=String(body._thinkingTailFollow);
  // Reopening re-arms and seeds from a real layout.
  _toggleThinkingCard(card);
  body.clientHeight=400;
  body.fireTransitionEnd();
  out.s16_latch_after_reopen=String(body._thinkingTailFollow);
  body.clientHeight=200;
  body.scrollHeight=800;
  _renderThinkingInto(row,'thinking grew after the re-toggle');
  out.s16_follows_after_reopen=body.scrollTop;
}

// ── S17: opening SETTLED history never binds follow state ──────────────────
{
  // Reviewer repro: open a history card, scroll to its bottom, reflow
  // 1280 -> 390, re-render. This head jumped 2460 -> 5430; master kept 2460.
  const {row,card,body}=makeThinkingRow('thinking', false);
  card.classList.remove('open');
  body.clientHeight=0;
  body.scrollHeight=3000;
  _toggleThinkingCard(card);
  body.clientHeight=540;
  body.fireTransitionEnd();
  out.s17_latch=String(body._thinkingTailFollow);
  out.s17_settling=String(body._thinkingTailSettling);
  // Narrow reflow makes the same text taller; a settled card must not chase it.
  body.scrollTop=2460;
  body.scrollHeight=6000;
  _renderThinkingInto(row,'thinking');
  out.s17_scroll_after_reflow=body.scrollTop;
}

// ── S18: a stale settle timer from a previous open is a no-op ──────────────
{
  // Reviewer repro: close and quickly reopen; the FIRST open's timer fires
  // while the second open is still animating. It used to measure a collapsed
  // body, seed HOLD, and strand a card that fits when fully open.
  const {row,card,body}=makeThinkingRow('thinking', true);
  card.classList.remove('open');
  body.clientHeight=0;
  body.scrollHeight=1000;
  _toggleThinkingCard(card);   // open #1
  _toggleThinkingCard(card);   // close before the transition ends
  _toggleThinkingCard(card);   // reopen (supersedes open #1's settle window)
  // The stale timer from open #1 fires mid-animation of the current open —
  // only that stale settle runs now (the current transition is still going).
  body.clientHeight=0;
  const stale=(body._listeners['transitionend']||[])[0];
  if(stale) stale({type:'transitionend',target:body,propertyName:'max-height'});
  out.s18_latch_after_stale=String(body._thinkingTailFollow);
  out.s18_still_settling=body._thinkingTailSettling===true;
  // The current open's transition finishes: the card fits, so it follows.
  body.clientHeight=1000;
  body.fireTransitionEnd('max-height');
  out.s18_latch_after_real=String(body._thinkingTailFollow);
  body.clientHeight=200;
  body.scrollHeight=1200;
  _renderThinkingInto(row,'thinking grew after the quick reopen');
  out.s18_follows=body.scrollTop;
}

// ── S19: a COMPLETED card keeps no follow state across capture/restore ─────
{
  // Reviewer repro: follow live reasoning -> complete -> narrow 1280->390 ->
  // re-render. The completed body still carries its streaming latch, and
  // capture/restore used to trust it, jumping 2790 -> 6090. A settled
  // destination restores its saved absolute offset instead.
  const {row,body}=makeThinkingRow('thinking');   // live while streaming
  body.scrollHeight=800;
  body.scrollTop=780;
  _bindThinkingTailFollow(body);
  body.fireScroll();
  // Completion strips the live markers (settlement demotion).
  row.removeAttribute('data-live-thinking-row');
  row.removeAttribute('data-live-thinking');
  row.removeAttribute('data-thinking-active');
  const state=_captureWorklogDetailDisclosureState(row);
  const entry=state.get('thinking::k1#0');
  out.s19_snap_has_atbottom=!!(entry&&('atBottom' in entry));
  out.s19_snap_scrolltop=entry?entry.scrollTop:null;
  // Rebuild of the completed card after a 1280->390 reflow (content taller).
  const rebuilt=makeThinkingRow('thinking', false);
  rebuilt.body.scrollHeight=6000;
  _restoreWorklogDetailDisclosureState(rebuilt.row,state);
  out.s19_rebuild_scroll=rebuilt.body.scrollTop;
  out.s19_rebuild_latch=String(rebuilt.body._thinkingTailFollow);
}

// ── S20: a collapsed following card resumes follow after a rebuild ─────────
{
  // Greptile P1: capture saved atBottom but restore bound only OPEN bodies, so
  // reopening the rebuilt card measured its top and put it on hold.
  const {row,card,body}=makeThinkingRow('thinking');   // live
  card.classList.remove('open');
  body.scrollHeight=800;
  body.scrollTop=600;
  _bindThinkingTailFollow(body, true);   // was following when collapsed
  const state=_captureWorklogDetailDisclosureState(row);
  const entry=state.get('thinking::k1#0');
  out.s20_snap=entry?{open:entry.open,atBottom:entry.atBottom}:null;
  // Scene update rebuilds the collapsed card (content grew).
  const rebuilt=makeThinkingRow('thinking grown while rebuilt');
  rebuilt.card.classList.remove('open');
  rebuilt.body.scrollHeight=1200;
  _restoreWorklogDetailDisclosureState(rebuilt.row,state);
  out.s20_latch_while_closed=String(rebuilt.body._thinkingTailFollow);
  out.s20_scroll_while_closed=rebuilt.body.scrollTop;
  // Reopening resumes follow at the new tail instead of seeding HOLD.
  _toggleThinkingCard(rebuilt.card);
  rebuilt.body.clientHeight=1000;
  rebuilt.body.fireTransitionEnd('max-height');
  out.s20_latch_after_reopen=String(rebuilt.body._thinkingTailFollow);
  out.s20_scroll_after_reopen=rebuilt.body.scrollTop;
}

// ── S21: a rebuild that interrupts the FIRST open finishes the open ────────
{
  // Greptile P1: capture omits atBottom while the latch is still unset (the
  // settle window). The rebuild discards the armed body, restore reopened the
  // replacement without arming anything, and the card stayed follow-less.
  const {row,card,body}=makeThinkingRow('thinking');   // live
  card.classList.remove('open');
  body.clientHeight=0;
  body.scrollHeight=1000;
  _toggleThinkingCard(card);   // first open: settle armed, latch not set yet
  out.s21_latch_during_open=String(body._thinkingTailFollow);
  const state=_captureWorklogDetailDisclosureState(row);
  const entry=state.get('thinking::k1#0');
  out.s21_snap_has_atbottom=!!(entry&&('atBottom' in entry));
  // Scene update rebuilds mid-open: the armed body is discarded.
  const rebuilt=makeThinkingRow('thinking');
  rebuilt.body.scrollHeight=1000;
  _restoreWorklogDetailDisclosureState(rebuilt.row,state);
  out.s21_armed_after_restore=rebuilt.body._thinkingTailSettling===true;
  // The replacement finishes its open and seeds from its real position.
  rebuilt.body.clientHeight=1000;
  rebuilt.body.fireTransitionEnd('max-height');
  out.s21_latch_after_settle=String(rebuilt.body._thinkingTailFollow);
  rebuilt.body.clientHeight=200;
  rebuilt.body.scrollHeight=1400;
  _renderThinkingInto(rebuilt.row,'thinking grew after the rebuild');
  out.s21_follows=rebuilt.body.scrollTop;
}

console.log(JSON.stringify(out, null, 2));
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


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_collapsed_card_opens_at_top_without_a_yank():
    out = _run_scenarios()

    # While collapsed: no latch, no pin, and no forced layout (no scrollHeight
    # reads at all) across deltas.
    assert out["s10_scroll_reads"] == 0
    assert out["s10_latch_seeded"] is False
    assert out["s10_scroll"] == 0
    # Opening seeds the latch from the real position: at the top of a long
    # card that is a hold, and the next delta must not move the reader.
    assert out["s10_open"] is True
    assert out["s10_latch"] == "held"
    assert out["s10_after_write_scroll"] == 0


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_transparent_open_handler_seeds_follow_state():
    out = _run_scenarios()

    # _setTransparentCardOpen must run the same open-time settling as the
    # inline toggle: opened at the top of a long card that is a hold, and the
    # next delta must not yank the reader.
    assert out["s12_open_latch"] == "held"
    assert out["s12_after_write_scroll"] == 0
    # A body that was following re-pins at the tail when reopened.
    assert out["s12_reopen_scroll"] == 800
    assert out["s12_reopen_latch"] == "true"


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_no_delayed_pin_after_scroll_up_and_no_rearm_when_open():
    out = _run_scenarios()

    # The rehydrate path re-applies open on every refresh: it must not arm a
    # delayed settle on an already-open card.
    assert out["s13_rearmed"] is False
    # A scroll-up between arm and settle wins over the armed pin.
    assert out["s13_latch_after_scrollup"] == "held"
    assert out["s13_scroll_after_settle"] == 0


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_delta_during_the_open_animation_does_not_pin_a_holding_reader():
    out = _run_scenarios()

    # The open window is published so both writers can stand down.
    assert out["s14_settling"] is True
    # Deltas arriving before the layout is stable must not move the reader.
    assert out["s14_scroll_mid_animation"] == 0
    assert out["s14_scroll_mid_animation_2"] == 0
    # No latch is seeded while settling, so nothing can claim "following".
    assert out["s14_latch_mid"] == "undefined"
    # Once max-height ends the window closes and the real position decides.
    assert out["s14_settling_cleared"] is True
    assert out["s14_scroll_after_settle"] == 0


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_settle_waits_for_max_height_not_opacity():
    out = _run_scenarios()

    # opacity finishes first and must be ignored: settling stays armed.
    assert out["s15_still_settling_after_opacity"] is True
    assert out["s15_latch_after_opacity"] == "undefined"
    # max-height marks a stable layout: a card that fits seeds follow, not hold.
    assert out["s15_settled"] is True
    assert out["s15_latch_after_maxheight"] == "true"
    assert out["s15_follows_after_settle"] == 1200


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_quick_retoggle_does_not_strand_a_live_card_on_hold():
    out = _run_scenarios()

    assert out["s16_closed"] is True
    # The stale settle fires against a collapsed body: it must not seed HOLD.
    assert out["s16_latch_after_stale_settle"] == "undefined"
    # Reopening measures a real layout and resumes following.
    assert out["s16_latch_after_reopen"] == "true"
    assert out["s16_follows_after_reopen"] == 600


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_opening_settled_history_never_binds_follow_state():
    out = _run_scenarios()

    # Opening a settled card must leave it completely unbound...
    assert out["s17_latch"] == "undefined"
    assert out["s17_settling"] == "undefined"
    # ...so a 1280->390 reflow keeps the reader's position instead of chasing
    # the new taller tail.
    assert out["s17_scroll_after_reflow"] == 2460


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_stale_settle_timer_from_a_previous_open_is_a_noop():
    out = _run_scenarios()

    # The stale timer fires mid-animation of the current open: it must not
    # seed HOLD on a card that will fit when fully open...
    assert out["s18_latch_after_stale"] == "undefined"
    # ...nor clear the new open's settling window.
    assert out["s18_still_settling"] is True
    # The current transition settles normally and the card follows.
    assert out["s18_latch_after_real"] == "true"
    assert out["s18_follows"] == 1000


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_completed_card_keeps_no_follow_state_across_capture_restore():
    out = _run_scenarios()

    # Capture must ignore the latch a completed body inherited from streaming.
    assert out["s19_snap_has_atbottom"] is False
    assert out["s19_snap_scrolltop"] == 780
    # The settled destination restores its saved absolute offset instead of
    # chasing the new tail of the reflowed content.
    assert out["s19_rebuild_scroll"] == 780
    assert out["s19_rebuild_latch"] == "undefined"


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_collapsed_following_card_resumes_follow_after_rebuild():
    out = _run_scenarios()

    # Capture keeps the intent of a collapsed following card...
    assert out["s20_snap"] == {"open": False, "atBottom": True}
    # ...restore carries the latch onto the closed body without scrolling it...
    assert out["s20_latch_while_closed"] == "true"
    assert out["s20_scroll_while_closed"] == 0
    # ...and reopening resumes follow at the new tail instead of seeding HOLD.
    assert out["s20_latch_after_reopen"] == "true"
    assert out["s20_scroll_after_reopen"] == 200


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_rebuild_that_interrupts_the_first_open_finishes_the_open():
    out = _run_scenarios()

    # The latch is not established yet during the settle window...
    assert out["s21_latch_during_open"] == "undefined"
    # ...so capture legitimately omits atBottom.
    assert out["s21_snap_has_atbottom"] is False
    # Restore re-arms the open on the replacement body instead of leaving it
    # follow-less forever.
    assert out["s21_armed_after_restore"] is True
    assert out["s21_latch_after_settle"] == "true"
    # Growth after the rebuild is followed again.
    assert out["s21_follows"] == 1200


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_collapse_and_expand_mid_stream_keeps_following():
    out = _run_scenarios()

    assert out["s11_before_collapse_scroll"] == 600
    assert out["s11_collapsed"] is True
    # No pin while collapsed (the body stays where it was)...
    assert out["s11_collapse_scroll"] == 600
    # ...and expanding resumes follow at the new tail.
    assert out["s11_after_expand_scroll"] == 800
    assert out["s11_latch"] == "true"


def test_live_marker_is_cleared_at_settlement():
    src = (Path(__file__).resolve().parents[1] / "static" / "ui.js").read_text()

    # Every row-level settlement/demotion strip that clears data-live-thinking
    # must also clear data-live-thinking-row, or _thinkingRowIsLive stays true
    # on persisted cards (#7988 review).
    strips = src.count("removeAttribute('data-live-thinking')")
    live_row_strips = src.count("removeAttribute('data-live-thinking-row')")
    assert strips >= 5
    assert live_row_strips == strips
