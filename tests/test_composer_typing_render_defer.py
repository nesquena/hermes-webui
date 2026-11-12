"""Live streaming renders yield to active composer typing, then catch up."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="node not on PATH")


def run_case(name):
    script = r'''
const fs = require('fs');
const vm = require('vm');
const source = fs.readFileSync(process.argv[1], 'utf8');
function extract(name){
  const start=source.indexOf('function '+name+'(');
  if(start<0) throw new Error(name+' not found');
  const brace=source.indexOf('{',start);let depth=0;
  for(let i=brace;i<source.length;i++){
    if(source[i]==='{')depth++;
    else if(source[i]==='}'&&--depth===0)return source.slice(start,i+1);
  }
  throw new Error(name+' did not close');
}
const listeners={};
const composer={addEventListener(type,fn,opts){
  (listeners[type]||(listeners[type]=[])).push({fn,opts});
}};
let now=0,timers=[],nextTimer=1,renders=0,active=true;
function addTimer(fn,delay){const timer={id:nextTimer++,fn,at:now+delay};timers.push(timer);return timer.id;}
function flushDue(advance){
  const target=now+advance;let runs=0;
  for(;;){
    timers.sort((a,b)=>a.at-b.at||a.id-b.id);
    const index=timers.findIndex(t=>t.at<=target);
    if(index<0)break;
    const [timer]=timers.splice(index,1);now=Math.max(now,timer.at);timer.fn();
    if(++runs>100)throw new Error('timer runaway');
  }
  now=target;
}
const context={
  console,performance:{now:()=>now},setTimeout:addTimer,requestAnimationFrame:fn=>addTimer(fn,0),
  document:{getElementById:id=>id==='msg'?composer:null},window:{},S:{activeStreamId:'stream-1'},
  listeners,flushDue,
  // Tolerate sibling pacing changes to _scheduleRender (cost-based interval).
  LIVE_RENDER_BUSY_FACTOR:4,_lastRenderCostMs:0,
  get now(){return now;},set now(value){now=value;},
  get timers(){return timers;},set timers(value){timers=value;},
  get renders(){return renders;},set renders(value){renders=value;},
  get active(){return active;},set active(value){active=value;},
};
vm.createContext(context);
const marker='const COMPOSER_TYPING_RENDER_DEFER_MS';
const start=source.indexOf(marker);
if(start<0)throw new Error('composer typing helper not found');
vm.runInContext(source.slice(start,source.indexOf('// TTS:',start)),context);
vm.runInContext(extract('_scheduleRender'),context);
vm.runInContext(`
function typing(type='input'){
  for(const listener of listeners[type]||[])listener.fn({type});
}
var assistantText='live',liveReasoningText='',_cachedParsed=null,_cachedParsedText='',_cachedParsedReasoning='';
var _renderPending=false,_streamFinalized=false,_lastRenderMs=0,_pendingRafHandle=null;
const streamId='stream-1',segmentStart=0;
let _smdParser=null,assistantBody={innerHTML:'',classList:{remove(){}}},assistantRow={};
function _isActiveSession(){return active;}
function _flushPendingReasoningRender(){}
function _shouldUseLiveProseFade(){return false;}
function _parseStreamState(){return {displayText:assistantText};}
function _renderLiveThinking(){renders++;}
function _renderStreamingFadeMarkdown(){return true;}
function _resetStreamFadeState(){}
function _smdNewParser(){throw new Error('unexpected incremental parser');}
function _smdWrite(){}
function _stripXmlToolCalls(text){return text;}
function _syncLiveWorklogReasonsForAnchor(){}
function _upsertAnchorProcessProse(){}
function renderMd(text){return text;}
function esc(text){return text;}
function scrollIfPinned(){}
function _throttledSnapshotLiveTurn(){}
`,context);
const result=vm.runInContext(`(()=>{
  now=0;timers=[];renders=0;active=true;
  _renderPending=false;_streamFinalized=false;_lastRenderMs=-66;_composerTypingUntil=0;
  _composerTypingDeferStarted=null;
  return ${JSON.stringify('CASE')} === 'listener' ? (()=>{
    _bindComposerTypingRenderDefer();
    typing('keydown');
    return {input:listeners.input.length,keydown:listeners.keydown.length,
      passive:listeners.input[0].opts.passive&&listeners.keydown[0].opts.passive,
      deadline:_composerTypingUntil-now};
  })() : ${JSON.stringify('CASE')} === 'continuous' ? (()=>{
    typing();_scheduleRender();flushDue(0);
    for(let i=0;i<4;i++){flushDue(200);typing();}
    const beforeCap=renders;
    flushDue(200);typing();flushDue(0);
    return {renders,beforeCap,now,pending:_renderPending,timers:timers.length};
  })() : ${JSON.stringify('CASE')} === 'duplicates' ? (()=>{
    typing();_scheduleRender();flushDue(0);
    for(let i=0;i<5;i++)_scheduleRender();
    const queued=timers.length;flushDue(250);flushDue(1000);
    return {renders,queued,pending:_renderPending,timers:timers.length};
  })() : ${JSON.stringify('CASE')} === 'finalized' ? (()=>{
    typing();_scheduleRender();flushDue(0);_streamFinalized=true;flushDue(250);
    return {renders,pending:_renderPending};
  })() : ${JSON.stringify('CASE')} === 'typing' ? (()=>{
    typing();_scheduleRender();flushDue(0);const deferred=renders;
    flushDue(249);const beforeWindow=renders;flushDue(1);
    return {deferred,beforeWindow,renders,pending:_renderPending};
  })() : (()=>{
    _scheduleRender();flushDue(0);
    return {renders,pending:_renderPending};
  })();
})()`,context);
process.stdout.write(JSON.stringify(result));
'''.replace("${JSON.stringify('CASE')}", json.dumps(name))
    result = subprocess.run(
        [NODE, "-e", script, str(ROOT / "static" / "messages.js")],
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_no_typing_renders_immediately():
    assert run_case("immediate") == {"renders": 1, "pending": False}


def test_typing_defers_then_renders_after_window():
    assert run_case("typing") == {
        "deferred": 0,
        "beforeWindow": 0,
        "renders": 1,
        "pending": False,
    }


def test_continuous_typing_cannot_defer_past_one_second():
    assert run_case("continuous") == {
        "renders": 1,
        "beforeCap": 0,
        "now": 1000,
        "pending": False,
        "timers": 0,
    }


def test_deferral_coalesces_into_one_render():
    assert run_case("duplicates") == {
        "renders": 1,
        "queued": 1,
        "pending": False,
        "timers": 0,
    }


def test_finalized_stream_does_not_render_after_deferral():
    assert run_case("finalized") == {"renders": 0, "pending": False}


def test_composer_listeners_register_once_and_passively():
    assert run_case("listener") == {
        "input": 1,
        "keydown": 1,
        "passive": True,
        "deadline": 250,
    }
