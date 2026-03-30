async function newSession(flash){
  MSG_QUEUE.length=0;updateQueueBadge();
  const inheritWs=S.session?S.session.workspace:null;
  const data=await api('/api/session/new',{method:'POST',body:JSON.stringify({model:$('modelSelect').value,workspace:inheritWs})});
  S.session=data.session;S.messages=data.session.messages||[];
  if(flash)S.session._flash=true;
  localStorage.setItem('hermes-webui-session',S.session.session_id);
  syncTopbar();await loadDir('.');renderMessages();
  // don't call renderSessionList here - callers do it when needed
}

async function loadSession(sid){
  stopApprovalPolling();hideApprovalCard();
  const data=await api(`/api/session?session_id=${encodeURIComponent(sid)}`);
  S.session=data.session;
  localStorage.setItem('hermes-webui-session',S.session.session_id);
  // B9: sanitize empty assistant messages that can appear when agent only ran tool calls
  data.session.messages=(data.session.messages||[]).filter(m=>{
    if(!m||!m.role)return false;
    if(m.role==='tool')return false;
    if(m.role==='assistant'){let c=m.content||'';if(Array.isArray(c))c=c.filter(p=>p&&p.type==='text').map(p=>p.text||'').join('');return String(c).trim().length>0;}
    return true;
  });
  if(INFLIGHT[sid]){
    S.messages=INFLIGHT[sid].messages;
    syncTopbar();await loadDir('.');renderMessages();appendThinking();
    setBusy(true);setStatus('Hermes is thinking\u2026');
    startApprovalPolling(sid);
  }else{
    MSG_QUEUE.length=0;updateQueueBadge();  // clear queue when switching sessions
    S.messages=data.session.messages||[];
    syncTopbar();await loadDir('.');renderMessages();highlightCode();
  }
}

let _allSessions = [];  // cached for search filter
let _renamingSid = null;  // session_id currently being renamed (blocks list re-renders)

async function renderSessionList(){
  try{
    if(!($('sessionSearch').value||'').trim()) _contentSearchResults = [];
    const data=await api('/api/sessions');
    _allSessions = data.sessions||[];
    renderSessionListFromCache();  // no-ops if rename is in progress
  }catch(e){console.warn('renderSessionList',e);}
}

let _searchDebounceTimer = null;
let _contentSearchResults = [];  // results from /api/sessions/search content scan

function filterSessions(){
  // Immediate client-side title filter (no flicker)
  renderSessionListFromCache();
  // Debounced content search via API for message text
  const q = ($('sessionSearch').value || '').trim();
  clearTimeout(_searchDebounceTimer);
  if (!q) { _contentSearchResults = []; return; }
  _searchDebounceTimer = setTimeout(async () => {
    try {
      const data = await api(`/api/sessions/search?q=${encodeURIComponent(q)}&content=1&depth=5`);
      const titleIds = new Set(_allSessions.filter(s => (s.title||'Untitled').toLowerCase().includes(q.toLowerCase())).map(s=>s.session_id));
      _contentSearchResults = (data.sessions||[]).filter(s => s.match_type === 'content' && !titleIds.has(s.session_id));
      renderSessionListFromCache();
    } catch(e) { /* ignore */ }
  }, 350);
}

function renderSessionListFromCache(){
  // Don't re-render while user is actively renaming a session (would destroy the input)
  if(_renamingSid) return;
  const q=($('sessionSearch').value||'').toLowerCase();
  const titleMatches=q?_allSessions.filter(s=>(s.title||'Untitled').toLowerCase().includes(q)):_allSessions;
  // Merge content matches (deduped): content matches appended after title matches
  const titleIds=new Set(titleMatches.map(s=>s.session_id));
  const sessions=q?[...titleMatches,..._contentSearchResults.filter(s=>!titleIds.has(s.session_id))]:titleMatches;
  const list=$('sessionList');list.innerHTML='';
  // Date grouping: Today / Yesterday / Earlier
  const now=Date.now();
  const ONE_DAY=86400000;
  let lastGroup='';
  for(const s of sessions.slice(0,50)){
    const ts=(s.updated_at||0)*1000;
    const group=ts>now-ONE_DAY?'Today':ts>now-2*ONE_DAY?'Yesterday':'Earlier';
    if(group!==lastGroup){
      lastGroup=group;
      const hdr=document.createElement('div');
      hdr.style.cssText='font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:.08em;color:var(--muted);padding:10px 10px 4px;opacity:.8;';
      hdr.textContent=group;
      list.appendChild(hdr);
    }
    const el=document.createElement('div');
    const isActive=S.session&&s.session_id===S.session.session_id;
    el.className='session-item'+(isActive?' active':'')+(isActive&&S.session&&S.session._flash?' new-flash':'');
    if(isActive&&S.session&&S.session._flash)delete S.session._flash;
    const title=document.createElement('span');
    title.className='session-title';title.textContent=s.title||'Untitled';
    title.title='Double-click to rename';

    // Rename: called directly when we confirm it's a double-click
    const startRename=()=>{
      _renamingSid = s.session_id;
      const inp=document.createElement('input');
      inp.className='session-title-input';
      inp.value=s.title||'Untitled';
      ['click','mousedown','dblclick','pointerdown'].forEach(ev=>
        inp.addEventListener(ev, e2=>e2.stopPropagation())
      );
      const finish=async(save)=>{
        _renamingSid = null;
        if(save){
          const newTitle=inp.value.trim()||'Untitled';
          title.textContent=newTitle;
          s.title=newTitle;
          if(S.session&&S.session.session_id===s.session_id){S.session.title=newTitle;syncTopbar();}
          try{await api('/api/session/rename',{method:'POST',body:JSON.stringify({session_id:s.session_id,title:newTitle})});}
          catch(err){setStatus('Rename failed: '+err.message);}
        }
        inp.replaceWith(title);
        // Allow list re-renders again after a short delay
        setTimeout(()=>{ if(_renamingSid===null) renderSessionListFromCache(); },50);
      };
      inp.onkeydown=e2=>{
        if(e2.key==='Enter'){e2.preventDefault();e2.stopPropagation();finish(true);}
        if(e2.key==='Escape'){e2.preventDefault();e2.stopPropagation();finish(false);}
      };
      // onblur: cancel only -- no accidental saves
      inp.onblur=()=>{ if(_renamingSid===s.session_id) finish(false); };
      title.replaceWith(inp);
      setTimeout(()=>{inp.focus();inp.select();},10);
    };

    const trash=document.createElement('button');
    trash.className='session-trash';trash.innerHTML='&#128465;';trash.title='Delete';
    trash.onclick=async(e)=>{e.stopPropagation();e.preventDefault();await deleteSession(s.session_id);};
    el.appendChild(title);el.appendChild(trash);

    // Use a click timer to distinguish single-click (navigate) from double-click (rename).
    // This prevents loadSession from firing on the first click of a double-click,
    // which would re-render the list and destroy the dblclick target before it fires.
    let _clickTimer=null;
    el.onclick=async(e)=>{
      if(_renamingSid) return; // ignore while any rename is active
      if(e.target===trash||trash.contains(e.target)) return; // trash handles itself
      clearTimeout(_clickTimer);
      _clickTimer=setTimeout(async()=>{
        _clickTimer=null;
        if(_renamingSid) return;
        await loadSession(s.session_id);renderSessionListFromCache();
      }, 220);
    };
    el.ondblclick=async(e)=>{
      e.stopPropagation();
      e.preventDefault();
      clearTimeout(_clickTimer); // cancel the pending single-click navigation
      _clickTimer=null;
      startRename();
    };
    list.appendChild(el);
  }
}

async function deleteSession(sid){
  if(!confirm('Delete this conversation?'))return;
  try{
    await api('/api/session/delete',{method:'POST',body:JSON.stringify({session_id:sid})});
  }catch(e){setStatus(`Delete failed: ${e.message}`);return;}
  if(S.session&&S.session.session_id===sid){
    S.session=null;S.messages=[];S.entries=[];
    localStorage.removeItem('hermes-webui-session');
    // load the most recent remaining session, or show blank if none left
    const remaining=await api('/api/sessions');
    if(remaining.sessions&&remaining.sessions.length){
      await loadSession(remaining.sessions[0].session_id);
    }else{
      $('topbarTitle').textContent='Hermes';
      $('topbarMeta').textContent='Start a new conversation';
      $('msgInner').innerHTML='';
      $('emptyState').style.display='';
      $('fileTree').innerHTML='';
    }
  }
  showToast('Conversation deleted');
  await renderSessionList();
}


