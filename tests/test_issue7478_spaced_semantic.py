"""Real semantic drain with virtual time: spacing must not multiply scan work."""
import pytest
from tests.test_issue6391_live_scene_paint import run_js
from tests.test_issue3455_think_block_extraction import _extract_block, MESSAGES_JS


def helpers():
    text = _extract_block(MESSAGES_JS, 'const _thinkPairs=') + ';\n'
    for name in ['_thinkingFenceMarkerAt', '_nextThinkingOpener', '_textTailIsPartialOpener', '_lineIsIndentedCode', '_mergeInlineThinkingReasoning', '_extractInlineThinkingFromContent']:
        text += _extract_block(MESSAGES_JS, 'function ' + name + '(') + '\n'
    if 'function _createIncrementalSemanticState(' in MESSAGES_JS:
        text += _extract_block(MESSAGES_JS, 'function _createIncrementalSemanticState(') + '\n'
    return text


def runtime():
    if 'function _createIncrementalSemanticState(' not in MESSAGES_JS:
        return ''
    return r"""
let _semanticState=_createIncrementalSemanticState(),_semanticCursor=0,_semanticSegmentStart=0,_semanticFallback=null,_semanticFallbackCursor=-1,_semanticRaw='';
const _semanticMetrics={fullParses:0,fullBytes:0,resyncReasons:{}};
function _scheduleRender(){}
for(const name of ['_consumeSemanticProse','_resyncSemanticProse','_semanticSnapshot'])eval(extract(messageSource,name));
"""


@pytest.mark.parametrize("mode", ["transparent_stream", "compact_worklog"])
@pytest.mark.parametrize("spacing", ["spaced", "burst", "alternating"])
def test_spaced_tokens_bound_full_history_scans(mode, spacing):
    run_js(helpers() + runtime() + r"""
let assistantText='',liveReasoningText='',segmentStart=0,_anchorPaintDisposed=false;
let _semanticProseTimer=null,_semanticProseDirty=false;
let now=0,id=0,timers=new Map(),scans=0,bytes=0;
const setTimeout=(fn,ms)=>{timers.set(++id,{fn,at:now+ms});return id;};
const clearTimeout=id=>timers.delete(id);
function advance(ms){now+=ms;for(const [id,t] of [...timers]) if(t.at<=now){timers.delete(id);t.fn();}}
const assistantRow={},activeSid='s',INFLIGHT={s:{messages:[]}},_throttledPersist=()=>{};
let reasoningText='';
eval(extract(messageSource,'_splitThinkFromContent'));
eval(extract(messageSource,'syncInflightAssistantMessage'));
const window={_toolViewMode:MODE};let tokenCount=0;
const rows=[];let row=null;
const _upsertAnchorProcessProse=text=>{if(!row){row={kind:'prose'};rows.push(row);}row.text=text;};
for(const name of ['_stripXmlToolCalls','_parseStreamState','_parseCurrentSegmentDisplayText','_drainSemanticProse','_scheduleSemanticProse']) eval(extract(messageSource,name));
const fullParse=_parseStreamState;
_parseStreamState=()=>{scans++;bytes+=assistantText.length;return fullParse();};
function token(text){assistantText+=text;tokenCount++;_scheduleSemanticProse(text);advance(SPACING==='spaced'?40:SPACING==='burst'?0:tokenCount%2?40:0);}
for(let i=0;i<2000;i++)token('x');
token('<thi');token('nk>secret');token('</thi');token('nk>before');
token('<fun');token('ction_calls><invoke>hidden tool</invoke>');token('</function_');token('calls>');
token('<｜DS');token('ML｜function_calls>hidden');token('</｜DSML｜function_');token('calls>');
_drainSemanticProse();rows.push({kind:'tool',text:'work'});segmentStart=assistantText.length;if(typeof _semanticSegmentStart!=='undefined')_semanticSegmentStart=rows[0].text.length;row=null;
for(let i=0;i<2000;i++)token('y');
token('after');_drainSemanticProse(true);
assert.ok(scans<=4,`full-history calls=${scans}, bytes=${bytes}`);
let _anchorPaintGeneration=0,finished=0;
const cancelAnimationFrame=()=>{};
eval(extract(messageSource,'_completeOwnedTerminal'));
_pendingTerminalFinish={generation:0,finish(){assert.equal(INFLIGHT.s.messages[0].content,'x'.repeat(2000)+'before'+'y'.repeat(2000)+'after');assert.equal(INFLIGHT.s.messages[0].reasoning,'secret');finished++;}};
_completeOwnedTerminal();assert.equal(finished,1);
assert.equal(rows[0].text,'x'.repeat(2000)+'before');
assert.equal(rows[1].kind,'tool');assert.equal(rows[2].text,'y'.repeat(2000)+'after');
assert.ok(scans<=4,`full-history calls=${scans}, bytes=${bytes}`);
assert.ok(bytes<=assistantText.length*4,`full-history bytes=${bytes}`);
if(typeof _semanticState!=='undefined'){
assert.equal(_semanticState.stats.incrementalBytes,assistantText.length);
assert.ok(_semanticState.stats.steps<assistantText.length*10);
if(process.env.SEMANTIC_EVIDENCE)fs.writeFileSync(process.env.SEMANTIC_EVIDENCE+'/'+MODE+'-'+SPACING+'.json',JSON.stringify({tokenCount,fullParses:scans,fullBytes:bytes,incrementalBytes:_semanticState.stats.incrementalBytes,steps:_semanticState.stats.steps,finished,rows}));
}
""".replace('MODE', repr(mode)).replace('SPACING', repr(spacing)))


def test_long_prose_coalesces_publication_without_delaying_semantics_or_boundaries():
    run_js(helpers() + runtime() + r"""
let assistantText='',liveReasoningText='',segmentStart=0,_anchorPaintDisposed=false;
let _semanticProseTimer=null,_semanticProseDirty=false;
let now=0,id=0,timers=new Map(),published=0,renderChecks=0;
const setTimeout=(fn,ms)=>{timers.set(++id,{fn,at:now+ms});return id;};
const clearTimeout=id=>timers.delete(id);
function advance(ms){now+=ms;for(const [id,t] of [...timers])if(t.at<=now){timers.delete(id);t.fn();}}
const assistantRow={},syncInflightAssistantMessage=()=>{published++;},_upsertAnchorProcessProse=()=>{};
let _anchorPaintGeneration=0,_renderPending=false,_streamFinalized=false;
let _cachedParsed=null,_cachedParsedText='',_cachedParsedReasoning='';
const _isActiveSession=()=>{renderChecks++;return false;};
for(const f of ['_stripXmlToolCalls','_parseStreamState','_drainSemanticProse','_scheduleSemanticProse','_scheduleRender'])eval(extract(messageSource,f));
assistantText='x'.repeat(20000);_scheduleSemanticProse(assistantText);
assert.equal(_semanticSnapshot().content,assistantText,'receipt owns semantic state immediately');
_scheduleRender();assert.equal(renderChecks,0,'do not race receipt publication with a second paint producer');
advance(16);assert.equal(published,0,'publication is coalesced, not semantic parsing');
assistantText+='<thi';_scheduleSemanticProse('<thi');
advance(16);assert.equal(published,1,'publication is non-resetting and bounded to 32ms even for long prose');
assert.equal(_semanticSnapshot().content,'x'.repeat(20000));
assistantText+='nk>secret</think>tail';_scheduleSemanticProse('nk>secret</think>tail');
assert.equal(_semanticSnapshot().reasoning,'secret');
_drainSemanticProse(true);assert.equal(published,2,'boundary drains synchronously');
assert.equal(_semanticSnapshot().content,'x'.repeat(20000)+'tail');
assert.equal(timers.size,0);assert.equal(_semanticMetrics.fullParses,0);
assert.equal(_semanticState.stats.incrementalBytes,assistantText.length);
""")


def test_reasoning_snapshot_caches_do_not_rescan_on_prose_ticks():
    run_js(helpers() + r"""
const original=_mergeInlineThinkingReasoning;let scanned=0;
_mergeInlineThinkingReasoning=(base,parts)=>{scanned+=base.length+parts.reduce((n,p)=>n+p.length,0);return original(base,parts);};
const p=_createIncrementalSemanticState(),channel='r'.repeat(10000);
p.write('<think>inline</think>');
for(let i=0;i<10000;i++){p.write('x');p.snapshot(channel);p.snapshot('');}
assert.ok(scanned<50000,`snapshot scans=${scanned}`);
assert.ok(p.stats.mergeBytes<50000,`merge work=${p.stats.mergeBytes}`);
""")


def test_reconnect_retains_split_thinking_and_bounds_uncertain_rescans():
    run_js(helpers() + runtime() + r"""
let assistantText='<thi',liveReasoningText='',segmentStart=0,_anchorPaintDisposed=false;
let _semanticProseTimer=null,_semanticProseDirty=false;
const assistantRow={},syncInflightAssistantMessage=()=>{},_upsertAnchorProcessProse=()=>{};
const setTimeout=()=>1,clearTimeout=()=>{};
for(const f of ['_stripXmlToolCalls','_parseStreamState','_drainSemanticProse'])eval(extract(messageSource,f));
_resyncSemanticProse('reconnect');
const delta='nk>secret</think>answer';assistantText+=delta;_consumeSemanticProse(delta);
assert.equal(_semanticSnapshot().content,'answer');assert.equal(_semanticSnapshot().reasoning,'secret');
const malformed='<｜'+ ' '.repeat(300);assistantText+=malformed;_consumeSemanticProse(malformed);
for(let i=0;i<10000;i++){assistantText+='x';_consumeSemanticProse('x');_semanticProseDirty=true;_drainSemanticProse();}
assert.equal(_semanticMetrics.fullParses,1,'uncertainty must not turn timer ticks into rescans');
_drainSemanticProse(true);_drainSemanticProse(true);
assert.equal(_semanticMetrics.fullParses,2,'one rescan per changed uncertain boundary');
assert.equal(_semanticSnapshot().content,_extractInlineThinkingFromContent(_stripXmlToolCalls(assistantText),'',{streaming:true}).content);
""")


def test_rewind_and_terminal_partial_fallback():
    run_js(helpers() + runtime() + r"""
let assistantText='',liveReasoningText='',segmentStart=0,_anchorPaintDisposed=false;
let _semanticProseTimer=null,_semanticProseDirty=false;
const assistantRow={},syncInflightAssistantMessage=()=>{},_upsertAnchorProcessProse=()=>{};
const setTimeout=()=>1,clearTimeout=()=>{};
for(const f of ['_stripXmlToolCalls','_parseStreamState','_drainSemanticProse'])eval(extract(messageSource,f));
assistantText='first';_consumeSemanticProse('first');
assistantText='other';_consumeSemanticProse();
assert.equal(_semanticSnapshot().content,'other','same-length discontinuity must resync');
assistantText='replacement growth';_consumeSemanticProse();
assert.equal(_semanticSnapshot().content,'replacement growth','untrusted growth must resync');
assistantText='short';_consumeSemanticProse();assert.equal(_semanticSnapshot().content,'short');
assistantText+=' <func';_consumeSemanticProse(' <func');_semanticProseDirty=true;
_drainSemanticProse(true);
assert.equal(_semanticSnapshot().content,'short <func','terminal fallback must not lose literal partial delimiter');
assert.ok(_semanticMetrics.fullParses<=4);
""")


@pytest.mark.parametrize('chunk_size', [1, 2, 7, 65536])
@pytest.mark.parametrize('raw', [
    'before<｜DSML<function_calls>hidden</function_calls>after',
    '\n\t<think>secret</think><function_calls>hidden</function_calls>answer',
    '\r\n  \n    <think>secret</think><｜DSML｜function_calls>hidden</｜DSML｜function_calls>answer',
    '\n\t<think>secret</think>mention DSML literally',
    '    <think>literal</think>\n<function_calls>hidden</function_calls><think>second</think>answer',
])
def test_ambiguous_xml_fails_closed_until_bounded_boundary_resync(raw, chunk_size):
    run_js(helpers() + runtime() + r"""
let assistantText='',liveReasoningText='',segmentStart=0,_anchorPaintDisposed=false;
let _semanticProseTimer=null,_semanticProseDirty=false;
const assistantRow={},syncInflightAssistantMessage=()=>{},_upsertAnchorProcessProse=()=>{};
const setTimeout=()=>1,clearTimeout=()=>{};
for(const f of ['_stripXmlToolCalls','_parseStreamState','_drainSemanticProse'])eval(extract(messageSource,f));
for(let i=0;i<RAW.length;i+=CHUNK_SIZE){const delta=RAW.slice(i,i+CHUNK_SIZE);assistantText+=delta;_consumeSemanticProse(delta);_semanticProseDirty=true;_drainSemanticProse();}
assert.equal(_semanticState.uncertain(),true,'uncertain XML must request a safe resync');
assert.equal(_semanticMetrics.fullParses,0,'token/timer path never rescans');
_drainSemanticProse(true);_drainSemanticProse(true);
const expected=_extractInlineThinkingFromContent(_stripXmlToolCalls(assistantText),'',{streaming:true});
assert.equal(_semanticSnapshot().content,expected.content);
assert.equal(_semanticSnapshot().reasoning,expected.reasoning);
assert.equal(_semanticMetrics.fullParses,1,'duplicate drains reuse one rescan');
""".replace('RAW', __import__('json').dumps(raw)).replace('CHUNK_SIZE', str(chunk_size)))


def test_pending_boundary_preserves_distinct_durable_and_live_reasoning():
    run_js(helpers() + runtime() + r"""
let assistantText='answer<thi',liveReasoningText='',reasoningText='earlier reasoning',segmentStart=0,_anchorPaintDisposed=false;
let _semanticProseTimer=null,_semanticProseDirty=false;
const assistantRow={},syncInflightAssistantMessage=()=>{},_upsertAnchorProcessProse=()=>{};
const setTimeout=()=>1,clearTimeout=()=>{};
for(const f of ['_stripXmlToolCalls','_parseStreamState','_drainSemanticProse'])eval(extract(messageSource,f));
_consumeSemanticProse();_drainSemanticProse(true);
assert.equal(_semanticSnapshot(reasoningText).reasoning,reasoningText,'fallback must preserve durable channel reasoning');
assert.equal(_semanticSnapshot().reasoning,'','live reset stays distinct from durable state');
for(let i=0;i<1000;i++){_semanticSnapshot(reasoningText);_semanticSnapshot();}
assert.ok(_semanticMetrics.fullParses<=2);
""")


def test_pending_fence_is_not_a_reasoning_transition():
    run_js(helpers() + r"""
for(const text of ['before\n`','before\n``','before\n~','before\n~~']){
 const state=_createIncrementalSemanticState();state.write(text);
 assert.equal(state.hasPending(),true);
 assert.equal(state.snapshot().inThinking,false,'a partial code fence is not thinking');
}
""")


def test_xml_prefix_trim_does_not_drop_post_tool_prose():
    run_js(helpers() + runtime() + r"""
let assistantText='  before',liveReasoningText='',segmentStart=0,_anchorPaintDisposed=false;
let _semanticProseTimer=null,_semanticProseDirty=false;
const assistantRow={},syncInflightAssistantMessage=()=>{},_upsertAnchorProcessProse=()=>{};
const setTimeout=()=>1,clearTimeout=()=>{};
for(const f of ['_stripXmlToolCalls','_parseStreamState','_parseCurrentSegmentDisplayText','_drainSemanticProse'])eval(extract(messageSource,f));
_consumeSemanticProse();segmentStart=assistantText.length;_semanticSegmentStart=_semanticSnapshot().content.length;
for(const ch of '<function_calls>hidden</function_calls>after'){assistantText+=ch;_consumeSemanticProse(ch);}
assert.equal(_parseCurrentSegmentDisplayText(),'after','global XML prefix trim must adjust the post-tool semantic offset');
""")


def test_split_tool_marker_survives_nonterminal_semantic_boundary():
    run_js(helpers() + runtime() + r"""
let assistantText='before<fun',liveReasoningText='',segmentStart=0,_anchorPaintDisposed=false;
let _semanticProseTimer=null,_semanticProseDirty=false;
const assistantRow={},syncInflightAssistantMessage=()=>{},_upsertAnchorProcessProse=()=>{};
const setTimeout=()=>1,clearTimeout=()=>{};
for(const f of ['_stripXmlToolCalls','_parseStreamState','_parseCurrentSegmentDisplayText','_drainSemanticProse'])eval(extract(messageSource,f));
_consumeSemanticProse();_drainSemanticProse('semantic');
assert.equal(_semanticMetrics.fullParses,0,'a trusted split delimiter remains parser-owned at nonterminal boundaries');
segmentStart=assistantText.length;_semanticSegmentStart=_semanticSnapshot().content.length;
for(const ch of 'ction_calls>hidden</function_calls>after'){assistantText+=ch;_consumeSemanticProse(ch);}
assert.equal(_parseCurrentSegmentDisplayText(),'after');
assert.equal(_semanticSnapshot().content,'beforeafter');
_drainSemanticProse(true);assert.equal(_semanticMetrics.fullParses,0);
_resyncSemanticProse('reconnect');
assert.equal(_parseCurrentSegmentDisplayText(),'after','resume preserves offsets across a split tool marker');
""")


def test_growing_reasoning_does_not_hash_the_full_body_per_token():
    run_js(helpers() + r"""
let hashedUnits=0;const NativeSet=globalThis.Set;
globalThis.Set=class extends NativeSet {
 has(value){if(typeof value==='string')hashedUnits+=value.length;return super.has(value);}
};
const state=_createIncrementalSemanticState();
state.write('<think>old</think><think>');
for(let i=0;i<4000;i++){state.write('z');state.snapshot('external');}
state.write('</think>answer');const value=state.snapshot('external');
assert.equal(value.reasoning,'external\n\nold\n\n'+'z'.repeat(4000));
assert.ok(hashedUnits<=8*state.stats.incrementalBytes,JSON.stringify({hashedUnits,input:state.stats.incrementalBytes}));
""")


def test_uncertain_lookahead_stays_bounded():
    run_js(helpers() + r"""
const p=_createIncrementalSemanticState();p.write('before<｜'+ ' '.repeat(300));
assert.equal(p.uncertain(),true);
for(let i=0;i<10000;i++)p.write('x');
assert.ok(p.stats.maxPending<=257);assert.ok(p.stats.steps<600);
assert.equal(p.snapshot().content,'before');
""")


def test_incremental_split_markers_match_batch_semantics():
    run_js(helpers() + r"""
eval(extract(messageSource,'_stripXmlToolCalls'));
const cases=[
 'plain prose', '<think>reason</think>answer',
 '  before<function_calls>hidden</function_calls>after',
 '</function_calls>literal', '<｜DSML |bad>',
 '  </function_calls> literal', '  mention DSML and function_calls literally',
 ' \t<think>reason</think>answer',
 '<think>a\n\nb</think><think>b</think>end',
 'before<think>first</think>middle<think>second</think>after',
 '<think>same</think><think>same</think>visible',
 '<think>first</think>    <think>second</think>answer',
 'before<function_calls><invoke>x</invoke></function_calls>after',
 '<function_calls>hidden</function_calls>    <think>reason</think>answer',
 'before<｜DSML｜function_calls><｜DSML｜invoke>x</｜DSML｜invoke></｜DSML｜function_calls>after',
 'before< ｜ DSML | function_calls>x</ ｜ DSML | function_calls>after',
 '`<think>literal</think>` prose', '```xml\n<think>literal</think>\n```\n<think>secret</think>answer',
 '    <think>literal</think>\nnormal<think>secret</think>answer',
 '  ~~~\n<think>literal</think>\n  ~~~\nanswer',
 '<|channel>thought\nreason<channel|>answer', '<|turn|>thinking\nreason<turn|>answer',
 'before<think>  split reasoning  \n content  </think>after',
];
for(const raw of cases){
 const expected=_extractInlineThinkingFromContent(_stripXmlToolCalls(raw),'',{streaming:true});
 for(let size=1;size<=raw.length;size++){
  const p=_createIncrementalSemanticState();
  for(let i=0;i<raw.length;i+=size)p.write(raw.slice(i,i+size));
  const got=p.snapshot();
  assert.equal(got.content,expected.content,JSON.stringify({raw,size,got,expected}));
  assert.equal(got.reasoning,expected.reasoning,JSON.stringify({raw,size,got,expected}));
  assert.ok(p.stats.steps<raw.length*12);
 }
}
""")


@pytest.mark.parametrize('mode', ['transparent_stream', 'compact_worklog'])
@pytest.mark.parametrize('pending', [False, True])
def test_channel_reasoning_delta_work_is_linear(mode, pending):
    run_js(helpers() + runtime() + r"""
let assistantText=PENDING?'answer<thi':'<think>inline</think>answer';
let reasoningText='',liveReasoningText='',segmentStart=0;
let _terminalStateReached=false,_streamFinalized=false;
const S={session:null},activeSid='s',streamId='r';
const _ownsActiveStreamOrBackground=()=>true;
for(const name of ['_stripXmlToolCalls','_parseStreamState'])eval(extract(messageSource,name));
_consumeSemanticProse();if(PENDING)_resyncSemanticProse('reconnect');
const callbacks={};const source={addEventListener(name,fn){callbacks[name]=fn;}};
const a=messageSource.indexOf("    source.addEventListener('reasoning',e=>{");
const b=messageSource.indexOf("    source.addEventListener('tool',",a);
eval(messageSource.slice(a,b));
let snapshots=0;
function syncInflightAssistantMessage(){
 snapshots++;
 const suffix=PENDING?'':'\n\ninline';
 assert.equal(_semanticSnapshot(reasoningText).reasoning,reasoningText.trim()+suffix);
 assert.equal(_semanticSnapshot().reasoning,liveReasoningText.trim()+suffix);
}
for(let i=0;i<2000;i++){
 callbacks.reasoning({data:JSON.stringify({text:'r'})});
 if(i===999)liveReasoningText=''; // post-tool live reset, durable base retained
}
assert.equal(snapshots,2000);
let work=_semanticState.stats.mergeBytes;
if(_semanticFallback?.reasoningState)work+=_semanticFallback.reasoningState.stats.mergeBytes;
assert.ok(work<30000,`channel merge work ${work} for 2000 one-character deltas`);
assert.ok(_semanticMetrics.fullParses<=1);
if(process.env.SEMANTIC_EVIDENCE)fs.writeFileSync(process.env.SEMANTIC_EVIDENCE+'/channel-'+MODE+'-'+PENDING+'.json',JSON.stringify({snapshots,work,full:_semanticMetrics,primary:_semanticState.stats,recovery:_semanticFallback?.reasoningState?.stats}));
""".replace('PENDING', 'true' if pending else 'false').replace('MODE', repr(mode)))


def test_channel_reasoning_append_keeps_batch_merge_parity():
    run_js(helpers()+r"""
for(const raw of ['<think>a</think><think>b\n\nc</think>', '<think>a</think><think>b</think><think>open']){
 const state=_createIncrementalSemanticState();state.write(raw);
 let base='';
 for(const ch of '  a\n\nb\n\nc  \n\na extra\n\nopen'){
  const before=base;base+=ch;
  state.appendReasoning(ch,[[before,base]]);
  const expected=_extractInlineThinkingFromContent(raw,base,{streaming:true});
  assert.equal(state.snapshot(base).reasoning,expected.reasoning,JSON.stringify({raw,base}));
 }
}
""")


def test_interleaved_channel_and_inline_reasoning_matches_oracle():
    run_js(helpers()+r"""
const bases=['','a','b','a\n\nb','  a\n\n b \n\nc  ','\n\n','a\n\n\nb','open'];
const raws=['<think>a</think><think>b</think>answer','<think>a\n\nb</think><think>c</think>','<think>a</think><think>open','<think>a</think><think>a\n\nb</think>'];
for(const raw of raws)for(const initial of bases){
 const p=_createIncrementalSemanticState();let base=initial,rawSoFar='';p.snapshot(base);
 for(const ch of raw){
  rawSoFar+=ch;p.write(ch);
  const before=base;base+='b';p.appendReasoning('b',[[before,base]]);
  const expected=_extractInlineThinkingFromContent(rawSoFar,base,{streaming:true});
  if(!p.hasPending())assert.equal(p.snapshot(base).reasoning,expected.reasoning,JSON.stringify({rawSoFar,base}));
 }
}
""")
