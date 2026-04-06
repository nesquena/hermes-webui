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
      MSG_QUEUE.push(text);
      $('msg').value='';autoResize();
      updateQueueBadge();
      showToast(`Queued: "${text.slice(0,40)}${text.length>40?'\u2026':''}"`,2000);
    }
    return;
  }
  if(!S.session){await newSession();await renderSessionList();}

  const activeSid=S.session.session_id;

  setStatus(S.pendingFiles&&S.pendingFiles.length?'Uploading…':'Sending…');
  let uploaded=[];
  try{uploaded=await uploadPendingFiles();}
  catch(e){if(!text){setStatus(`❌ ${e.message}`);return;}}

  let msgText=text;
  if(uploaded.length&&!msgText)msgText=`I've uploaded ${uploaded.length} file(s): ${uploaded.join(', ')}`;
  else if(uploaded.length)msgText=`${text}\n\n[Attached files: ${uploaded.join(', ')}]`;
  if(!msgText){setStatus('Nothing to send');return;}

  $('msg').value='';autoResize();
  const displayText=text||(uploaded.length?`Uploaded: ${uploaded.join(', ')}`:'(file upload)');
  const userMsg={role:'user',content:displayText,attachments:uploaded.length?uploaded:undefined,_ts:Date.now()/1000};
  S.toolCalls=[];  // clear tool calls from previous turn
  clearLiveToolCards();  // clear any leftover live cards from last turn
  S.messages.push(userMsg);renderMessages();appendThinking();setBusy(true);  // activity bar shown via setBusy
  INFLIGHT[activeSid]={messages:[...S.messages],uploaded};
  startApprovalPolling(activeSid);
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
    // Show Cancel button
    const cancelBtn=$('btnCancel');
    if(cancelBtn) cancelBtn.style.display='';
  }catch(e){
    delete INFLIGHT[activeSid];
    stopApprovalPolling();
    // Only hide approval card if it belongs to the session that just finished
    if(!_approvalSessionId || _approvalSessionId===activeSid) hideApprovalCard();removeThinking();
    S.messages.push({role:'assistant',content:`**Error:** ${e.message}`});
    renderMessages();setBusy(false);setStatus('Error: '+e.message);
    return;
  }

  // Open SSE stream and render tokens live
  let assistantText='';
  let assistantRow=null;
  let assistantBody=null;

  function ensureAssistantRow(){
    if(assistantRow)return;
    removeThinking();
    const tr=$('toolRunningRow');if(tr)tr.remove();
    $('emptyState').style.display='none';
    assistantRow=document.createElement('div');assistantRow.className='msg-row';
    assistantBody=document.createElement('div');assistantBody.className='msg-body';
    const role=document.createElement('div');role.className='msg-role assistant';
    const icon=document.createElement('div');icon.className='role-icon assistant';icon.textContent=(window._botName||'Hermes').charAt(0).toUpperCase();
    const lbl=document.createElement('span');lbl.style.fontSize='12px';lbl.textContent=window._botName||'Hermes';
    const lbl=document.createElement('span');lbl.style.fontSize='12px';lbl.textContent=window._botName||'Hermes';
    role.appendChild(icon);role.appendChild(lbl);
    assistantRow.appendChild(role);assistantRow.appendChild(assistantBody);
    $('msgInner').appendChild(assistantRow);
  }

  // ── Shared SSE handler wiring (used for initial connection and reconnect) ──
  let _reconnectAttempted=false;

  // rAF-throttled rendering: buffer tokens, render at most once per frame
  let _renderPending=false;
  function _scheduleRender(){
    if(_renderPending) return;
    _renderPending=true;
    requestAnimationFrame(()=>{
      _renderPending=false;
      if(assistantBody) assistantBody.innerHTML=renderMd(assistantText);
      scrollIfPinned();
    });
  }

  function _wireSSE(source){
    source.addEventListener('token',e=>{
      if(!S.session||S.session.session_id!==activeSid) return;
      const d=JSON.parse(e.data);
      assistantText+=d.text;
      ensureAssistantRow();
      _scheduleRender();
    });

    source.addEventListener('tool',e=>{
      const d=JSON.parse(e.data);
      if(S.session&&S.session.session_id===activeSid){
        setStatus(`${d.name}${d.preview?' · '+d.preview.slice(0,55):''}`);
      }
      if(!S.session||S.session.session_id!==activeSid) return;
      removeThinking();
      const oldRow=$('toolRunningRow');if(oldRow)oldRow.remove();
      const tc={name:d.name, preview:d.preview||'', args:d.args||{}, snippet:'', done:false};
      S.toolCalls.push(tc);
      appendLiveToolCard(tc);
      scrollIfPinned();
    });

    source.addEventListener('approval',e=>{
      const d=JSON.parse(e.data);
      d._session_id=activeSid;
      showApprovalCard(d);
    });

    source.addEventListener('done',e=>{
      source.close();
      const d=JSON.parse(e.data);
      delete INFLIGHT[activeSid];
      clearInflight();
      stopApprovalPolling();
      if(!_approvalSessionId || _approvalSessionId===activeSid) hideApprovalCard();
      if(S.session&&S.session.session_id===activeSid){
        S.activeStreamId=null;
        const _cb=$('btnCancel');if(_cb)_cb.style.display='none';
      }
      if(S.session&&S.session.session_id===activeSid){
        S.session=d.session;S.messages=d.session.messages||[];
        // Stamp _ts on the last assistant message if it has no timestamp
        const lastAsst=[...S.messages].reverse().find(m=>m.role==='assistant');
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
        syncTopbar();renderMessages();loadDir('.');
      }
      renderSessionList();setBusy(false);setStatus('');
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
      // Application-level error sent explicitly by the server (rate limit, crash, etc.)
      // This is distinct from the SSE network 'error' event below.
      source.close();
      delete INFLIGHT[activeSid];clearInflight();stopApprovalPolling();
      if(!_approvalSessionId||_approvalSessionId===activeSid) hideApprovalCard();
      if(S.session&&S.session.session_id===activeSid){
        S.activeStreamId=null;const _cbe=$('btnCancel');if(_cbe)_cbe.style.display='none';
        clearLiveToolCards();if(!assistantText)removeThinking();
        try{
          const d=JSON.parse(e.data);
          const isRateLimit=d.type==='rate_limit';
          const icon=isRateLimit?'⏱️':'⚠️';
          const label=isRateLimit?'Rate limit reached':'Error';
          const hint=d.hint?`\n\n*${d.hint}*`:'';
          S.messages.push({role:'assistant',content:`**${icon} ${label}:** ${d.message}${hint}`});
        }catch(_){
          S.messages.push({role:'assistant',content:'**⚠️ Error:** An error occurred. Check server logs.'});
        }
        renderMessages();
      }else if(typeof trackBackgroundError==='function'){
        const _errTitle=(typeof _allSessions!=='undefined'&&_allSessions.find(s=>s.session_id===activeSid)||{}).title||null;
        try{const d=JSON.parse(e.data);trackBackgroundError(activeSid,_errTitle,d.message||'Error');}
        catch(_){trackBackgroundError(activeSid,_errTitle,'Error');}
      }
      if(!S.session||!INFLIGHT[S.session.session_id]){setBusy(false);setStatus('');}
    });

    source.addEventListener('warning',e=>{
      // Non-fatal warning from server (e.g. fallback activated, retrying)
      if(!S.session||S.session.session_id!==activeSid) return;
      try{
        const d=JSON.parse(e.data);
        // Show as a small inline notice, not a full error
        setStatus(`⚠️ ${d.message||'Warning'}`);
        // If it's a fallback notice, show it briefly then clear
        if(d.type==='fallback') setTimeout(()=>setStatus(''),4000);
      }catch(_){}
    });

    source.addEventListener('error',e=>{
      source.close();
      // Attempt one reconnect if the stream is still active server-side
      if(!_reconnectAttempted && streamId){
        _reconnectAttempted=true;
        setStatus('Connection lost \u2014 reconnecting\u2026');
        setTimeout(async()=>{
          try{
            const st=await api(`/api/chat/stream/status?stream_id=${encodeURIComponent(streamId)}`);
            if(st.active){
              setStatus('Reconnected');
              _wireSSE(new EventSource(new URL(`/api/chat/stream?stream_id=${encodeURIComponent(streamId)}`,location.origin).href,{withCredentials:true}));
              return;
            }
          }catch(_){}
          _handleStreamError();
        },1500);
        return;
      }
      _handleStreamError();
    });

    source.addEventListener('cancel',e=>{
      source.close();
      delete INFLIGHT[activeSid];clearInflight();stopApprovalPolling();
      if(!_approvalSessionId||_approvalSessionId===activeSid) hideApprovalCard();
      if(S.session&&S.session.session_id===activeSid){
        S.activeStreamId=null;const _cbc=$('btnCancel');if(_cbc)_cbc.style.display='none';
      }
      if(S.session&&S.session.session_id===activeSid){
        clearLiveToolCards();if(!assistantText)removeThinking();
        S.messages.push({role:'assistant',content:'*Task cancelled.*'});renderMessages();
      }
      renderSessionList();
      if(!S.session||!INFLIGHT[S.session.session_id]){setBusy(false);setStatus('');}
    });
  }

  function _handleStreamError(){
    delete INFLIGHT[activeSid];clearInflight();stopApprovalPolling();
    if(!_approvalSessionId||_approvalSessionId===activeSid) hideApprovalCard();
    if(S.session&&S.session.session_id===activeSid){
      S.activeStreamId=null;const _cbe=$('btnCancel');if(_cbe)_cbe.style.display='none';
      clearLiveToolCards();if(!assistantText)removeThinking();
      S.messages.push({role:'assistant',content:'**Error:** Connection lost'});renderMessages();
    }else{
      // User switched away — show background error banner
      if(typeof trackBackgroundError==='function'){
        // Look up session title from the session list cache so the banner names it correctly
        const _errTitle=(typeof _allSessions!=='undefined'&&_allSessions.find(s=>s.session_id===activeSid)||{}).title||null;
        trackBackgroundError(activeSid,_errTitle,'Connection lost');
      }
    }
    if(!S.session||!INFLIGHT[S.session.session_id]){setBusy(false);setStatus('Error: Connection lost');}
  }

  _wireSSE(new EventSource(new URL(`/api/chat/stream?stream_id=${encodeURIComponent(streamId)}`,location.origin).href,{withCredentials:true}));

}

function transcript(){
  const bn=window._botName||'Hermes';
  const lines=[`# ${bn} session ${S.session?.session_id||''}`,``,
    `Workspace: ${S.session?.workspace||''}`,`Model: ${S.session?.model||''}`,``];
  for(const m of S.messages){
    if(!m||m.role==='tool')continue;
    let c=m.content||'';
    if(Array.isArray(c))c=c.filter(p=>p&&p.type==='text').map(p=>p.text||'').join('\n');
    const ct=String(c).trim();
    if(!ct&&!m.attachments?.length)continue;
    const attach=m.attachments?.length?`\n\n_Files: ${m.attachments.join(', ')}_`:'';
    const label=m.role==='user'?'user':bn;
    lines.push(`## ${label}`,'',ct+attach,'');
  }
  return lines.join('\n');
}

function autoResize(){const el=$('msg');el.style.height='auto';el.style.height=Math.min(el.scrollHeight,200)+'px';updateSendBtn();}


// ── Approval polling ──
let _approvalPollTimer = null;

// showApprovalCard moved above respondApproval

function hideApprovalCard() {
  $("approvalCard").classList.remove("visible");
  $("approvalCmd").textContent = "";
  $("approvalDesc").textContent = "";
}

// Track session_id of the active approval so respond goes to the right session
let _approvalSessionId = null;

function showApprovalCard(pending) {
  $("approvalDesc").textContent = pending.description || "";
  $("approvalCmd").textContent = pending.command || "";
  const keys = pending.pattern_keys || (pending.pattern_key ? [pending.pattern_key] : []);
  $("approvalDesc").textContent = (pending.description || "") + (keys.length ? " [" + keys.join(", ") + "]" : "");
  _approvalSessionId = pending._session_id || (S.session && S.session.session_id) || null;
  $("approvalCard").classList.add("visible");
}

async function respondApproval(choice) {
  const sid = _approvalSessionId || (S.session && S.session.session_id);
  if (!sid) return;
  hideApprovalCard();
  _approvalSessionId = null;
  try {
    await api("/api/approval/respond", {
      method: "POST",
      body: JSON.stringify({ session_id: sid, choice })
    });
  } catch(e) { setStatus("Approval error: " + e.message); }
}

function startApprovalPolling(sid) {
  stopApprovalPolling();
  _approvalPollTimer = setInterval(async () => {
    if (!S.busy || !S.session || S.session.session_id !== sid) {
      stopApprovalPolling(); hideApprovalCard(); return;
    }
    try {
      const data = await api("/api/approval/pending?session_id=" + encodeURIComponent(sid));
      if (data.pending) { data.pending._session_id=sid; showApprovalCard(data.pending); }
      else { hideApprovalCard(); }
    } catch(e) { /* ignore poll errors */ }
  }, 1500);
}

function stopApprovalPolling() {
  if (_approvalPollTimer) { clearInterval(_approvalPollTimer); _approvalPollTimer = null; }
}
// ── Panel navigation (Chat / Tasks / Skills / Memory) ──

