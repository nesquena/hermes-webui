function _markSessionViewed(sid, messageCount) {
  if(typeof _setSessionViewedCount!=='function' || !sid) return;
  const next = Number.isFinite(messageCount) ? Number(messageCount) : 0;
  _setSessionViewedCount(sid, next);
}

function _isDocumentVisibleAndFocused() {
  if(typeof document!=='undefined' && document.visibilityState && document.visibilityState!=='visible') return false;
  if(typeof document!=='undefined' && typeof document.hasFocus==='function' && !document.hasFocus()) return false;
  return true;
}

function _isSessionCurrentPane(sid) {
  if(!sid || !S.session || S.session.session_id!==sid) return false;
  // During session switching, S.session still points at the previous row until
  // the next metadata request resolves. Do not let a just-finished old stream
  // update the chat pane while the user is moving to another session.
  if(typeof _loadingSessionId!=='undefined' && _loadingSessionId && _loadingSessionId!==sid) return false;
  return true;
}

function _isSessionActivelyViewed(sid) {
  if(!_isSessionCurrentPane(sid)) return false;
  if(!_isDocumentVisibleAndFocused()) return false;
  return true;
}

function _markActiveSessionViewedOnReturn() {
  if(!_isDocumentVisibleAndFocused() || !S.session || !S.session.session_id) return;
  _markSessionViewed(S.session.session_id, S.session.message_count || (S.messages&&S.messages.length) || 0);
  if(typeof _clearSessionCompletionUnread==='function') _clearSessionCompletionUnread(S.session.session_id);
  if(typeof renderSessionListFromCache==='function') renderSessionListFromCache();
}

function _deferStreamErrorIfOffline(){
  if(typeof isOfflineBannerVisible==='function' && isOfflineBannerVisible()){
    setComposerStatus(t('offline_stream_waiting'));
    return true;
  }
  if(typeof showOfflineBanner==='function' && navigator.onLine===false){
    showOfflineBanner('browser');
    setComposerStatus(t('offline_stream_waiting'));
    return true;
  }
  return false;
}

document.addEventListener('visibilitychange', _markActiveSessionViewedOnReturn);
window.addEventListener('focus', _markActiveSessionViewedOnReturn);
// TTS: pause speech synthesis when user focuses the composer (#499)
const _msgEl=document.getElementById('msg');
if(_msgEl) _msgEl.addEventListener('focus', ()=>{ if('speechSynthesis' in window && speechSynthesis.speaking) speechSynthesis.pause(); });
if(_msgEl) _msgEl.addEventListener('blur', ()=>{ if('speechSynthesis' in window && speechSynthesis.paused) speechSynthesis.resume(); });

// Guard against concurrent send() calls.  Without this, two rapid sends
// (e.g. queue drain + user click) can both pass the S.busy check because
// setBusy(true) is only called after the first await inside send().
let _sendInProgress = false;
const _sessionTitleProvisionalBySid = new Map();

function _sessionTitleLooksDefaultOrProvisional(titleText, provisionalText){
  const title=String(titleText||'').replace(/\s+/g,' ').trim();
  if(!title||title==='Untitled'||title==='New Chat')return true;
  const provisional=String(provisionalText||'').replace(/\s+/g,' ').trim().slice(0,64);
  return !!provisional&&title===provisional;
}

function _firstUserMessageTitleCandidate(){
  const first=(S.messages||[]).find(m=>m&&m.role==='user'&&m.content);
  return first?String(first.content||'').trim().slice(0,64):'';
}

function applySessionTitleUpdate(sid, titleText, options={}){
  const newTitle=String(titleText||'').trim();
  if(!sid||!newTitle)return false;
  const row=(typeof _allSessions!=='undefined'&&Array.isArray(_allSessions))
    ? _allSessions.find(s=>s&&s.session_id===sid)
    : null;
  const currentTitle=S.session&&S.session.session_id===sid
    ? S.session.title
    : row&&row.title;
  if(!options.force){
    const expected=String(options.expectedCurrent||'').trim();
    const remembered=_sessionTitleProvisionalBySid.get(sid)||'';
    const provisionalCandidates=[options.provisionalText,remembered,_firstUserMessageTitleCandidate()];
    const allowed=(expected&&String(currentTitle||'').trim()===expected)
      || String(currentTitle||'').trim()===newTitle
      || provisionalCandidates.some(p=>_sessionTitleLooksDefaultOrProvisional(currentTitle, p));
    if(!allowed)return false;
  }
  if(S.session&&S.session.session_id===sid){
    S.session.title=newTitle;
    if(typeof syncTopbar==='function') syncTopbar();
  }
  if(row) row.title=newTitle;
  if(options.rememberProvisional) _sessionTitleProvisionalBySid.set(sid,newTitle);
  if(typeof renderSessionListFromCache==='function') renderSessionListFromCache();
  else if(typeof renderSessionList==='function') renderSessionList();
  return true;
}

async function send(){
  // Reject concurrent invocations early — before any await yields control.
  // If a send is already in-flight (e.g. queue drain), re-queue the message
  // instead of silently dropping it.
  if (_sendInProgress) {
    const _text=$('msg').value.trim();
    if(_text && S.session && S.session.session_id){
      queueSessionMessage(S.session.session_id,{text:_text,files:[...S.pendingFiles],model:S.session&&S.session.model||($('modelSelect')&&$('modelSelect').value)||'',model_provider:S.session&&S.session.model_provider||null,profile:S.activeProfile||'default'});
      $('msg').value='';autoResize();
      S.pendingFiles=[];renderTray();
      updateQueueBadge(S.session.session_id);
      showToast(`Queued: "${_text.slice(0,40)}${_text.length>40?'…':''}"`,2000);
    }
    return;
  }
  _sendInProgress = true;
  try{
  const text=$('msg').value.trim();
  if(!text&&!S.pendingFiles.length)return;
  // Don't send while an inline message edit is active
  if(document.querySelector('.msg-edit-area'))return;

  // Dismiss handoff hint when user sends a message (resets seen_at).
  if(S.session&&S.session.session_id&&typeof _dismissHandoffHint==='function'){
    _dismissHandoffHint(S.session.session_id);
  }

  const compressionRunning=typeof isCompressionUiRunning==='function'&&isCompressionUiRunning();
  // If busy or a manual compression is still running, handle based on busy_input_mode
  if(S.busy||compressionRunning){
    if(text){
      if(!S.session){await newSession();await renderSessionList();}
      // Busy-control slash commands must be intercepted HERE, before the
      // busyMode routing block, so the user can always type /steer, /interrupt,
      // or /queue while the agent is running and have them execute immediately.
      // Without this intercept they fall through to the queue and execute after
      // the current turn ends — by which point there is no active stream and
      // cmdSteer / cmdInterrupt say "No active task to stop."
      if(text.startsWith('/')){
        const _pc=typeof parseCommand==='function'&&parseCommand(text);
        if(_pc&&['steer','interrupt','queue','terminal','goal'].includes(_pc.name)){
          const _bc=COMMANDS.find(c=>c.name===_pc.name);
          if(_bc){
            $('msg').value='';autoResize();
            await _bc.fn(_pc.args);
            return;
          }
        }
      }
      const busyMode=window._busyInputMode||'queue';
      if(busyMode==='steer'&&S.activeStreamId&&typeof _trySteer==='function'){
        // Real steer: clear the input first so the user gets immediate
        // feedback, then ship the steer payload via /api/chat/steer.
        // _trySteer falls back to queue+cancel internally if the agent
        // isn't running / cached / steer-capable.
        $('msg').value='';autoResize();
        // Do NOT clear pendingFiles yet — _trySteer may fall back to
        // interrupt+queue and needs the files for queueSessionMessage.
        // _trySteer clears pendingFiles itself in the fallback path, and
        // the server returns accepted:true (no files sent) on success.
        await _trySteer(text, /*explicitSteer=*/false);
        // After _trySteer: clear any remaining files (success path).
        S.pendingFiles=[];renderTray();
      } else if(busyMode==='interrupt'){
        // Queue the message, then cancel so drain re-sends it.
        queueSessionMessage(S.session.session_id,{text,files:[...S.pendingFiles],model:S.session&&S.session.model||($('modelSelect')&&$('modelSelect').value)||'',model_provider:S.session&&S.session.model_provider||null,profile:S.activeProfile||'default'});
        updateQueueBadge(S.session.session_id);
        $('msg').value='';autoResize();
        S.pendingFiles=[];renderTray();
        if(S.activeStreamId&&typeof cancelStream==='function'){
          showToast(t('busy_interrupt_confirm'),2000);
          await cancelStream();
        } else {
          showToast(`Queued: "${text.slice(0,40)}${text.length>40?'…':''}"`,2000);
        }
      } else {
        // Default: queue mode (current behavior). Also the fallback for
        // 'steer' mode when no stream is active or _trySteer is unavailable.
        queueSessionMessage(S.session.session_id,{text,files:[...S.pendingFiles],model:S.session&&S.session.model||($('modelSelect')&&$('modelSelect').value)||'',model_provider:S.session&&S.session.model_provider||null,profile:S.activeProfile||'default'});
        $('msg').value='';autoResize();
        S.pendingFiles=[];renderTray();
        updateQueueBadge(S.session.session_id);
        showToast(`Queued: "${text.slice(0,40)}${text.length>40?'…':''}"`,2000);
      }
    }
    return;
  }
  if(S.session&&(S.session.read_only||S.session.is_read_only)){
    if(typeof showToast==='function') showToast('Read-only imported sessions cannot be modified.',3000);
    return;
  }
  // Slash command intercept -- local commands handled without agent round-trip.
  // We push the user message BEFORE running the handler for echo-worthy
  // commands so chat order is correct: some handlers (e.g. cmdHelp) push
  // their assistant response synchronously.  If we pushed AFTER, S.messages
  // would be [assistant, user] and the chat would show the response above
  // the user's own input — reverse chronological order (#840 ordering bug).
  if(text.startsWith('/')&&!S.pendingFiles.length){
    const _parsedCmd=parseCommand(text);
    const _cmd=_parsedCmd?COMMANDS.find(c=>c.name===_parsedCmd.name):null;
    if(_cmd){
      let _pushedUser=false;
      if(!_cmd.noEcho){
        if(!S.session){await newSession();await renderSessionList();}
        S.messages.push({role:'user',content:text,_ts:Date.now()/1000});
        _pushedUser=true;
        renderMessages();
      }
      // Run the handler directly (we already looked it up).  If it returns
      // false it's opting out — e.g. /reasoning <level> falls through so the
      // agent sees the raw text.  Roll back the echo push in that case so
      // the normal send path doesn't duplicate it.
      if(_cmd.fn(_parsedCmd.args)===false){
        if(_pushedUser){S.messages.pop();renderMessages();}
        // Fall through to normal send path
      } else {
        $('msg').value='';autoResize();hideCmdDropdown();return;
      }
    }
    if(_parsedCmd&&!_cmd){
      const _agentCmd=typeof getAgentCommandMetadata==='function'
        ? await getAgentCommandMetadata(_parsedCmd.name)
        : null;
      if(_agentCmd&&_agentCmd.cli_only){
        if(!S.session){await newSession();await renderSessionList();}
        S.messages.push({role:'user',content:text,_ts:Date.now()/1000});
        S.messages.push({role:'assistant',content:cliOnlyCommandResponse(_parsedCmd.name,_agentCmd),_ts:Date.now()/1000});
        renderMessages();
        $('msg').value='';autoResize();hideCmdDropdown();return;
      }
      if(_agentCmd&&_agentCmd.category==='Plugin'){
        if(!S.session){await newSession();await renderSessionList();}
        S.messages.push({role:'user',content:text,_ts:Date.now()/1000});
        let _pluginOutput='(no output)';
        try{
          _pluginOutput=typeof executeAgentPluginCommand==='function'
            ? await executeAgentPluginCommand(text,_agentCmd)
            : 'Plugin command runtime unavailable in WebUI.';
        }catch(e){
          _pluginOutput=`Plugin command error: ${e&&e.message||e}`;
        }
        S.messages.push({role:'assistant',content:String(_pluginOutput||'(no output)'),_ts:Date.now()/1000});
        renderMessages();
        $('msg').value='';autoResize();hideCmdDropdown();return;
      }
    }
  }
  if(!S.session){await newSession();await renderSessionList();}

  const activeSid=S.session.session_id;

  setComposerStatus(S.pendingFiles&&S.pendingFiles.length?'Uploading…':'');
  let uploaded=[];
  try{uploaded=await uploadPendingFiles();}
  catch(e){if(!text){setComposerStatus(`Upload error: ${e.message}`);return;}}
  // Clear the uploading status now that upload is done — if we don't clear here
  // it stays visible for the entire duration of the agent stream, since
  // setComposerStatus('') is only called in setBusy(false), not setBusy(true).
  setComposerStatus('');

  const uploadedNames=uploaded.map(u=>u.name||u);
  const uploadedPaths=uploaded.map(u=>u&&u.is_image?(u.name||u.filename||u):(u.path||u.name||u));
  let msgText=text;
  if(uploaded.length&&!msgText)msgText=`I've uploaded ${uploaded.length} file(s): ${uploadedPaths.join(', ')}`;
  else if(uploaded.length)msgText=`${text}\n\n[Attached files: ${uploadedPaths.join(', ')}]`;
  if(!msgText){setComposerStatus('Nothing to send');return;}

  $('msg').value='';autoResize();
  // Clear persisted composer draft since message was sent.
  if (activeSid && typeof _clearComposerDraft === 'function') _clearComposerDraft(activeSid);
  const displayText=text||(uploaded.length?`Uploaded: ${uploadedNames.join(', ')}`:'(file upload)');
  const userMsg={role:'user',content:displayText,attachments:uploaded.length?uploadedNames:undefined,_ts:Date.now()/1000};
  S.toolCalls=[];  // clear tool calls from previous turn
  clearLiveToolCards();  // clear any leftover live cards from last turn
  S.messages.push(userMsg);renderMessages();appendThinking();setBusy(true);
  // First optimistic pass: make the local user turn visible before /api/chat/start
  // can save pending state on the server.
  if(typeof upsertActiveSessionForLocalTurn==='function'){
    upsertActiveSessionForLocalTurn({title:displayText.slice(0,64),messageCount:S.messages.length,timestampMs:Date.now()});
  }
  INFLIGHT[activeSid]={messages:[...S.messages],uploaded:uploadedNames,toolCalls:[]};
  if(typeof saveInflightState==='function'){
    saveInflightState(activeSid,{streamId:null,messages:INFLIGHT[activeSid].messages,uploaded:uploadedNames,toolCalls:[]});
  }
  if(typeof renderSessionListFromCache==='function') renderSessionListFromCache();
  startApprovalPolling(activeSid);
  startClarifyPolling(activeSid);
  _fetchYoloState(activeSid);  // sync YOLO pill with backend state
  S.activeStreamId = null;  // will be set after stream starts
  if(typeof updateSendBtn==='function') updateSendBtn();

  // Set provisional title from user message immediately so session appears
  // in the sidebar right away with a meaningful name. /api/chat/start persists
  // the server-side provisional title and may refine this optimistic text.
  if(S.session&&(S.session.title==='Untitled'||!S.session.title)){
    const provisionalTitle=displayText.slice(0,64);
    applySessionTitleUpdate(activeSid, provisionalTitle, {force:true, rememberProvisional:true});
    if(typeof upsertActiveSessionForLocalTurn==='function'){
      // Second optimistic pass: carry the provisional title into the cached row
      // without re-fetching /api/sessions before pending state exists server-side.
      upsertActiveSessionForLocalTurn({title:provisionalTitle,messageCount:S.messages.length,timestampMs:Date.now()});
    }
  } else if(typeof upsertActiveSessionForLocalTurn==='function'){
    upsertActiveSessionForLocalTurn({title:S.session&&S.session.title||displayText.slice(0,64),messageCount:S.messages.length,timestampMs:Date.now()});
  } else {
    renderSessionListFromCache();  // ensure it's visible even if already titled
  }

  // Start the agent via POST, get a stream_id back
  let streamId;
  try{
    const startData=await api('/api/chat/start',{method:'POST',body:JSON.stringify({
      session_id:activeSid,message:msgText,
      model:S.session.model||$('modelSelect').value,workspace:S.session.workspace,
      model_provider:S.session.model_provider||null,
      profile:S.activeProfile||S.session.profile||'default',
      attachments:uploaded.length?uploaded:undefined
    })});

    if(startData.title) applySessionTitleUpdate(activeSid, startData.title, {provisionalText:displayText.slice(0,64), rememberProvisional:true});

    if(startData.effective_model && S.session){
      S.session.model=startData.effective_model;
      S.session.model_provider=startData.effective_model_provider||S.session.model_provider||null;
      localStorage.setItem('hermes-webui-model', startData.effective_model);
      if(typeof _writePersistedModelState==='function') _writePersistedModelState(startData.effective_model,S.session.model_provider||null);
      if($('modelSelect')) _applyModelToDropdown(startData.effective_model, $('modelSelect'),S.session.model_provider||null);
      if(typeof syncTopbar==='function') syncTopbar();
    }else if(startData.effective_model_provider && S.session){
      S.session.model_provider=startData.effective_model_provider;
      if(typeof _writePersistedModelState==='function') _writePersistedModelState(S.session.model||'',S.session.model_provider||null);
      if($('modelSelect')&&typeof _applyModelToDropdown==='function') _applyModelToDropdown(S.session.model||'', $('modelSelect'), S.session.model_provider||null);
      if(typeof syncModelChip==='function') syncModelChip();
      if(typeof syncTopbar==='function') syncTopbar();
    }
    streamId=startData.stream_id;
    S.activeStreamId = streamId;
    // setBusy(true) already ran with activeStreamId=null; refresh now that we
    // have a stream id so the primary button can switch to Stop (see getComposerPrimaryAction).
    if(typeof updateSendBtn==='function') updateSendBtn();
    if(S.session&&typeof startData.pending_started_at==='number'){
      S.session.pending_started_at=startData.pending_started_at;
    }
    if(S.session&&S.session.session_id===activeSid){
      S.session.active_stream_id = streamId;
    }
    if(typeof upsertActiveSessionForLocalTurn==='function'){
      // Third optimistic pass: stream_id is now known, so the row can reconcile
      // against real active-stream metadata before the background refresh lands.
      upsertActiveSessionForLocalTurn({title:S.session&&S.session.title||displayText.slice(0,64),messageCount:S.messages.length,timestampMs:Date.now()});
    }
    markInflight(activeSid, streamId);
    if(typeof saveInflightState==='function'){
      saveInflightState(activeSid,{streamId,messages:INFLIGHT[activeSid].messages,uploaded:uploadedNames,toolCalls:INFLIGHT[activeSid].toolCalls||[]});
    }
    // Refresh session list so background streaming indicators appear immediately for the
    // session that was just started and any others that may already be running.
    if(typeof renderSessionList === 'function') {
      void renderSessionList();
    }
  }catch(e){
    const errMsg=String((e&&e.message)||'');
    const conflictActiveStream=/session already has an active stream/i.test(errMsg);
    if(conflictActiveStream){
      delete INFLIGHT[activeSid];
      if(typeof clearInflightState==='function') clearInflightState(activeSid);
      stopApprovalPolling();
      stopClarifyPolling();
      // Keep the user's attempted turn by queueing it for after the current run.
      queueSessionMessage(activeSid,{text:msgText,files:[],model:S.session&&S.session.model||($('modelSelect')&&$('modelSelect').value)||'',model_provider:S.session&&S.session.model_provider||null,profile:S.activeProfile||'default'});
      updateQueueBadge(activeSid);
      showToast('Current session is still running. Reconnected and queued your message.',2600);
      try{
        await loadSession(activeSid);
        setComposerStatus('');
        return;
      }catch(_){
        // Fall through to standard error handling if session reload fails.
      }
    }

    delete INFLIGHT[activeSid];
    stopApprovalPolling();
    stopClarifyPolling();
    // Only hide approval card if it belongs to the session that just finished
    if(!_approvalSessionId || _approvalSessionId===activeSid) hideApprovalCard(true);removeThinking();
    if(!_clarifySessionId || _clarifySessionId===activeSid) hideClarifyCard(true, 'terminal');
    S.messages.push({role:'assistant',content:`**Error:** ${errMsg}`});
    _queueDrainSid=activeSid;renderMessages();setBusy(false);setComposerStatus(`Error: ${errMsg}`);
    if(typeof clearOptimisticSessionStreaming==='function') clearOptimisticSessionStreaming(activeSid);
    // Reconcile with server truth after immediately clearing the optimistic spinner.
    if(typeof renderSessionList==='function') void renderSessionList();
    return;
  }

  // Open SSE stream and render tokens live
  attachLiveStream(activeSid, streamId, uploadedNames);

  }finally{ _sendInProgress=false; }
}

const LIVE_STREAMS={};

function closeLiveStream(sessionId, streamId){
  const live=LIVE_STREAMS[sessionId];
  if(!live) return;
  if(streamId&&live.streamId!==streamId) return;
  try{live.source.close();}catch(_){ }
  delete LIVE_STREAMS[sessionId];
}

function attachLiveStream(activeSid, streamId, uploaded=[], options={}){
  if(!activeSid||!streamId) return;
  const reconnecting=!!options.reconnecting;
  if(!INFLIGHT[activeSid]) INFLIGHT[activeSid]={messages:[...S.messages],uploaded:[...uploaded],toolCalls:[]};
  else {
    if(uploaded.length) INFLIGHT[activeSid].uploaded=[...uploaded];
    if(!Array.isArray(INFLIGHT[activeSid].toolCalls)) INFLIGHT[activeSid].toolCalls=[];
  }
  const existingLive=LIVE_STREAMS[activeSid];
  if(
    existingLive&&existingLive.streamId===streamId&&existingLive.source&&
    // A same-stream transport can be reused unless the browser has already
    // marked it closed; closed streams must still fall through to reopen.
    (typeof EventSource==='undefined'||existingLive.source.readyState!==EventSource.CLOSED)
  ){
    return;
  }
  closeLiveStream(activeSid);

  let assistantText='';
  let reasoningText='';
  let liveReasoningText='';
  let _latestGoalStatus=null;
  let _pendingGoalContinuation=null;
  let assistantRow=null;
  let assistantBody=null;
  let segmentStart=0;      // char offset in assistantText where current segment begins
  let _freshSegment=false; // true after a tool call — forces a new DOM segment
  // streaming-markdown state: incremental DOM-building parser per segment
  let _smdParser=null;     // current smd parser instance (null until first content)
  let _smdWrittenLen=0;    // how many chars of displayText have been fed to smd parser
  let _smdWrittenText='';  // exact displayText snapshot used for prefix-alignment checks
  // On reconnect, the assistantBody already has partial smd-rendered content.
  // We clear it on first new token and restart the parser from the reconnect point.
  let _smdReconnect=reconnecting;
  // Thinking tag patterns for streaming display
  const _thinkPairs=[
    {open:'<think>',close:'</think>'},
    {open:'<|channel>thought\n',close:'<channel|>'},
    {open:'<|turn|>thinking\n',close:'<turn|>'}  // Gemma 4
  ];

  function _isActiveSession(){
    return !!(S.session&&S.session.session_id===activeSid);
  }
  function _clearActivePaneInflightIfOwner(){
    if(_isActiveSession()) clearInflight();
  }
  function _approvalBelongsToOwner(){
    return _approvalSessionId===activeSid||(!_approvalSessionId&&_isActiveSession());
  }
  function _clarifyBelongsToOwner(){
    return _clarifySessionId===activeSid||(!_clarifySessionId&&_isActiveSession());
  }
  function _clearApprovalForOwner(){
    _clearApprovalPendingForSession(activeSid);
    if(!_approvalBelongsToOwner()) return;
    stopApprovalPolling();
    hideApprovalCard(true);
  }
  function _clearClarifyForOwner(reason){
    _clearClarifyPendingForSession(activeSid);
    if(!_clarifyBelongsToOwner()) return;
    stopClarifyPolling();
    hideClarifyCard(true, reason||'terminal');
  }
  function _clearOwnerInflightState(){
    delete INFLIGHT[activeSid];
    clearInflightState(activeSid);
    _clearActivePaneInflightIfOwner();
  }
  function _setActivePaneIdleIfOwner(){
    if(_isActiveSession()||!S.session||!INFLIGHT[S.session.session_id]){
      setBusy(false);
      setComposerStatus('');
      if(typeof setStatus==='function') setStatus('');
    }
  }
  function persistInflightState(){
    const inflight=INFLIGHT[activeSid];
    if(!inflight||typeof saveInflightState!=='function') return;
    saveInflightState(activeSid,{
      streamId,
      messages:inflight.messages||[],
      uploaded:inflight.uploaded||[...uploaded],
      toolCalls:inflight.toolCalls||[],
    });
  }
  // Throttled variant for token-by-token updates. persistInflightState()
  // calls saveInflightState() which does JSON.parse + JSON.stringify + write
  // on the entire inflight map every call. On a fast model at 60 tok/s with
  // a 10KB messages array this is ~36MB of JSON churn per second — a major
  // GC pressure source that causes the renderer to crash under load.
  // State transitions (tool events, done, error) still call persistInflightState()
  // directly so no more than 2s of progress is lost on a crash.
  let _persistTimer=null;
  function _throttledPersist(){
    if(_persistTimer) return;
    _persistTimer=setTimeout(()=>{_persistTimer=null;persistInflightState();},2000);
  }
  function _closeSource(){
    closeLiveStream(activeSid, streamId);
  }
  function syncInflightAssistantMessage(){
    const inflight=INFLIGHT[activeSid];
    if(!inflight) return;
    if(!Array.isArray(inflight.messages)) inflight.messages=[];
    let assistantIdx=-1;
    for(let i=inflight.messages.length-1;i>=0;i--){
      const msg=inflight.messages[i];
      if(msg&&msg.role==='assistant'&&msg._live){assistantIdx=i;break;}
    }
    const ts=Date.now()/1000;
    if(assistantIdx>=0){
      inflight.messages[assistantIdx].content=assistantText;
      inflight.messages[assistantIdx].reasoning=reasoningText||undefined;
      inflight.messages[assistantIdx]._ts=inflight.messages[assistantIdx]._ts||ts;
      _throttledPersist();
      return;
    }
    inflight.messages.push({role:'assistant',content:assistantText,reasoning:reasoningText||undefined,_live:true,_ts:ts});
    _throttledPersist();
  }
  function ensureAssistantRow(force=false){
    if(!_isActiveSession()) return;
    if(assistantRow&&!assistantRow.isConnected){assistantRow=null;assistantBody=null;}
    if(!force&&!assistantRow){
      const parsed=_parseStreamState();
      if(!String((parsed&&parsed.displayText)||'').trim()) return;
    }
    let turn=$('liveAssistantTurn');
    if(!turn){
      appendThinking();
      turn=$('liveAssistantTurn');
    }
    const blocks=(typeof _assistantTurnBlocks==='function')?_assistantTurnBlocks(turn):null;
    if(!blocks) return;
    if(!assistantRow){
      // Only reuse an existing segment on the very first creation (e.g. reconnect).
      // After a tool call _freshSegment=true, so we always create a new segment
      // below the tool card rather than re-attaching to the old one above it.
      if(!_freshSegment){
        const existing=blocks.querySelector('[data-live-assistant="1"]');
        if(existing){
          assistantRow=existing;
          assistantBody=existing.querySelector('.msg-body');
        }
      }
    }
    if(assistantRow){
      if(typeof placeLiveToolCardsHost==='function') placeLiveToolCardsHost();
      return;
    }

    const tr=$('toolRunningRow');if(tr)tr.remove();
    $('emptyState').style.display='none';
    assistantRow=document.createElement('div');
    assistantRow.className='assistant-segment';
    assistantRow.setAttribute('data-live-assistant','1');
    assistantBody=document.createElement('div');assistantBody.className='msg-body';
    assistantRow.appendChild(assistantBody);
    blocks.appendChild(assistantRow);
    _freshSegment=false; // consumed — next reuse check is normal again
  }

  // ── Shared SSE handler wiring (used for initial connection and reconnect) ──
  let _reconnectAttempted=false;
  let _terminalStateReached=false;

  // Bug A fix (#631): track whether the stream has been finalized so any rAF
  // scheduled by a trailing 'token'/'reasoning' event that arrives in the same
  // microtask batch as 'done' does not fire after renderMessages() has already
  // settled the DOM — which was causing the thinking card to reappear below
  // the final answer or the response to render twice.
  let _streamFinalized=false;
  let _pendingRafHandle=null;
  let _streamFadeVisibleText='';
  let _streamFadeLastTickMs=0;
  let _streamFadeWordCarry=0;
  let _streamFadeStartedAt=0;
  let _streamFadeLastTargetWords=0;
  let _streamFadeLastArrivalMs=0;
  let _streamFadeArrivalWps=0;
  let _streamFadeLatestAnimationEndAt=0;
  let _streamFadeAppendOffset=0;
  let _streamFadeVisibleWords=0;
  let _streamFadeHoldUntilMs=0;
  let _streamFadeCurrentMs=200;
  let _streamFadeReduceMotionMql=null;
  let _streamFadeReduceMotion=false;
  let _streamFadeReduceMotionOnChange=null;
  const _STREAM_FADE_MS=200;
  const _STREAM_FADE_MAX_MS=350;
  const _STREAM_FADE_STAGGER_MS=16;
  const _STREAM_FADE_DONE_MAX_MS=320;
  const _streamFadeEnabledForStream=window._fadeTextEffect===true;

  // rAF-throttled rendering: buffer tokens, render at most once per frame
  let _renderPending=false;
  // Extract display text from assistantText, stripping completed thinking blocks
  // and hiding content still inside an open thinking block.
  function _stripXmlToolCalls(s){
    // Strip <function_calls>...</function_calls> blocks (DeepSeek XML tool syntax).
    // These are processed as tool calls server-side; showing them raw in the bubble
    // looks broken. Also handles orphaned opening tags mid-stream. (#702)
    // Also handles DSML-prefixed variants from DeepSeek/Bedrock, including
    // spacing variants like "<｜DSML |function_calls" and truncated prefixes.
    if(!s) return s;
    const lo=String(s).toLowerCase();
    if(lo.indexOf('function_calls')===-1 && lo.indexOf('dsml')===-1) return s;
    // Support both plain <function_calls> and DSML-prefixed variants.
    s=s.replace(/<(?:\s*｜\s*DSML\s*[｜|]\s*)?function_calls>[\s\S]*?<\/(?:\s*｜\s*DSML\s*[｜|]\s*)?function_calls>/gi,'');
    // Also remove truncated opening tags (missing closing ">" at stream tail).
    s=s.replace(/<(?:\s*｜\s*DSML\s*[｜|]\s*)?function_calls(?:>|$)[\s\S]*$/i,'');
    // Remove malformed DSML tag fragments like "<｜DSML |" that can leak in tokens.
    s=s.replace(/<\s*｜\s*DSML\s*[｜|]\s*/gi,'');
    return s.trim();
  }
  function _streamDisplay(){
    const raw=_stripXmlToolCalls(assistantText);
    // Always run think-block stripping even when reasoningText is populated.
    // Some providers emit reasoning content via on_reasoning AND wrap it in
    // <think> tags in the token stream — the early-return caused the thinking
    // card and main response to show identical content (closes #852).
    for(const {open,close} of _thinkPairs){
      // Trim leading whitespace before checking for the open tag — some models
      // (e.g. MiniMax) emit newlines before <think>.
      const trimmed=raw.trimStart();
      if(trimmed.startsWith(open)){
        const ci=trimmed.indexOf(close,open.length);
        if(ci!==-1){
          // Thinking block complete — strip it, show the rest
          return trimmed.slice(ci+close.length).replace(/^\s+/,'');
        }
        // Still inside thinking block — show placeholder
        return '';
      }
      // Hide partial tag prefixes while streaming so users don't see
      // `<thi`, `<think`, etc. before the model finishes the token.
      if(open.startsWith(trimmed)) return '';
    }
    return raw;
  }
  function _parseStreamState(){
    const raw=_stripXmlToolCalls(assistantText);
    if(reasoningText){
      return {thinkingText:liveReasoningText, displayText:_streamDisplay(), inThinking:false};
    }
    for(const {open,close} of _thinkPairs){
      const trimmed=raw.trimStart();
      if(trimmed.startsWith(open)){
        const ci=trimmed.indexOf(close,open.length);
        if(ci!==-1){
          return {
            thinkingText: trimmed.slice(open.length, ci).trim(),
            displayText: trimmed.slice(ci+close.length).replace(/^\s+/,''),
            inThinking:false,
          };
        }
        return {
          thinkingText: trimmed.slice(open.length).trim(),
          displayText:'',
          inThinking:true,
        };
      }
      if(open.startsWith(trimmed)){
        return {thinkingText:'', displayText:'', inThinking:true};
      }
    }
    return {thinkingText:'', displayText:raw, inThinking:false};
  }
  function _renderLiveThinking(parsed){
    if(window._showThinking===false){removeThinking();return;}
    const text=(parsed&&parsed.thinkingText)||'';
    if(text||(parsed&&parsed.inThinking)){
      if(typeof updateThinking==='function') updateThinking(text||'Thinking…');
      else appendThinking();
      return;
    }
    // Only remove thinking if we're not in an active reasoning phase.
    // When reasoningText is set but liveReasoningText was just reset (post-tool),
    // don't wipe the finalized thinking card — it has no id anymore so
    // removeThinking() won't find it anyway, but guard explicitly.
    if(!reasoningText) removeThinking();
  }
  // Helper: create (or recreate) the smd parser bound to a given DOM element.
  // Called when assistantBody is first created and after each tool-call segment reset.
  function _smdNewParser(el, fade=false){
    _smdWrittenLen=0;
    _smdWrittenText='';
    if(!window.smd){_smdParser=null;return;}
    const renderer=fade ? _streamFadeRenderer(el) : window.smd.default_renderer(el);
    _smdParser=window.smd.parser(renderer);
  }
  // Helper: end the current smd parser (flushes remaining state) and null it out.
  function _smdEndParser(){
    if(_smdParser&&window.smd){
      try{window.smd.parser_end(_smdParser);}catch(_){}
      // parser_end may flush remaining markdown that creates new links/images —
      // re-sanitize the body before the DOM is handed off to highlightCode / renderMessages.
      if(assistantBody){_sanitizeSmdLinks(assistantBody);}
    }
    _smdParser=null;
    _smdWrittenLen=0;
    _smdWrittenText='';
  }
  // Helper: feed new displayText delta to the smd parser.
  // Only feeds chars beyond what has already been written (_smdWrittenLen).
  function _smdWrite(displayText, fade=false){
    if(!_smdParser||!window.smd) return;
    displayText=String(displayText||'');
    // Self-heal desyncs: if displayText no longer starts with what we've already
    // written (e.g. due to stream sanitization/tag stripping), incremental slicing
    // can skip characters. Rebuild parser from the full current displayText.
    if(_smdWrittenText && !displayText.startsWith(_smdWrittenText)){
      _smdParser=null;
      _smdWrittenLen=0;
      _smdWrittenText='';
      if(assistantBody) assistantBody.innerHTML='';
      _smdNewParser(assistantBody,fade);
      if(!_smdParser) return;
    }
    const delta=displayText.slice(_smdWrittenText.length);
    if(!delta) return;
    try{window.smd.parser_write(_smdParser,delta);}catch(_){}
    _smdWrittenLen=displayText.length;
    _smdWrittenText=displayText;
    // streaming-markdown does NOT sanitize URL schemes. The default live path
    // scans after writes; fade mode blocks unsafe href/src in its renderer.set_attr.
    if(assistantBody&&!fade){_sanitizeSmdLinks(assistantBody);}
  }
  // Allowed URL schemes for anchors and images rendered from agent-streamed markdown.
  // Matches the effective allowlist of renderMd() (http/https via regex + relative).
  const _SMD_SAFE_URL_RE=/^(?:https?:|mailto:|tel:|\/|#|\?|\.)/i;
  function _sanitizeSmdLinks(root){
    if(!root||!root.querySelectorAll) return;
    const _a=root.querySelectorAll('a[href]');
    for(let i=0;i<_a.length;i++){
      const n=_a[i],v=n.getAttribute('href')||'';
      if(!_SMD_SAFE_URL_RE.test(v)){n.removeAttribute('href');n.setAttribute('data-blocked-scheme','1');}
    }
    const _im=root.querySelectorAll('img[src]');
    for(let i=0;i<_im.length;i++){
      const n=_im[i],v=n.getAttribute('src')||'';
      if(!_SMD_SAFE_URL_RE.test(v)){n.removeAttribute('src');n.setAttribute('data-blocked-scheme','1');}
    }
  }

  function _resetStreamFadeState(){
    _streamFadeVisibleText='';
    _streamFadeLastTickMs=0;
    _streamFadeWordCarry=0;
    _streamFadeStartedAt=0;
    _streamFadeLastTargetWords=0;
    _streamFadeLastArrivalMs=0;
    _streamFadeArrivalWps=0;
    _streamFadeLatestAnimationEndAt=0;
    _streamFadeAppendOffset=0;
    _streamFadeVisibleWords=0;
    _streamFadeHoldUntilMs=0;
    _streamFadeCurrentMs=_STREAM_FADE_MS;
  }
  function _cancelAnimationFramePendingStreamRender(){
    if(_pendingRafHandle===null) return;
    cancelAnimationFrame(_pendingRafHandle);
    clearTimeout(_pendingRafHandle);
    _pendingRafHandle=null;
    _renderPending=false;
  }
  function _shouldUseStreamFade(){
    return _streamFadeEnabledForStream;
  }
  function _streamFadeSkipNode(node){
    if(!node||node.nodeType!==1) return false;
    const tag=(node.tagName||'').toLowerCase();
    return tag==='pre'||tag==='code'||tag==='script'||tag==='style'||tag==='textarea'||tag==='svg'||tag==='math';
  }
  function _streamFadeReduceMotionEnabled(){
    if(!window.matchMedia) return false;
    if(!_streamFadeReduceMotionMql){
      _streamFadeReduceMotionMql=window.matchMedia('(prefers-reduced-motion: reduce)');
      _streamFadeReduceMotion=!!_streamFadeReduceMotionMql.matches;
      _streamFadeReduceMotionOnChange=e=>{_streamFadeReduceMotion=!!e.matches;};
      try{_streamFadeReduceMotionMql.addEventListener('change',_streamFadeReduceMotionOnChange);}
      catch(_){try{_streamFadeReduceMotionMql.addListener(_streamFadeReduceMotionOnChange);}catch(_){}}
    }
    return _streamFadeReduceMotion;
  }
  function _streamFadeCleanupReduceMotionListener(){
    if(!_streamFadeReduceMotionMql||!_streamFadeReduceMotionOnChange) return;
    try{_streamFadeReduceMotionMql.removeEventListener('change',_streamFadeReduceMotionOnChange);}
    catch(_){try{_streamFadeReduceMotionMql.removeListener(_streamFadeReduceMotionOnChange);}catch(_){}}
    _streamFadeReduceMotionMql=null;
    _streamFadeReduceMotionOnChange=null;
  }
  function _streamFadeBindCleanup(el){
    if(!el||el._streamFadeCleanupBound) return;
    el._streamFadeCleanupBound=true;
    el.addEventListener('animationend',e=>{
      const span=e.target;
      if(!span||!span.classList||!span.classList.contains('stream-fade-word')) return;
      span.replaceWith(document.createTextNode(span.textContent||''));
    });
  }
  function _streamFadeRenderer(el){
    _streamFadeBindCleanup(el);
    const renderer=window.smd.default_renderer(el);
    const baseAddText=renderer.add_text;
    const baseSetAttr=renderer.set_attr;
    renderer.add_text=(data,text)=>{
      const parent=data&&data.nodes&&data.nodes[data.index];
      if(!parent||_streamFadeSkipNode(parent)){baseAddText(data,text);return;}
      const frag=document.createDocumentFragment();
      const wordRe=/(\S+)(\s*)/g;
      const value=String(text||'');
      const reduceMotion=_streamFadeReduceMotionEnabled();
      const appendStartedAt=performance.now();
      let last=0, match, changed=false;
      while((match=wordRe.exec(value))){
        if(match.index>last) frag.appendChild(document.createTextNode(value.slice(last,match.index)));
        if(reduceMotion){
          frag.appendChild(document.createTextNode(match[1]));
          if(match[2]) frag.appendChild(document.createTextNode(match[2]));
          last=match.index+match[0].length;
          changed=true;
          continue;
        }
        const span=document.createElement('span');
        span.className='stream-fade-word is-new';
        const fadeMs=_streamFadeCurrentMs||_STREAM_FADE_MS;
        const delayMs=_streamFadeAppendOffset*_STREAM_FADE_STAGGER_MS;
        span.style.animationDelay=delayMs+'ms';
        if(fadeMs!==_STREAM_FADE_MS) span.style.setProperty('--stream-fade-ms',fadeMs+'ms');
        span.textContent=match[1];
        frag.appendChild(span);
        _streamFadeAppendOffset+=1;
        _streamFadeLatestAnimationEndAt=Math.max(_streamFadeLatestAnimationEndAt,appendStartedAt+delayMs+fadeMs);
        if(match[2]) frag.appendChild(document.createTextNode(match[2]));
        last=match.index+match[0].length;
        changed=true;
      }
      if(!changed){baseAddText(data,text);return;}
      if(last<value.length) frag.appendChild(document.createTextNode(value.slice(last)));
      parent.appendChild(frag);
    };
    renderer.set_attr=(data,attr,value)=>{
      const isHref=window.smd&&attr===window.smd.HREF;
      const isSrc=window.smd&&attr===window.smd.SRC;
      if((isHref||isSrc)&&!_SMD_SAFE_URL_RE.test(String(value||''))){
        const node=data&&data.nodes&&data.nodes[data.index];
        if(node&&node.setAttribute) node.setAttribute('data-blocked-scheme','1');
        return;
      }
      baseSetAttr(data,attr,value);
    };
    return renderer;
  }
  function _streamFadeWordCountOf(text){
    const m=String(text||'').match(/\S+/g);
    return m?m.length:0;
  }
  function _streamFadePauseAfter(text, paragraphBreakIndex){
    if(paragraphBreakIndex>=0) return 90;
    const trimmed=String(text||'').trimEnd();
    if(/[.!?]["')\]]*$/.test(trimmed)) return 45;
    if(/[:;]["')\]]*$/.test(trimmed)) return 30;
    return 0;
  }
  function _streamFadeNextText(targetText){
    targetText=String(targetText||'');
    const now=performance.now();
    if(!targetText){
      const hadVisible=!!_streamFadeVisibleText;
      _resetStreamFadeState();
      return {text:'', caughtUp:true, changed:hadVisible};
    }
    if(!_streamFadeVisibleText||!targetText.startsWith(_streamFadeVisibleText)){
      // Markdown/tool stripping can rewrite the visible prefix. Reset safely rather than
      // trying to animate across incompatible strings or stale word birth timestamps.
      _resetStreamFadeState();
    }
    if(!_streamFadeLastTickMs){
      _streamFadeLastTickMs=now;
      _streamFadeStartedAt=now;
    }
    if(_streamFadeVisibleText===targetText) return {text:_streamFadeVisibleText,caughtUp:true,changed:false};

    const remaining=targetText.slice(_streamFadeVisibleText.length);
    const backlogWords=_streamFadeWordCountOf(remaining);
    const targetWords=_streamFadeVisibleWords+backlogWords;
    const elapsedMs=Math.max(16,Math.min(120,now-_streamFadeLastTickMs));
    _streamFadeLastTickMs=now;

    // OpenWebUI fades the actual arriving tokens, so long/fast responses naturally
    // appear to accelerate. Hermes has a playout buffer, so track incoming word
    // velocity and play out faster than it instead of using a metronomic cadence.
    // LLM telemetry is usually tokens/sec, but the UI reveals words. A fixed word
    // cadence can look stuck even when token throughput is high, so combine:
    //   1) live target-word arrival velocity, 2) backlog pressure, 3) time ramp.
    if(!_streamFadeLastArrivalMs){
      _streamFadeLastArrivalMs=now;
      _streamFadeLastTargetWords=targetWords;
    } else if(targetWords>_streamFadeLastTargetWords){
      const arrivalElapsedMs=Math.max(16, now-_streamFadeLastArrivalMs);
      const instantArrivalWps=(targetWords-_streamFadeLastTargetWords)*1000/arrivalElapsedMs;
      // EWMA smooths bursty token chunks without hiding sustained fast output.
      _streamFadeArrivalWps=_streamFadeArrivalWps
        ? (_streamFadeArrivalWps*0.65 + instantArrivalWps*0.35)
        : instantArrivalWps;
      _streamFadeLastArrivalMs=now;
      _streamFadeLastTargetWords=targetWords;
    } else if(targetWords<_streamFadeLastTargetWords){
      _streamFadeLastTargetWords=targetWords;
      _streamFadeLastArrivalMs=now;
      _streamFadeArrivalWps=0;
    }

    if(now<_streamFadeHoldUntilMs){
      return {text:_streamFadeVisibleText,caughtUp:false,changed:false};
    }

    const streamAgeSeconds=Math.max(0, (now-(_streamFadeStartedAt||now))/1000);
    const baseWps=22 + Math.min(streamAgeSeconds*2.5, 28); // 22 → 50 wps over long answers
    const arrivalWps=_streamFadeArrivalWps ? Math.min(_streamFadeArrivalWps*1.05 + 8, 160) : 0;
    const backlogWps=backlogWords>0 ? Math.min(22 + backlogWords*1.1, 160) : 0;
    const wordsPerSecond=Math.min(160, Math.max(baseWps, arrivalWps, backlogWps));
    const speedFadeRatio=Math.max(0,Math.min(1,(wordsPerSecond-50)/(160-50)));
    _streamFadeCurrentMs=Math.round(_STREAM_FADE_MS+(_STREAM_FADE_MAX_MS-_STREAM_FADE_MS)*speedFadeRatio);

    _streamFadeWordCarry+=elapsedMs*wordsPerSecond/1000;
    if(!_streamFadeVisibleText) _streamFadeWordCarry=Math.max(_streamFadeWordCarry,1);
    let wordsToReveal=Math.floor(_streamFadeWordCarry);
    // At very high throughput, cap each frame to a small readable wave. Sustained
    // playback still catches up, but whole paragraphs no longer pop in at once.
    const waveCap=backlogWords>=160?3:2;
    wordsToReveal=Math.min(wordsToReveal,waveCap,backlogWords);
    if(wordsToReveal<1) return {text:_streamFadeVisibleText,caughtUp:false,changed:false};
    _streamFadeWordCarry=Math.max(0,_streamFadeWordCarry-wordsToReveal);

    let cut=0;
    const wordRe=/(\s*\S+\s*)/g;
    let match;
    while(wordsToReveal>0&&(match=wordRe.exec(remaining))){
      cut=wordRe.lastIndex;
      wordsToReveal-=1;
    }
    if(cut<=0) cut=Math.min(remaining.length,4);
    const chunk=remaining.slice(0,cut);
    const paragraphMatch=chunk.match(/\n\s*\n/);
    const paragraphBreak=paragraphMatch ? paragraphMatch.index : -1;
    if(paragraphMatch) cut=paragraphBreak+paragraphMatch[0].length;
    const revealed=remaining.slice(0,cut);
    _streamFadeVisibleText+=revealed;
    _streamFadeVisibleWords+=_streamFadeWordCountOf(revealed);
    const pauseMs=_streamFadePauseAfter(revealed,paragraphBreak);
    if(pauseMs) _streamFadeHoldUntilMs=now+pauseMs;
    if(_streamFadeVisibleText.length>targetText.length) _streamFadeVisibleText=targetText;
    return {text:_streamFadeVisibleText,caughtUp:_streamFadeVisibleText===targetText,changed:true};
  }
  function _renderStreamingFadeMarkdown(displayText){
    if(!assistantBody) return true;
    const next=_streamFadeNextText(displayText);
    if(!next.changed) return next.caughtUp;
    assistantBody.classList.add('stream-fade-active');
    if(!_smdParser&&window.smd){
      if(_smdReconnect){assistantBody.innerHTML='';_smdReconnect=false;}
      _smdNewParser(assistantBody,true);
    }
    if(_smdParser){
      _streamFadeAppendOffset=0;
      _smdWrite(next.text,true);
    }else{
      assistantBody.innerHTML=renderMd ? renderMd(next.text||'') : esc(next.text||'');
      _sanitizeSmdLinks(assistantBody);
    }
    return next.caughtUp;
  }
  function _streamFadeCurrentDisplayText(){
    const parsed=_parseStreamState();
    return segmentStart===0
      ? parsed.displayText
      : _stripXmlToolCalls(assistantText.slice(segmentStart));
  }
  function _drainStreamFadeBeforeDone(onDone){
    const step=()=>{
      if(!assistantBody){onDone();return;}
      const target=_streamFadeCurrentDisplayText();
      const caughtUp=_renderStreamingFadeMarkdown(target);
      scrollIfPinned();
      if(caughtUp){
        // parser_end can flush pending markdown text; include that final text in
        // the fade wait instead of replacing it immediately in renderMessages().
        if(_smdParser) _smdEndParser();
        // Let the last released words visibly finish their stagger + fade before
        // the final renderMessages() DOM replacement removes the live spans.
        const remainingAnimationMs=Math.max(_STREAM_FADE_MS, _streamFadeLatestAnimationEndAt-performance.now());
        setTimeout(onDone, Math.min(remainingAnimationMs, _STREAM_FADE_DONE_MAX_MS));
        return;
      }
      setTimeout(()=>requestAnimationFrame(step), 33);
    };
    step();
  }
  function _resetAssistantSegment(){
    assistantRow=null;
    assistantBody=null;
    segmentStart=assistantText.length;
    _freshSegment=true;
    _smdEndParser();
    _resetStreamFadeState();
  }

  let _lastRenderMs=0;
  function _scheduleRender(){
    if(_renderPending) return;
    if(_streamFinalized) return; // Bug A: don't schedule new rAF after stream finalized
    _renderPending=true;
    // Cap render rate to ~15fps. The browser's rAF fires at 60fps, but each DOM
    // update takes 50-150ms on large sessions. During GC pauses, rAF callbacks
    // accumulate and then execute all at once, blocking the main thread for
    // multi-second stretches and crashing the renderer (Chrome error code 4/5).
    // Throttling to 66ms intervals prevents this pileup without noticeable
    // visual degradation — streaming text updates still feel immediate.
    // performance.now() is monotonic so tab suspend/resume and NTP adjustments
    // can't produce negative or enormous deltas.
    const sinceLastMs=performance.now()-_lastRenderMs;
    const _doRender=()=>{
      _pendingRafHandle=null;
      _renderPending=false;
      // Guard: a pending setTimeout+rAF can outlive stream finalization.
      if(_streamFinalized) return;
      _lastRenderMs=performance.now();
      const parsed=_parseStreamState();
      _renderLiveThinking(parsed);
      if(assistantBody){
        const displayText = segmentStart===0
          ? parsed.displayText                          // first segment: uses think-tag stripping
          : _stripXmlToolCalls(assistantText.slice(segmentStart));
        if(_shouldUseStreamFade()){
          const caughtUp=_renderStreamingFadeMarkdown(displayText);
          if(!caughtUp&&!_streamFinalized){
            setTimeout(()=>_scheduleRender(), 33);
          }
        } else {
          assistantBody.classList.remove('stream-fade-active');
          _resetStreamFadeState();
          if(!_smdParser&&window.smd){
            // On reconnect: prior content in assistantBody came from a different smd parser run.
            // Clear it and start fresh — renderMessages() on done will restore the full content.
            if(_smdReconnect){assistantBody.innerHTML='';_smdReconnect=false;}
            _smdNewParser(assistantBody);
          }
          if(_smdParser){
            _smdWrite(displayText);
          } else {
            // Fallback: smd not loaded yet, reconnect session, or smd unavailable — use renderMd
            // for every live segment. Without this, the first segment inserts raw
            // parsed.displayText and users see unformatted markdown until done.
            const fallbackText = segmentStart===0
              ? parsed.displayText
              : _stripXmlToolCalls(assistantText.slice(segmentStart));
            assistantBody.innerHTML = renderMd ? renderMd(fallbackText) : esc(fallbackText);
          }
        }
      }
      scrollIfPinned();
    };
    const frameIntervalMs=_shouldUseStreamFade()?33:66;
    if(sinceLastMs>=frameIntervalMs){
      _pendingRafHandle=requestAnimationFrame(_doRender);
    } else {
      _pendingRafHandle=setTimeout(()=>requestAnimationFrame(_doRender), frameIntervalMs-sinceLastMs);
    }
  }

  function _wireSSE(source){
    // Note on #631 Bug B: the original PR description stated the server
    // "replays buffered token events" on reconnect, and proposed resetting
    // the accumulators here so the re-sent tokens wouldn't double the prefix.
    // That is NOT how the server actually works — api/routes._handle_sse_stream
    // reads a one-shot queue.Queue() that delivers each event to exactly one
    // consumer; a reconnect picks up from the current queue position and gets
    // only events produced during the outage.  Resetting the accumulators here
    // would wipe the already-displayed content and restart the response from
    // the first post-reconnect token — a real data-loss regression.
    //
    // The "doubled response" / "stuck cursor" symptom is fully explained by
    // Bug A (trailing rAF after `done` inserting a new live-turn wrapper) —
    // the fixes below (_streamFinalized guard + cancelAnimationFrame in the
    // terminal handlers) address it without needing a reset here.

    source.addEventListener('token',e=>{
      if(_terminalStateReached||_streamFinalized) return;
      if(!S.session||S.session.session_id!==activeSid) return;
      const d=JSON.parse(e.data);
      assistantText+=d.text;
      syncInflightAssistantMessage();
      if(!S.session||S.session.session_id!==activeSid) return;
      const parsed=_parseStreamState();
      if(String((parsed&&parsed.displayText)||'').trim()||assistantRow) ensureAssistantRow();
      _scheduleRender();
    });

    source.addEventListener('interim_assistant',e=>{
      if(_terminalStateReached||_streamFinalized) return;
      if(!S.session||S.session.session_id!==activeSid) return;
      const d=JSON.parse(e.data);
      const visible=String(d&&d.text?d.text:'').trim();
      const alreadyStreamed=!!(d&&d.already_streamed);
      if(!visible){
        return;
      }
      if(alreadyStreamed){
        _resetAssistantSegment();
        return;
      }
      assistantText+=visible;
      syncInflightAssistantMessage();
      if(!S.session||S.session.session_id!==activeSid) return;
      const parsed=_parseStreamState();
      if(String((parsed&&parsed.displayText)||'').trim()||assistantRow) ensureAssistantRow();
      _scheduleRender();
    });

    source.addEventListener('reasoning',e=>{
      if(_terminalStateReached||_streamFinalized) return;
      const d=JSON.parse(e.data);
      reasoningText += d.text || '';
      liveReasoningText += d.text || '';
      syncInflightAssistantMessage();
      if(!S.session||S.session.session_id!==activeSid) return;
      // Render thinking card synchronously — not via rAF — so the DOM is
      // up-to-date before a 'tool' event in the same microtask batch calls
      // finalizeThinkingCard(). The old rAF-only path caused a race where
      // the thinking row was still a spinner when finalized.
      if(window._showThinking!==false){
        if(typeof updateThinking==='function') updateThinking(liveReasoningText||'Thinking…');
        else appendThinking(liveReasoningText);
      }
      _scheduleRender();
    });

    source.addEventListener('tool',e=>{
      const d=JSON.parse(e.data);
      if(d.name==='clarify') return;
      const tc={name:d.name, preview:d.preview||'', args:d.args||{}, snippet:'', done:false, tid:d.tid||`live-${Date.now()}-${Math.random().toString(36).slice(2,8)}`};
      const inflight = INFLIGHT[activeSid] || (INFLIGHT[activeSid] = {
        messages:[...S.messages],
        uploaded:[],
        toolCalls:[]
      });
      if(!Array.isArray(inflight.toolCalls)) inflight.toolCalls=[];
      INFLIGHT[activeSid].toolCalls.push(tc);
      S.toolCalls=INFLIGHT[activeSid].toolCalls;
      persistInflightState();

      if(!S.session||S.session.session_id!==activeSid) return;
      // NOTE: don't removeThinking() here — keep the thinking card visible
      // above the tool card so the turn reads top-to-bottom as:
      // user → thinking → tool cards → response. Removing it caused the card
      // to be re-created below everything when reasoning resumed post-tool.
      if(typeof finalizeThinkingCard==='function') finalizeThinkingCard();
      liveReasoningText='';
      const oldRow=$('toolRunningRow');if(oldRow)oldRow.remove();
      appendLiveToolCard(tc);
      // Reset the live assistant row reference so that any text tokens arriving
      // after this tool call create a NEW segment appended below the tool card,
      // rather than updating the old segment that sits above it in the DOM.
      _freshSegment=true;
      _smdEndParser();
      _resetAssistantSegment();
      scrollIfPinned();
    });

    source.addEventListener('tool_complete',e=>{
      const d=JSON.parse(e.data);
      if(d.name==='clarify') return;
      const inflight=INFLIGHT[activeSid];
      if(!inflight) return;
      if(!Array.isArray(inflight.toolCalls)) inflight.toolCalls=[];
      let tc=null;
      for(let i=inflight.toolCalls.length-1;i>=0;i--){
        const cur=inflight.toolCalls[i];
        if(cur&&cur.done===false&&(!d.name||cur.name===d.name)){
          tc=cur;
          break;
        }
      }
      if(!tc){
        tc={name:d.name||'tool', preview:d.preview||'', args:d.args||{}, snippet:'', done:true};
        inflight.toolCalls.push(tc);
      }
      tc.preview=d.preview||tc.preview||'';
      tc.args=d.args||tc.args||{};
      tc.done=true;
      tc.is_error=!!d.is_error;
      if(d.duration!==undefined) tc.duration=d.duration;
      S.toolCalls=inflight.toolCalls;
      persistInflightState();
      if(!S.session||S.session.session_id!==activeSid) return;
      appendLiveToolCard(tc);
      scrollIfPinned();
    });

    source.addEventListener('approval',e=>{
      const d=JSON.parse(e.data);
      showApprovalForSession(activeSid, d, 1);
      playNotificationSound();
      sendBrowserNotification('Approval required',d.description||'Tool approval needed');
    });

    source.addEventListener('clarify',e=>{
      const d=JSON.parse(e.data);
      showClarifyForSession(activeSid, d);
      playNotificationSound();
      sendBrowserNotification('Clarification needed',d.question||'Tool clarification needed');
    });

    source.addEventListener('title',e=>{
      let d={};
      try{ d=JSON.parse(e.data||'{}'); }catch(_){}
      if((d.session_id||activeSid)!==activeSid) return;
      applySessionTitleUpdate(activeSid, d.title);
    });

    source.addEventListener('title_status',e=>{
      let d={};
      try{ d=JSON.parse(e.data||'{}'); }catch(_){}
      if((d.session_id||activeSid)!==activeSid) return;
      try{
        console.info('[title]', {
          status:String(d.status||''),
          reason:String(d.reason||''),
          title:String(d.title||''),
          raw_preview:String(d.raw_preview||''),
          session_id:String(d.session_id||activeSid)
        });
      }catch(_){}
    });

    function _resolveGoalMessage(d){
      const key=String(d && d.message_key ? d.message_key : '').trim();
      const args=Array.isArray(d && d.message_args) ? d.message_args : [];
      const raw=String(d&&d.message||'').trim();
      if(key && typeof t==='function'){
        try{
          const translated=String(t(key,...args));
          if(translated && translated!==key)return translated;
        }catch(_){}
      }
      return raw;
    }

    source.addEventListener('goal',e=>{
      try{
        const d=JSON.parse(e.data||'{}');
        if((d.session_id||activeSid)!==activeSid) return;
        const goalState=String(d.state||'').trim();
        const goalEvaluatingMessage=t('goal_evaluating_progress');
        if(goalState==='evaluating'){
          setComposerStatus(goalEvaluatingMessage);
          return;
        }
        const msg=_resolveGoalMessage(d);
        if(!msg)return;
        _latestGoalStatus={message:msg,decision:d.decision||null,state:goalState||null};
        setComposerStatus(msg);
        showToast(msg.split('\n')[0],2600);
      }catch(_){}
    });

    source.addEventListener('goal_continue',e=>{
      try{
        const d=JSON.parse(e.data||'{}');
        const sid=d.session_id||activeSid;
        const continuation_prompt=String(d.continuation_prompt||d.text||'').trim();
        if(!continuation_prompt||sid!==activeSid)return;
        _pendingGoalContinuation={
          sid,
          text:continuation_prompt,
          model:S.session&&S.session.model||'',
          model_provider:S.session&&S.session.model_provider||null,
          profile:S.activeProfile||'default',
        };
        const toast=t('goal_continuing_toast');
        const cmsg=_resolveGoalMessage(d);
        showToast((toast&&cmsg&&cmsg!==toast)?cmsg.split('\n')[0]:toast,2200);
      }catch(_){}
    });

    source.addEventListener('done',e=>{
      _terminalStateReached=true;
      if(_persistTimer){clearTimeout(_persistTimer);_persistTimer=null;}
      const _doneData=JSON.parse(e.data);
      const _finishDone=()=>{
        // Bug A fix: cancel any pending rAF and mark stream finalized before
        // the DOM is settled by renderMessages, so no trailing token/reasoning rAF
        // can reintroduce a stale thinking card or duplicate content.
        _streamFinalized=true;
        _cancelAnimationFramePendingStreamRender();
        _streamFadeCleanupReduceMotionListener();
        if(typeof finalizeThinkingCard==='function') finalizeThinkingCard();
        // Finalize smd parser — flushes any remaining buffered markdown state
        // and runs Prism + copy buttons on the live segment before the DOM is replaced
        if(assistantBody){
          const _finBody=assistantBody;
          _smdEndParser();
          requestAnimationFrame(()=>{
            if(typeof highlightCode==='function') highlightCode(_finBody);
            if(typeof addCopyButtons==='function') addCopyButtons(_finBody);
            if(typeof renderKatexBlocks==='function') renderKatexBlocks();
          });
        } else {
          _smdEndParser();
        }
        const d=_doneData;
        const isActiveSession=_isSessionCurrentPane(activeSid);
        const isSessionViewed=_isSessionActivelyViewed(activeSid);
        const completedSession=d.session||{session_id:activeSid};
        const completedSid=completedSession.session_id||activeSid;
        if(!isSessionViewed && typeof _markSessionCompletionUnread==='function'){
          _markSessionCompletionUnread(completedSid, completedSession.message_count);
        }
        _clearOwnerInflightState();
        if(typeof _markSessionCompletedInList==='function'){
          _markSessionCompletedInList(completedSession, activeSid);
        }
        _clearApprovalForOwner();
        _clearClarifyForOwner('terminal');
        const shouldFollowOnDone=isActiveSession&&((typeof _shouldFollowMessagesOnDomReplace==='function')
          ? _shouldFollowMessagesOnDomReplace()
          : (typeof _isMessagePaneNearBottom==='function'&&_isMessagePaneNearBottom(1200)));
        if(isActiveSession){
          S.activeStreamId=null;
        }
        if(isActiveSession){
          // Capture previous session totals BEFORE overwriting S.session with the new
          // cumulative values from the done event. prevIn/prevOut are the totals as of
          // the start of this turn; curIn/curOut are the full post-turn totals — the
          // delta is the per-turn usage for #1159.
          const _prevIn=(S.session&&S.session.input_tokens)||0;
          const _prevOut=(S.session&&S.session.output_tokens)||0;
          const _prevCost=(S.session&&S.session.estimated_cost)||0;
          S.session=d.session;S.messages=d.session.messages||[];if(typeof _messagesTruncated!=='undefined')_messagesTruncated=!!d.session._messages_truncated;
          if(S.session&&S.session.session_id){
            localStorage.setItem('hermes-webui-session',S.session.session_id);
            if(typeof _setActiveSessionUrl==='function') _setActiveSessionUrl(S.session.session_id);
          }
          if(
            window._compressionUi&&window._compressionUi.automatic&&
            window._compressionUi.sessionId===activeSid&&
            d.session&&d.session.session_id
          ){
            window._compressionUi={...window._compressionUi, sessionId:d.session.session_id};
          }
          // Find the last assistant message once for both reasoning persistence and timestamp
          const lastAsst=[...S.messages].reverse().find(m=>m.role==='assistant');
          // Persist reasoning trace so thinking card survives page reload
          if(reasoningText&&lastAsst&&!lastAsst.reasoning) lastAsst.reasoning=reasoningText;
          // Stamp _ts on the last assistant message if it has no timestamp
          if(lastAsst&&!lastAsst._ts&&!lastAsst.timestamp) lastAsst._ts=Date.now()/1000;
          if(d.usage){
            S.lastUsage=d.usage;_syncCtxIndicator(d.usage);
            // #503 — compute per-turn cost delta and attach to last assistant message
            if(lastAsst){
              const prevIn=_prevIn;
              const prevOut=_prevOut;
              const prevCost=_prevCost;
              const curIn=d.usage.input_tokens||0;
              const curOut=d.usage.output_tokens||0;
              const curCost=d.usage.estimated_cost||0;
              // Only set delta if values actually increased (skip no-op turns)
              if(curIn>prevIn||curOut>prevOut){
                lastAsst._turnUsage={
                  input_tokens:Math.max(0,curIn-prevIn),
                  output_tokens:Math.max(0,curOut-prevOut),
                  estimated_cost:Math.max(0,curCost-prevCost),
                };
              }
              if(typeof d.usage.duration_seconds==='number'){
                lastAsst._turnDuration=d.usage.duration_seconds;
              }
              if(typeof d.usage.tps==='number'&&d.usage.tps>0){
                lastAsst._turnTps=d.usage.tps;
              }
              if(d.usage.gateway_routing){
                lastAsst._gatewayRouting=d.usage.gateway_routing;
                if(S.session)S.session.gateway_routing=d.usage.gateway_routing;
                if(S.session&&Array.isArray(S.session.gateway_routing_history))S.session.gateway_routing_history.push(d.usage.gateway_routing);
                else if(S.session)S.session.gateway_routing_history=[d.usage.gateway_routing];
              }
            }
          }
          if(d.session.tool_calls&&d.session.tool_calls.length){
            S.toolCalls=d.session.tool_calls.map(tc=>({...tc,done:true}));
          } else {
            S.toolCalls=S.toolCalls.map(tc=>({...tc,done:true}));
          }
          if(typeof _copyActivityDisclosureState==='function'&&lastAsst){
            const assistantIdx=S.messages.indexOf(lastAsst);
            if(assistantIdx>=0) _copyActivityDisclosureState('live:'+streamId, 'assistant:'+assistantIdx);
          }
          if(uploaded.length){
            const lastUser=[...S.messages].reverse().find(m=>m.role==='user');
            if(lastUser)lastUser.attachments=uploaded;
          }
          if(_latestGoalStatus&&_latestGoalStatus.message){
            S.messages.push({
              role:'assistant',
              content:String(_latestGoalStatus.message),
              _ts:Date.now()/1000,
              _goalStatus:true,
              _transient:true,
            });
          }
          clearLiveToolCards();
          S.busy=false;
          // No-reply guard (#373): if agent returned nothing, show inline error
          if(!S.messages.some(m=>m.role==='assistant'&&String(m.content||'').trim())&&!assistantText){removeThinking();S.messages.push({role:'assistant',content:'**No response received.** Check your API key and model selection.'});}
          if(isSessionViewed) _markSessionViewed(completedSid, completedSession.message_count ?? S.messages.length);
          syncTopbar();renderMessages({preserveScroll:true});
          if(shouldFollowOnDone&&typeof scrollToBottom==='function') scrollToBottom();
          loadDir('.');
          // TTS auto-read: speak the last assistant response if enabled (#499)
          if(typeof autoReadLastAssistant==='function') setTimeout(()=>autoReadLastAssistant(), 300);
        }
        if(isActiveSession&&_pendingGoalContinuation&&typeof queueSessionMessage==='function'){
          const _goalNext=_pendingGoalContinuation;
          _pendingGoalContinuation=null;
          queueSessionMessage(_goalNext.sid,{
            text:_goalNext.text,
            files:[],
            model:_goalNext.model,
            model_provider:_goalNext.model_provider,
            profile:_goalNext.profile,
          });
          if(typeof updateQueueBadge==='function')updateQueueBadge(_goalNext.sid);
        }
        if(isActiveSession) _queueDrainSid=activeSid;
        renderSessionList();
        _setActivePaneIdleIfOwner();
        playNotificationSound();
        sendBrowserNotification('Response complete',assistantText?assistantText.slice(0,100):'Task finished');
      };
      if(_shouldUseStreamFade()&&assistantBody){
        _cancelAnimationFramePendingStreamRender();
        _drainStreamFadeBeforeDone(_finishDone);
        return;
      }
      _finishDone();
    });

    source.addEventListener('stream_end',e=>{
      _terminalStateReached=true;
      try{
        const d=JSON.parse(e.data||'{}');
        if((d.session_id||activeSid)!==activeSid) return;
      }catch(_){}
      source.close();
    });

    source.addEventListener('pending_steer_leftover',e=>{
      // The agent finished its turn with steer text still stashed (no
      // tool-result boundary fired). Match the CLI's leftover-delivery
      // behaviour: queue the leftover text as a next-turn user message
      // so the existing drain in setBusy(false) ships it.
      try{
        const d=JSON.parse(e.data||'{}');
        const sid=d.session_id||activeSid;
        const txt=String(d.text||'').trim();
        if(!txt||sid!==activeSid) return;
        if(typeof queueSessionMessage==='function'){
          queueSessionMessage(sid,{
            text:txt,files:[],
            model:S.session&&S.session.model||'',
            model_provider:S.session&&S.session.model_provider||null,
            profile:S.activeProfile||'default',
          });
          if(typeof updateQueueBadge==='function') updateQueueBadge(sid);
          showToast(t('steer_leftover_queued'),3000);
        }
      }catch(_){}
    });

    source.addEventListener('compressing',e=>{
      // Context auto-compression is starting. Surface the same calm running
      // compression card as manual /compress while the summarizer LLM call runs.
      if(!S.session||S.session.session_id!==activeSid) return;
      let d={};
      try{ d=JSON.parse(e.data||'{}')||{}; }catch(_){ d={}; }
      if(d.session_id&&d.session_id!==activeSid) return;
      if(typeof setCompressionUi==='function'){
        setCompressionUi({
          sessionId:activeSid,
          phase:'running',
          automatic:true,
          message:d.message||'Auto-compressing context...',
        });
      }
      if(typeof renderMessages==='function') renderMessages({preserveScroll:true});
    });

    source.addEventListener('compressed',e=>{
      // Context was auto-compressed during this turn. Render it through the
      // same transient compression-card path as manual /compress, without
      // inserting a fake assistant message into history or model context.
      if(!S.session||S.session.session_id!==activeSid) return;
      let d={};
      try{ d=JSON.parse(e.data||'{}')||{}; }catch(_){ d={}; }
      if(d.session_id&&d.session_id!==activeSid) return;
      const message=String(d.message||'Context auto-compressed to continue the conversation').trim();
      if(d.usage&&typeof _syncCtxIndicator==='function'){
        S.lastUsage={...(S.lastUsage||{}),...d.usage};
        _syncCtxIndicator(S.lastUsage);
      }
      if(typeof setCompressionUi==='function'){
        setCompressionUi({
          sessionId:activeSid,
          phase:'done',
          automatic:true,
          message,
          summary:{headline:message},
        });
      }
      if(typeof _setCompressionSessionLock==='function') _setCompressionSessionLock(null);
      if(!S.busy&&typeof renderMessages==='function') renderMessages();
      showToast(message||'Context compressed', 8000);
    });

    source.addEventListener('metering',e=>{
      try{
        const d=JSON.parse(e.data||'{}');
        if((d.session_id||activeSid)!==activeSid) return;
        if(d.usage&&typeof _syncCtxIndicator==='function'){
          if(S.session&&S.session.session_id===activeSid){
            S.lastUsage={...(S.lastUsage||{}),...d.usage};
            _syncCtxIndicator(S.lastUsage);
          }
        }
        if(d.estimated===true||d.tps_available!==true||typeof d.tps!=='number'||d.tps<=0){
          if(typeof _setLiveAssistantTps==='function') _setLiveAssistantTps(null);
          return;
        }
        if(typeof _setLiveAssistantTps==='function') _setLiveAssistantTps(d.tps);
      }catch(_){}
    });

    source.addEventListener('apperror',e=>{
      _terminalStateReached=true;
      if(_persistTimer){clearTimeout(_persistTimer);_persistTimer=null;}
      _streamFinalized=true;
      _cancelAnimationFramePendingStreamRender();
      _streamFadeCleanupReduceMotionListener();
      _smdEndParser();
      if(typeof finalizeThinkingCard==='function') finalizeThinkingCard();
      // Application-level error sent explicitly by the server (rate limit, crash, etc.)
      // This is distinct from the SSE network 'error' event below.
      source.close();
      _clearOwnerInflightState();
      _clearApprovalForOwner();
      _clearClarifyForOwner('terminal');
      if(S.session&&S.session.session_id===activeSid){
        S.activeStreamId=null;
        clearLiveToolCards();if(!assistantText)removeThinking();
        try{
          const d=JSON.parse(e.data);
          const isRateLimit=d.type==='rate_limit';
          const isQuotaExhausted=d.type==='quota_exhausted';
          const isAuthMismatch=d.type==='auth_mismatch';
          const isModelNotFound=d.type==='model_not_found';
          const isCancelled=d.type==='cancelled';
          const isInterrupted=d.type==='interrupted';
          const isNoResponse=d.type==='no_response'||d.type==='silent_failure';
          const label=isCancelled?'Task cancelled':isInterrupted?'Response interrupted':isQuotaExhausted?'Out of credits':isRateLimit?'Rate limit reached':isAuthMismatch?(typeof t==='function'?t('provider_mismatch_label'):'Provider mismatch'):isModelNotFound?(typeof t==='function'?t('model_not_found_label'):'Model not found'):isNoResponse?'No response from provider':'Error';
          const hint=d.hint?`\n\n*${d.hint}*`:'';
          const details=d.details?String(d.details).replace(/```/g,'`\u200b``'):'';
          const detailsLabel=isCancelled?'Cancellation details':isInterrupted?'Interruption details':undefined;
          S.messages.push({role:'assistant',content:`**${label}:** ${d.message}${hint}`,provider_details:details,provider_details_label:detailsLabel});
        }catch(_){
          S.messages.push({role:'assistant',content:'**Error:** An error occurred. Check server logs.'});
        }
        _markSessionViewed(activeSid, S.messages.length);
        renderMessages({preserveScroll:true});
      }else if(typeof trackBackgroundError==='function'){
        const _errTitle=(typeof _allSessions!=='undefined'&&_allSessions.find(s=>s.session_id===activeSid)||{}).title||null;
        try{const d=JSON.parse(e.data);trackBackgroundError(activeSid,_errTitle,d.message||'Error');}
        catch(_){trackBackgroundError(activeSid,_errTitle,'Error');}
      }
      _setActivePaneIdleIfOwner();
      renderSessionList(); // clear streaming indicator immediately on apperror
    });

    source.addEventListener('warning',e=>{
      // Non-fatal warning from server (e.g. fallback activated, retrying)
      if(!S.session||S.session.session_id!==activeSid) return;
      try{
        const d=JSON.parse(e.data);
        // Show as a small inline notice, not a full error
        setComposerStatus(`${d.message||'Warning'}`);
        // If it's a fallback notice, show it briefly then clear
        if(d.type==='fallback') setTimeout(()=>setComposerStatus(''),4000);
      }catch(_){}
    });

    source.addEventListener('error',async e=>{
      source.close();
      if(_deferStreamErrorIfOffline()) return;
      if(_terminalStateReached || _streamFinalized){
        _closeSource();
        return;
      }
      // Attempt one reconnect if the stream is still active server-side
      if(!_reconnectAttempted && streamId){
        _reconnectAttempted=true;
        setComposerStatus('Reconnecting…');
        setTimeout(async()=>{
          try{
            const st=await api(`/api/chat/stream/status?stream_id=${encodeURIComponent(streamId)}`);
            if(st.active){
              setComposerStatus('Reconnected');
              _wireSSE(new EventSource(new URL(`api/chat/stream?stream_id=${encodeURIComponent(streamId)}`,document.baseURI||location.href).href,{withCredentials:true}));
              return;
            }
          }catch(_){
            if(_deferStreamErrorIfOffline()) return;
          }
          if(await _restoreSettledSession()) return;
          if(_deferStreamErrorIfOffline()) return;
          _handleStreamError();
        },1500);
        return;
      }
      if(await _restoreSettledSession()) return;
      if(_deferStreamErrorIfOffline()) return;
      _handleStreamError();
    });

    source.addEventListener('cancel',e=>{
      _terminalStateReached=true;
      if(_persistTimer){clearTimeout(_persistTimer);_persistTimer=null;}
      _streamFinalized=true;
      _cancelAnimationFramePendingStreamRender();
      _streamFadeCleanupReduceMotionListener();
      _smdEndParser();
      if(typeof finalizeThinkingCard==='function') finalizeThinkingCard();
      source.close();
      _clearOwnerInflightState();
      _clearApprovalForOwner();
      _clearClarifyForOwner('cancelled');
      if(S.session&&S.session.session_id===activeSid){
        S.activeStreamId=null;
      }
      // Fetch latest session from server to get accurate message list (includes cancel status)
      // This ensures messages stay in sync with server, fixing race condition where local
      // "*Task cancelled.*" message gets lost when done event overwrites S.messages
      (async()=>{
        try{
          const data=await api(`/api/session?session_id=${encodeURIComponent(activeSid)}`);
          if(data&&data.session&&S.session&&S.session.session_id===activeSid){
            S.session=data.session;
            S.messages=(data.session.messages||[]).filter(m=>m&&m.role);
            clearLiveToolCards();if(!assistantText)removeThinking();
            _markSessionViewed(activeSid, data.session.message_count ?? S.messages.length);
            renderMessages({preserveScroll:true});
          }
        }catch(_){
          // Fallback to local cancel message if API fails
          if(S.session&&S.session.session_id===activeSid){
            clearLiveToolCards();if(!assistantText)removeThinking();
            S.messages.push({role:'assistant',content:'**Task cancelled:** Task cancelled.\n\n*The run was cancelled by the user before Skyly finished. No provider failure occurred.*',provider_details:'Task cancelled.',provider_details_label:'Cancellation details',_error:true});renderMessages({preserveScroll:true});
            _markSessionViewed(activeSid, S.messages.length);
          }
        }
      })();
      renderSessionList();
      _setActivePaneIdleIfOwner();
    });
  }

  async function _restoreSettledSession(){
    try{
      const data=await api(`/api/session?session_id=${encodeURIComponent(activeSid)}`);
      const session=data&&data.session;
      if(!session) return false;
      if(session.active_stream_id||session.pending_user_message) return false;
      _clearOwnerInflightState();
      _closeSource();
      _clearApprovalForOwner();
      _clearClarifyForOwner('terminal');
      const isSessionViewed=_isSessionActivelyViewed(activeSid);
      const completedSid=session.session_id||activeSid;
      if(!isSessionViewed && typeof _markSessionCompletionUnread==='function'){
        _markSessionCompletionUnread(completedSid, session.message_count);
      }
      const isActiveSession=_isSessionCurrentPane(activeSid);
      if(isActiveSession){
        S.activeStreamId=null;
        clearLiveToolCards();if(!assistantText)removeThinking();
        S.session=session;S.messages=(session.messages||[]).filter(m=>m&&m.role);
        if(S.session&&S.session.session_id){
          localStorage.setItem('hermes-webui-session',S.session.session_id);
          if(typeof _setActiveSessionUrl==='function') _setActiveSessionUrl(S.session.session_id);
        }
        const hasMessageToolMetadata=S.messages.some(m=>{
          if(!m||m.role!=='assistant') return false;
          // Recognize both the standard `tool_calls` (used by completed assistant
          // turns where the LLM emitted tool_call entries) and the WebUI-internal
          // `_partial_tool_calls` (used on Stop/Cancel partial messages — see
          // api/streaming.py cancel_stream).
          const hasTc=Array.isArray(m.tool_calls)&&m.tool_calls.length>0;
          const hasPartialTc=Array.isArray(m._partial_tool_calls)&&m._partial_tool_calls.length>0;
          const hasTu=Array.isArray(m.content)&&m.content.some(p=>p&&p.type==='tool_use');
          return hasTc||hasPartialTc||hasTu;
        });
        if(!hasMessageToolMetadata&&session.tool_calls&&session.tool_calls.length){
          S.toolCalls=(session.tool_calls||[]).map(tc=>({...tc,done:true}));
        }else{
          S.toolCalls=[];
        }
        if(isSessionViewed) _markSessionViewed(completedSid, session.message_count ?? S.messages.length);
        syncTopbar();renderMessages({preserveScroll:true});
      }
      if(_isActiveSession()) _queueDrainSid=activeSid;
      renderSessionList();
      _setActivePaneIdleIfOwner();
      return true;
    }catch(_){
      return false;
    }
  }

  function _handleStreamError(){
    // Opus review Q1: mirror done/apperror/cancel finalization so any pending rAF
    // cannot fire after renderMessages() has settled the DOM with the error message.
    if(_persistTimer){clearTimeout(_persistTimer);_persistTimer=null;}
    _streamFinalized=true;
    _cancelAnimationFramePendingStreamRender();
    _streamFadeCleanupReduceMotionListener();
    if(typeof finalizeThinkingCard==='function') finalizeThinkingCard();
    _clearOwnerInflightState();
    _closeSource();
    _clearApprovalForOwner();
    _clearClarifyForOwner('terminal');
    if(S.session&&S.session.session_id===activeSid){
      S.activeStreamId=null;
      clearLiveToolCards();if(!assistantText)removeThinking();
      S.messages.push({role:'assistant',content:'**Error:** Connection lost'});renderMessages({preserveScroll:true});
      _markSessionViewed(activeSid, S.messages.length);
    }else{
      if(typeof trackBackgroundError==='function'){
        const _errTitle=(typeof _allSessions!=='undefined'&&_allSessions.find(s=>s.session_id===activeSid)||{}).title||null;
        trackBackgroundError(activeSid,_errTitle,'Connection lost');
      }
    }
    _setActivePaneIdleIfOwner();
  }

  (async()=>{
    // Reattach path can carry stale stream ids after server restart; preflight
    // status avoids opening a dead SSE URL that will 404 in the console.
    if(reconnecting){
      try{
        const st=await api(`/api/chat/stream/status?stream_id=${encodeURIComponent(streamId)}`);
        if(!st.active){
          _clearOwnerInflightState();
          _clearApprovalForOwner();
          _clearClarifyForOwner('terminal');
          if(S.session&&S.session.session_id===activeSid){
            S.activeStreamId=null;
            clearLiveToolCards();
            removeThinking();
            if(_isActiveSession()) _queueDrainSid=activeSid;
            _setActivePaneIdleIfOwner();
            renderMessages({preserveScroll:true});
            renderSessionList();
          }
          return;
        }
      }catch(_){}
    }
    _wireSSE(new EventSource(new URL(`api/chat/stream?stream_id=${encodeURIComponent(streamId)}`,document.baseURI||location.href).href,{withCredentials:true}));
  })();

}

function transcript(){
  const lines=[`# Hermes session ${S.session?.session_id||''}`,``,
    `Workspace: ${S.session?.workspace||''}`,`Model: ${S.session?.model||''}`,``];
  for(const m of S.messages){
    if(!m||m.role==='tool')continue;
    let c=m.content||'';
    if(Array.isArray(c))c=c.filter(p=>p&&p.type==='text').map(p=>p.text||'').join('\n');
    const ct=String(c).trim();
    if(!ct&&!m.attachments?.length)continue;
    const attach=m.attachments?.length?`\n\n_Files: ${m.attachments.join(', ')}_`:'';
    lines.push(`## ${m.role}`,'',ct+attach,'');
  }
  return lines.join('\n');
}

function autoResize(){const el=$('msg');el.style.height='auto';el.style.height=Math.min(el.scrollHeight,200)+'px';updateSendBtn();}


// ── YOLO mode state ──
// Session-scoped; stored server-side in memory (tools/approval.py).
// Lifecycle:
//   • Page reload: state PERSISTS — _fetchYoloState() re-syncs from backend.
//   • Cross-tab: state is SHARED — enabling YOLO in Tab A affects Tab B for
//     the same session (both poll the same server-side flag).
//   • Server restart: state is LOST — in-memory only, not persisted to disk.
//   • Session switch: state resets — loadSession() clears _yoloEnabled and
//     fetches the new session's state.
let _yoloEnabled = false;

async function _fetchYoloState(sid) {
  try {
    const data = await api('/api/session/yolo?session_id=' + encodeURIComponent(sid));
    _yoloEnabled = !!data.yolo_enabled;
    _updateYoloPill();
  } catch (_) { /* ignore */ }
}

function _updateYoloPill() {
  const pill = $('yoloPill');
  if (!pill) return;
  pill.style.display = _yoloEnabled ? '' : 'none';
  if (_yoloEnabled) {
    pill.title = t('yolo_pill_title_active');
    pill.setAttribute('data-i18n-title', 'yolo_pill_title_active');
  }
  if (typeof applyLocaleToDOM === 'function') applyLocaleToDOM();
}

async function toggleYoloFromApproval() {
  const sid = S.session && S.session.session_id;
  if (!sid) return;
  try {
    await api('/api/session/yolo', {
      method: 'POST',
      body: JSON.stringify({ session_id: sid, enabled: true }),
    });
    _yoloEnabled = true;
    _updateYoloPill();
    hideApprovalCard(true);
    showToast(t('yolo_enabled'));
  } catch (e) { showToast('YOLO: ' + e.message); }
}

// ── Approval polling ──
let _approvalPollTimer = null;
let _approvalHideTimer = null;
let _approvalVisibleSince = 0;
let _approvalSignature = '';
const APPROVAL_MIN_VISIBLE_MS = 30000;

// showApprovalCard moved above respondApproval

function _clearApprovalHideTimer() {
  if (_approvalHideTimer) {
    clearTimeout(_approvalHideTimer);
    _approvalHideTimer = null;
  }
}

function _resetApprovalCardState() {
  _clearApprovalHideTimer();
  _approvalVisibleSince = 0;
  _approvalSignature = '';
}

function hideApprovalCard(force=false) {
  const card = $("approvalCard");
  if (!card) return;
  if (!force && _approvalVisibleSince) {
    const remaining = APPROVAL_MIN_VISIBLE_MS - (Date.now() - _approvalVisibleSince);
    if (remaining > 0) {
      const scheduledSignature = _approvalSignature;
      _clearApprovalHideTimer();
      _approvalHideTimer = setTimeout(() => {
        _approvalHideTimer = null;
        if (_approvalSignature !== scheduledSignature) return;
        hideApprovalCard(true);
      }, remaining);
      return;
    }
  }
  _approvalSessionId = null;
  _resetApprovalCardState();
  card.classList.remove("visible");
  $("approvalCmd").textContent = "";
  $("approvalDesc").textContent = "";
}

// Track session_id of the active approval so respond goes to the right session
let _approvalSessionId = null;
let _approvalCurrentId = null;  // approval_id of the card currently shown
let _approvalPendingBySession = new Map();

function _promptActiveSessionId() {
  return (S.session && S.session.session_id) || null;
}

function _approvalPromptBelongsToActiveSession(sid) {
  return !!(sid && _promptActiveSessionId() === sid);
}

function _rememberApprovalPending(pending, pendingCount) {
  if (!pending) return null;
  const sid = pending._session_id || _promptActiveSessionId();
  if (!sid) return null;
  const nextPending = {...pending, _session_id: sid};
  _approvalPendingBySession.set(sid, {pending: nextPending, pendingCount: pendingCount || 1});
  return sid;
}

function _clearApprovalPendingForSession(sid) {
  if (sid) _approvalPendingBySession.delete(sid);
}

function _hideApprovalCardIfOwner(sid, force=false) {
  if (!sid || _approvalSessionId === sid) hideApprovalCard(force);
}

function _renderPendingApprovalForActiveSession() {
  const sid = _promptActiveSessionId();
  if (!sid) return;
  if (_approvalSessionId && _approvalSessionId !== sid) hideApprovalCard(true);
  const entry = _approvalPendingBySession.get(sid);
  if (entry) showApprovalCard(entry.pending, entry.pendingCount);
}

function showApprovalForSession(sid, pending, pendingCount) {
  if (!pending) return;
  pending._session_id = sid;
  showApprovalCard(pending, pendingCount);
}

function showApprovalCard(pending, pendingCount) {
  const sid = _rememberApprovalPending(pending, pendingCount);
  if (!_approvalPromptBelongsToActiveSession(sid)) return;
  const keys = pending.pattern_keys || (pending.pattern_key ? [pending.pattern_key] : []);
  const desc = (pending.description || "") + (keys.length ? " [" + keys.join(", ") + "]" : "");
  const cmd = pending.command || "";
  const sig = JSON.stringify({desc, cmd, sid: pending._session_id || (S.session && S.session.session_id) || null});
  const card = $("approvalCard");
  const sameApproval = card.classList.contains("visible") && _approvalSignature === sig;
  $("approvalDesc").textContent = desc;
  $("approvalCmd").textContent = cmd;
  _approvalSessionId = sid;
  _approvalCurrentId = pending.approval_id || null;
  _approvalSignature = sig;
  // Show "1 of N" counter when multiple approvals are queued
  const counter = $("approvalCounter");
  if (counter) {
    if (pendingCount && pendingCount > 1) {
      counter.textContent = "1 of " + pendingCount + " pending";
      counter.style.display = "";
    } else {
      counter.style.display = "none";
    }
  }
  if (!sameApproval) {
    _approvalVisibleSince = Date.now();
    _clearApprovalHideTimer();
  }
  // Re-enable buttons in case a previous approval disabled them
  ["approvalBtnOnce","approvalBtnSession","approvalBtnAlways","approvalBtnDeny"].forEach(id => {
    const b = $(id); if (b) { b.disabled = false; b.classList.remove("loading"); }
  });
  card.classList.add("visible");
  if (typeof applyLocaleToDOM === "function") applyLocaleToDOM();
  const onceBtn = $("approvalBtnOnce");
  if (onceBtn) setTimeout(() => onceBtn.focus({preventScroll: true}), 50);
}

async function respondApproval(choice) {
  const sid = _approvalSessionId || (S.session && S.session.session_id);
  if (!sid) return;
  const approvalId = _approvalCurrentId;
  // Disable all buttons immediately to prevent double-submit
  ["approvalBtnOnce","approvalBtnSession","approvalBtnAlways","approvalBtnDeny"].forEach(id => {
    const b = $(id);
    if (b) { b.disabled = true; if (b.id === "approvalBtn" + choice.charAt(0).toUpperCase() + choice.slice(1)) b.classList.add("loading"); }
  });
  _approvalSessionId = null;
  _approvalCurrentId = null;
  _clearApprovalPendingForSession(sid);
  hideApprovalCard(true);
  try {
    await api("/api/approval/respond", {
      method: "POST",
      body: JSON.stringify({ session_id: sid, choice, approval_id: approvalId })
    });
  } catch(e) { setStatus(t("approval_responding") + " " + e.message); }
}

function startApprovalPolling(sid) {
  stopApprovalPolling();
  _approvalPollingSessionId = sid || null;
  // ── SSE (preferred): long-lived connection, server pushes instantly ──
  try {
    const es = new EventSource(new URL('api/approval/stream?session_id=' + encodeURIComponent(sid), document.baseURI || location.href).href);
    let _fallbackActive = false;

    es.addEventListener('initial', e => {
      const d = JSON.parse(e.data);
      if (d.pending) { showApprovalForSession(sid, d.pending, d.pending_count || 1); }
      else { _clearApprovalPendingForSession(sid); _hideApprovalCardIfOwner(sid); }
    });

    es.addEventListener('approval', e => {
      const d = JSON.parse(e.data);
      if (d.pending) { showApprovalForSession(sid, d.pending, d.pending_count || 1); }
      else { _clearApprovalPendingForSession(sid); _hideApprovalCardIfOwner(sid); }
    });

    es.onerror = () => {
      // SSE failed — fall back to HTTP polling (3s interval)
      if (_fallbackActive) return;
      _fallbackActive = true;
      try { es.close(); } catch(_){}
      _startApprovalFallbackPoll(sid);
    };

    // If the session changes or stops being busy, close the SSE.
    // We detect this via a periodic check (cheap — no network request).
    _approvalSSEHealthTimer = setInterval(() => {
      if (!S.busy || !S.session || S.session.session_id !== sid) {
        stopApprovalPolling(); _hideApprovalCardIfOwner(sid, true);
      }
    }, 5000);

    _approvalEventSource = es;
  } catch(_e) {
    // EventSource constructor failed — use polling directly
    _startApprovalFallbackPoll(sid);
  }
}

let _approvalEventSource = null;
let _approvalSSEHealthTimer = null;
let _approvalPollingSessionId = null;

function _startApprovalFallbackPoll(sid) {
  _approvalPollTimer = setInterval(async () => {
    if (!S.busy || !S.session || S.session.session_id !== sid) {
      stopApprovalPolling(); _hideApprovalCardIfOwner(sid, true); return;
    }
    try {
      const data = await api("/api/approval/pending?session_id=" + encodeURIComponent(sid));
      if (data.pending) { showApprovalForSession(sid, data.pending, data.pending_count||1); }
      else { _clearApprovalPendingForSession(sid); _hideApprovalCardIfOwner(sid); }
    } catch(e) { /* ignore poll errors */ }
  }, 1500);  // matches the v0.50.247 polling cadence so degraded-mode users see the same responsiveness
}

function stopApprovalPollingForSession(sid) {
  if(sid && _approvalPollingSessionId && _approvalPollingSessionId!==sid) return;
  stopApprovalPolling();
}

function stopApprovalPolling() {
  if (_approvalPollTimer) { clearInterval(_approvalPollTimer); _approvalPollTimer = null; }
  if (_approvalEventSource) { try { _approvalEventSource.close(); } catch(_){} _approvalEventSource = null; }
  if (_approvalSSEHealthTimer) { clearInterval(_approvalSSEHealthTimer); _approvalSSEHealthTimer = null; }
  _approvalPollingSessionId = null;
}

// ── Clarify polling ──
let _clarifyPollTimer = null;
let _clarifyHideTimer = null;
let _clarifyVisibleSince = 0;
let _clarifySignature = '';
let _clarifySessionId = null;
let _clarifyMissingEndpointWarned = false;
let _clarifyCountdownTimer = null;
let _clarifyExpiresAt = 0;
let _clarifyPendingBySession = new Map();
const CLARIFY_MIN_VISIBLE_MS = 30000;

function _clarifyPromptBelongsToActiveSession(sid) {
  return !!(sid && _promptActiveSessionId() === sid);
}

function _rememberClarifyPending(pending) {
  if (!pending) return null;
  const sid = pending._session_id || _promptActiveSessionId();
  if (!sid) return null;
  const nextPending = {...pending, _session_id: sid};
  _clarifyPendingBySession.set(sid, {pending: nextPending});
  return sid;
}

function _clearClarifyPendingForSession(sid) {
  if (sid) _clarifyPendingBySession.delete(sid);
}

function _hideClarifyCardIfOwner(sid, force=false, reason="dismissed") {
  if (!sid || _clarifySessionId === sid) hideClarifyCard(force, reason);
}

function _renderPendingClarifyForActiveSession() {
  const sid = _promptActiveSessionId();
  if (!sid) return;
  if (_clarifySessionId && _clarifySessionId !== sid) hideClarifyCard(true, 'session');
  const entry = _clarifyPendingBySession.get(sid);
  if (entry) showClarifyCard(entry.pending);
}

function showClarifyForSession(sid, pending) {
  if (!pending) return;
  pending._session_id = sid;
  showClarifyCard(pending);
}

function _renderPendingPromptsForActiveSession() {
  _renderPendingApprovalForActiveSession();
  _renderPendingClarifyForActiveSession();
}

function _ensureClarifyCardDom() {
  let card = $("clarifyCard");
  if (card) return card;
  const host = $("msgInner") || $("messages");
  if (!host) return null;
  card = document.createElement("div");
  card.className = "clarify-card";
  card.id = "clarifyCard";
  card.setAttribute("role", "dialog");
  card.setAttribute("aria-labelledby", "clarifyHeading");
  card.setAttribute("aria-describedby", "clarifyQuestion clarifyHint");
  card.innerHTML = `
    <div class="clarify-inner">
      <div class="clarify-header">
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 17h.01"/><path d="M9.09 9a3 3 0 1 1 5.82 1c0 2-3 2-3 4"/><circle cx="12" cy="12" r="10"/></svg>
        <span id="clarifyHeading" data-i18n="clarify_heading">Clarification needed</span>
        <span class="clarify-countdown" id="clarifyCountdown"></span>
      </div>
      <div class="clarify-question" id="clarifyQuestion"></div>
      <div class="clarify-choices" id="clarifyChoices"></div>
      <div class="clarify-response">
        <input class="clarify-input" id="clarifyInput" type="text" data-i18n-placeholder="clarify_input_placeholder" placeholder="Type your response…">
        <button class="clarify-submit" id="clarifySubmit" data-i18n="clarify_send">Send</button>
      </div>
      <div class="clarify-hint" id="clarifyHint" data-i18n="clarify_hint">Please choose one option, or type your own response below.</div>
    </div>
  `;
  host.appendChild(card);
  const submit = $("clarifySubmit");
  if (submit) submit.onclick = () => respondClarify();
  if (typeof applyLocaleToDOM === "function") applyLocaleToDOM();
  return card;
}

function _clearClarifyHideTimer() {
  if (_clarifyHideTimer) {
    clearTimeout(_clarifyHideTimer);
    _clarifyHideTimer = null;
  }
}

function _clearClarifyCountdownTimer() {
  if (_clarifyCountdownTimer) {
    clearInterval(_clarifyCountdownTimer);
    _clarifyCountdownTimer = null;
  }
  _clarifyExpiresAt = 0;
  const countdown = $("clarifyCountdown");
  if (countdown) {
    countdown.textContent = "";
    countdown.classList.remove("urgent");
  }
}

function _clarifyExpiryMs(pending) {
  const expiresAt = Number(pending && pending.expires_at);
  if (Number.isFinite(expiresAt) && expiresAt > 0) return expiresAt * 1000;
  const requestedAt = Number(pending && pending.requested_at);
  const timeoutSeconds = Number(pending && pending.timeout_seconds);
  if (Number.isFinite(requestedAt) && Number.isFinite(timeoutSeconds)) {
    return (requestedAt + timeoutSeconds) * 1000;
  }
  return 0;
}

function _updateClarifyCountdown() {
  const countdown = $("clarifyCountdown");
  if (!countdown || !_clarifyExpiresAt) return;
  const remaining = Math.max(0, Math.ceil((_clarifyExpiresAt - Date.now()) / 1000));
  countdown.textContent = `${remaining}s`;
  countdown.classList.toggle("urgent", remaining <= 10);
}

function _startClarifyCountdown(pending) {
  const expiresAt = _clarifyExpiryMs(pending);
  if (_clarifyCountdownTimer && _clarifyExpiresAt === expiresAt) return;
  _clearClarifyCountdownTimer();
  _clarifyExpiresAt = expiresAt;
  if (!_clarifyExpiresAt) return;
  _updateClarifyCountdown();
  _clarifyCountdownTimer = setInterval(_updateClarifyCountdown, 1000);
}

function _stashClarifyDraft(reason) {
  if (reason !== "expired" && reason !== "terminal") return false;
  const input = $("clarifyInput");
  const draft = String((input && input.value) || "").trim();
  if (!draft) return false;
  const sid = _clarifySessionId || (S.session && S.session.session_id) || "unknown";
  const key = `hermes-clarify-draft-${sid}-${_clarifySignature || "unknown"}`;
  try {
    sessionStorage.setItem(key, JSON.stringify({
      draft,
      reason,
      saved_at: Date.now(),
    }));
  } catch (_) {}
  const composer = $('msg');
  if (composer) {
    const current = String(composer.value || "");
    composer.value = current.trim() ? `${current.replace(/\s+$/, "")}\n\n${draft}` : draft;
    if (typeof autoResize === "function") autoResize();
    if (typeof updateSendBtn === "function") updateSendBtn();
  }
  const notice = reason === "expired"
    ? "Clarification timed out. Your draft was kept in the composer."
    : "Clarification closed. Your draft was kept in the composer.";
  if (typeof setComposerStatus === "function") setComposerStatus(notice);
  else if (typeof setStatus === "function") setStatus(notice);
  if (typeof showToast === "function") showToast(notice, 5000);
  return true;
}

function _resetClarifyCardState() {
  _clearClarifyHideTimer();
  _clearClarifyCountdownTimer();
  _clarifyVisibleSince = 0;
  _clarifySignature = '';
}

function hideClarifyCard(force=false, reason="dismissed") {
  const card = $("clarifyCard");
  if (!card) {
    _clarifySessionId = null;
    _resetClarifyCardState();
    if (typeof unlockComposerForClarify === "function") unlockComposerForClarify();
    return;
  }
  if (!force && reason !== "expired" && _clarifyVisibleSince) {
    const remaining = CLARIFY_MIN_VISIBLE_MS - (Date.now() - _clarifyVisibleSince);
    if (remaining > 0) {
      const scheduledSignature = _clarifySignature;
      _clearClarifyHideTimer();
      _clarifyHideTimer = setTimeout(() => {
        _clarifyHideTimer = null;
        if (_clarifySignature !== scheduledSignature) return;
        hideClarifyCard(true, reason);
      }, remaining);
      return;
    }
  }
  _stashClarifyDraft(reason);
  _clarifySessionId = null;
  _resetClarifyCardState();
  card.classList.remove("visible");
  if (typeof unlockComposerForClarify === "function") unlockComposerForClarify();
  $("clarifyQuestion").textContent = "";
  $("clarifyChoices").innerHTML = "";
  $("clarifyInput").value = "";
  $("clarifyInput").disabled = false;
  $("clarifyInput").onkeydown = null;
  const submit = $("clarifySubmit");
  if (submit) { submit.disabled = false; submit.classList.remove("loading"); }
}

function _clarifySetControlsDisabled(disabled, loading=false) {
  const input = $("clarifyInput");
  const submit = $("clarifySubmit");
  if (input) input.disabled = disabled;
  if (submit) {
    submit.disabled = disabled;
    submit.classList.toggle("loading", !!loading);
  }
  const choices = $("clarifyChoices");
  if (choices) {
    choices.querySelectorAll("button").forEach(btn => {
      btn.disabled = disabled;
      if (loading && btn.dataset && btn.dataset.choice === "other") {
        btn.classList.toggle("loading", false);
      }
    });
  }
}

function showClarifyCard(pending) {
  const sid = _rememberClarifyPending(pending);
  if (!_clarifyPromptBelongsToActiveSession(sid)) return;
  const question = pending.question || pending.description || '';
  const choices = Array.isArray(pending.choices_offered)
    ? pending.choices_offered
    : (Array.isArray(pending.choices) ? pending.choices : []);
  const sig = JSON.stringify({
    question,
    choices,
    sid: pending._session_id || (S.session && S.session.session_id) || null,
  });
  const card = _ensureClarifyCardDom();
  if (!card) return;
  const questionEl = $("clarifyQuestion");
  const choicesEl = $("clarifyChoices");
  const input = $("clarifyInput");
  const sameClarify = card.classList.contains("visible") && _clarifySignature === sig;
  _clarifySessionId = sid;
  _clarifySignature = sig;
  _startClarifyCountdown(pending);
  if (!sameClarify) {
    _clarifyVisibleSince = Date.now();
    _clearClarifyHideTimer();
  }
  if (questionEl) questionEl.textContent = question;
  if (choicesEl) {
    choicesEl.innerHTML = '';
    choicesEl.style.display = choices.length ? '' : 'none';
    if (choices.length) {
      choices.forEach((choice, idx) => {
        const btn = document.createElement('button');
        btn.type = 'button';
        btn.className = 'clarify-choice';
        btn.dataset.choice = choice;
        btn.onclick = () => respondClarify(choice);
        const badge = document.createElement('span');
        badge.className = 'clarify-choice-badge';
        badge.textContent = String(idx + 1);
        const text = document.createElement('span');
        text.className = 'clarify-choice-text';
        text.textContent = choice;
        btn.appendChild(badge);
        btn.appendChild(text);
        choicesEl.appendChild(btn);
      });
      const other = document.createElement('button');
      other.type = 'button';
      other.className = 'clarify-choice other';
      other.dataset.choice = 'other';
      other.setAttribute('data-i18n', 'clarify_other');
      const otherBadge = document.createElement('span');
      otherBadge.className = 'clarify-choice-badge other';
      otherBadge.textContent = '•';
      const otherText = document.createElement('span');
      otherText.className = 'clarify-choice-text';
      otherText.textContent = t('clarify_other') || 'Other';
      other.appendChild(otherBadge);
      other.appendChild(otherText);
      other.onclick = () => {
        const el = $("clarifyInput");
        if (el) {
          el.focus();
          if (typeof el.select === 'function') el.select();
        }
      };
      choicesEl.appendChild(other);
    }
  }
  if (input) {
    if (!sameClarify) input.value = '';
    input.disabled = false;
    input.onkeydown = (e) => {
      if (e.key === 'Enter') {
        e.preventDefault();
        respondClarify();
      }
    };
  }
  if (typeof lockComposerForClarify === "function") {
    lockComposerForClarify(question ? `Clarification needed: ${question}` : "Clarification needed");
  }
  _clarifySetControlsDisabled(false, false);
  card.classList.add("visible");
  if (typeof applyLocaleToDOM === "function") applyLocaleToDOM();
  if (input && !sameClarify) setTimeout(() => input.focus({preventScroll: true}), 50);
}

async function respondClarify(response) {
  const sid = _clarifySessionId || (S.session && S.session.session_id);
  if (!sid) return;
  const input = $("clarifyInput");
  let value = typeof response === 'string' ? response : (input ? input.value : '');
  value = String(value || '').trim();
  if (!value) {
    if (input) input.focus();
    return;
  }
  _clarifySessionId = null;
  _clearClarifyPendingForSession(sid);
  _clarifySetControlsDisabled(true, true);
  hideClarifyCard(true, 'sent');
  try {
    await api("/api/clarify/respond", {
      method: "POST",
      body: JSON.stringify({ session_id: sid, response: value })
    });
  } catch(e) { setStatus(t("clarify_responding") + " " + e.message); }
}

var _clarifyEventSource = null;
var _clarifyFallbackTimer = null;
var _clarifyHealthTimer = null;
let _clarifyPollingSessionId = null;

function startClarifyPolling(sid) {
  stopClarifyPolling();
  _clarifyPollingSessionId = sid || null;
  _clarifyMissingEndpointWarned = false;

  // SSE primary path: long-lived connection pushes events instantly.
  try {
    _clarifyEventSource = new EventSource(new URL('api/clarify/stream?session_id=' + encodeURIComponent(sid), document.baseURI || location.href).href);
  } catch(e) {
    _startClarifyFallbackPoll(sid);
    return;
  }

  _clarifyEventSource.addEventListener('initial', function(ev) {
    try {
      var d = JSON.parse(ev.data);
      if (d.pending) { showClarifyForSession(sid, d.pending); }
      else { _clearClarifyPendingForSession(sid); _hideClarifyCardIfOwner(sid, false, 'expired'); }
    } catch(e) {}
  });

  _clarifyEventSource.addEventListener('clarify', function(ev) {
    try {
      var d = JSON.parse(ev.data);
      if (d.pending) { showClarifyForSession(sid, d.pending); }
      else { _clearClarifyPendingForSession(sid); _hideClarifyCardIfOwner(sid, false, 'expired'); }
    } catch(e) {}
  });

  _clarifyEventSource.onerror = function() {
    stopClarifyPolling();
    _startClarifyFallbackPoll(sid);
  };

  // Stale-detector: track last event timestamp; only reconnect if no event
  // (initial or clarify) has arrived in 60s. The server sends a keepalive
  // comment line every 30s but EventSource silently consumes those; we only
  // bump lastEventAt on actual application events. With no real events for
  // 60s on a long-lived clarify connection the server is effectively silent
  // and a reconnect is the safe move.
  //
  // Without the lastEventAt gate the original PR force-reconnected every 60s
  // regardless of activity, which churned one TCP/SSE setup per minute per
  // active session. (Opus pre-release review of v0.50.249.)
  let _lastClarifyEventAt = Date.now();
  const _markClarifyEvent = () => { _lastClarifyEventAt = Date.now(); };
  _clarifyEventSource.addEventListener('initial', _markClarifyEvent);
  _clarifyEventSource.addEventListener('clarify', _markClarifyEvent);
  _clarifyHealthTimer = setInterval(function() {
    if (Date.now() - _lastClarifyEventAt < 60000) return;
    if (_clarifyEventSource) {
      try { _clarifyEventSource.close(); } catch(_){}
      _clarifyEventSource = null;
    }
    clearInterval(_clarifyHealthTimer); _clarifyHealthTimer = null;
    startClarifyPolling(sid);
  }, 60000);
}

function _startClarifyFallbackPoll(sid) {
  _clarifyFallbackTimer = setInterval(async () => {
    if (!S.session || S.session.session_id !== sid) {
      stopClarifyPolling(); _hideClarifyCardIfOwner(sid, true, 'session'); return;
    }
    try {
      const data = await api("/api/clarify/pending?session_id=" + encodeURIComponent(sid));
      if (data.pending) { showClarifyForSession(sid, data.pending); }
      else { _clearClarifyPendingForSession(sid); _hideClarifyCardIfOwner(sid, false, 'expired'); }
    } catch(e) {
      const msg = String((e && e.message) || "");
      if (!_clarifyMissingEndpointWarned && /(^|\b)(404|not found)(\b|$)/i.test(msg)) {
        _clarifyMissingEndpointWarned = true;
        setComposerStatus("Clarify unavailable on current server build. Restart server.");
        if (typeof showToast === "function") {
          showToast("Clarify endpoint unavailable. Please restart server.", 5000);
        }
        stopClarifyPolling();
      }
    }
  }, 3000);
}

function stopClarifyPollingForSession(sid) {
  if(sid && _clarifyPollingSessionId && _clarifyPollingSessionId!==sid) return;
  stopClarifyPolling();
}

function stopClarifyPolling() {
  if (_clarifyEventSource) { try { _clarifyEventSource.close(); } catch(_){} _clarifyEventSource = null; }
  if (_clarifyFallbackTimer) { clearInterval(_clarifyFallbackTimer); _clarifyFallbackTimer = null; }
  if (_clarifyHealthTimer) { clearInterval(_clarifyHealthTimer); _clarifyHealthTimer = null; }
  _clarifyPollingSessionId = null;
}

// ── Notifications and Sound ──────────────────────────────────────────────────

function playNotificationSound(){
  if(!window._soundEnabled) return;
  try{
    const ctx=new (window.AudioContext||window.webkitAudioContext)();
    const osc=ctx.createOscillator();
    const gain=ctx.createGain();
    osc.connect(gain);gain.connect(ctx.destination);
    osc.type='sine';osc.frequency.setValueAtTime(660,ctx.currentTime);
    osc.frequency.setValueAtTime(880,ctx.currentTime+0.1);
    gain.gain.setValueAtTime(0.3,ctx.currentTime);
    gain.gain.exponentialRampToValueAtTime(0.01,ctx.currentTime+0.3);
    osc.start(ctx.currentTime);osc.stop(ctx.currentTime+0.3);
    osc.onended=()=>ctx.close();
  }catch(e){console.warn('Notification sound failed:',e);}
}

function sendBrowserNotification(title,body){
  if(!window._notificationsEnabled||!document.hidden) return;
  if(!('Notification' in window)) return;
  const botName=window._botName||'Hermes';
  if(Notification.permission==='granted'){
    new Notification(title||botName,{body:body});
  }else if(Notification.permission!=='denied'){
    Notification.requestPermission().then(p=>{
      if(p==='granted') new Notification(title||botName,{body:body});
    });
  }
}

// ── /btw ephemeral stream ────────────────────────────────────────────────────
// Connects to the ephemeral SSE stream from /api/btw and renders the answer
// in a visually distinct bubble that is NOT persisted to session history.

function attachBtwStream(parentSid, streamId, question){
  if(!parentSid||!streamId) return;
  const src=new EventSource(new URL('api/chat/stream?stream_id='+encodeURIComponent(streamId), document.baseURI||location.href).href);
  let answer='';
  let btwRow=null;
  let _streamDone=false;
  function _ensureBtwRow(){
    if(btwRow&&btwRow.isConnected) return;
    const inner=$('msgInner');
    if(!inner) return;
    btwRow=document.createElement('div');
    btwRow.className='msg-row msg-row-btw';
    btwRow.dataset.role='assistant';
    btwRow.dataset.btw='1';
    const labelEl=document.createElement('div');
    labelEl.className='msg-btw-label';
    labelEl.textContent=t('btw_label');
    const qEl=document.createElement('div');
    qEl.className='msg-body';
    qEl.textContent=question;
    const ansEl=document.createElement('div');
    ansEl.className='msg-body msg-btw-answer';
    ansEl.textContent='...';
    btwRow.appendChild(labelEl);
    btwRow.appendChild(qEl);
    btwRow.appendChild(ansEl);
    inner.appendChild(btwRow);
    btwRow.scrollIntoView({behavior:'smooth',block:'end'});
  }
  src.addEventListener('token',e=>{
    try{answer+=JSON.parse(e.data).text||'';}catch(_){}
    _ensureBtwRow();
    const ansEl=btwRow&&btwRow.querySelector('.msg-btw-answer');
    if(ansEl) ansEl.innerHTML=renderMd(answer);
  });
  src.addEventListener('done',e=>{
    _streamDone=true;
    src.close();
    try{
      const d=JSON.parse(e.data);
      if(d.answer&&!answer) answer=d.answer;
    }catch(_){}
    if(S.session&&S.session.session_id===parentSid) _ensureBtwRow();
    if(btwRow&&btwRow.isConnected){
      const ansEl=btwRow.querySelector('.msg-btw-answer');
      if(ansEl) ansEl.innerHTML=renderMd(answer||t('btw_no_answer'));
    }
    showToast(t('btw_done'));
  });
  src.addEventListener('apperror',e=>{
    _streamDone=true;
    src.close();
    try{
      const d=JSON.parse(e.data);
      showToast(t('btw_failed')+(d.message||''));
    }catch(_){showToast(t('btw_failed'));}
    if(btwRow&&btwRow.isConnected) btwRow.remove();
  });
  src.addEventListener('stream_end',()=>{_streamDone=true;src.close();});
  src.onerror=()=>{src.close();if(!_streamDone&&btwRow&&btwRow.isConnected) btwRow.remove();};
}

// ── /background task tracking ────────────────────────────────────────────────

let _bgPollTimers={};
let _bgActiveTasks=new Set();

function showBackgroundBadge(taskId){
  _bgActiveTasks.add(taskId);
  const badge=$('bgBadge');
  if(badge){
    badge.textContent=String(_bgActiveTasks.size);
    badge.style.display=_bgActiveTasks.size?'':'none';
  }
}
function hideBackgroundBadge(taskId){
  _bgActiveTasks.delete(taskId);
  const badge=$('bgBadge');
  if(badge){
    badge.textContent=String(_bgActiveTasks.size);
    badge.style.display=_bgActiveTasks.size?'':'none';
  }
}
function startBackgroundPolling(parentSid, taskId, prompt){
  if(_bgPollTimers[taskId]) return;
  async function _poll(){
    try{
      const r=await api('/api/background/status?session_id='+encodeURIComponent(parentSid));
      if(r&&r.results){
        for(const res of r.results){
          if(res.task_id===taskId){
            hideBackgroundBadge(taskId);
            delete _bgPollTimers[taskId];
            const msg={role:'assistant',content:`**${t('bg_label')}** ${prompt.slice(0,80)}\n\n${res.answer||t('bg_no_answer')}`,'_background':true,_ts:Date.now()/1000};
            S.messages.push(msg);
            renderMessages({preserveScroll:true});
            showToast(t('bg_complete'));
            return;
          }
        }
      }
    }catch(_){}
    _bgPollTimers[taskId]=setTimeout(_poll,3000);
  }
  _poll();
}

// ── Panel navigation (Chat / Tasks / Skills / Memory) ──
