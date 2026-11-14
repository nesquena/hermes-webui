"""Stage 1: source-executing semantic parity, independent of paint timing."""
import subprocess
from pathlib import Path

from tests.test_issue3455_think_block_extraction import _extract_block

ROOT = Path(__file__).resolve().parents[1]


def runtime():
    source = (ROOT / 'static/messages.js').read_text()
    names = ['_thinkingFenceMarkerAt', '_nextThinkingOpener', '_textTailIsPartialOpener',
             '_lineIsIndentedCode', '_mergeInlineThinkingReasoning',
             '_extractInlineThinkingFromContent', '_createIncrementalSemanticState',
             '_consumeSemanticProse', '_resyncSemanticProse', '_semanticSnapshot',
             '_parseStreamStateBatch', '_flushSemanticBoundary', '_stripXmlToolCalls',
             '_parseStreamState', '_parseCurrentSegmentDisplayText',
             '_splitThinkFromContent', 'syncInflightAssistantMessage']
    text = _extract_block(source, 'const _thinkPairs=') + ';\n'
    for name in names:
        text += _extract_block(source, 'function ' + name + '(') + '\n'
    for name in ['_durableSemanticSnapshot']:
        if 'function ' + name + '(' in source:
            text += _extract_block(source, 'function ' + name + '(') + '\n'
    text += r'''
const assert=require('node:assert/strict');
let assistantText='',reasoningText='',liveReasoningText='',segmentStart=0;
const activeSid='s',INFLIGHT={s:{messages:[]}},_throttledPersist=()=>{};
let _semanticState=_createIncrementalSemanticState();
let _durableSemanticState=_createIncrementalSemanticState(undefined,true);
let _segmentSemanticState=null;
let _semanticCursor=0,_semanticSegmentStart=0,_semanticRaw='';
let _semanticFallback=null,_semanticFallbackCursor=-1;
const _semanticMetrics={fullParses:0,fullBytes:0,resyncReasons:{}};
function token(text){assistantText+=text;_consumeSemanticProse(text);syncInflightAssistantMessage();}
'''
    return text


def run_js(body):
    result = subprocess.run(['node'], input=runtime()+body, text=True,
                            capture_output=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr


def test_xml_pass_order_preserves_cross_block_delimiters():
    run_js(r'''
const cases=[
 '<func<function_calls>hidden</function_calls>tion_calls>tail',
 '<func<｜DSML |tion_calls>tail',
];
for(const raw of cases){
 const state=_createIncrementalSemanticState();
 for(const ch of raw)state.write(ch);
 const expected=_extractInlineThinkingFromContent(_stripXmlToolCalls(raw),'',{streaming:true});
 assert.equal(state.snapshot().content,expected.content,raw);
}
''')


def test_adjacent_blocks_and_one_time_trim_have_linear_work():
    run_js(r'''
const rows=[];
for(const size of [10000,50000,100000]){
 const block='<function_calls>x</function_calls>';
 for(const raw of ['<func'+block.repeat(Math.floor(size/block.length))+'tion_calls>tail',
                  '    <think>r</think>'+'x'.repeat(size)+'DSML']){
  const state=_createIncrementalSemanticState();
  for(let i=0;i<raw.length;i+=10){state.write(raw.slice(i,i+10));state.snapshot();}
  const want=_extractInlineThinkingFromContent(_stripXmlToolCalls(raw),'',{streaming:true});
  assert.equal(state.snapshot().content,want.content);assert.equal(state.snapshot().reasoning,want.reasoning);
  assert.ok(state.stats.steps<raw.length*10,JSON.stringify(state.stats));
  const before=state.stats.steps;
  for(let i=0;i<1000;i++){state.write('x');state.snapshot();}
  assert.ok(state.stats.steps-before<=4000,JSON.stringify({before,after:state.stats.steps}));
  rows.push({size,input:raw.length,stats:state.stats});
 }
}
console.log(JSON.stringify({adversarial:rows}));
''')


def test_long_xml_whitespace_snapshot_does_not_reslice_growing_prefix():
    run_js(r'''
const original=String.prototype.slice;let prefixWork=0;
String.prototype.slice=function(start,end){
 if(start===0&&end===2&&this.length>256)prefixWork+=this.length;
 return original.call(this,start,end);
};
const state=_createIncrementalSemanticState();state.write('<think>r<｜');
for(let i=0;i<5000;i++){state.write(' ');state.snapshot();}
String.prototype.slice=original;
assert.ok(prefixWork<20000,`growing pending-prefix slice work: ${prefixWork}`);
''')


def test_ordered_xml_spaced_work_is_linear():
    run_js(r'''
const oracle=_extractInlineThinkingFromContent;
let fullCalls=0,fullUnits=0;
_extractInlineThinkingFromContent=(text,...args)=>{fullCalls++;fullUnits+=text.length;return oracle(text,...args);};
const rows=[];
for(const shape of ['plain','thinking','xml','dsml','composition','malformed','whitespace'])for(const size of [10000,50000,100000]){
 const wrappers={
  plain:['',''], thinking:['<think>','</think>'],
  xml:['<function_calls>','</function_calls>'],
  dsml:['< ｜ DSML | function_calls>','</｜DSML｜function_calls>'],
  composition:['<func<function_calls>','</function_calls>tion_calls>tail'],
  malformed:['<func<｜DSML |','tion_calls>tail'],
  whitespace:['<think>r<｜','DSML |bad>z</think>answer'],
 };
 const [before,after]=wrappers[shape];
 const raw=before+(shape==='whitespace'?' ': 'x').repeat(size-before.length-after.length)+after;
 assistantText='';reasoningText='';liveReasoningText='';segmentStart=0;
 _semanticState=_createIncrementalSemanticState();_durableSemanticState=_createIncrementalSemanticState(undefined,true);
 _semanticCursor=0;_semanticRaw='';_semanticFallback=null;_semanticFallbackCursor=-1;_segmentSemanticState=null;
 INFLIGHT.s.messages=[];fullCalls=0;fullUnits=0;
 const started=performance.now();
 for(let i=0;i<raw.length;i+=10){token(raw.slice(i,i+10));_parseStreamState();}
 const stats=_semanticState.stats,savedStats=_durableSemanticState.stats;
 rows.push({shape,size,incrementalInputUnits:stats.incrementalBytes+savedStats.incrementalBytes,
  parserSteps:stats.steps+savedStats.steps,mergeUnits:stats.mergeBytes+savedStats.mergeBytes,
  fullCalls,fullHistoryUnits:fullUnits,ms:performance.now()-started});
 assert.equal(fullCalls,0,JSON.stringify(rows.at(-1)));
 assert.ok(stats.steps+savedStats.steps<=size*16,JSON.stringify(rows.at(-1)));
 const expected=oracle(_stripXmlToolCalls(raw),'',{streaming:true}),persist=oracle(raw,'',{streaming:false});
 assert.equal(_parseStreamState().content,expected.content,shape);
 assert.equal(_parseStreamState().reasoning,expected.reasoning,shape);
 assert.equal(INFLIGHT.s.messages[0].content,persist.content,shape);
 assert.equal(INFLIGHT.s.messages[0].reasoning||'',persist.reasoning,shape);
}
console.log(JSON.stringify({performance:rows}));
''')


def test_ordered_xml_channel_updates_survive_provisional_rollback():
    run_js(r'''
token('<think>prior</think><think>current');
for(const delta of ['<func','<function_calls>','hidden','</function_calls>','tion_calls>tail']){
 const old=reasoningText;reasoningText+='channel';liveReasoningText=reasoningText;
 _semanticState.appendReasoning('channel',[[old,reasoningText]]);
 _durableSemanticState.appendReasoning('channel',[[old,reasoningText]]);
 token(delta);
 const want=_extractInlineThinkingFromContent(_stripXmlToolCalls(assistantText),reasoningText,{streaming:true});
 assert.equal(_parseStreamState().content,want.content);
 assert.equal(_parseStreamState().reasoning,want.reasoning);
 const saved=_splitThinkFromContent(assistantText,reasoningText);
 assert.equal(INFLIGHT.s.messages[0].reasoning,saved.reasoning);
}
''')


def test_ordered_xml_seeded_composition_differential():
    run_js(r'''
const atoms=['x',' ', '\n','`','```\n','    ','<think>a</think>','<think>open',
 '<function_calls>','</function_calls>','<｜DSML｜function_calls>','</ ｜ DSML | function_calls>',
 '<｜DSML |','<func','tion_calls>','<thi','nk>','</think>','<','｜','DSML','<|turn|>thinking\n'];
let seed=7478,checkpoints=0;
const next=()=>{seed=(Math.imul(seed,1664525)+1013904223)>>>0;return seed;};
for(let example=0;example<600;example++){
 let raw='';for(let i=0,n=2+next()%10;i<n;i++)raw+=atoms[next()%atoms.length];
 for(const size of [1,7,raw.length]){
  const live=_createIncrementalSemanticState(),saved=_createIncrementalSemanticState(undefined,true);
  let prefix='';
  for(let i=0;i<raw.length;i+=size){
   const delta=raw.slice(i,i+size);prefix+=delta;live.write(delta);saved.write(delta);
   for(const [state,input,streaming] of [[live,_stripXmlToolCalls(prefix),true],[saved,prefix,false]]){
    const want=_extractInlineThinkingFromContent(input,'channel',{streaming}),got=state.snapshot('channel');
    for(const key of ['content','reasoning','inThinking'])assert.equal(got[key],want[key],JSON.stringify({example,raw,prefix,size,streaming,key,got,want}));
   }
   checkpoints++;
  }
 }
}
console.log(JSON.stringify({examples:600,checkpoints}));
''')


def test_ordered_xml_differential_matrix():
    run_js(r'''
const plain='<function_calls>hidden</function_calls>';
const dsml='< ｜ DSML | function_calls>hidden</｜DSML｜function_calls>';
const cases=[
 '<func'+plain+'tion_calls>tail',
 '<func<｜DSML |tion_calls>tail',
 '<func'+plain+plain+'tion_calls>tail',
 '<func'+dsml+plain+'tion_calls>tail',
 '<thi'+plain+'nk>secret</think>answer',
 '<think>reason</thi'+plain+'nk>answer',
 '<thi<｜DSML |nk>secret</think>answer',
 '<function_calls>outer<function_calls>inner</function_calls>tail</function_calls>',
 'before'+plain+dsml+'after',
 '    <think>literal</think>'+dsml+'after',
 '<func'+plain+'tion_calls', '<func'+plain+'tion_callsX',
 '<function_calls>unterminated</function_call',
 '<｜ DSML | function_calls', '<｜ DSML | function_callsX',
 '<'+ ' '.repeat(300)+'｜'+ '\n '.repeat(100)+'DSML |function_calls>x</function_calls>tail',
 '<think>r<'+ ' '.repeat(300)+'｜DSML |bad>z</think>answer',
 '```\n<func'+plain+'tion_calls>literal',
 'prefix<｜DSML |<｜DSML |<think>hidden</think>answer',
 '<function_calls>x</function_calls><think>unfinished',
 'answer<think>literal unfinished'+plain,
 '</function_calls>literal', '<｜DSML |broken>',
];
let checkpoints=0;
for(const raw of cases)for(const size of [1,2,3,7,17,raw.length]){
 const live=_createIncrementalSemanticState(),saved=_createIncrementalSemanticState(undefined,true);
 const segment=_createIncrementalSemanticState(undefined,false,true);
 let prefix='';
 for(let i=0;i<raw.length;i+=size){
  const delta=raw.slice(i,i+size);prefix+=delta;
  live.write(delta);saved.write(delta);segment.write(delta);
  const stripped=_stripXmlToolCalls(prefix);
  for(const [view,input,streaming] of [[live,stripped,true],[saved,prefix,false]]){
   const got=view.snapshot('channel'),want=_extractInlineThinkingFromContent(input,'channel',{streaming});
   for(const key of ['content','reasoning','inThinking'])assert.equal(got[key],want[key],JSON.stringify({raw,prefix,size,streaming,key,got,want}));
  }
  assert.equal(segment.snapshot().content,stripped,JSON.stringify({raw,prefix,size,segment:true}));
  checkpoints++;
 }
 assert.ok(live.stats.steps<=raw.length*16,JSON.stringify(live.stats));
}
console.log(JSON.stringify({cases:cases.length,checkpoints}));
''')


def test_ordered_xml_publication_recovery_and_boundaries():
    run_js(r'''
const cases=[
 '<func<function_calls>x</function_calls>tion_calls>tail',
 '<func<｜DSML |tion_calls>tail',
 '<thi<function_calls>x</function_calls>nk>secret</think>answer',
 'answer<think>literal unfinished',
];
for(const raw of cases)for(let cut=0;cut<=raw.length;cut++){
 assistantText='';reasoningText='channel';liveReasoningText='channel';segmentStart=0;
 _semanticState=_createIncrementalSemanticState();_durableSemanticState=_createIncrementalSemanticState(undefined,true);
 _semanticCursor=0;_semanticRaw='';_semanticFallback=null;_semanticFallbackCursor=-1;_segmentSemanticState=null;
 INFLIGHT.s.messages=[];
 token(raw.slice(0,cut));
 const check=()=>{
  const live=_extractInlineThinkingFromContent(_stripXmlToolCalls(assistantText),liveReasoningText,{streaming:true});
  const saved=_splitThinkFromContent(assistantText,reasoningText);
  assert.equal(_parseStreamState().content,live.content,JSON.stringify({raw,cut,assistantText,phase:'live'}));
  assert.equal(_parseStreamState().reasoning,live.reasoning);
  assert.equal(INFLIGHT.s.lastAssistantText,assistantText);
  assert.equal(INFLIGHT.s.messages[0].content,saved.content);
  assert.equal(INFLIGHT.s.messages[0].reasoning||'',saved.reasoning);
 };
 check();_flushSemanticBoundary();check();_flushSemanticBoundary(true);check();
 _resyncSemanticProse('recovery');syncInflightAssistantMessage();check();
 for(const ch of raw.slice(cut)){token(ch);check();}
}
''')


def test_post_tool_segment_matches_current_master_raw_boundary():
    source = (ROOT / 'static/messages.js').read_text()
    reset = _extract_block(source, 'function _resetAssistantSegment(')
    run_js(r'''
let assistantRow=null,assistantBody=null,_freshSegment=false;
const _smdEndParser=()=>{},_resetStreamFadeState=()=>{};
''' + reset + r'''
for(const [before,after] of [['before','<think>post tool</think>answer'],
 ['    <think>literal</think>','<function_calls>hidden</function_calls>after'],
 ['before<fun','ction_calls>hidden</function_calls>after']]){
 token(before);_resetAssistantSegment();
 for(const ch of after){
  token(ch);
  assert.equal(_parseCurrentSegmentDisplayText(),_stripXmlToolCalls(assistantText.slice(segmentStart)));
 }
}
''')


def test_chunked_grammar_combinations_match_current_master():
    run_js(r'''
const atoms=['x',' ', '\n', '`', '```\n', '    ', '<think>a</think>', '<think>open',
 '<function_calls>x</function_calls>', '<｜DSML |broken>', '<f', '<', '</think>',
 '<｜ DSML | function_calls>x</function_calls>', '<|turn|>thinking\na<turn|>'];
const failures=[];
for(const a of atoms)for(const b of atoms)for(const size of [1,3,17]){
 const raw=a+b+' end';
 for(const durable of [false,true]){
  const state=_createIncrementalSemanticState(undefined,durable);let prefix='';
  for(let i=0;i<raw.length;i+=size){
   const delta=raw.slice(i,i+size);prefix+=delta;state.write(delta);
   const want=_extractInlineThinkingFromContent(durable?prefix:_stripXmlToolCalls(prefix),'',{streaming:!durable});
   const got=state.snapshot();
   if(['content','reasoning','inThinking'].some(k=>got[k]!==want[k])){
    if(failures.length<12)failures.push({raw,prefix,size,durable,got,want});
    break;
   }
  }
 }
}
assert.deepEqual(failures,[]);
''')


def test_reasoning_channel_publication_work_is_linear():
    source = (ROOT / 'static/messages.js').read_text()
    start = source.index("    source.addEventListener('reasoning',e=>{")
    end = source.index("    source.addEventListener('tool',e=>{", start)
    run_js(r'''
let _terminalStateReached=false,_streamFinalized=false;
const _ownsActiveStreamOrBackground=()=>true,S={session:null},streamId='r';
const callbacks={},source={addEventListener(k,v){callbacks[k]=v;}};
''' + source[start:end] + r'''
for(let i=0;i<4000;i++)callbacks.reasoning({data:JSON.stringify({text:'r'})});
assert.equal(INFLIGHT.s.messages[0].reasoning,'r'.repeat(4000));
assert.ok(_durableSemanticState.stats.mergeBytes<12000,JSON.stringify(_durableSemanticState.stats));
''')


def test_every_prefix_preserves_batch_grammar():
    run_js(r'''
const cases=[
 'plain prose','<think>reason</think>answer','answer<think>literal unfinished',
 '    <function_calls>x</function_calls><think>r</think>answer',
 '<function_calls>x</function_calls>    <think>r</think>answer',
 '  before<function_calls>hidden</function_calls>after',
 'before<｜DSML<function_calls>hidden</function_calls>after',
 '    <think>literal</think>\n<function_calls>hidden</function_calls>answer',
 '<think>a\n\nb</think><think>b</think>end',
 '<think>same</think><think>same</think>visible',
 '`<think>literal</think>` prose','```xml\n<think>literal</think>\n```\n<think>secret</think>answer',
 '  ~~~\n<think>literal</think>\n  ~~~\nanswer',
 '<|channel>thought\nreason<channel|>answer', '<|turn|>thinking\nreason<turn|>answer',
 'before<｜'+ ' '.repeat(300)+'DSML |function_calls>x</function_calls>after',
 'answer<fun','before\n``','<think>  split reasoning  \n content  </think>after',
];
const failures=[];
for(const raw of cases){
 const live=_createIncrementalSemanticState(),saved=_createIncrementalSemanticState(undefined,true);
 let prefix='';
 for(const ch of raw){
  prefix+=ch;live.write(ch);saved.write(ch);
  for(const [name,state,input,streaming] of [['live',live,_stripXmlToolCalls(prefix),true],['durable',saved,prefix,false]]){
   const got=state.snapshot(''),want=_extractInlineThinkingFromContent(input,'',{streaming});
   for(const key of ['content','reasoning','inThinking'])if(got[key]!==want[key]){
    if(!failures.some(f=>f.raw===raw&&f.name===name&&f.key===key))failures.push({raw,name,key,prefix,actual:got[key],expected:want[key]});
   }
  }
 }
}
assert.deepEqual(failures,[]);
''')


def test_uncertain_xml_publishes_current_semantics():
    run_js(r'''
const raw='before<｜DSML<function_calls>hidden</function_calls>after';
for(const ch of raw)token(ch);
const want=_extractInlineThinkingFromContent(_stripXmlToolCalls(raw),'',{streaming:true});
assert.equal(_parseStreamState().content,want.content);
assert.equal(INFLIGHT.s.messages[0].content,raw);
assert.equal(INFLIGHT.s.lastAssistantText,raw);
''')


def test_tool_events_seal_corrected_semantics():
    source = (ROOT / 'static/messages.js').read_text()
    start = source.index("    source.addEventListener('tool',e=>{")
    end = source.index('    // Phase 2: dedicated `todo_state`', start)
    run_js(r'''
let _terminalStateReached=false,_streamFinalized=false;
const S={session:{session_id:activeSid},activeStreamId:'stream'},streamId='stream';
const callbacks={},source={addEventListener(k,v){callbacks[k]=v;}};
const _completeAutomaticCompressionOnLiveProgress=()=>{},upsertLiveToolCall=()=>({});
let sealed=[],applied=[];
const _upsertAnchorProcessProse=text=>sealed.push(text);
const _applyToAnchor=type=>applied.push({type,sealed:sealed.at(-1)});
const $=()=>null,assistantRow=null,assistantBody=null;
const ensureAssistantRow=()=>{},_renderAnchorLiveScene=()=>true,snapshotLiveTurn=()=>{};
const _smdEndParser=()=>{},_resetAssistantSegment=()=>{},scrollIfPinned=()=>{};
const _maybeNotifyPersistentStateSaved=()=>{};
let _freshSegment=false;
function _flushPendingSegmentRender(){_flushSemanticBoundary();}
''' + source[start:end] + r'''
for(const type of ['tool','tool_complete']){
 token('    <think>secret</think><function_calls>hidden</function_calls>after');
 const expected=_extractInlineThinkingFromContent(_stripXmlToolCalls(assistantText),'',{streaming:true}).content;
 callbacks[type]({data:JSON.stringify({name:'terminal',tid:'t'})});
 assert.equal(applied.at(-1).sealed,expected,type+' sealed before correction');
}
''')


def test_recovery_does_not_seed_channel_with_merged_inline_reasoning():
    source = (ROOT / 'static/messages.js').read_text()
    start = source.index('  const _liveInflightAssistantMessages = reconnecting')
    end = source.index('  let visibleInterimSnippets=', start)
    run_js(r'''
token('<think>abc');
assert.equal(INFLIGHT.s.messages[0].reasoning,'abc');
assert.equal(INFLIGHT.s.lastReasoningText,'');
const reconnecting=true;
''' + '{\n' + source[start:end] + '}\n' + r'''
_resyncSemanticProse('recovery');
token('d');
assert.equal(INFLIGHT.s.messages[0].reasoning,'abcd');
assert.equal(INFLIGHT.s.lastReasoningText,'');
''')


def test_unfinished_nonleading_thinking_keeps_durable_grammar():
    run_js(r'''
for(const ch of 'answer<think>literal unfinished')token(ch);
assert.equal(INFLIGHT.s.messages[0].content,assistantText);
assert.equal(INFLIGHT.s.messages[0].reasoning,undefined);
assert.equal(_parseStreamState().content,'answer');
assert.equal(_parseStreamState().reasoning,'literal unfinished');
token('</think> tail');
assert.equal(INFLIGHT.s.messages[0].content,'answer tail');
assert.equal(INFLIGHT.s.messages[0].reasoning,'literal unfinished');
''')
