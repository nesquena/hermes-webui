"""Late legacy prose must not acquire a second visible scene owner."""
import pytest

from tests.test_issue6391_live_scene_paint import run_js


@pytest.mark.parametrize("mode", ["compact_worklog", "transparent_stream", "hide_all_activity"])
@pytest.mark.parametrize("owner", [True, False])
def test_new_legacy_segment_respects_an_already_painted_scene(mode, owner):
    run_js(r"""
const mode=MODE, owns=OWNS;
const activeSid='a',streamId='s';
let assistantRow=null,assistantBody=null,_freshSegment=true;
let _assistantSegmentSeq=0,_currentLiveSegmentSeq=0,_currentActivityBurstId=1;
const INFLIGHT={a:{}},S={session:{session_id:'a'},activeStreamId:'s'};
const _isActiveSession=()=>true;
const chatActivityMode=()=>mode;
const isLiveAnchorActivitySceneOwner=id=>owns&&id===streamId;
function node(){return {children:[],attrs:{},style:{},hidden:false,isConnected:true,
  classList:{values:new Set(),add(x){this.values.add(x);},contains(x){return this.values.has(x);}},
  setAttribute(k,v){this.attrs[k]=v;},getAttribute(k){return this.attrs[k]||null;},
  appendChild(x){this.children.push(x);return x;}};}
const turn=node();turn.dataset={sessionId:activeSid};
const blocks=node();blocks.appendChild=x=>{
  // Assert at insertion, not after a later cleanup or an arbitrary sleep.
  const sceneOwns=owns&&mode!=='hide_all_activity';
  assert.equal(x.hidden,sceneOwns,'legacy process prose visibility at insertion');
  assert.equal(x.classList.contains('assistant-segment-worklog-source'),sceneOwns);
  assert.equal(x.getAttribute('aria-hidden'),sceneOwns?'true':null);
  blocks.children.push(x);
};
const _assistantTurnBlocks=()=>blocks;
const $=id=>id==='liveAssistantTurn'?turn:id==='emptyState'?node():null;
const document={createElement:()=>node()};
const _semanticSnapshot=()=>({displayText:'Lifecycle terminal process check'});
eval(extract(messageSource,'ensureAssistantRow'));
ensureAssistantRow();
assert.equal(blocks.children.length,1);
assert.equal(assistantRow.getAttribute('data-live-assistant'),'1');
assert.equal(assistantRow.children[0],assistantBody);
ensureAssistantRow();assert.equal(blocks.children.length,1,'same segment is reused');
// A post-tool segment follows the same creation path without scanning history.
assistantRow=null;assistantBody=null;_freshSegment=true;
ensureAssistantRow();assert.equal(blocks.children.length,2);
""".replace("MODE", repr(mode)).replace("OWNS", str(owner).lower()))
