"""Regression: the assistant answer is displayed TWICE (#6948 follow-up;
upstream symptom report #2051).

Two independent renderers can put a second copy of the same answer on screen
after a turn settles. Both are covered here.

1. The live-turn node (static/ui.js)
------------------------------------
`renderMessages()` re-attaches the preserved `#liveAssistantTurn` node after the
transcript wipe, and `restoreLiveTurnHtmlForSession()` re-inserts the
`INFLIGHT[sid].liveTurnHtml` snapshot on a session switch / reconnect. Each has
ONE branch that *appends* — i.e. adds a turn the settled rebuild did not
produce. The assistant row is persisted a few ms BEFORE the stream's terminal
event clears `S.activeStreamId`, so a dead live node left behind by an INFLIGHT
entry that outlived its stream is appended under the settled copy: the same
answer twice, self-sustaining across later renders (the node keeps
id=liveAssistantTurn and is re-preserved by the #6948 guard), healed only by a
reload.

The contract (`_settledTranscriptOwnsLiveTurn`) is an OWNERSHIP proof, never a
rendered-text comparison:

  * the live body comes from the streaming smd parser and the settled body from
    renderMd + post-processing, so comparing textContent would only ever match
    single-paragraph plain text — lists, fenced code, tables and `_emphasis_`
    render differently in the two pipelines;
  * comparing text would DELETE a genuinely live turn whenever the previous
    answer reads the same ("Done.", a greeting, a same-prompt resend), because
    while the current turn is unpersisted the last settled assistant message is
    the PREVIOUS turn's answer.

So the append is skipped only when the transcript ENDS with a settled assistant
message that the SAME stream produced (persisted anchor identity, or a
source-vs-source text match when the turn has no persisted scene), and the
preserved node carries nothing the settled rebuild could not have produced (no
unpersisted tool card / reasoning row, no second live segment — #3714).

2. The settled activity scene (static/ui.js)
--------------------------------------------
`_anchorSceneRowsForRendering(scene,{settled:true})` feeds both settled
renderers. A `process_prose` row whose text IS the final answer is suppressed
per-row by the transparent-stream renderer but NOT by the compact-worklog row
builder, which rebuilds it as a second `.assistant-segment` above the settled
answer. The row is now dropped in the shared filter, using a near-equality test
without the absolute `shorter>=80` floor that made the existing matcher a no-op
for short answers (the reported case: the row is the answer minus its last
streamed token, 35 vs 36 chars). LIVE rendering is untouched — while streaming,
the inline live segment is hidden and the prose row IS the visible answer.
"""
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.js_source_extract import extract_function

ROOT = Path(__file__).resolve().parents[1]
UI_JS = (ROOT / "static" / "ui.js").read_text(encoding="utf-8")
NODE = shutil.which("node")


def js(*names):
    """Real source of the named ui.js functions — never a re-implementation."""
    return "\n".join(extract_function(UI_JS, name) for name in names)


def run_node(script):
    out = subprocess.run([NODE, "-e", script], capture_output=True, text=True)
    assert out.returncode == 0, f"node harness failed:\n{out.stderr}\n{out.stdout}"
    assert "OK" in out.stdout, out.stdout
    return out.stdout


# Minimal DOM stand-in: class/attribute selector matching over a node tree.
DOM_STUB = """
const assert=require('assert');
class El{
  constructor(spec){
    spec=spec||{};
    this.classes=new Set(spec.classes||[]);
    this.attrs=Object.assign({},spec.attrs||{});
    this.children=spec.children||[];
  }
  _matchesToken(tok){
    tok=String(tok).trim();
    if(tok.startsWith('.')) return this.classes.has(tok.slice(1));
    const m=/^\\[([a-zA-Z0-9-]+)="([^"]*)"\\]$/.exec(tok);
    if(m) return String(this.attrs[m[1]]===undefined?'':this.attrs[m[1]])===m[2];
    return false;
  }
  querySelectorAll(sel){
    const toks=String(sel).split(',');
    const out=[];
    const walk=(node)=>{
      for(const child of node.children){
        if(toks.some(t=>child._matchesToken(t))) out.push(child);
        walk(child);
      }
    };
    walk(this);
    return out;
  }
}
const seg=(text)=>new El({attrs:{'data-live-assistant':'1'},classes:['assistant-segment'],
  children:[new El({classes:['msg-body'],children:[]})]});
const turnWith=(...kids)=>new El({classes:['assistant-turn'],children:kids});
const toolCard=()=>new El({classes:['tool-card-row']});
const emptyWorklogShell=()=>new El({classes:['live-worklog'],attrs:{'data-live-worklog-shell':'1'}});
var S={messages:[],activeStreamId:null,session:{session_id:'s1'}};
var INFLIGHT={};
function setState(messages,inflight,activeStreamId){
  S.messages=messages;
  S.activeStreamId=activeStreamId===undefined?null:activeStreamId;
  for(const k of Object.keys(INFLIGHT)) delete INFLIGHT[k];
  if(inflight) INFLIGHT['s1']=inflight;
}
const user=(content)=>({role:'user',content});
const settledAnswer=(content,streamId)=>{
  const m={role:'assistant',content};
  if(streamId) m._anchor_stream_id=streamId;
  return m;
};
const sceneIdentityAnswer=(content,streamId)=>({
  role:'assistant',content,
  _anchor_activity_scene:{identity:{stream_id:streamId},final_answer:content},
});
"""


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_dead_live_turn_is_dropped_only_when_the_stream_is_provably_settled():
    """The #2051 duplicate is suppressed; a live turn is never deleted."""
    script = DOM_STUB + js(
        "msgContent",
        "_settledAssistantStreamId",
        "_messageHasLiveAssistantProjection",
        "_liveTurnCarriesUnsettledContent",
        "_settledTranscriptOwnsLiveTurn",
    ) + """
const ANSWER='Hey! What can I help you with today?';

// ── the bug: stream settled, INFLIGHT not yet cleaned → dead leftover ──
setState([user('Hi'),settledAnswer(ANSWER,'st1')],{streamId:'st1'},'st1');
assert.strictEqual(
  _settledTranscriptOwnsLiveTurn('s1',turnWith(seg(ANSWER),emptyWorklogShell())),true,
  'dead live node whose stream the settled transcript owns must be dropped (#2051)');

// Identity may live only on the persisted scene.
setState([user('Hi'),sceneIdentityAnswer(ANSWER,'st1')],{streamId:'st1'},null);
assert.strictEqual(_settledTranscriptOwnsLiveTurn('s1',turnWith(seg(ANSWER))),true,
  'scene identity.stream_id is accepted as ownership proof');

// Shape independence: the decision must not depend on rendered text at all, so
// a multi-block answer (fenced code / list / table), which the smd parser and
// renderMd render to DIFFERENT textContent, is handled like plain prose.
setState([user('Hi'),settledAnswer('Here you go:\\n\\n```python\\nprint(1)\\n```','st1')],
  {streamId:'st1'},'st1');
assert.strictEqual(
  _settledTranscriptOwnsLiveTurn('s1',turnWith(seg('Here you go:print(1)'))),true,
  'a code-block answer must be de-duplicated too (renderer-independent)');

// ── false positives that a text comparison would cause ──
// The current turn is unpersisted: the transcript ends with the USER turn while
// the live turn streams text identical to the PREVIOUS answer.
setState([user('Hi'),settledAnswer('Done.','st1'),user('Hi')],{streamId:'st2'},'st2');
assert.strictEqual(_settledTranscriptOwnsLiveTurn('s1',turnWith(seg('Done.'))),false,
  'a genuinely live turn must never be dropped because an earlier answer matches');
// markInflight() shape at send time (no snapshot yet, stream just started).
setState([user('Hi'),settledAnswer('Done.','st1'),user('Hi')],{sid:'s1',streamId:'st2',ts:1},'st2');
assert.strictEqual(_settledTranscriptOwnsLiveTurn('s1',turnWith(seg('Do'))),false,
  'partial live text under a fresh stream must never be dropped');
// A settled tail from a DIFFERENT stream never owns this live turn.
setState([user('Hi'),settledAnswer('Done.','st1')],{streamId:'st2'},'st2');
assert.strictEqual(_settledTranscriptOwnsLiveTurn('s1',turnWith(seg('Done.'))),false,
  'a tail from another stream must not authorize dropping the live turn');
// Reconnect/terminal projection: S.messages still carries a live marker.
setState([user('Hi'),Object.assign(settledAnswer('Done.','st1'),{_live:true})],
  {streamId:'st1'},null);
assert.strictEqual(_settledTranscriptOwnsLiveTurn('s1',turnWith(seg('Done.'))),false,
  'a live projection in S.messages means the turn has not settled');
// Ownership unknown (no INFLIGHT stream id, no active stream) → fail closed.
setState([user('Hi'),settledAnswer('Done.','st1')],{},null);
assert.strictEqual(_settledTranscriptOwnsLiveTurn('s1',turnWith(seg('Done.'))),false,
  'unknown live owner must fail closed');
setState([],{streamId:'st1'},'st1');
assert.strictEqual(_settledTranscriptOwnsLiveTurn('s1',turnWith(seg('Done.'))),false,
  'an empty transcript owns nothing');

// ── #3714: live-only structure is never discarded ──
setState([user('Hi'),settledAnswer(ANSWER,'st1')],{streamId:'st1'},'st1');
assert.strictEqual(
  _settledTranscriptOwnsLiveTurn('s1',turnWith(toolCard(),seg(ANSWER))),false,
  'a preserved turn carrying an unpersisted tool card must be kept');
assert.strictEqual(
  _settledTranscriptOwnsLiveTurn('s1',turnWith(seg('first'),seg(ANSWER))),false,
  'a multi-segment live turn must not be dropped on a tail-segment match');
assert.strictEqual(
  _settledTranscriptOwnsLiveTurn('s1',turnWith(new El({classes:['wl-reason']}),seg(ANSWER))),false,
  'a preserved turn carrying an unpersisted reasoning row must be kept');
assert.strictEqual(_settledTranscriptOwnsLiveTurn('s1',{}),false,
  'an unknown node shape must fail closed');

// ── no persisted scene identity: SOURCE text vs SOURCE text ──
setState([user('Hi'),settledAnswer(ANSWER,null)],{streamId:'st1',lastAssistantText:ANSWER},'st1');
assert.strictEqual(_settledTranscriptOwnsLiveTurn('s1',turnWith(seg(ANSWER))),true,
  'streamed markdown identical to the persisted answer proves ownership');
setState([user('Hi'),settledAnswer(ANSWER,null)],
  {streamId:'st1',lastAssistantText:'Hey! What can I help'},'st1');
assert.strictEqual(_settledTranscriptOwnsLiveTurn('s1',turnWith(seg('Hey! What can I help'))),false,
  'a still-growing stream must not be dropped against a longer settled answer');
setState([user('Hi'),settledAnswer(ANSWER,null)],{streamId:'st1'},'st1');
assert.strictEqual(_settledTranscriptOwnsLiveTurn('s1',turnWith(seg(ANSWER))),false,
  'no identity and no streamed source text → fail closed');
console.log('OK');
"""
    run_node(script)


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_append_decision_renders_the_answer_exactly_once():
    """Observable outcome of the two append branches, driven by the real
    predicate: one copy for a settled leftover, and the live turn is still
    attached in every case where the turn may still be running."""
    script = DOM_STUB + js(
        "msgContent",
        "_settledAssistantStreamId",
        "_messageHasLiveAssistantProjection",
        "_liveTurnCarriesUnsettledContent",
        "_settledTranscriptOwnsLiveTurn",
    ) + """
// Mirrors the two call sites: the branch appends unless the settled transcript
// provably owns the node (static/ui.js renderMessages re-attach and
// restoreLiveTurnHtmlForSession).
function visibleAnswers(messages,inflight,activeStreamId,turn){
  setState(messages,inflight,activeStreamId);
  const settledCopies=messages.filter(m=>m.role==='assistant'&&!m._live).length;
  const appended=!_settledTranscriptOwnsLiveTurn('s1',turn)?1:0;
  return {settledCopies,appended,total:settledCopies+appended};
}
const ANSWER='Hey! What can I help you with today?';
// #2051: exactly one copy of the answer.
assert.deepStrictEqual(
  visibleAnswers([user('Hi'),settledAnswer(ANSWER,'st1')],{streamId:'st1'},'st1',
    turnWith(seg(ANSWER),emptyWorklogShell())),
  {settledCopies:1,appended:0,total:1},
  'the settled answer must render exactly once');
// A live turn is still attached while the turn is running (#3877).
assert.deepStrictEqual(
  visibleAnswers([user('Hi'),settledAnswer(ANSWER,'st1'),user('Hi again')],
    {streamId:'st2'},'st2',turnWith(seg(ANSWER))),
  {settledCopies:1,appended:1,total:2},
  'a running turn must stay on screen even when it repeats an earlier answer');
// A leftover carrying unpersisted structure is kept whole (#3714).
assert.deepStrictEqual(
  visibleAnswers([user('Hi'),settledAnswer(ANSWER,'st1')],{streamId:'st1'},'st1',
    turnWith(toolCard(),seg(ANSWER))).appended,1,
  'never discard live-only blocks the rebuild did not produce');
console.log('OK');
"""
    run_node(script)


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_settled_scene_drops_the_prose_row_that_is_the_final_answer():
    script = DOM_STUB + js(
        "_anchorSceneIsSettledSuccessfulCompression",
        "_anchorSceneProseMatchesFinalAnswer",
        "_anchorSceneProseDuplicatesFinalAnswer",
        "_anchorSceneRowsForRendering",
    ) + """
function _anchorSceneToolRowLogicalKey(row){ return (row&&row.row_id)||''; }
function _anchorSceneMergeToolRows(_prev,row){ return row; }
const FINAL='Hey! What can I help you with today?';
const scene={
  final_answer:FINAL,
  activity_rows:[
    {role:'thinking',row_id:'t1',text:'thinking out loud'},
    {role:'prose',row_id:'p1',text:'Let me check the config first.'},
    // the reported shape: the answer minus its last streamed token (35 vs 36)
    {role:'prose',row_id:'p2',text:'Hey! What can I help you with today'},
    {role:'terminal',row_id:'d1',source_event_type:'done'},
  ],
};
const texts=(opts)=>_anchorSceneRowsForRendering(scene,opts).map(r=>r.text);
assert.deepStrictEqual(texts({settled:true}),['thinking out loud','Let me check the config first.'],
  'the settled scene must not rebuild the final answer as a second segment (#2051)');
assert.deepStrictEqual(texts({settled:false}),
  ['thinking out loud','Let me check the config first.','Hey! What can I help you with today'],
  'LIVE rendering must keep the prose row — it IS the visible answer while streaming');
// Exact repeats and multi-block answers are dropped too; source text is compared,
// so the markdown pipeline never matters here.
const code={final_answer:'Here you go:\\n\\n```python\\nprint(1)\\n```',activity_rows:[
  {role:'prose',row_id:'p1',text:'Here you go:\\n\\n```python\\nprint(1)\\n```'},
  {role:'prose',row_id:'p2',text:'Working on it.'}]};
assert.deepStrictEqual(_anchorSceneRowsForRendering(code,{settled:true}).map(r=>r.text),
  ['Working on it.'],'an exact fenced-code duplicate must be dropped, intermediates kept');
// Codex #4568: a SHORT distinct intermediate that merely prefixes a LONG answer
// must survive — the >=0.9 length ratio, not the removed absolute floor, is what
// protects it.
const long_={final_answer:'Hey! What can I help you with today? I can read files, run commands and edit code.',
  activity_rows:[{role:'prose',row_id:'p1',text:'Hey! What can'}]};
assert.deepStrictEqual(_anchorSceneRowsForRendering(long_,{settled:true}).map(r=>r.text),
  ['Hey! What can'],'a distinct short intermediate must not be swallowed by a long answer');
// The pinned per-row matcher is untouched: its absolute floor still rejects the
// short near-miss that the new helper accepts.
assert.strictEqual(_anchorSceneProseMatchesFinalAnswer('Hey! What can I help you with today',FINAL),false,
  '_anchorSceneProseMatchesFinalAnswer must keep its >=80 char floor');
assert.strictEqual(_anchorSceneProseDuplicatesFinalAnswer('Hey! What can I help you with today',FINAL),true,
  'the new helper must catch the short near-miss');
assert.strictEqual(_anchorSceneProseDuplicatesFinalAnswer('Hey! What can',FINAL),false,
  'the new helper must keep the >=0.9 ratio guard');
console.log('OK');
"""
    run_node(script)


def test_both_append_branches_are_gated_by_the_ownership_check():
    """Wiring: the predicate only helps if it guards the two branches that ADD a
    turn (the replace branches cannot duplicate and must stay unguarded)."""
    restore = UI_JS[UI_JS.index("function restoreLiveTurnHtmlForSession"):]
    restore = restore[:restore.index("\nfunction markInflight")]
    assert "if(existing) existing.replaceWith(restored);" in restore
    assert "_settledTranscriptOwnsLiveTurn(sid, restored)" in restore, (
        "the restore append branch must be gated by the ownership check"
    )
    assert "inflight.liveTurnHtml=null;" in restore, (
        "a refused snapshot must be released, or it is re-refused on every restore"
    )
    reattach = UI_JS[UI_JS.index("    const _rebuilt=document.getElementById('liveAssistantTurn');"):]
    reattach = reattach[:reattach.index("  // Only force-scroll when not actively streaming")]
    assert "_settledTranscriptOwnsLiveTurn(sid,_preservedLiveTurn)" in reattach, (
        "the renderMessages append branch must be gated by the ownership check"
    )
    assert reattach.count("_settledTranscriptOwnsLiveTurn(") == 1, (
        "the segment-swap and whole-turn replace branches must stay unguarded — "
        "they replace a node instead of adding one and cannot duplicate"
    )
