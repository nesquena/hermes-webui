async function send(){
  const text=$('msg').value.trim();
  if(!text&&!S.pendingFiles.length)return;
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

  // Append workspace file/dir context if user has navigated somewhere
  const _wsCtxFile = typeof _previewCurrentPath==='string' && _previewCurrentPath ? _previewCurrentPath : null;
  const _wsCtxDir  = typeof _currentDir==='string' && _currentDir && _currentDir!=='.' ? _currentDir : null;
  const _wsCtx = _wsCtxFile || _wsCtxDir;
  if(_wsCtx){
    let ctxStr=`[Workspace context: ${_wsCtx}`;
    
    // If viewing a file, append its tags if any exist
    if(_wsCtxFile && typeof getFileTags==='function'){
      const fileTags = getFileTags(_wsCtxFile);
      if(fileTags.length) ctxStr+=` (tags: ${fileTags.join(', ')})`;
    }
    
    // Append folder label if one exists for the directory
    // When viewing a file, also include the parent folder's label
    const pathForLabel = _wsCtxFile || _wsCtx;
    const folderLabel = typeof getFolderLabel==='function' ? getFolderLabel(pathForLabel) : '';
    if(folderLabel) ctxStr+=` (folder-label: ${folderLabel})`;
    
    ctxStr+=']';
    msgText += `\n\n${ctxStr}`;
  }

  $('msg').value='';autoResize();
  const displayText=text||(uploaded.length?`Uploaded: ${uploaded.join(', ')}`:'(file upload)');
  const userMsg={role:'user',content:displayText,attachments:uploaded.length?uploaded:undefined,_ts:Date.now()/1000};
  S.toolCalls=[];  // clear tool calls from previous turn
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
    // Patch cache and re-render without a network call
    const _cached=_allSessions.find(s=>s.session_id===activeSid);
    if(_cached) _cached.title=provisionalTitle;
    refreshSessionList();
  } else {
    refreshSessionList();
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
  let _renderPending=false; // rAF throttle flag

  function ensureAssistantRow(){
    if(assistantRow)return;
    removeThinking();
    const tr=$('toolRunningRow');if(tr)tr.remove();
    $('emptyState').style.display='none';
    assistantRow=document.createElement('div');assistantRow.className='msg-row assistant-row';
    assistantBody=document.createElement('div');assistantBody.className='msg-body';
    const role=document.createElement('div');role.className='msg-role assistant';
    const icon=document.createElement('div');icon.className='role-icon assistant';icon.textContent='H';
    const lbl=document.createElement('span');lbl.style.fontSize='12px';lbl.textContent='Hermes';
    role.appendChild(icon);role.appendChild(lbl);
    assistantRow.appendChild(role);assistantRow.appendChild(assistantBody);
    // Tool cards are now appended directly into msgInner (inline), no flush needed.
    $('msgInner').appendChild(assistantRow);
  }

  // Throttled render: batch token updates to one DOM write per animation frame
  function scheduleRender(){
    if(_renderPending) return;
    _renderPending=true;
    requestAnimationFrame(()=>{
      _renderPending=false;
      if(assistantBody) assistantBody.innerHTML=renderMd(assistantText);
      scrollIfPinned();
    });
  }

  // ── SSE event handlers ──
  // Each handler is a named function for readability. They close over
  // activeSid, streamId, uploaded, and the assistant* streaming state.

  function isActiveSession(){
    return S.session && S.session.session_id===activeSid;
  }

  // Shared cleanup for done/cancel/error: tear down inflight tracking,
  // approval polling, cancel button, and activeStreamId.
  function cleanupStream(){
    delete INFLIGHT[activeSid];
    clearInflight();
    stopApprovalPolling();
    if(!_approvalSessionId || _approvalSessionId===activeSid) hideApprovalCard();
    if(isActiveSession()){
      S.activeStreamId=null;
      const cb=$('btnCancel'); if(cb) cb.style.display='none';
    }
  }

  function handleToken(e){
    if(!isActiveSession()) return;
    const d=JSON.parse(e.data);
    assistantText+=d.text;
    ensureAssistantRow();
    scheduleRender();
  }

  function handleTool(e){
    const d=JSON.parse(e.data);
    if(isActiveSession()){
      setStatus(`${d.name}${d.preview?' · '+d.preview.slice(0,55):''}`);
    }
    if(!isActiveSession()) return;
    removeThinking();
    const oldRow=$('toolRunningRow'); if(oldRow) oldRow.remove();
    const tc={tid:d.tid||'', name:d.name, preview:d.preview||'', args:d.args||{}, snippet:'', done:false};
    S.toolCalls.push(tc);
    // Append tool card directly into msgInner (inline with messages)
    const card=buildToolCard(tc);
    if(tc.tid) card.dataset.tid=tc.tid;
    $('msgInner').appendChild(card);
    scrollIfPinned();
  }

  function handleToolDone(e){
    const d=JSON.parse(e.data);
    if(!isActiveSession()) return;
    // Find the matching tool call by tid (if available) or by name (fallback)
    let tc=null;
    if(d.tid){
      tc=S.toolCalls.find(t=>t.tid===d.tid);
    }
    if(!tc){
      // Fallback: match by name, preferring the last incomplete one
      tc=[...S.toolCalls].reverse().find(t=>t.name===d.name&&!t.done);
    }
    if(tc){
      tc.done=true;
      tc.snippet=d.snippet||'';
      // Update the live tool card inline in msgInner
      const container=$('msgInner');
      if(container){
        const card=container.querySelector(`.tool-card-row[data-tid="${CSS.escape(d.tid||'')}"]`);
        if(card){
          const updated=buildToolCard(tc);
          if(tc.tid) updated.dataset.tid=tc.tid;
          card.replaceWith(updated);
        }
      }
    }
    scrollIfPinned();
  }

  function handleTurnEnd(e){
    if(!isActiveSession()) return;
    // Reset for potential next assistant turn (agent may emit more text after tools)
    assistantText='';
    assistantRow=null;
    assistantBody=null;
  }

  function handleApproval(e){
    const d=JSON.parse(e.data);
    d._session_id=activeSid;
    showApprovalCard(d);
  }

  function handleClarify(e){
    const d=JSON.parse(e.data);
    showClarifyDialog(d.question, d.choices||[], d.stream_id||streamId);
  }

  function handleTitle(e){
    const d=JSON.parse(e.data);
    if(!d.title) return;
    if(S.session&&S.session.session_id===d.session_id){
      S.session.title=d.title;
      syncTopbar();
    }
    const cached=_allSessions.find(s=>s.session_id===d.session_id);
    if(cached) cached.title=d.title;
    refreshSessionList();
  }

  function handleDone(source, e){
    source.close();
    const d=JSON.parse(e.data);
    cleanupStream();
    if(isActiveSession()){
      S.session=d.session; S.messages=d.session.messages||[];
      if(d.usage) S.usage=d.usage;
      if(d.session.tool_calls&&d.session.tool_calls.length){
        S.toolCalls=d.session.tool_calls.map(tc=>({...tc,done:true}));
      } else {
        S.toolCalls=S.toolCalls.map(tc=>({...tc,done:true}));
      }
      if(uploaded.length){
        const lastUser=[...S.messages].reverse().find(m=>m.role==='user');
        if(lastUser) lastUser.attachments=uploaded;
      }
      S.busy=false;
      syncTopbar(); renderMessages();
      loadDir(_currentDir||'.');
    }
    refreshSessionList(); setBusy(false); setStatus('');
  }

  function handleCancel(source, e){
    source.close();
    cleanupStream();
    if(isActiveSession()){
      if(!assistantText) removeThinking();
      S.messages.push({role:'assistant',content:'*Task cancelled.*'}); renderMessages();
    }
    refreshSessionList();
    if(!S.session||!INFLIGHT[S.session.session_id]){ setBusy(false); setStatus(''); }
  }

  function _handleStreamError(){
    cleanupStream();
    if(isActiveSession()){
      if(!assistantText) removeThinking();
      S.messages.push({role:'assistant',content:'**Error:** Connection lost'}); renderMessages();
    }else{
      if(typeof trackBackgroundError==='function'){
        const _errTitle=(typeof _allSessions!=='undefined'&&_allSessions.find(s=>s.session_id===activeSid)||{}).title||null;
        trackBackgroundError(activeSid,_errTitle,'Connection lost');
      }
    }
    if(!S.session||!INFLIGHT[S.session.session_id]){ setBusy(false); setStatus('Error: Connection lost'); }
  }

  // ── Wire SSE events (used for initial connection and reconnect) ──
  let _reconnectAttempted=false;

  function _wireSSE(source){
    source.addEventListener('token', handleToken);
    source.addEventListener('tool', handleTool);
    source.addEventListener('tool_done', handleToolDone);
    source.addEventListener('turn_end', handleTurnEnd);
    source.addEventListener('approval', handleApproval);
    source.addEventListener('clarify', handleClarify);
    source.addEventListener('title', handleTitle);
    source.addEventListener('done', e => handleDone(source, e));
    source.addEventListener('cancel', e => handleCancel(source, e));
    source.addEventListener('error', e=>{
      source.close();
      if(!_reconnectAttempted && streamId){
        _reconnectAttempted=true;
        setStatus('Connection lost \u2014 reconnecting\u2026');
        setTimeout(async()=>{
          try{
            const st=await api(`/api/chat/stream/status?stream_id=${encodeURIComponent(streamId)}`);
            if(st.active){
              setStatus('Reconnected');
              _wireSSE(new EventSource(`/api/chat/stream?stream_id=${encodeURIComponent(streamId)}`,{withCredentials:true}));
              return;
            }
          }catch(_){}
          _handleStreamError();
        },1500);
        return;
      }
      _handleStreamError();
    });
  }

  _wireSSE(new EventSource(`/api/chat/stream?stream_id=${encodeURIComponent(streamId)}`,{withCredentials:true}));

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

function autoResize(){const el=$('msg');el.style.height='auto';el.style.height=Math.min(el.scrollHeight,200)+'px';}


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
  }, 5000);
}

function stopApprovalPolling() {
  if (_approvalPollTimer) { clearInterval(_approvalPollTimer); _approvalPollTimer = null; }
}
// ── Clarify dialog ──
let _clarifyStreamId = null;

function showClarifyDialog(question, choices, streamId) {
  _clarifyStreamId = streamId;
  // Create or reuse overlay
  let overlay = $('clarifyOverlay');
  if (!overlay) {
    overlay = document.createElement('div');
    overlay.id = 'clarifyOverlay';
    overlay.className = 'clarify-overlay';
    document.body.appendChild(overlay);
  }
  let choicesHtml = '';
  if (choices && choices.length) {
    choicesHtml = '<div class="clarify-choices">' +
      choices.map((c, i) => `<button class="clarify-choice-btn" onclick="respondClarify(this.textContent)">${esc(c)}</button>`).join('') +
      '<button class="clarify-choice-btn clarify-other" onclick="showClarifyFreeform()">Other...</button>' +
      '</div>';
  }
  overlay.innerHTML = `
    <div class="clarify-card">
      <div class="clarify-header"><span class="clarify-icon">&#10067;</span> Hermes needs your input</div>
      <div class="clarify-question">${esc(question)}</div>
      ${choicesHtml}
      <div class="clarify-freeform" ${choices && choices.length ? 'style="display:none"' : ''}>
        <textarea class="clarify-input" placeholder="Type your answer..." rows="2"></textarea>
        <button class="clarify-submit" onclick="respondClarifyFreeform()">Submit</button>
      </div>
    </div>`;
  overlay.style.display = 'flex';
  // Focus textarea if no choices
  if (!choices || !choices.length) {
    const ta = overlay.querySelector('.clarify-input');
    if (ta) setTimeout(() => ta.focus(), 100);
  }
  // Enter key submits freeform
  const ta = overlay.querySelector('.clarify-input');
  if (ta) ta.addEventListener('keydown', e => {
    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); respondClarifyFreeform(); }
  });
}

function showClarifyFreeform() {
  const overlay = $('clarifyOverlay');
  if (!overlay) return;
  const ff = overlay.querySelector('.clarify-freeform');
  if (ff) { ff.style.display = ''; }
  const ta = overlay.querySelector('.clarify-input');
  if (ta) ta.focus();
}

async function respondClarify(text) {
  const overlay = $('clarifyOverlay');
  if (overlay) overlay.style.display = 'none';
  if (!_clarifyStreamId) return;
  try {
    await api('/api/clarify/respond', {
      method: 'POST',
      body: JSON.stringify({ stream_id: _clarifyStreamId, response: text })
    });
  } catch(e) { setStatus('Clarify error: ' + e.message); }
  _clarifyStreamId = null;
}

async function respondClarifyFreeform() {
  const overlay = $('clarifyOverlay');
  if (!overlay) return;
  const ta = overlay.querySelector('.clarify-input');
  const text = (ta ? ta.value.trim() : '') || 'Use your best judgement.';
  await respondClarify(text);
}

// ── Panel navigation (Chat / Tasks / Skills / Memory) ──

