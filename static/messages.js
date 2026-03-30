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

  setStatus('Uploading…');
  let uploaded=[];
  try{uploaded=await uploadPendingFiles();}
  catch(e){if(!text){setStatus(`❌ ${e.message}`);return;}}

  let msgText=text;
  if(uploaded.length&&!msgText)msgText=`I've uploaded ${uploaded.length} file(s): ${uploaded.join(', ')}`;
  else if(uploaded.length)msgText=`${text}\n\n[Attached files: ${uploaded.join(', ')}]`;
  if(!msgText){setStatus('Nothing to send');return;}

  $('msg').value='';autoResize();
  const displayText=text||(uploaded.length?`Uploaded: ${uploaded.join(', ')}`:'(file upload)');
  const userMsg={role:'user',content:displayText,attachments:uploaded.length?uploaded:undefined};
  S.messages.push(userMsg);renderMessages();appendThinking();setBusy(true);  // activity bar shown via setBusy
  INFLIGHT[activeSid]={messages:[...S.messages],uploaded};
  startApprovalPolling(activeSid);

  // Start the agent via POST, get a stream_id back
  let streamId;
  try{
    const startData=await api('/api/chat/start',{method:'POST',body:JSON.stringify({
      session_id:activeSid,message:msgText,
      model:$('modelSelect').value,workspace:S.session.workspace
    })});
    streamId=startData.stream_id;
    markInflight(activeSid, streamId);
  }catch(e){
    delete INFLIGHT[activeSid];
    stopApprovalPolling();hideApprovalCard();removeThinking();
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
    const icon=document.createElement('div');icon.className='role-icon assistant';icon.textContent='H';
    const lbl=document.createElement('span');lbl.style.fontSize='12px';lbl.textContent='Hermes';
    role.appendChild(icon);role.appendChild(lbl);
    assistantRow.appendChild(role);assistantRow.appendChild(assistantBody);
    $('msgInner').appendChild(assistantRow);
  }

  const es=new EventSource(`/api/chat/stream?stream_id=${encodeURIComponent(streamId)}`);

  es.addEventListener('token',e=>{
    const d=JSON.parse(e.data);
    assistantText+=d.text;
    ensureAssistantRow();
    assistantBody.innerHTML=renderMd(assistantText);
    $('messages').scrollTop=$('messages').scrollHeight;
  });

  es.addEventListener('tool',e=>{
    const d=JSON.parse(e.data);
    // B10: replace thinking dots with a tool-running indicator (not both at once)
    removeThinking();
    setStatus(`${d.name}${d.preview?' · '+d.preview.slice(0,55):''}`);
    // Show a compact "working" row if no tokens have arrived yet
    if(!assistantRow){
      const toolRow=document.createElement('div');toolRow.id='toolRunningRow';toolRow.className='msg-row';
      const icon=document.createElement('div');icon.className='role-icon assistant';icon.textContent='H';
      const lbl=document.createElement('div');lbl.className='msg-role assistant';lbl.appendChild(icon);lbl.appendChild(document.createTextNode('Hermes'));
      const body=document.createElement('div');body.className='msg-body';body.style.cssText='color:var(--muted);font-size:13px;font-style:italic;';
      body.innerHTML=`&#9881; ${d.name} <span style="opacity:.55;font-size:12px">running…</span>`;
      toolRow.appendChild(lbl);toolRow.appendChild(body);
      // Remove any existing tool row before adding new one
      const old=$('toolRunningRow');if(old)old.remove();
      $('msgInner').appendChild(toolRow);
      $('messages').scrollTop=$('messages').scrollHeight;
    }
  });

  es.addEventListener('approval',e=>{
    const d=JSON.parse(e.data);
    showApprovalCard(d);
  });

  es.addEventListener('done',e=>{
    es.close();
    const d=JSON.parse(e.data);
    delete INFLIGHT[activeSid];
    clearInflight();
    stopApprovalPolling();hideApprovalCard();
    if(S.session&&S.session.session_id===activeSid){
      S.session=d.session;S.messages=d.session.messages||[];
      if(uploaded.length){
        const lastUser=[...S.messages].reverse().find(m=>m.role==='user');
        if(lastUser)lastUser.attachments=uploaded;
      }
      syncTopbar();renderMessages();loadDir('.');
    }
    renderSessionList();setBusy(false);setStatus('');
  });

  es.addEventListener('error',e=>{
    es.close();
    delete INFLIGHT[activeSid];
    clearInflight();
    stopApprovalPolling();hideApprovalCard();
    let msg='Connection error';
    try{const d=JSON.parse(e.data);msg=d.message||msg;}catch(_){}
    if(S.session&&S.session.session_id===activeSid){
      if(!assistantText){removeThinking();}
      S.messages.push({role:'assistant',content:`**Error:** ${msg}`});
      renderMessages();
    }
    setBusy(false);setStatus('Error: '+msg);
  });

  // Handle SSE connection errors (network drop etc)
  es.onerror=()=>{
    if(es.readyState===EventSource.CLOSED){
      delete INFLIGHT[activeSid];
      stopApprovalPolling();hideApprovalCard();
      if(!assistantText&&S.session&&S.session.session_id===activeSid){
        removeThinking();
        S.messages.push({role:'assistant',content:'**Error:** Connection lost'});
        renderMessages();
      }
      setBusy(false);setStatus('');
    }
  };
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

function showApprovalCard(pending) {
  $("approvalDesc").textContent = pending.description || "";
  $("approvalCmd").textContent = pending.command || "";
  // show pattern key(s) for context
  const keys = pending.pattern_keys || (pending.pattern_key ? [pending.pattern_key] : []);
  $("approvalDesc").textContent = (pending.description || "") + (keys.length ? " [" + keys.join(", ") + "]" : "");
  $("approvalCard").classList.add("visible");
}

function hideApprovalCard() {
  $("approvalCard").classList.remove("visible");
  $("approvalCmd").textContent = "";
  $("approvalDesc").textContent = "";
}

async function respondApproval(choice) {
  if (!S.session) return;
  hideApprovalCard();
  try {
    await api("/api/approval/respond", {
      method: "POST",
      body: JSON.stringify({ session_id: S.session.session_id, choice })
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
      if (data.pending) { showApprovalCard(data.pending); }
      else { hideApprovalCard(); }
    } catch(e) { /* ignore poll errors */ }
  }, 1500);
}

function stopApprovalPolling() {
  if (_approvalPollTimer) { clearInterval(_approvalPollTimer); _approvalPollTimer = null; }
}
// ── Panel navigation (Chat / Tasks / Skills / Memory) ──

