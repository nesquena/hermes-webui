async function newSession(flash){
  MSG_QUEUE.length=0;updateQueueBadge();
  S.toolCalls=[];
  const inheritWs=S.session?S.session.workspace:null;
  const data=await api('/api/session/new',{method:'POST',body:JSON.stringify({model:$('modelSelect').value,workspace:inheritWs})});
  S.session=data.session;S.messages=data.session.messages||[];
  if(flash)S.session._flash=true;
  history.replaceState({s:S.session.session_id},'','?s='+encodeURIComponent(S.session.session_id));
  syncTopbar();await loadDir('.');renderMessages();
  // don't call renderSessionList here - callers do it when needed
}

async function loadSession(sid){
  stopApprovalPolling();hideApprovalCard();
  const data=await api(`/api/session?session_id=${encodeURIComponent(sid)}`);
  S.session=data.session;
  history.pushState({s:sid},'','?s='+encodeURIComponent(sid));
  // B9: sanitize empty assistant messages that can appear when agent only ran tool calls
  data.session.messages=(data.session.messages||[]).filter(m=>{
    if(!m||!m.role)return false;
    if(m.role==='tool')return false;
    if(m.role==='assistant'){let c=m.content||'';if(Array.isArray(c))c=c.filter(p=>p&&p.type==='text').map(p=>p.text||'').join('');return String(c).trim().length>0;}
    return true;
  });
  if(INFLIGHT[sid]){
    S.messages=INFLIGHT[sid].messages;
    // Render messages first, then append in-flight tool cards directly into msgInner
    syncTopbar();await loadDir('.');renderMessages();
    for(const tc of (S.toolCalls||[])){
      if(tc&&tc.name){
        const card=buildToolCard(tc);
        if(tc.tid) card.dataset.tid=tc.tid;
        $('msgInner').appendChild(card);
      }
    }
    appendThinking();
    setBusy(true);setStatus('Hermes is thinking\u2026');
    startApprovalPolling(sid);
  }else{
    MSG_QUEUE.length=0;updateQueueBadge();  // clear queue for the viewed session
    S.messages=data.session.messages||[];
    S.toolCalls=(data.session.tool_calls||[]).map(tc=>({...tc,done:true}));
    // Reset per-session visual state: the viewed session is idle even if another
    // session's stream is still running in the background.
    // We directly update the DOM instead of calling setBusy(false), because
    // setBusy(false) drains MSG_QUEUE which we don't want here.
    S.busy=false;
    S.activeStreamId=null;
    $('btnSend').disabled=false;
    $('btnSend').style.opacity='1';
    const _dots=$('activityDots');if(_dots)_dots.style.display='none';
    const _cb=$('btnCancel');if(_cb)_cb.style.display='none';
    setStatus('');
    syncTopbar();await loadDir('.');renderMessages();highlightCode();
  }
}

let _allSessions = [];  // cached for search filter
let _renamingSid = null;  // session_id currently being renamed (blocks list re-renders)
let _showArchived = false;  // toggle to show archived sessions
let _subagentMap = {};   // parent_session_id -> [child session objects]
let _childSessionIds = new Set();  // session_ids that are subagents (hidden from main list)
let _expandedParents = {};  // parent_session_id -> true/false collapse state
try{_expandedParents=JSON.parse(localStorage.getItem('hermes-subagent-expanded')||'{}');}catch(e){}
function _saveExpandedParents(){try{localStorage.setItem('hermes-subagent-expanded',JSON.stringify(_expandedParents));}catch(e){}}

function _buildSubagentMap(){
  _subagentMap={};
  _childSessionIds=new Set();
  for(const s of _allSessions){
    if(s.parent_session_id){
      if(!_subagentMap[s.parent_session_id]) _subagentMap[s.parent_session_id]=[];
      _subagentMap[s.parent_session_id].push(s);
      _childSessionIds.add(s.session_id);
    }
  }
}

// ── Session item renderer (extracted helper) ─────────────────────────────────
function _renderSessionItem(s){
  const el=document.createElement('div');
  const isActive=S.session&&s.session_id===S.session.session_id;
  el.className='session-item'+(isActive?' active':'')+(isActive&&S.session&&S.session._flash?' new-flash':'')+(s.archived?' archived':'');
  if(isActive&&S.session&&S.session._flash)delete S.session._flash;
  const rawTitle=(s.title||'Untitled').replace(/ #(\d+)$/, ' $1');
  const tags=(rawTitle.match(/#[\w-]+/g)||[]);
  const cleanTitle=tags.length?rawTitle.replace(/#[\w-]+/g,'').trim():rawTitle;
  const title=document.createElement('span');
  title.className='session-title';
  title.textContent=cleanTitle||'Untitled';
  title.title='Double-click to rename';
  if(s.source==='cli'){
    const cliChip=document.createElement('span');
    cliChip.className='session-source-cli';
    cliChip.innerHTML='<i class="fas fa-terminal"></i>';
    cliChip.title='Started from CLI';
    title.prepend(cliChip);
  }
  if(s.source==='telegram'){
    const tgChip=document.createElement('span');
    tgChip.className='session-source-telegram';
    tgChip.innerHTML='<i class="fab fa-telegram"></i>';
    tgChip.title='Started from Telegram';
    title.prepend(tgChip);
  }
  for(const tag of tags){
    const chip=document.createElement('span');
    chip.className='session-tag';
    chip.textContent=tag;
    chip.title='Click to filter by '+tag;
    chip.onclick=(e)=>{
      e.stopPropagation();
      const searchBox=$('sessionSearch');
      if(searchBox){searchBox.value=tag;filterSessions();}
    };
    title.appendChild(chip);
  }
  const startRename=()=>{
    _renamingSid=s.session_id;
    const inp=document.createElement('input');
    inp.className='session-title-input';
    inp.value=s.title||'Untitled';
    ['click','mousedown','dblclick','pointerdown'].forEach(ev=>
      inp.addEventListener(ev,e2=>e2.stopPropagation())
    );
    const finish=async(save)=>{
      _renamingSid=null;
      if(save){
        const newTitle=inp.value.trim()||'Untitled';
        title.textContent=newTitle;
        s.title=newTitle;
        if(S.session&&S.session.session_id===s.session_id){S.session.title=newTitle;syncTopbar();}
        try{await api('/api/session/rename',{method:'POST',body:JSON.stringify({session_id:s.session_id,title:newTitle})});}
        catch(err){setStatus('Rename failed: '+err.message);}
      }
      inp.replaceWith(title);
      setTimeout(()=>{if(_renamingSid===null) renderSessionListFromCache();},50);
    };
    inp.onkeydown=e2=>{
      if(e2.key==='Enter'){e2.preventDefault();e2.stopPropagation();finish(true);}
      if(e2.key==='Escape'){e2.preventDefault();e2.stopPropagation();finish(false);}
    };
    inp.onblur=()=>{if(_renamingSid===s.session_id) finish(false);};
    title.replaceWith(inp);
    setTimeout(()=>{inp.focus();inp.select();},10);
  };
  const pin=document.createElement('span');
  pin.className='session-pin'+(s.pinned?' pinned':'');
  pin.innerHTML=s.pinned?'<i class="fas fa-star"></i>':'<i class="far fa-star"></i>';
  pin.title=s.pinned?'Unpin':'Pin to top';
  pin.onclick=async(e)=>{
    e.stopPropagation();e.preventDefault();
    const newPinned=!s.pinned;
    try{
      await api('/api/session/pin',{method:'POST',body:JSON.stringify({session_id:s.session_id,pinned:newPinned})});
      s.pinned=newPinned;
      if(S.session&&S.session.session_id===s.session_id) S.session.pinned=newPinned;
      refreshSessionList();
    }catch(err){showToast('Pin failed: '+err.message);}
  };
  const dup=document.createElement('button');
  dup.className='session-dup';dup.innerHTML='<i class="fas fa-copy"></i>';dup.title='Duplicate';
  dup.onclick=async(e)=>{
    e.stopPropagation();e.preventDefault();
    try{
      const res=await api('/api/session/new',{method:'POST',body:JSON.stringify({workspace:s.workspace,model:s.model})});
      if(res.session){
        const copyTitle=(s.title||'Untitled')+' (copy)';
        await api('/api/session/rename',{method:'POST',body:JSON.stringify({session_id:res.session.session_id,title:copyTitle})});
        res.session.title=copyTitle;
        _allSessions.unshift(res.session);
        await loadSession(res.session.session_id);refreshSessionList();
        showToast('Session duplicated');
      }
    }catch(err){showToast('Duplicate failed: '+err.message);}
  };
  const trash=document.createElement('button');
  trash.className='session-trash';trash.innerHTML='<i class="fas fa-trash"></i>';trash.title='Delete';
  trash.onclick=async(e)=>{e.stopPropagation();e.preventDefault();await deleteSession(s.session_id);};
  el.appendChild(pin);el.appendChild(title);el.appendChild(dup);el.appendChild(trash);
  let _clickTimer=null;
  el.onclick=async(e)=>{
    if(_renamingSid) return;
    if([trash,dup].some(b=>e.target===b||b.contains(e.target))) return;
    clearTimeout(_clickTimer);
    _clickTimer=setTimeout(async()=>{
      _clickTimer=null;
      if(_renamingSid) return;
      await loadSession(s.session_id);renderSessionListFromCache();
    },220);
  };
  el.ondblclick=async(e)=>{
    e.stopPropagation();
    e.preventDefault();
    clearTimeout(_clickTimer);
    _clickTimer=null;
    startRename();
  };

  // Subagent expand/collapse: wrap parent + children in a container
  const children=_subagentMap[s.session_id];
  if(children&&children.length){
    const wrap=document.createElement('div');
    wrap.className='session-parent-wrap';

    // Add expand chevron before title
    const expandBtn=document.createElement('span');
    expandBtn.className='session-expand-btn'+(_expandedParents[s.session_id]?' open':'');
    expandBtn.innerHTML='<i class="fas fa-chevron-right"></i>';
    expandBtn.title=children.length+' subagent'+(children.length>1?'s':'');
    el.insertBefore(expandBtn,el.firstChild);

    const childContainer=document.createElement('div');
    childContainer.className='subagent-list'+(_expandedParents[s.session_id]?'':' hidden');

    for(const child of children){
      const childEl=document.createElement('div');
      const isChildActive=S.session&&child.session_id===S.session.session_id;
      childEl.className='session-item subagent-item'+(isChildActive?' active':'');
      childEl.dataset.session=child.session_id;
      const childTitle=document.createElement('span');
      childTitle.className='session-title';
      childTitle.innerHTML='<i class="fas fa-robot subagent-icon"></i> '+(child.title||'Untitled');
      const childCount=document.createElement('span');
      childCount.className='session-count';
      childCount.textContent=child.message_count||'';
      childEl.appendChild(childTitle);
      childEl.appendChild(childCount);
      childEl.onclick=async()=>{await loadSession(child.session_id);renderSessionListFromCache();};
      childContainer.appendChild(childEl);
    }

    expandBtn.onclick=(e)=>{
      e.stopPropagation();
      const nowExpanded=childContainer.classList.toggle('hidden');
      expandBtn.classList.toggle('open',!nowExpanded);
      _expandedParents[s.session_id]=!nowExpanded;
      _saveExpandedParents();
    };

    wrap.appendChild(el);
    wrap.appendChild(childContainer);
    return wrap;
  }

  return el;
}

async function renderSessionList(){
  try{
    if(!($('sessionSearch').value||'').trim()) _contentSearchResults=[];
    const data=await api('/api/sessions');
    _allSessions=data.sessions||[];
    renderSessionListFromCache();
  }catch(e){console.warn('renderSessionList',e);}
}

// Re-render from cache without a network call.
// Use this after mutations that already patch _allSessions in-memory.
function refreshSessionList(){
  renderSessionListFromCache();
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
  _buildSubagentMap();
  const q=($('sessionSearch').value||'').toLowerCase();
  const titleMatches=q?_allSessions.filter(s=>(s.title||'Untitled').toLowerCase().includes(q)):_allSessions;
  const titleIds=new Set(titleMatches.map(s=>s.session_id));
  const allMatched=q?[...titleMatches,..._contentSearchResults.filter(s=>!titleIds.has(s.session_id))]:titleMatches;
  // Filter out child sessions from main list (they render under their parent)
  // When searching, show all matches including children (for discoverability)
  const sessions=allMatched.filter(s=>!s.archived&&(q||!_childSessionIds.has(s.session_id)));
  const list=$('sessionList');list.innerHTML='';
  const pinned=sessions.filter(s=>s.pinned);
  const unpinned=sessions.filter(s=>!s.pinned);

  // Load collapse state from localStorage
  let collapsed={};
  try{collapsed=JSON.parse(localStorage.getItem('hermes-session-groups-collapsed')||'{}');}catch(e){}
  const saveCollapsed=()=>{
    try{localStorage.setItem('hermes-session-groups-collapsed',JSON.stringify(collapsed));}catch(e){}
  };

  const now=Date.now();
  const ONE_DAY=86400000;
  const getGroupLabel=(s)=>{
    const ts=(s.created_at||s.updated_at||0)*1000;
    if(ts>now-ONE_DAY) return 'Today';
    if(ts>now-2*ONE_DAY) return 'Yesterday';
    const d=new Date(ts);
    return d.toLocaleDateString(undefined,{weekday:'short',month:'short',day:'numeric'});
  };

  const renderGroup=(label,items,isPinned)=>{
    const groupDiv=document.createElement('div');
    groupDiv.className='session-date-group';

    const header=document.createElement('div');
    const colorClass=isPinned?'pinned':label==='Today'?'today':label==='Yesterday'?'yesterday':'older';
    header.className='session-date-header '+colorClass;

    const caret=document.createElement('i');
    caret.className='fas fa-chevron-down session-date-caret';

    const labelSpan=document.createElement('span');
    labelSpan.textContent=isPinned?'\u2605 Pinned':label;

    header.appendChild(caret);
    header.appendChild(labelSpan);

    const body=document.createElement('div');
    body.className='session-date-body';

    if(collapsed[label]){
      body.style.display='none';
      caret.classList.add('collapsed');
    }

    header.onclick=()=>{
      const isNowCollapsed=body.style.display==='none';
      body.style.display=isNowCollapsed?'':'none';
      caret.classList.toggle('collapsed',!isNowCollapsed);
      collapsed[label]=!isNowCollapsed;
      saveCollapsed();
    };

    for(const s of items){
      body.appendChild(_renderSessionItem(s));
    }

    groupDiv.appendChild(header);
    groupDiv.appendChild(body);
    return groupDiv;
  };

  // Pinned group
  if(pinned.length){
    list.appendChild(renderGroup('Pinned',pinned,true));
  }

  // Group unpinned by date label
  const dateGroups=[];
  let curLabel=null;
  let curItems=[];
  for(const s of unpinned){
    const lbl=getGroupLabel(s);
    if(lbl!==curLabel){
      if(curItems.length) dateGroups.push({label:curLabel,items:curItems});
      curLabel=lbl;
      curItems=[s];
    } else {
      curItems.push(s);
    }
  }
  if(curItems.length) dateGroups.push({label:curLabel,items:curItems});

  for(const g of dateGroups){
    list.appendChild(renderGroup(g.label,g.items,false));
  }

  // Scroll active item into view (needed on page load)
  const activeEl=list.querySelector('.session-item.active');
  if(activeEl) activeEl.scrollIntoView({block:'nearest',behavior:'instant'});
}

// ── Topbar inline rename ─────────────────────────────────────────────────────
function startTopbarRename(){
  if(!S.session) return;
  const titleEl=$('topbarTitle');
  const renameBtn=$('btnRenameSession');
  if(!titleEl) return;
  const current=S.session.title||'Untitled';
  
  // Create a container for the input + buttons
  const cont=document.createElement('div');
  cont.style.cssText='display:flex;align-items:center;gap:6px;min-width:0;';
  
  const inp=document.createElement('input');
  inp.value=current;
  inp.style.cssText='background:rgba(255,255,255,.08);border:1px solid rgba(0,122,204,.6);border-radius:4px;color:var(--text);font-size:15px;font-weight:600;padding:2px 8px;outline:none;flex:1;min-width:120px;max-width:400px;font-family:inherit;letter-spacing:-.01em;';
  
  // Define finish function first so button onclick can reference it
  const finish=async(save)=>{
    const newTitle=inp.value.trim();
    cont.replaceWith(titleEl);
    if(renameBtn) renameBtn.style.display='';
    if(save && newTitle && newTitle!==current){
      S.session.title=newTitle;
      titleEl.textContent=newTitle;
      document.title=newTitle+' \u2014 Hermes';
      try{
        await api('/api/session/rename',{method:'POST',body:JSON.stringify({session_id:S.session.session_id,title:newTitle})});
        // Update the in-memory cache so renderSessionListFromCache reflects the new name immediately
        const cached=_allSessions.find(s=>s.session_id===S.session.session_id);
        if(cached) cached.title=newTitle;
        renderSessionListFromCache();
      }catch(e){showToast('Rename failed: '+e.message);}
    }
  };
  
  const btnOk=document.createElement('button');
  btnOk.innerHTML='<i class="fas fa-check"></i>';
  btnOk.style.cssText='background:none;border:none;color:var(--green);cursor:pointer;font-size:13px;padding:2px 4px;opacity:.8;transition:opacity .15s;flex-shrink:0;';
  btnOk.title='Accept (Enter)';
  // onmousedown + preventDefault keeps focus on the input so blur doesn't fire first
  btnOk.onmousedown=(e)=>{e.preventDefault();finish(true);};

  const btnCancel=document.createElement('button');
  btnCancel.innerHTML='<i class="fas fa-xmark"></i>';
  btnCancel.style.cssText='background:none;border:none;color:var(--accent);cursor:pointer;font-size:13px;padding:2px 4px;opacity:.8;transition:opacity .15s;flex-shrink:0;';
  btnCancel.title='Cancel (Esc)';
  btnCancel.onmousedown=(e)=>{e.preventDefault();finish(false);};
  
  cont.appendChild(inp);
  cont.appendChild(btnOk);
  cont.appendChild(btnCancel);
  
  if(renameBtn) renameBtn.style.display='none';
  
  inp.onkeydown=e=>{
    if(e.key==='Enter'){e.preventDefault();finish(true);}
    if(e.key==='Escape'){e.preventDefault();finish(false);}
  };
  inp.onblur=()=>{
    // Small delay to let button click register before blur
    setTimeout(()=>finish(false), 50);
  };
  
  titleEl.replaceWith(cont);
  setTimeout(()=>{inp.focus();inp.select();},10);
}

async function deleteSession(sid){
  if(!confirm('Delete this conversation?'))return;
  try{
    await api('/api/session/delete',{method:'POST',body:JSON.stringify({session_id:sid})});
  }catch(e){setStatus(`Delete failed: ${e.message}`);return;}
  // Remove from in-memory cache immediately
  const idx=_allSessions.findIndex(s=>s.session_id===sid);
  if(idx!==-1) _allSessions.splice(idx,1);
  if(S.session&&S.session.session_id===sid){
    S.session=null;S.messages=[];S.entries=[];
    history.replaceState({},'','/');
    // Find next session from cache (already mutated above)
    const visible=_allSessions.filter(s=>!s.archived);
    if(visible.length){
      await loadSession(visible[0].session_id);
    }else{
      const btnRename=$('btnRenameSession');
      if(btnRename) btnRename.style.display='none';
      $('topbarTitle').textContent='Hermes';
      $('topbarMeta').textContent='Start a new conversation';
      $('msgInner').innerHTML='';
      $('emptyState').style.display='';
      $('fileTree').innerHTML='';
    }
  }
  showToast('Conversation deleted');
  refreshSessionList();
}


