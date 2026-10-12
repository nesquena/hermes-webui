"""Accepted terminal work must survive ordinary owner teardown."""
import pytest
from tests.test_issue6391_live_scene_paint import run_js


@pytest.mark.parametrize('path', ['page', 'switch'])
def test_shared_detach_paths_finish_current_owner(path):
    run_js(r"""
const order=[],INFLIGHT={};
const source={readyState:1,close(){order.push('close');this.readyState=2;}};
const LIVE_STREAMS={session:{streamId:'stream',source,
 finishScene(){order.push('finish');},flushScene(){order.push('flush');},
 disposeScene(){order.push('dispose');}}};
const snapshotLiveTurnHtmlForSession=()=>order.push('snapshot');
const _resumeSessionStreamAfterLiveChat=()=>{};
for(const f of ['closeLiveStream','closeOtherLiveStreams','_disposeLiveScenePaints']) eval(extract(messageSource,f));
if(PATH==='page') _disposeLiveScenePaints(); else closeOtherLiveStreams('other');
assert.deepEqual(order,['finish','flush','snapshot','dispose','close']);
""".replace('PATH',repr(path)))


@pytest.mark.parametrize('path', ['page', 'switch', 'supersede'])
def test_pending_fade_terminal_and_cache_share_teardown(path):
    run_js(r"""
let _anchorPaintGeneration=0,_anchorPaintDisposed=false,_anchorPaintScheduler=null;
let _terminalStateReached=true;
_pendingTerminalFinish=null;_terminalFadeTimer=1;_terminalFadeFrame=2;
const activeSid='session',streamId='stream',INFLIGHT={},order=[];
const source={readyState:1,close(){order.push('close');this.readyState=2;}};
const LIVE_STREAMS={};
const clearTimeout=()=>{},cancelAnimationFrame=()=>{};
const _anchorSceneRenderProjectionCaches=new Map([['old',{sessionId:activeSid,streamId,sourceRows:new Map([['p',{text:'substantial prose'}]])}]]);
const snapshotLiveTurnHtmlForSession=()=>order.push('snapshot');
const _resumeSessionStreamAfterLiveChat=()=>{};
const _drainSemanticProse=()=>{},_rememberRunJournalCursor=()=>{},_withDeferredAnchorScenePaint=fn=>fn;
const _dropAnchorRegistry=()=>{};
let _pendingProsePaint=null,_anchorFullPaintRequired=false;
for(const f of ['closeLiveStream','closeOtherLiveStreams','_disposeLiveScenePaints','_completeOwnedTerminal','_wireSSE']) eval(extract(messageSource,f));
eval(extract(src,'_releaseAnchorSceneRenderProjections'));
let persist=0,lifecycle=0;
_pendingTerminalFinish={generation:0,finish(){order.push('finish');persist++;lifecycle++;closeLiveStream(activeSid,streamId,source);}};
LIVE_STREAMS[activeSid]={streamId,source,
 finishScene:_completeOwnedTerminal,flushScene:()=>order.push('flush'),
 disposeScene(){order.push('dispose');_anchorPaintDisposed=true;_anchorPaintGeneration++;_releaseAnchorSceneRenderProjections(activeSid,streamId);}};
if(PATH==='page') _disposeLiveScenePaints();
else if(PATH==='switch') closeOtherLiveStreams('other');
else _wireSSE({readyState:1,addEventListener(){},close(){}});
assert.deepEqual(order,['finish','flush','snapshot','dispose','close']);
assert.equal(persist,1);assert.equal(lifecycle,1);assert.equal(_pendingTerminalFinish,null);
assert.equal(_anchorSceneRenderProjectionCaches.size,0);
_completeOwnedTerminal();closeLiveStream(activeSid,streamId,source);
assert.equal(persist,1);assert.equal(lifecycle,1);
""".replace('PATH',repr(path)))


def test_detach_finishes_before_snapshot_and_handles_terminal_reentry():
    run_js(r"""
const order=[];
const INFLIGHT={};
const source={readyState:1,close(){order.push('close');this.readyState=2;}};
const live={streamId:'stream',source,
 finishScene(){order.push('finish');closeLiveStream('session','stream',source);},
 flushScene(){order.push('flush');},disposeScene(){order.push('dispose');}};
const LIVE_STREAMS={session:live};
const snapshotLiveTurnHtmlForSession=()=>order.push('snapshot');
const _resumeSessionStreamAfterLiveChat=()=>{};
eval(extract(messageSource,'closeLiveStream'));
closeLiveStream('session','stream',source);
closeLiveStream('session','stream',source);
assert.deepEqual(order,['finish','flush','snapshot','dispose','close']);
assert.equal(LIVE_STREAMS.session,undefined);
""")
