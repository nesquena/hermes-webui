"""Producer semantic snapshots coalesce publication, never parsing."""
from tests.test_issue6391_live_scene_paint import run_js
from tests.test_issue7478_spaced_semantic import helpers, runtime


def test_batched_prose_preserves_thinking_and_boundary_order():
    run_js(helpers() + runtime() + r"""
let assistantText='',liveReasoningText='',segmentStart=0,_anchorPaintDisposed=false;
let _semanticProseTimer=null,_semanticProseDirty=false;
const setTimeout=()=>1,clearTimeout=()=>{};
const syncInflightAssistantMessage=()=>{},assistantRow={};
const rows=[];let proseRow=null;
const _upsertAnchorProcessProse=text=>{if(!proseRow){proseRow={role:'prose'};rows.push(proseRow);}proseRow.text=text;};
for(const f of ['_stripXmlToolCalls','_parseStreamState','_parseCurrentSegmentDisplayText','_drainSemanticProse','_scheduleSemanticProse']) eval(extract(messageSource,f));
function token(text){assistantText+=text;_scheduleSemanticProse(text);_drainSemanticProse();}
token('<thi');assert.equal(rows.length,0);
token('nk>private reasoning');assert.equal(rows.length,0);
token('</think>before');
assert.equal(rows[0].text,'before');assert.equal(_semanticSnapshot().thinkingText,'private reasoning');
rows.push({role:'tool',text:'work'});segmentStart=assistantText.length;_semanticSegmentStart=rows[0].text.length;proseRow=null;
token('after');
assert.deepEqual(rows.map(r=>[r.role,r.text]),[['prose','before'],['tool','work'],['prose','after']]);
token(' terminal');assert.equal(rows[2].text,'after terminal');
token('<think>post-tool secret</think>visible');
assert.equal(rows[2].text,'after terminalvisible');
assert.equal(_semanticProseDirty,false);assert.equal(_semanticProseTimer,null);
assert.equal(_semanticMetrics.fullParses,0);
""")


def test_semantic_prose_batches_long_token_burst_and_drains():
    run_js(helpers() + runtime() + r"""
let assistantText='',liveReasoningText='',segmentStart=0,_anchorPaintDisposed=false;
let _semanticProseTimer=null,_semanticProseDirty=false,prose='';
const setTimeout=()=>1,clearTimeout=()=>{};
const syncInflightAssistantMessage=()=>{},assistantRow={};
const _parseStreamState=()=>{throw Error('normal growth must not rescan');};
const _upsertAnchorProcessProse=x=>{prose=x;};
for(const f of ['_drainSemanticProse','_scheduleSemanticProse']) eval(extract(messageSource,f));
for(let i=0;i<10000;i++){assistantText+='x';_scheduleSemanticProse('x');}
assert.equal(prose,'');_drainSemanticProse();
assert.equal(prose.length,10000);assert.equal(_semanticState.stats.incrementalBytes,10000);
_drainSemanticProse();assert.equal(prose.length,10000);
""")
