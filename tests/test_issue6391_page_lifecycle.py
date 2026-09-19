"""Page lifecycle must complete claimed terminal work or transfer recovery."""
from tests.test_issue6391_live_scene_paint import run_js

def test_pagehide_finishes_before_detach_and_bfcache_uses_canonical_reload():
    run_js(r"""
const order=[],LIVE_STREAMS={s:{streamId:'r',finishScene(){order.push('finish');},disposeScene(){order.push('dispose');}}};
const INFLIGHT={s:{streamId:'r'}},S={session:{session_id:'s'}};
const snapshotLiveTurnHtmlForSession=()=>{};
const _resumeSessionStreamAfterLiveChat=()=>{};
const saveInflightState=()=>{};
eval(extract(messageSource,'closeLiveStream'));
const loadSession=sid=>{order.push('reload:'+sid);return Promise.resolve();};
eval(extract(messageSource,'_disposeLiveScenePaints'));
eval(extract(messageSource,'_restoreLiveSceneAfterPageShow'));
_disposeLiveScenePaints();assert.deepEqual(order,['finish','dispose']);assert.equal(Object.keys(LIVE_STREAMS).length,0);
_restoreLiveSceneAfterPageShow({persisted:true});assert.equal(order.at(-1),'reload:s');
const count=order.length;_restoreLiveSceneAfterPageShow({persisted:false});assert.equal(order.length,count);
""")
