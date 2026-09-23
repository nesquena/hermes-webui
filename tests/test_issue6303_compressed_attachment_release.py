"""#6304: a rotated-send recovery must release its replaced transport.

The production send/recovery/attachment handlers are composed by the existing
Worklog harness. Only the continuation load's I/O/navigation boundary is driven
by a deferred fixture; it deliberately does not clean up transports for us.
"""
import json
import textwrap

import pytest

from tests.test_issue6303_worklog_reattach_registry import _run_harness


@pytest.mark.parametrize("outcome", ["success", "failure", "superseded"])
@pytest.mark.parametrize("pending_snapshot", [False, True])
def test_rotated_send_releases_before_continuation_load(outcome, pending_snapshot):
    setup = "const scenario = " + json.dumps({
        "outcome": outcome, "pending": pending_snapshot,
    }) + ";\n" + textwrap.dedent(r"""
        const SID = 'rotated-parent', TARGET = 'continuation', STREAM = 'old-stream';
        const results = {}, input = {value:'keep this draft', focus(){}, dispatchEvent(){}};
        $ = id => id === 'msg' ? input : null;
        var _sendInProgress = false, _sendInProgressSid = null;
        var _pendingSelections = [], _forcedSkillDirectivePending = null;
        var _pendingMoaConfig = null, _slashDisplayTextOverride = '', _queueDrainSid = null;
        function _formatSelectedTextReplyQuote(value){ return String(value || ''); }
        function _clearPendingSelections(){ _pendingSelections = []; }
        function uploadPendingFiles(){ return Promise.resolve([]); }
        function _clearComposerDraft(){ return Promise.resolve(); }
        function _saveComposerDraftNow(){}
        function renderTray(){}
        function renderSessionListFromCache(){}
        function _fetchYoloState(){}
        function clearOptimisticSessionStreaming(){}
        function autoResize(){}
        function updateSendBtn(){}
        function applySessionTitleUpdate(){}
        function startClarifyPolling(){}
        function showToast(){}
        function setBusy(value){ S.busy = value; }
        let resolveSnapshot, releaseLoad, rejectLoad, loadEntered;
        const loading = new Promise(resolve => { loadEntered = resolve; });
        const loadBarrier = new Promise((resolve, reject) => { releaseLoad=resolve; rejectLoad=reject; });
        let oldLive, source, posts=0;
        async function loadSession(target){
          results.target = target;
          results.closedBeforeLoad = source.readyState === EventSource.CLOSED;
          results.unregisteredBeforeLoad = LIVE_STREAMS[SID] !== oldLive;
          results.payloadReleasedBeforeLoad = oldLive.ownerRef.value === null;
          loadEntered();
          await loadBarrier;
          // A superseding navigation owns the new view, just as loadSession's
          // generation guard does; no transport cleanup is hidden in this stub.
          if(scenario.outcome !== 'superseded'){
            S.session = {session_id:target};
            S.messages = [{role:'assistant', content:'continuation history'}];
          }
        }
        __apiHandler = url => {
          if(url.includes('/api/chat/start')){
            posts++;
            const error = new Error('session rotated');
            error.status=409;
            error.body=JSON.stringify({code:'session_rotated',continuation_session_id:TARGET});
            return Promise.reject(error);
          }
          if(url.includes('/api/session?')){
            return new Promise(resolve => { resolveSnapshot=resolve; });
          }
          return Promise.resolve({});
        };
        (async()=>{
          S.session={session_id:SID,title:'Parent',model:'test-model'};
          S.activeStreamId=STREAM;
          S.pendingFiles=[];
          S.messages=[{role:'user',content:'old request'}];
          INFLIGHT[SID]={messages:S.messages.slice(),uploaded:[],toolCalls:[],streamId:STREAM,
            activityBurstAnchors:[],currentActivityBurstId:0,currentLiveSegmentSeq:0};
          attachLiveStream(SID,STREAM,[],{});
          source=__esCreated[0]; oldLive=LIVE_STREAMS[SID];
          if(scenario.pending){
            source.dispatch('cancel',{type:'cancelled',status:'cancelled',event_id:'old-stream:7'});
            for(let i=0;i<8;i++) await Promise.resolve();
            if(!resolveSnapshot) throw new Error('terminal snapshot barrier not reached');
            results.snapshotRetainedBeforeSend=LIVE_STREAMS[SID]===oldLive;
          }
          S.activeStreamId=null; S.busy=false;
          const sending=send();
          await loading;
          results.markedReplaced=oldLive.ownerRef.replaced;
          let newer, newerOwner, beforeNewer;
          if(scenario.outcome==='superseded'){
            S.session={session_id:SID,title:'Returned parent'};
            S.activeStreamId='new-stream'; S.busy=true;
            S.messages=[{role:'user',content:'new owner history'}];
            newerOwner=INFLIGHT[SID]={messages:S.messages.slice(),uploaded:[],toolCalls:[],
              streamId:'new-stream',activityBurstAnchors:[],currentActivityBurstId:0,currentLiveSegmentSeq:0};
            attachLiveStream(SID,'new-stream',[],{});
            newer=LIVE_STREAMS[SID];
            // User goes to another view while the continuation load is pending.
            S.session={session_id:'other-view'};
            S.messages=[{role:'user',content:'new view history'}];
            input.value='new view draft';
            beforeNewer=JSON.stringify(S.messages);
          }
          if(scenario.outcome==='failure') rejectLoad(new Error('continuation unavailable'));
          else releaseLoad();
          await sending;
          const beforeLate=JSON.stringify(S.messages);
          if(resolveSnapshot){
            resolveSnapshot({session:{session_id:SID,active_stream_id:null,pending_user_message:null,
              messages:[{role:'assistant',content:'STALE TERMINAL SNAPSHOT'}],tool_calls:[]}});
            for(let i=0;i<10;i++) await Promise.resolve();
          }
          results.lateSnapshotIgnored=JSON.stringify(S.messages)===beforeLate;
          results.sourceClosed=source.readyState===EventSource.CLOSED;
          results.payloadReleased=oldLive.ownerRef.value===null;
          results.posts=posts;
          results.draft=input.value;
          if(newer){
            results.newerProtected=LIVE_STREAMS[SID]===newer && newer.source.readyState===EventSource.OPEN
              && INFLIGHT[SID]===newerOwner && newer.ownerRef.value===newerOwner
              && JSON.stringify(S.messages)===beforeNewer;
          }
          process.stdout.write(JSON.stringify(results)+'\n',()=>process.exit(0));
        })().catch(error=>{console.error(error.stack||String(error));process.exit(2);});
    """)
    result = _run_harness(setup, include_send=True)
    assert result["target"] == "continuation"
    assert result["markedReplaced"] is True
    if pending_snapshot:
        assert result["snapshotRetainedBeforeSend"] is True
    assert result["closedBeforeLoad"] is True, "replaced EventSource must close before any continuation await"
    assert result["unregisteredBeforeLoad"] is True
    assert result["payloadReleasedBeforeLoad"] is True
    assert result["sourceClosed"] and result["payloadReleased"]
    assert result["lateSnapshotIgnored"] is True
    assert result["posts"] == 1, "a rotated send must not automatically replay its POST"
    if outcome == "superseded":
        assert result["newerProtected"] is True
        assert result["draft"] == "new view draft"
    else:
        assert result["draft"] == "keep this draft"
