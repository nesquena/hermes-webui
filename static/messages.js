async function send(){
  const text=$('msg').value.trim();
  if(!text&&!S.pendingFiles.length)return;
  // Slash command intercept -- local commands handled without agent round-trip
  if(text.startsWith('/')&&!S.pendingFiles.length&&executeCommand(text)){
    $('msg').value='';autoResize();hideCmdDropdown();return;
  }
  // Don't send while an inline message edit is active
  if(document.querySelector('.msg-edit-area'))return;
  // If busy, queue the message instead of dropping it
  if(S.busy){
    if(text){
      if(!S.session){await newSession();await renderSessionList();}
      queueSessionMessage(S.session.session_id,{text,files:[...S.pendingFiles]});
      $('msg').value='';autoResize();
      S.pendingFiles=[];renderTray();
      updateQueueBadge(S.session.session_id);
      showToast(`Queued: "${text.slice(0,40)}${text.length>40?'…':''}"`,2000);
    }
    return;
  }
  if(!S.session){await newSession();await renderSessionList();}

  const activeSid=S.session.session_id;

  setComposerStatus(S.pendingFiles&&S.pendingFiles.length?'Uploading…':'');
  let uploaded=[];
  try{uploaded=await uploadPendingFiles();}
  catch(e){if(!text){setComposerStatus(`Upload error: ${e.message}`);return;}}

  let msgText=text;
  if(uploaded.length&&!msgText)msgText=`I've uploaded ${uploaded.length} file(s): ${uploaded.join(', ')}`;
  else if(uploaded.length)msgText=`${text}\n\n[Attached files: ${uploaded.join(', ')}]`;
  if(!msgText){setComposerStatus('Nothing to send');return;}

  $('msg').value='';autoResize();
  const displayText=text||(uploaded.length?`Uploaded: ${uploaded.join(', ')}`:'(file upload)');
  const userMsg={role:'user',content:displayText,attachments:uploaded.length?uploaded:undefined,_ts:Date.now()/1000};
  S.toolCalls=[];  // clear tool calls from previous turn
  clearLiveToolCards();  // clear any leftover live cards from last turn
  S.messages.push(userMsg);renderMessages();appendThinking();setBusy(true);
  INFLIGHT[activeSid]={messages:[...S.messages],uploaded,toolCalls:[]};
  if(typeof saveInflightState==='function'){
    saveInflightState(activeSid,{streamId:null,messages:INFLIGHT[activeSid].messages,uploaded,toolCalls:[]});
  }
  startApprovalPolling(activeSid);
  startClarifyPolling(activeSid);
  S.activeStreamId = null;  // will be set after stream starts

  // Set provisional title from user message immediately so session appears
  // in the sidebar right away with a meaningful name (server may refine later)
  if(S.session&&(S.session.title==='Untitled'||!S.session.title)){
    const provisionalTitle=displayText.slice(0,64);
    S.session.title=provisionalTitle;
    syncTopbar();
    // Persist it and refresh the sidebar now -- don't wait for done
    api('/api/session/rename',{method:'POST',body:JSON.stringify({
      session_id:activeSid, title:provisionalTitle
    })}).catch(()=>{});  // fire-and-forget, server refines on done
    renderSessionList();  // session appears in sidebar immediately
  } else {
    renderSessionList();  // ensure it's visible even if already titled
  }

  // Start the agent via POST, get a stream_id back
  let streamId;
  try{
    const startData=await api('/api/chat/start',{method:'POST',body:JSON.stringify({
      session_id:activeSid,message:msgText,
      model:S.session.model||$('modelSelect').value,workspace:S.session.workspace,
      attachments:uploaded.length?uploaded:undefined
    })});
    streamId=startData.stream_id;
    S.activeStreamId = streamId;
    markInflight(activeSid, streamId);
    if(typeof saveInflightState==='function'){
      saveInflightState(activeSid,{streamId,messages:INFLIGHT[activeSid].messages,uploaded,toolCalls:INFLIGHT[activeSid].toolCalls||[]});
    }
    // Show Cancel button
    const cancelBtn=$('btnCancel');
    if(cancelBtn) cancelBtn.style.display='inline-flex';
  }catch(e){
    const errMsg=String((e&&e.message)||'');
    const conflictActiveStream=/session already has an active stream/i.test(errMsg);
    if(conflictActiveStream){
      delete INFLIGHT[activeSid];
      if(typeof clearInflightState==='function') clearInflightState(activeSid);
      stopApprovalPolling();
      stopClarifyPolling();
      // Keep the user's attempted turn by queueing it for after the current run.
      queueSessionMessage(activeSid,{text:msgText,files:[]});
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
    if(!_clarifySessionId || _clarifySessionId===activeSid) hideClarifyCard(true);
    S.messages.push({role:'assistant',content:`**Error:** ${errMsg}`});
    renderMessages();setBusy(false);setComposerStatus(`Error: ${errMsg}`);
    return;
  }

  // Open SSE stream and render tokens live
  attachLiveStream(activeSid, streamId, uploaded);

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
  closeLiveStream(activeSid);
  if(!INFLIGHT[activeSid]) INFLIGHT[activeSid]={messages:[...S.messages],uploaded:[...uploaded],toolCalls:[]};
  else {
    if(uploaded.length) INFLIGHT[activeSid].uploaded=[...uploaded];
    if(!Array.isArray(INFLIGHT[activeSid].toolCalls)) INFLIGHT[activeSid].toolCalls=[];
  }

  let assistantText='';
  let reasoningText='';
  let assistantRow=null;
  let assistantBody=null;
  // Thinking tag patterns for streaming display
  const _thinkPairs=[
    {open:'<think>',close:'</think>'},
    {open:'<|channel>thought\n',close:'<channel|>'}
  ];

  function _isActiveSession(){
    return !!(S.session&&S.session.session_id===activeSid);
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
      persistInflightState();
      return;
    }
    inflight.messages.push({role:'assistant',content:assistantText,reasoning:reasoningText||undefined,_live:true,_ts:ts});
    persistInflightState();
  }
  function ensureAssistantRow(){
    if(!_isActiveSession()) return;
    if(assistantRow&&!assistantRow.isConnected){assistantRow=null;assistantBody=null;}
    if(!assistantRow){
      const existing=$('msgInner').querySelector('.msg-row[data-live-assistant="1"]');
      if(existing){
        assistantRow=existing;
        assistantBody=existing.querySelector('.msg-body');
      }
    }
    if(assistantRow){
      if(typeof placeLiveToolCardsHost==='function') placeLiveToolCardsHost();
      return;
    }

    removeThinking();
    const tr=$('toolRunningRow');if(tr)tr.remove();
    $('emptyState').style.display='none';
    assistantRow=document.createElement('div');assistantRow.className='msg-row';
    assistantBody=document.createElement('div');assistantBody.className='msg-body';
    const role=document.createElement('div');role.className='msg-role assistant';
    const _bn=window._botName||'Hermes';
    const icon=document.createElement('div');icon.className='role-icon assistant';icon.textContent=_bn.charAt(0).toUpperCase();
    const lbl=document.createElement('span');lbl.style.fontSize='12px';lbl.textContent=_bn;
    role.appendChild(icon);role.appendChild(lbl);
    assistantRow.appendChild(role);assistantRow.appendChild(assistantBody);
    $('msgInner').appendChild(assistantRow);
  }

  // ── Shared SSE handler wiring (used for initial connection and reconnect) ──
  let _reconnectAttempted=false;
  let _terminalStateReached=false;

  // rAF-throttled rendering: buffer tokens, render at most once per frame
  let _renderPending=false;
  // Extract display text from assistantText, stripping completed thinking blocks
  // and hiding content still inside an open thinking block.
  function _streamDisplay(){
    const raw=assistantText;
    if(reasoningText) return raw;
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
    const raw=assistantText;
    if(reasoningText){
      return {thinkingText:reasoningText, displayText:_streamDisplay(), inThinking:false};
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
    const text=(parsed&&parsed.thinkingText)||'';
    if(text||(parsed&&parsed.inThinking)){
      if(typeof updateThinking==='function') updateThinking(text||'Thinking…');
      else appendThinking();
      return;
    }
    removeThinking();
  }
  function _scheduleRender(){
    if(_renderPending) return;
    _renderPending=true;
    requestAnimationFrame(()=>{
      _renderPending=false;
      const parsed=_parseStreamState();
      _renderLiveThinking(parsed);
      if(assistantBody){
        assistantBody.innerHTML=parsed.displayText?renderMd(parsed.displayText):'';
      }
      scrollIfPinned();
    });
  }

  function _wireSSE(source){
    source.addEventListener('token',e=>{
      if(!S.session||S.session.session_id!==activeSid) return;
      const d=JSON.parse(e.data);
      assistantText+=d.text;
      syncInflightAssistantMessage();
      if(!S.session||S.session.session_id!==activeSid) return;

      ensureAssistantRow();
      _scheduleRender();
    });

    source.addEventListener('reasoning',e=>{
      const d=JSON.parse(e.data);
      reasoningText += d.text || '';
      syncInflightAssistantMessage();
      if(!S.session||S.session.session_id!==activeSid) return;
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
      removeThinking();
      const oldRow=$('toolRunningRow');if(oldRow)oldRow.remove();
      appendLiveToolCard(tc);
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
      d._session_id=activeSid;
      showApprovalCard(d, 1);
      playNotificationSound();
      sendBrowserNotification('Approval required',d.description||'Tool approval needed');
    });

    source.addEventListener('clarify',e=>{
      const d=JSON.parse(e.data);
      d._session_id=activeSid;
      showClarifyCard(d);
      playNotificationSound();
      sendBrowserNotification('Clarification needed',d.question||'Tool clarification needed');
    });

    source.addEventListener('title',e=>{
      let d={};
      try{ d=JSON.parse(e.data||'{}'); }catch(_){}
      if((d.session_id||activeSid)!==activeSid) return;
      const newTitle=String(d.title||'').trim();
      if(!newTitle) return;
      if(S.session&&S.session.session_id===activeSid){
        S.session.title=newTitle;
        syncTopbar();
      }
      if(typeof _allSessions!=='undefined'&&Array.isArray(_allSessions)){
        const row=_allSessions.find(s=>s&&s.session_id===activeSid);
        if(row) row.title=newTitle;
      }
      if(typeof renderSessionListFromCache==='function') renderSessionListFromCache();
      else if(typeof renderSessionList==='function') renderSessionList();
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

    source.addEventListener('done',e=>{
      _terminalStateReached=true;
      const d=JSON.parse(e.data);
      delete INFLIGHT[activeSid];
      clearInflight();clearInflightState(activeSid);
      stopApprovalPolling();
      stopClarifyPolling();
      if(!_approvalSessionId || _approvalSessionId===activeSid) hideApprovalCard(true);
      if(!_clarifySessionId || _clarifySessionId===activeSid) hideClarifyCard(true);
      if(S.session&&S.session.session_id===activeSid){
        S.activeStreamId=null;
        const _cb=$('btnCancel');if(_cb)_cb.style.display='none';
      }
      if(S.session&&S.session.session_id===activeSid){
        S.session=d.session;S.messages=d.session.messages||[];
        // Find the last assistant message once for both reasoning persistence and timestamp
        const lastAsst=[...S.messages].reverse().find(m=>m.role==='assistant');
        // Persist reasoning trace so thinking card survives page reload
        if(reasoningText&&lastAsst&&!lastAsst.reasoning) lastAsst.reasoning=reasoningText;
        // Stamp _ts on the last assistant message if it has no timestamp
        if(lastAsst&&!lastAsst._ts&&!lastAsst.timestamp) lastAsst._ts=Date.now()/1000;
        if(d.usage){S.lastUsage=d.usage;_syncCtxIndicator(d.usage);}
        if(d.session.tool_calls&&d.session.tool_calls.length){
          S.toolCalls=d.session.tool_calls.map(tc=>({...tc,done:true}));
        } else {
          S.toolCalls=S.toolCalls.map(tc=>({...tc,done:true}));
        }
        if(uploaded.length){
          const lastUser=[...S.messages].reverse().find(m=>m.role==='user');
          if(lastUser)lastUser.attachments=uploaded;
        }
        clearLiveToolCards();
        S.busy=false;
        // No-reply guard (#373): if agent returned nothing, show inline error
        if(!S.messages.some(m=>m.role==='assistant'&&String(m.content||'').trim())&&!assistantText){removeThinking();S.messages.push({role:'assistant',content:'**No response received.** Check your API key and model selection.'});}
        syncTopbar();renderMessages();loadDir('.');
      }
      renderSessionList();setBusy(false);setStatus('');
      setComposerStatus('');
      playNotificationSound();
      sendBrowserNotification('Response complete',assistantText?assistantText.slice(0,100):'Task finished');
    });

    source.addEventListener('stream_end',e=>{
      _terminalStateReached=true;
      try{
        const d=JSON.parse(e.data||'{}');
        if((d.session_id||activeSid)!==activeSid) return;
      }catch(_){}
      source.close();
    });

    source.addEventListener('compressed',e=>{
      // Context was auto-compressed during this turn -- show a system message
      if(!S.session||S.session.session_id!==activeSid) return;
      try{
        const d=JSON.parse(e.data);
        const sysMsg={role:'assistant',content:'*[Context was auto-compressed to continue the conversation]*'};
        S.messages.push(sysMsg);
        showToast(d.message||'Context compressed');
      }catch(err){}
    });

    source.addEventListener('apperror',e=>{
      _terminalStateReached=true;
      // Application-level error sent explicitly by the server (rate limit, crash, etc.)
      // This is distinct from the SSE network 'error' event below.
      source.close();
      delete INFLIGHT[activeSid];clearInflight();clearInflightState(activeSid);stopApprovalPolling();stopClarifyPolling();
      if(!_approvalSessionId||_approvalSessionId===activeSid) hideApprovalCard(true);
      if(!_clarifySessionId||_clarifySessionId===activeSid) hideClarifyCard(true);
      if(S.session&&S.session.session_id===activeSid){
        S.activeStreamId=null;const _cbe=$('btnCancel');if(_cbe)_cbe.style.display='none';
        clearLiveToolCards();if(!assistantText)removeThinking();
        try{
          const d=JSON.parse(e.data);
          const isRateLimit=d.type==='rate_limit';
          const isAuthMismatch=d.type==='auth_mismatch';
          const isNoResponse=d.type==='no_response';
          const label=isRateLimit?'Rate limit reached':isAuthMismatch?(typeof t==='function'?t('provider_mismatch_label'):'Provider mismatch'):isNoResponse?'No response received':'Error';
          const hint=d.hint?`\n\n*${d.hint}*`:'';
          S.messages.push({role:'assistant',content:`**${label}:** ${d.message}${hint}`});
        }catch(_){
          S.messages.push({role:'assistant',content:'**Error:** An error occurred. Check server logs.'});
        }
        renderMessages();
      }else if(typeof trackBackgroundError==='function'){
        const _errTitle=(typeof _allSessions!=='undefined'&&_allSessions.find(s=>s.session_id===activeSid)||{}).title||null;
        try{const d=JSON.parse(e.data);trackBackgroundError(activeSid,_errTitle,d.message||'Error');}
        catch(_){trackBackgroundError(activeSid,_errTitle,'Error');}
      }
      if(!S.session||!INFLIGHT[S.session.session_id]){setBusy(false);setComposerStatus('');}
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
      if(_terminalStateReached){
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
              _wireSSE(new EventSource(new URL(`/api/chat/stream?stream_id=${encodeURIComponent(streamId)}`,location.origin).href,{withCredentials:true}));
              return;
            }
          }catch(_){}
          if(await _restoreSettledSession()) return;
          _handleStreamError();
        },1500);
        return;
      }
      if(await _restoreSettledSession()) return;
      _handleStreamError();
    });

    source.addEventListener('cancel',e=>{
      _terminalStateReached=true;
      source.close();
      delete INFLIGHT[activeSid];clearInflight();clearInflightState(activeSid);stopApprovalPolling();stopClarifyPolling();
      if(!_approvalSessionId||_approvalSessionId===activeSid) hideApprovalCard(true);
      if(!_clarifySessionId||_clarifySessionId===activeSid) hideClarifyCard(true);
      if(S.session&&S.session.session_id===activeSid){
        S.activeStreamId=null;const _cbc=$('btnCancel');if(_cbc)_cbc.style.display='none';
      }
      if(S.session&&S.session.session_id===activeSid){
        clearLiveToolCards();if(!assistantText)removeThinking();
        S.messages.push({role:'assistant',content:'*Task cancelled.*'});renderMessages();
      }
      renderSessionList();
      if(!S.session||!INFLIGHT[S.session.session_id]){setBusy(false);setComposerStatus('');}
    });
  }

  async function _restoreSettledSession(){
    try{
      const data=await api(`/api/session?session_id=${encodeURIComponent(activeSid)}`);
      const session=data&&data.session;
      if(!session) return false;
      if(session.active_stream_id||session.pending_user_message) return false;
      delete INFLIGHT[activeSid];clearInflight();clearInflightState(activeSid);stopApprovalPolling();stopClarifyPolling();
      _closeSource();
      if(!_approvalSessionId||_approvalSessionId===activeSid) hideApprovalCard(true);
      if(!_clarifySessionId||_clarifySessionId===activeSid) hideClarifyCard(true);
      if(S.session&&S.session.session_id===activeSid){
        S.activeStreamId=null;const _cbe=$('btnCancel');if(_cbe)_cbe.style.display='none';
        clearLiveToolCards();if(!assistantText)removeThinking();
        S.session=session;S.messages=session.messages||[];
        syncTopbar();renderMessages();
      }
      renderSessionList();setBusy(false);setComposerStatus('');
      return true;
    }catch(_){
      return false;
    }
  }

  function _handleStreamError(){
    delete INFLIGHT[activeSid];clearInflight();clearInflightState(activeSid);stopApprovalPolling();stopClarifyPolling();
    _closeSource();
    if(!_approvalSessionId||_approvalSessionId===activeSid) hideApprovalCard(true);
    if(!_clarifySessionId||_clarifySessionId===activeSid) hideClarifyCard(true);
    if(S.session&&S.session.session_id===activeSid){
      S.activeStreamId=null;const _cbe=$('btnCancel');if(_cbe)_cbe.style.display='none';
      clearLiveToolCards();if(!assistantText)removeThinking();
      S.messages.push({role:'assistant',content:'**Error:** Connection lost'});renderMessages();
    }else{
      if(typeof trackBackgroundError==='function'){
        const _errTitle=(typeof _allSessions!=='undefined'&&_allSessions.find(s=>s.session_id===activeSid)||{}).title||null;
        trackBackgroundError(activeSid,_errTitle,'Connection lost');
      }
    }
    if(!S.session||!INFLIGHT[S.session.session_id]){setBusy(false);setComposerStatus('');}
  }

  (async()=>{
    // Reattach path can carry stale stream ids after server restart; preflight
    // status avoids opening a dead SSE URL that will 404 in the console.
    if(reconnecting){
      try{
        const st=await api(`/api/chat/stream/status?stream_id=${encodeURIComponent(streamId)}`);
        if(!st.active){
          delete INFLIGHT[activeSid];
          clearInflight();
          clearInflightState(activeSid);
          stopApprovalPolling();
          stopClarifyPolling();
          if(!_approvalSessionId||_approvalSessionId===activeSid) hideApprovalCard(true);
          if(!_clarifySessionId||_clarifySessionId===activeSid) hideClarifyCard(true);
          if(S.session&&S.session.session_id===activeSid){
            S.activeStreamId=null;
            const _cbe=$('btnCancel');if(_cbe)_cbe.style.display='none';
            clearLiveToolCards();
            removeThinking();
            setBusy(false);
            setComposerStatus('');
            renderMessages();
            renderSessionList();
          }
          return;
        }
      }catch(_){}
    }
    _wireSSE(new EventSource(new URL(`/api/chat/stream?stream_id=${encodeURIComponent(streamId)}`,location.origin).href,{withCredentials:true}));
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

function showApprovalCard(pending, pendingCount) {
  const keys = pending.pattern_keys || (pending.pattern_key ? [pending.pattern_key] : []);
  const desc = (pending.description || "") + (keys.length ? " [" + keys.join(", ") + "]" : "");
  const cmd = pending.command || "";
  const sig = JSON.stringify({desc, cmd, sid: pending._session_id || (S.session && S.session.session_id) || null});
  const card = $("approvalCard");
  const sameApproval = card.classList.contains("visible") && _approvalSignature === sig;
  $("approvalDesc").textContent = desc;
  $("approvalCmd").textContent = cmd;
  _approvalSessionId = pending._session_id || (S.session && S.session.session_id) || null;
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
  if (!sameApproval) card.scrollIntoView({block:"nearest", behavior:"smooth"});
  // Apply current locale to data-i18n elements inside the card
  if (typeof applyLocaleToDOM === "function") applyLocaleToDOM();
  // Focus Allow once button so Enter works immediately
  const onceBtn = $("approvalBtnOnce");
  if (onceBtn) setTimeout(() => onceBtn.focus(), 50);
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
  _approvalPollTimer = setInterval(async () => {
    if (!S.busy || !S.session || S.session.session_id !== sid) {
      stopApprovalPolling(); hideApprovalCard(true); return;
    }
    try {
      const data = await api("/api/approval/pending?session_id=" + encodeURIComponent(sid));
      if (data.pending) { data.pending._session_id=sid; showApprovalCard(data.pending, data.pending_count||1); }
      else { hideApprovalCard(); }
    } catch(e) { /* ignore poll errors */ }
  }, 1500);
}

function stopApprovalPolling() {
  if (_approvalPollTimer) { clearInterval(_approvalPollTimer); _approvalPollTimer = null; }
}

// ── Clarify polling ──
let _clarifyPollTimer = null;
let _clarifyHideTimer = null;
let _clarifyVisibleSince = 0;
let _clarifySignature = '';
let _clarifySessionId = null;
let _clarifyMissingEndpointWarned = false;
const CLARIFY_MIN_VISIBLE_MS = 30000;

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

function _resetClarifyCardState() {
  _clearClarifyHideTimer();
  _clarifyVisibleSince = 0;
  _clarifySignature = '';
}

function hideClarifyCard(force=false) {
  const card = $("clarifyCard");
  if (!card) {
    _clarifySessionId = null;
    _resetClarifyCardState();
    if (typeof unlockComposerForClarify === "function") unlockComposerForClarify();
    return;
  }
  if (!force && _clarifyVisibleSince) {
    const remaining = CLARIFY_MIN_VISIBLE_MS - (Date.now() - _clarifyVisibleSince);
    if (remaining > 0) {
      const scheduledSignature = _clarifySignature;
      _clearClarifyHideTimer();
      _clarifyHideTimer = setTimeout(() => {
        _clarifyHideTimer = null;
        if (_clarifySignature !== scheduledSignature) return;
        hideClarifyCard(true);
      }, remaining);
      return;
    }
  }
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
  _clarifySessionId = pending._session_id || (S.session && S.session.session_id) || null;
  _clarifySignature = sig;
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
  const msgInner = $("msgInner");
  if (msgInner && card.parentElement !== msgInner) {
    msgInner.appendChild(card);
  }
  card.classList.add("visible");
  if (!sameClarify) card.scrollIntoView({block:"nearest", behavior:"smooth"});
  if (typeof applyLocaleToDOM === "function") applyLocaleToDOM();
  if (input && !sameClarify) setTimeout(() => input.focus(), 50);
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
  _clarifySetControlsDisabled(true, true);
  hideClarifyCard(true);
  try {
    await api("/api/clarify/respond", {
      method: "POST",
      body: JSON.stringify({ session_id: sid, response: value })
    });
  } catch(e) { setStatus(t("clarify_responding") + " " + e.message); }
}

function startClarifyPolling(sid) {
  stopClarifyPolling();
  _clarifyMissingEndpointWarned = false;
  _clarifyPollTimer = setInterval(async () => {
    if (!S.session || S.session.session_id !== sid) {
      stopClarifyPolling(); hideClarifyCard(true); return;
    }
    try {
      const data = await api("/api/clarify/pending?session_id=" + encodeURIComponent(sid));
      if (data.pending) { data.pending._session_id=sid; showClarifyCard(data.pending); }
      else { hideClarifyCard(); }
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
      // Ignore transient poll errors; SSE clarify event still provides a fast path.
    }
  }, 1500);
}

function stopClarifyPolling() {
  if (_clarifyPollTimer) { clearInterval(_clarifyPollTimer); _clarifyPollTimer = null; }
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

// ── Panel navigation (Chat / Tasks / Skills / Memory) ──
