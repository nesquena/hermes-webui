"""Paint consumes producer-owned prose; it cannot reassign semantic ownership."""
from tests.test_issue6391_live_scene_paint import run_js

def test_segment_paint_cannot_mutate_anchor_prose():
    run_js(r"""
let _renderPending=false,assistantBody={innerHTML:''},segmentStart=0,assistantText='owned prose';
const _isActiveSession=()=>true,_semanticSnapshot=()=>({displayText:assistantText});
const _stripXmlToolCalls=x=>x,_smdParser=null,window={},renderMd=x=>x;
const assistantRow={};let mutations=0;
const _upsertAnchorProcessProse=()=>mutations++;
eval(extract(messageSource,'_flushPendingSegmentRender'));
_flushPendingSegmentRender({force:true});
assert.equal(assistantBody.innerHTML,'owned prose');
assert.equal(mutations,0,'render must not own semantic writes');
""")


def test_terminal_fade_reads_producer_state_without_semantic_writes():
    run_js(r"""
let _anchorPaintDisposed=false,_anchorPaintGeneration=0,assistantBody={};
let _streamFadeDomText='visible',_smdParser=null;
const _streamFadeCurrentDisplayText=()=> 'owned';
let fadePaints=0,mutations=0;
const _renderStreamingFadeMarkdown=()=>{fadePaints++;return true;};
const _upsertAnchorProcessProse=()=>mutations++;
const scrollIfPinned=()=>{},_STREAM_FADE_MS=100,_STREAM_FADE_DONE_MAX_MS=1000,_streamFadeLatestAnimationEndAt=0;
const setTimeout=()=>1;
eval(extract(messageSource,'_drainStreamFadeBeforeDone'));
_drainStreamFadeBeforeDone(()=>{});
assert.equal(fadePaints,1);assert.equal(mutations,0);
""")
