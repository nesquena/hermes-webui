// ── Session action icons (SVG, monochrome, inherit currentColor) ──
const ICONS={
  pin:'<svg width="14" height="14" viewBox="0 0 16 16" fill="currentColor" stroke="none"><polygon points="8,1.5 9.8,5.8 14.5,6.2 11,9.4 12,14 8,11.5 4,14 5,9.4 1.5,6.2 6.2,5.8"/></svg>',
  unpin:'<svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.3"><polygon points="8,2 9.8,6.2 14.2,6.2 10.7,9.2 12,13.8 8,11 4,13.8 5.3,9.2 1.8,6.2 6.2,6.2"/></svg>',
  folder:'<svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.3"><path d="M2 4.5h4l1.5 1.5H14v7H2z"/></svg>',
  archive:'<svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.3"><rect x="1.5" y="2" width="13" height="3" rx="1"/><path d="M2.5 5v8h11V5"/><line x1="6" y1="8.5" x2="10" y2="8.5"/></svg>',
  unarchive:'<svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.3"><rect x="1.5" y="2" width="13" height="3" rx="1"/><path d="M2.5 5v8h11V5"/><polyline points="6.5,7 8,5.5 9.5,7"/></svg>',
  dup:'<svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.3"><rect x="4.5" y="4.5" width="8.5" height="8.5" rx="1.5"/><path d="M3 11.5V3h8.5"/></svg>',
  trash:'<svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.3"><path d="M3.5 4.5h9M6.5 4.5V3h3v1.5M4.5 4.5v8.5h7v-8.5"/><line x1="7" y1="7" x2="7" y2="11"/><line x1="9" y1="7" x2="9" y2="11"/></svg>',
  more:'<svg width="14" height="14" viewBox="0 0 16 16" fill="currentColor" stroke="none"><circle cx="8" cy="3" r="1.25"/><circle cx="8" cy="8" r="1.25"/><circle cx="8" cy="13" r="1.25"/></svg>',
};

async function newSession(flash){
  updateQueueBadge();
  S.toolCalls=[];
  clearLiveToolCards();
  // Use profile default workspace for new sessions after a profile switch (one-shot),
  // otherwise inherit from the current session (or let server pick the default)
  const inheritWs=S._profileDefaultWorkspace||(S.session?S.session.workspace:null);
  S._profileDefaultWorkspace=null; // consume — only applies to the first new session after switch
  const data=await api('/api/session/new',{method:'POST',body:JSON.stringify({model:$('modelSelect').value,workspace:inheritWs})});
  S.session=data.session;S.messages=data.session.messages||[];
  S.lastUsage={...(data.session.last_usage||{})};
  if(flash)S.session._flash=true;
  localStorage.setItem('hermes-webui-session',S.session.session_id);
  // Reset per-session visual state: a fresh chat is idle even if another
  // conversation is still streaming in the background.
  S.busy=false;
  S.activeStreamId=null;
  updateSendBtn();
  const _cb=$('btnCancel');if(_cb)_cb.style.display='none';
  setStatus('');
  setComposerStatus('');
  updateQueueBadge(S.session.session_id);
  syncTopbar();renderMessages();loadDir('.');
  // don't call renderSessionList here - callers do it when needed
}

async function loadSession(sid){
  stopApprovalPolling();hideApprovalCard();
  const data=await api(`/api/session?session_id=${encodeURIComponent(sid)}`);
  S.session=data.session;
  S.lastUsage={...(data.session.last_usage||{})};
  localStorage.setItem('hermes-webui-session',S.session.session_id);
  // B9: sanitize empty assistant messages (PR #402) — build index map to remap
  // session-level tool_calls.assistant_msg_idx to the new sanitized positions.
  const allMsgs = data.session.messages || [];
  const sanitized = [];
  const origIdxToSanitizedIdx = {};
  let lastKeptAsstIdx = -1;
  for (let i = 0; i < allMsgs.length; i++) {
    const m = allMsgs[i];
    if (!m || !m.role) continue;
    if (m.role === 'tool') continue;
    if (m.role === 'assistant') {
      let c = m.content || '';
      if (Array.isArray(c)) c = c.filter(p => p && p.type === 'text').map(p => p.text || '').join('');
      if (!String(c).trim().length) { continue; }  // empty assistant — skip
      lastKeptAsstIdx = sanitized.length;
    }
    origIdxToSanitizedIdx[i] = sanitized.length;
    sanitized.push(m);
  }
  if (data.session.tool_calls && data.session.tool_calls.length) {
    for (const tc of data.session.tool_calls) {
      if (!tc || tc.assistant_msg_idx === undefined) continue;
      const origIdx = tc.assistant_msg_idx;
      tc.assistant_msg_idx = (origIdx in origIdxToSanitizedIdx)
        ? origIdxToSanitizedIdx[origIdx]
        : (lastKeptAsstIdx >= 0 ? lastKeptAsstIdx : -1);
    }
  }
  data.session.messages = sanitized;
  const activeStreamId=data.session.active_stream_id||null;
  if(!INFLIGHT[sid]&&activeStreamId&&typeof loadInflightState==='function'){
    const stored=loadInflightState(sid, activeStreamId);
    if(stored){
      INFLIGHT[sid]={
        messages:Array.isArray(stored.messages)&&stored.messages.length?stored.messages:[...(data.session.messages||[])],
        uploaded:Array.isArray(stored.uploaded)?stored.uploaded:[...(data.session.pending_attachments||[])],
        toolCalls:Array.isArray(stored.toolCalls)?stored.toolCalls:[],
        reattach:true,
      };
    }
  }
  if(INFLIGHT[sid]){
    S.messages=INFLIGHT[sid].messages;
    S.toolCalls=(INFLIGHT[sid].toolCalls||[]);
    S.busy=true;
    syncTopbar();renderMessages();appendThinking();loadDir('.');
    clearLiveToolCards();
    if(typeof placeLiveToolCardsHost==='function') placeLiveToolCardsHost();
    for(const tc of (S.toolCalls||[])){
      if(tc&&tc.name) appendLiveToolCard(tc);
    }
    setBusy(true);setComposerStatus('');
    startApprovalPolling(sid);
    S.activeStreamId=activeStreamId;
    const _cb=$('btnCancel');if(_cb&&activeStreamId)_cb.style.display='inline-flex';
    if(INFLIGHT[sid].reattach&&activeStreamId&&typeof attachLiveStream==='function'){
      INFLIGHT[sid].reattach=false;
      attachLiveStream(sid, activeStreamId, data.session.pending_attachments||[], {reconnecting:true});
    }
  }else{
    updateQueueBadge(sid);
    S.messages=data.session.messages||[];
    const pendingMsg=typeof getPendingSessionMessage==='function'?getPendingSessionMessage(data.session):null;
    if(pendingMsg) S.messages.push(pendingMsg);
    // Fix (PR #402): do NOT pre-fill S.toolCalls from session-level tool_calls —
    // those have stale assistant_msg_idx values after B9 sanitization. Instead,
    // set S.toolCalls=[] and let renderMessages() derive them from per-message
    // tool_calls (which already have correct sanitized-array indices).
    S.toolCalls=[];
    clearLiveToolCards();
    if(activeStreamId){
      S.busy=true;
      S.activeStreamId=activeStreamId;
      updateSendBtn();
      const _cb=$('btnCancel');if(_cb)_cb.style.display='inline-flex';
      setStatus('');
      setComposerStatus('');
      syncTopbar();renderMessages();appendThinking();loadDir('.');
      updateQueueBadge(sid);
      startApprovalPolling(sid);
      if(typeof attachLiveStream==='function') attachLiveStream(sid, activeStreamId, data.session.pending_attachments||[], {reconnecting:true});
      else if(typeof watchInflightSession==='function') watchInflightSession(sid, activeStreamId);
    }else{
      // Reset per-session visual state: the viewed session is idle even if another
      // session's stream is still running in the background.
      // We directly update the DOM instead of calling setBusy(false), because
      // setBusy(false) drains the viewed session's queued follow-up turns.
      S.busy=false;
      S.activeStreamId=null;
      updateSendBtn();
      const _cb=$('btnCancel');if(_cb)_cb.style.display='none';
      setStatus('');
      setComposerStatus('');
      updateQueueBadge(sid);
      syncTopbar();renderMessages();highlightCode();loadDir('.');
    }
  }
  // Sync context usage indicator from session data
  const _s=S.session;
  if(_s&&typeof _syncCtxIndicator==='function'){
    const u=S.lastUsage||{};
    const _pick=(latest,stored,dflt=0)=>latest!=null?latest:(stored!=null?stored:dflt);
    _syncCtxIndicator({
      input_tokens:      _pick(u.input_tokens,      _s.input_tokens),
      output_tokens:     _pick(u.output_tokens,     _s.output_tokens),
      estimated_cost:    _pick(u.estimated_cost,    _s.estimated_cost),
      context_length:    _pick(u.context_length,    _s.context_length),
      last_prompt_tokens:_pick(u.last_prompt_tokens,_s.last_prompt_tokens),
      threshold_tokens:  _pick(u.threshold_tokens,  _s.threshold_tokens),
    });
  }
}

let _allSessions = [];  // cached for search filter
let _renamingSid = null;  // session_id currently being renamed (blocks list re-renders)
let _showArchived = false;  // toggle to show archived sessions
let _allProjects = [];  // cached project list
let _activeProject = null;  // project_id filter (null = show all)
let _showAllProfiles = false;  // false = filter to active profile only
let _sessionActionMenu = null;
let _sessionActionAnchor = null;
let _sessionActionSessionId = null;

function closeSessionActionMenu(){
  if(_sessionActionMenu){
    _sessionActionMenu.remove();
    _sessionActionMenu = null;
  }
  if(_sessionActionAnchor){
    _sessionActionAnchor.classList.remove('active');
    const row=_sessionActionAnchor.closest('.session-item');
    if(row) row.classList.remove('menu-open');
    _sessionActionAnchor = null;
  }
  _sessionActionSessionId = null;
}

function _positionSessionActionMenu(anchorEl){
  if(!_sessionActionMenu || !anchorEl) return;
  const rect=anchorEl.getBoundingClientRect();
  const menuW=Math.min(280, Math.max(220, _sessionActionMenu.scrollWidth || 220));
  let left=rect.right-menuW;
  if(left<8) left=8;
  if(left+menuW>window.innerWidth-8) left=window.innerWidth-menuW-8;
  _sessionActionMenu.style.left=left+'px';
  _sessionActionMenu.style.top='8px';
  const menuH=_sessionActionMenu.offsetHeight || 0;
  let top=rect.bottom+6;
  if(top+menuH>window.innerHeight-8 && rect.top>menuH+12){
    top=rect.top-menuH-6;
  }
  if(top<8) top=8;
  _sessionActionMenu.style.top=top+'px';
}

function _buildSessionAction(label, meta, icon, onSelect, extraClass=''){
  const opt=document.createElement('button');
  opt.type='button';
  opt.className='ws-opt session-action-opt'+(extraClass?` ${extraClass}`:'');
  opt.innerHTML=
    `<span class="ws-opt-action">`
      + `<span class="ws-opt-icon">${icon}</span>`
      + `<span class="session-action-copy">`
        + `<span class="ws-opt-name">${esc(label)}</span>`
        + (meta?`<span class="session-action-meta">${esc(meta)}</span>`:'')
      + `</span>`
    + `</span>`;
  opt.onclick=async(e)=>{
    e.preventDefault();
    e.stopPropagation();
    await onSelect();
  };
  return opt;
}

function _openSessionActionMenu(session, anchorEl){
  if(_sessionActionMenu && _sessionActionSessionId===session.session_id && _sessionActionAnchor===anchorEl){
    closeSessionActionMenu();
    return;
  }
  closeSessionActionMenu();
  const menu=document.createElement('div');
  menu.className='session-action-menu open';
  menu.appendChild(_buildSessionAction(
    session.pinned?'Unpin conversation':'Pin conversation',
    session.pinned?'Remove from the pinned section':'Keep this conversation at the top',
    session.pinned?ICONS.pin:ICONS.unpin,
    async()=>{
      closeSessionActionMenu();
      const newPinned=!session.pinned;
      try{
        await api('/api/session/pin',{method:'POST',body:JSON.stringify({session_id:session.session_id,pinned:newPinned})});
        session.pinned=newPinned;
        if(S.session&&S.session.session_id===session.session_id) S.session.pinned=newPinned;
        renderSessionList();
      }catch(err){showToast('Pin failed: '+err.message);}
    },
    session.pinned?'is-active':''
  ));
  menu.appendChild(_buildSessionAction(
    'Move to project',
    session.project_id?'Change which project this conversation belongs to':'Assign this conversation to a project',
    ICONS.folder,
    async()=>{
      closeSessionActionMenu();
      _showProjectPicker(session, anchorEl);
    }
  ));
  menu.appendChild(_buildSessionAction(
    session.archived?'Restore conversation':'Archive conversation',
    session.archived?'Bring this conversation back into the main list':'Hide this conversation until archived is shown',
    session.archived?ICONS.unarchive:ICONS.archive,
    async()=>{
      closeSessionActionMenu();
      try{
        await api('/api/session/archive',{method:'POST',body:JSON.stringify({session_id:session.session_id,archived:!session.archived})});
        session.archived=!session.archived;
        if(S.session&&S.session.session_id===session.session_id) S.session.archived=session.archived;
        await renderSessionList();
        showToast(session.archived?'Session archived':'Session restored');
      }catch(err){showToast('Archive failed: '+err.message);}
    }
  ));
  menu.appendChild(_buildSessionAction(
    'Duplicate conversation',
    'Create a copy with the same workspace and model',
    ICONS.dup,
    async()=>{
      closeSessionActionMenu();
      try{
        const res=await api('/api/session/new',{method:'POST',body:JSON.stringify({workspace:session.workspace,model:session.model})});
        if(res.session){
          await api('/api/session/rename',{method:'POST',body:JSON.stringify({session_id:res.session.session_id,title:(session.title||'Untitled')+' (copy)'})});
          await loadSession(res.session.session_id);
          await renderSessionList();
          showToast('Session duplicated');
        }
      }catch(err){showToast('Duplicate failed: '+err.message);}
    }
  ));
  menu.appendChild(_buildSessionAction(
    'Delete conversation',
    'Permanently remove this conversation',
    ICONS.trash,
    async()=>{
      closeSessionActionMenu();
      await deleteSession(session.session_id);
    },
    'danger'
  ));
  document.body.appendChild(menu);
  _sessionActionMenu = menu;
  _sessionActionAnchor = anchorEl;
  _sessionActionSessionId = session.session_id;
  anchorEl.classList.add('active');
  const row=anchorEl.closest('.session-item');
  if(row) row.classList.add('menu-open');
  _positionSessionActionMenu(anchorEl);
}

document.addEventListener('click',e=>{
  if(!_sessionActionMenu) return;
  if(_sessionActionMenu.contains(e.target)) return;
  if(_sessionActionAnchor && _sessionActionAnchor.contains(e.target)) return;
  closeSessionActionMenu();
});
document.addEventListener('scroll',e=>{
  if(!_sessionActionMenu) return;
  if(_sessionActionMenu.contains(e.target)) return;
  closeSessionActionMenu();
}, true);
document.addEventListener('keydown',e=>{
  if(e.key==='Escape' && _sessionActionMenu) closeSessionActionMenu();
});
window.addEventListener('resize',()=>{
  if(_sessionActionMenu && _sessionActionAnchor) _positionSessionActionMenu(_sessionActionAnchor);
});

async function renderSessionList(){
  try{
    if(!($('sessionSearch').value||'').trim()) _contentSearchResults = [];
    const [sessData, projData] = await Promise.all([
      api('/api/sessions'),
      api('/api/projects'),
    ]);
    _allSessions = sessData.sessions||[];
    _allProjects = projData.projects||[];
    renderSessionListFromCache();  // no-ops if rename is in progress
  }catch(e){console.warn('renderSessionList',e);}
}

// ── Gateway session SSE (real-time sync for agent sessions) ──
let _gatewaySSE = null;
let _gatewayPollTimer = null;
let _gatewayProbeInFlight = false;
let _gatewaySSEWarningShown = false;
const _gatewayFallbackPollMs = 30000;

function startGatewayPollFallback(ms){
  const intervalMs = Math.max(5000, Number(ms) || _gatewayFallbackPollMs);
  if(_gatewayPollTimer) clearInterval(_gatewayPollTimer);
  _gatewayPollTimer = setInterval(() => { renderSessionList(); }, intervalMs);
}

function stopGatewayPollFallback(){
  if(_gatewayPollTimer){
    clearInterval(_gatewayPollTimer);
    _gatewayPollTimer = null;
  }
}

async function probeGatewaySSEStatus(){
  if(_gatewayProbeInFlight || !window._showCliSessions) return;
  _gatewayProbeInFlight = true;
  try{
    const resp = await fetch('/api/sessions/gateway/stream?probe=1', { credentials:'same-origin' });
    const data = await resp.json().catch(() => ({}));
    if(resp.ok && data.watcher_running){
      stopGatewayPollFallback();
      _gatewaySSEWarningShown = false;
      return;
    }
    if(resp.status === 503 || data.watcher_running === false){
      startGatewayPollFallback(data.fallback_poll_ms || _gatewayFallbackPollMs);
      renderSessionList();
      if(!_gatewaySSEWarningShown && typeof showToast === 'function'){
        showToast('Gateway sync unavailable — falling back to periodic refresh.', 5000);
        _gatewaySSEWarningShown = true;
      }
    }
  }catch(e){ /* ignore probe failures */ }
  finally{
    _gatewayProbeInFlight = false;
  }
}

function startGatewaySSE(){
  stopGatewaySSE();
  if(!window._showCliSessions) return;
  try{
    _gatewaySSE = new EventSource('/api/sessions/gateway/stream');
    _gatewaySSE.addEventListener('sessions_changed', (ev) => {
      try{
        const data = JSON.parse(ev.data);
        if(data.sessions){
          stopGatewayPollFallback();
          _gatewaySSEWarningShown = false;
          renderSessionList(); // re-fetch and re-render
        }
      }catch(e){ /* ignore parse errors */ }
    });
    _gatewaySSE.onerror = () => {
      void probeGatewaySSEStatus();
    };
  }catch(e){
    void probeGatewaySSEStatus();
  }
}

function stopGatewaySSE(){
  if(_gatewaySSE){
    _gatewaySSE.close();
    _gatewaySSE = null;
  }
  stopGatewayPollFallback();
  _gatewayProbeInFlight = false;
  _gatewaySSEWarningShown = false;
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

function _sessionTimestampMs(session) {
  const raw = Number(session && (session.updated_at || session.created_at || 0));
  return Number.isFinite(raw) ? raw * 1000 : 0;
}

function _localDayOrdinal(timestampMs) {
  const date = new Date(timestampMs);
  return Math.floor(Date.UTC(date.getFullYear(), date.getMonth(), date.getDate()) / 86400000);
}

function _sessionCalendarBoundaries(nowMs = Date.now()) {
  const now = new Date(nowMs);
  const startOfToday = new Date(now.getFullYear(), now.getMonth(), now.getDate());
  const startOfYesterday = new Date(now.getFullYear(), now.getMonth(), now.getDate() - 1);
  const startOfWeek = new Date(startOfToday);
  startOfWeek.setDate(startOfWeek.getDate() - ((startOfWeek.getDay() + 6) % 7));
  const startOfLastWeek = new Date(startOfWeek);
  startOfLastWeek.setDate(startOfLastWeek.getDate() - 7);
  return {
    startOfToday: startOfToday.getTime(),
    startOfYesterday: startOfYesterday.getTime(),
    startOfWeek: startOfWeek.getTime(),
    startOfLastWeek: startOfLastWeek.getTime(),
  };
}

function _formatSessionDate(timestampMs, nowMs = Date.now()) {
  const date = new Date(timestampMs);
  const now = new Date(nowMs);
  const options = {month:'short', day:'numeric'};
  if (date.getFullYear() !== now.getFullYear()) options.year = 'numeric';
  return date.toLocaleDateString(undefined, options);
}

function _formatRelativeSessionTime(timestampMs, nowMs = Date.now()) {
  if (!timestampMs) return t('session_time_unknown');
  const diffMs = Math.max(0, nowMs - timestampMs);
  const minute = 60 * 1000;
  const hour = 60 * minute;
  const {startOfToday, startOfYesterday, startOfWeek, startOfLastWeek} = _sessionCalendarBoundaries(nowMs);
  const dayDiff = Math.max(0, _localDayOrdinal(nowMs) - _localDayOrdinal(timestampMs));
  if (timestampMs >= startOfToday) {
    if (diffMs < minute) return t('session_time_just_now');
    if (diffMs < hour) {
      const minutes = Math.floor(diffMs / minute);
      return t('session_time_minutes_ago', minutes);
    }
    const hours = Math.floor(diffMs / hour);
    return t('session_time_hours_ago', hours);
  }
  if (timestampMs >= startOfYesterday) return t('session_time_bucket_yesterday');
  if (timestampMs >= startOfWeek) return t('session_time_days_ago', dayDiff);
  if (timestampMs >= startOfLastWeek) return t('session_time_last_week');
  return _formatSessionDate(timestampMs, nowMs);
}

function _sessionTimeBucketLabel(timestampMs, nowMs = Date.now()) {
  if (!timestampMs) return t('session_time_bucket_older');
  const {startOfToday, startOfYesterday, startOfWeek, startOfLastWeek} = _sessionCalendarBoundaries(nowMs);
  if (timestampMs >= startOfToday) return t('session_time_bucket_today');
  if (timestampMs >= startOfYesterday) return t('session_time_bucket_yesterday');
  if (timestampMs >= startOfWeek) return t('session_time_bucket_this_week');
  if (timestampMs >= startOfLastWeek) return t('session_time_bucket_last_week');
  return t('session_time_bucket_older');
}

function renderSessionListFromCache(){
  // Don't re-render while user is actively renaming a session (would destroy the input)
  if(_renamingSid) return;
  closeSessionActionMenu();
  const q=($('sessionSearch').value||'').toLowerCase();
  const titleMatches=q?_allSessions.filter(s=>(s.title||'Untitled').toLowerCase().includes(q)):_allSessions;
  // Merge content matches (deduped): content matches appended after title matches
  const titleIds=new Set(titleMatches.map(s=>s.session_id));
  const allMatched=q?[...titleMatches,..._contentSearchResults.filter(s=>!titleIds.has(s.session_id))]:titleMatches;
  // Filter by active profile (unless "All profiles" is toggled on)
  // Server backfills profile='default' for legacy sessions, so every session has a profile.
  // Show only sessions tagged to the active profile; 'All profiles' toggle overrides.
  const profileFiltered=_showAllProfiles?allMatched:allMatched.filter(s=>s.is_cli_session||s.profile===S.activeProfile);
  // Filter by active project
  const projectFiltered=_activeProject?profileFiltered.filter(s=>s.project_id===_activeProject):profileFiltered;
  // Filter archived unless toggle is on
  const sessions=_showArchived?projectFiltered:projectFiltered.filter(s=>!s.archived);
  const archivedCount=projectFiltered.filter(s=>s.archived).length;
  const list=$('sessionList');list.innerHTML='';
  // Project filter bar (only when projects exist)
  if(_allProjects.length>0){
    const bar=document.createElement('div');
    bar.className='project-bar';
    // "All" chip
    const allChip=document.createElement('span');
    allChip.className='project-chip'+(!_activeProject?' active':'');
    allChip.textContent='All';
    allChip.onclick=()=>{_activeProject=null;renderSessionListFromCache();};
    bar.appendChild(allChip);
    // Project chips
    for(const p of _allProjects){
      const chip=document.createElement('span');
      chip.className='project-chip'+(p.project_id===_activeProject?' active':'');
      if(p.color){
        const dot=document.createElement('span');
        dot.className='color-dot';
        dot.style.background=p.color;
        chip.appendChild(dot);
      }
      const nameSpan=document.createElement('span');
      nameSpan.textContent=p.name;
      chip.appendChild(nameSpan);
      chip.onclick=()=>{_activeProject=p.project_id;renderSessionListFromCache();};
      chip.ondblclick=(e)=>{e.stopPropagation();_startProjectRename(p,chip);};
      chip.oncontextmenu=(e)=>{e.preventDefault();_confirmDeleteProject(p);};
      bar.appendChild(chip);
    }
    // Create button
    const addBtn=document.createElement('button');
    addBtn.className='project-create-btn';
    addBtn.textContent='+';
    addBtn.title='New project';
    addBtn.onclick=(e)=>{e.stopPropagation();_startProjectCreate(bar,addBtn);};
    bar.appendChild(addBtn);
    list.appendChild(bar);
  }
  // Profile filter toggle (show sessions from other profiles)
  const otherProfileCount=allMatched.filter(s=>s.profile&&s.profile!==S.activeProfile).length;
  if(otherProfileCount>0&&!_showAllProfiles){
    const pfToggle=document.createElement('div');
    pfToggle.style.cssText='font-size:10px;padding:4px 10px;color:var(--muted);cursor:pointer;text-align:center;opacity:.7;';
    pfToggle.textContent='Show '+otherProfileCount+' from other profiles';
    pfToggle.onclick=()=>{_showAllProfiles=true;renderSessionListFromCache();};
    list.appendChild(pfToggle);
  } else if(_showAllProfiles&&otherProfileCount>0){
    const pfToggle=document.createElement('div');
    pfToggle.style.cssText='font-size:10px;padding:4px 10px;color:var(--muted);cursor:pointer;text-align:center;opacity:.7;';
    pfToggle.textContent='Show active profile only';
    pfToggle.onclick=()=>{_showAllProfiles=false;renderSessionListFromCache();};
    list.appendChild(pfToggle);
  }
  // Show/hide archived toggle if there are archived sessions
  if(archivedCount>0){
    const toggle=document.createElement('div');
    toggle.style.cssText='font-size:10px;padding:4px 10px;color:var(--muted);cursor:pointer;text-align:center;opacity:.7;';
    toggle.textContent=_showArchived?'Hide archived':'Show '+archivedCount+' archived';
    toggle.onclick=()=>{_showArchived=!_showArchived;renderSessionListFromCache();};
    list.appendChild(toggle);
  }
  // Empty state for active project filter
  if(_activeProject&&sessions.length===0){
    const empty=document.createElement('div');
    empty.style.cssText='padding:20px 14px;color:var(--muted);font-size:12px;text-align:center;opacity:.7;';
    empty.textContent='No sessions in this project yet.';
    list.appendChild(empty);
  }
  const orderedSessions=[...sessions].sort((a,b)=>_sessionTimestampMs(b)-_sessionTimestampMs(a));
  // Separate pinned from unpinned
  const pinned=orderedSessions.filter(s=>s.pinned);
  const unpinned=orderedSessions.filter(s=>!s.pinned);
  // Date grouping: Pinned / Today / Yesterday / This week / Last week / Older
  const now=Date.now();
  // Collapse state persisted in localStorage
  let _groupCollapsed={};
  try{_groupCollapsed=JSON.parse(localStorage.getItem('hermes-date-groups-collapsed')||'{}');}catch(e){}
  const _saveCollapsed=()=>{try{localStorage.setItem('hermes-date-groups-collapsed',JSON.stringify(_groupCollapsed));}catch(e){}};
  // Group sessions by date
  const groups=[];
  let curLabel=null,curItems=[];
  if(pinned.length) groups.push({label:'\u2605 Pinned',items:pinned,isPinned:true});
  for(const s of unpinned){
    const ts=_sessionTimestampMs(s);
    const label=_sessionTimeBucketLabel(ts, now);
    if(label!==curLabel){
      if(curItems.length) groups.push({label:curLabel,items:curItems});
      curLabel=label;curItems=[s];
    } else { curItems.push(s); }
  }
  if(curItems.length) groups.push({label:curLabel,items:curItems});
  // Render groups with collapsible headers
  for(const g of groups){
    const wrapper=document.createElement('div');
    wrapper.className='session-date-group';
    const hdr=document.createElement('div');
    hdr.className='session-date-header'+(g.isPinned?' pinned':'');
    const caret=document.createElement('span');
    caret.className='session-date-caret';
    caret.textContent='\u25B8'; // right-pointing triangle
    const label=document.createElement('span');
    label.textContent=g.label;
    hdr.appendChild(caret);hdr.appendChild(label);
    const body=document.createElement('div');
    body.className='session-date-body';
    if(_groupCollapsed[g.label]){body.style.display='none';caret.classList.add('collapsed');}
    hdr.onclick=()=>{
      const isCollapsed=body.style.display==='none';
      body.style.display=isCollapsed?'':'none';
      caret.classList.toggle('collapsed',!isCollapsed);
      _groupCollapsed[g.label]=!isCollapsed;
      _saveCollapsed();
    };
    wrapper.appendChild(hdr);
    for(const s of g.items){ body.appendChild(_renderOneSession(s)); }
    wrapper.appendChild(body);
    list.appendChild(wrapper);
  }
  // ── Render session items (extracted for group body use) ──
  // Note: declared after the groups loop but available via function hoisting.
  function _formatSourceTag(tag){
    // #429: return null for unknown/unrecognised tags so callers can suppress display.
    // Previously returned the raw tag string, causing 'N/A' or other junk values
    // from older hermes-agent state.db records to surface in the session list.
    const names={telegram:'via Telegram',discord:'via Discord',slack:'via Slack',cli:'CLI',feishu:'via Feishu',weixin:'via WeChat'};
    return names[tag]||null;
  }
  function _renderOneSession(s){
    const el=document.createElement('div');
    const isActive=S.session&&s.session_id===S.session.session_id;
    el.className='session-item'+(isActive?' active':'')+(isActive&&S.session&&S.session._flash?' new-flash':'')+(s.archived?' archived':'')+(s.is_cli_session?' cli-session':'');
    if(s.source_tag) el.dataset.source=s.source_tag;
    if(isActive&&S.session&&S.session._flash)delete S.session._flash;
    const rawTitle=s.title||'Untitled';
    const tags=(rawTitle.match(/#[\w-]+/g)||[]);
    let cleanTitle=tags.length?rawTitle.replace(/#[\w-]+/g,'').trim():rawTitle;
    // Guard: system prompt content must never surface as a visible session title
    const _SOURCE_DISPLAY={telegram:'Telegram',discord:'Discord',slack:'Slack',cli:'CLI',feishu:'Feishu',weixin:'WeChat'};
    if(cleanTitle.startsWith('[SYSTEM:')){
      cleanTitle=(_SOURCE_DISPLAY[s.source_tag]||'Gateway')+' session';
    }
    const sessionText=document.createElement('div');
    sessionText.className='session-text';
    const titleRow=document.createElement('div');
    titleRow.className='session-title-row';
    const title=document.createElement('span');
    title.className='session-title';
    title.textContent=cleanTitle||'Untitled';
    title.title='Double-click to rename';
    const tsMs=_sessionTimestampMs(s);
    titleRow.appendChild(title);
    const metaBits=[];
    if(s.is_cli_session && s.source_tag){const _stLabel=_formatSourceTag(s.source_tag);if(_stLabel)metaBits.push(_stLabel);}
    if(s.message_count) metaBits.push(t('n_messages', s.message_count));
    if(s.model) metaBits.push(String(s.model).split('/').pop());
    sessionText.appendChild(titleRow);
    if(metaBits.length){
      const meta=document.createElement('div');
      meta.className='session-meta';
      meta.textContent=metaBits.join(' · ');
      sessionText.appendChild(meta);
    }
    // Append tag chips after the title text
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

    // Rename: called directly when we confirm it's a double-click
    const startRename=()=>{
      closeSessionActionMenu();
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

    // Pin indicator (inline, only when pinned — no space reserved otherwise)
    if(s.pinned){
      const pinInd=document.createElement('span');
      pinInd.className='session-pin-indicator';
      pinInd.innerHTML=ICONS.pin;
      el.appendChild(pinInd);
    }
    // Project indicator: colored dot appended after the title
    if(s.project_id){
      const proj=_allProjects.find(p=>p.project_id===s.project_id);
      if(proj){
        const dot=document.createElement('span');
        dot.className='session-project-dot';
        dot.style.background=proj.color||'var(--blue)';
        dot.title=proj.name;
        title.appendChild(dot);
      }
    }
    el.appendChild(sessionText);
    // Single trigger button that opens a shared dropdown menu
    const actions=document.createElement('div');
    actions.className='session-actions';
    const menuBtn=document.createElement('button');
    menuBtn.type='button';
    menuBtn.className='session-actions-trigger';
    menuBtn.title='Conversation actions';
    menuBtn.setAttribute('aria-haspopup','menu');
    menuBtn.setAttribute('aria-label','Conversation actions');
    menuBtn.innerHTML=ICONS.more;
    menuBtn.onclick=(e)=>{
      e.stopPropagation();
      e.preventDefault();
      _openSessionActionMenu(s, menuBtn);
    };
    actions.appendChild(menuBtn);
    el.appendChild(actions);

    // Use a click timer to distinguish single-click (navigate) from double-click (rename).
    // This prevents loadSession from firing on the first click of a double-click,
    // which would re-render the list and destroy the dblclick target before it fires.
    let _clickTimer=null;
    el.onclick=async(e)=>{
      if(_renamingSid) return; // ignore while any rename is active
      if(actions.contains(e.target)) return;
      clearTimeout(_clickTimer);
      _clickTimer=setTimeout(async()=>{
        _clickTimer=null;
        if(_renamingSid) return;
        // For CLI sessions, import into WebUI store first (idempotent)
        if(s.is_cli_session){
          try{
            await api('/api/session/import_cli',{method:'POST',body:JSON.stringify({session_id:s.session_id})});
          }catch(e){ /* import failed -- fall through to read-only view */ }
        }
        await loadSession(s.session_id);renderSessionListFromCache();
        if(typeof closeMobileSidebar==='function')closeMobileSidebar();
      }, 220);
    };
    el.ondblclick=async(e)=>{
      e.stopPropagation();
      e.preventDefault();
      clearTimeout(_clickTimer); // cancel the pending single-click navigation
      _clickTimer=null;
      startRename();
    };
    return el;
  }
}

async function deleteSession(sid){
  const ok=await showConfirmDialog({
    message:'Delete this conversation?',
    confirmLabel:t('delete_title'),
    danger:true
  });
  if(!ok)return;
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
      $('topbarTitle').textContent=window._botName||'Hermes';
      $('topbarMeta').textContent='Start a new conversation';
      $('msgInner').innerHTML='';
      $('emptyState').style.display='';
      $('fileTree').innerHTML='';
    }
  }
  showToast('Conversation deleted');
  await renderSessionList();
}

// ── Project helpers ─────────────────────────────────────────────────────

const PROJECT_COLORS=['#7cb9ff','#f5c542','#e94560','#50c878','#c084fc','#fb923c','#67e8f9','#f472b6'];

function _showProjectPicker(session, anchorEl){
  // Close any existing picker
  document.querySelectorAll('.project-picker').forEach(p=>p.remove());
  const picker=document.createElement('div');
  picker.className='project-picker';
  // "No project" option
  const none=document.createElement('div');
  none.className='project-picker-item'+(!session.project_id?' active':'');
  none.textContent='No project';
  none.onclick=async()=>{
    picker.remove();
    document.removeEventListener('click',close);
    await api('/api/session/move',{method:'POST',body:JSON.stringify({session_id:session.session_id,project_id:null})});
    session.project_id=null;
    renderSessionListFromCache();
    showToast('Removed from project');
  };
  picker.appendChild(none);
  // Project options
  for(const p of _allProjects){
    const item=document.createElement('div');
    item.className='project-picker-item'+(session.project_id===p.project_id?' active':'');
    if(p.color){
      const dot=document.createElement('span');
      dot.className='color-dot';
      dot.style.cssText='width:6px;height:6px;border-radius:50%;background:'+p.color+';flex-shrink:0;';
      item.appendChild(dot);
    }
    const name=document.createElement('span');
    name.textContent=p.name;
    item.appendChild(name);
    item.onclick=async()=>{
      picker.remove();
      document.removeEventListener('click',close);
      await api('/api/session/move',{method:'POST',body:JSON.stringify({session_id:session.session_id,project_id:p.project_id})});
      session.project_id=p.project_id;
      renderSessionListFromCache();
      showToast('Moved to '+p.name);
    };
    picker.appendChild(item);
  }
  // "+ New project" shortcut at the bottom
  const createItem=document.createElement('div');
  createItem.className='project-picker-item project-picker-create';
  createItem.textContent='+ New project';
  createItem.onclick=async()=>{
    picker.remove();
    document.removeEventListener('click',close);
    const name=await showPromptDialog({
      message:t('project_name_prompt'),
      confirmLabel:t('create'),
      placeholder:'Project name'
    });
    if(!name||!name.trim()) return;
    const color=PROJECT_COLORS[_allProjects.length%PROJECT_COLORS.length];
    const res=await api('/api/projects/create',{method:'POST',body:JSON.stringify({name:name.trim(),color})});
    if(res.project){
      _allProjects.push(res.project);
      // Now move session into it
      await api('/api/session/move',{method:'POST',body:JSON.stringify({session_id:session.session_id,project_id:res.project.project_id})});
      session.project_id=res.project.project_id;
      await renderSessionList();
      showToast('Created "'+res.project.name+'" and moved session');
    }
  };
  picker.appendChild(createItem);
  // Append to body and position using getBoundingClientRect so it isn't clipped
  // by overflow:hidden on .session-item ancestors
  document.body.appendChild(picker);
  const rect=anchorEl.getBoundingClientRect();
  picker.style.position='fixed';
  picker.style.zIndex='999';
  // Prefer opening below; flip above if too close to bottom of viewport
  const spaceBelow=window.innerHeight-rect.bottom;
  if(spaceBelow<160&&rect.top>160){
    picker.style.bottom=(window.innerHeight-rect.top+4)+'px';
    picker.style.top='auto';
  }else{
    picker.style.top=(rect.bottom+4)+'px';
    picker.style.bottom='auto';
  }
  // Align right edge of picker with right edge of button; keep within viewport
  const pickerW=Math.min(220,Math.max(160,picker.scrollWidth||160));
  let left=rect.right-pickerW;
  if(left<8) left=8;
  picker.style.left=left+'px';
  // Close on outside click
  const close=(e)=>{if(!picker.contains(e.target)&&e.target!==anchorEl){picker.remove();document.removeEventListener('click',close);}};
  setTimeout(()=>document.addEventListener('click',close),0);
}

function _startProjectCreate(bar, addBtn){
  const inp=document.createElement('input');
  inp.className='project-create-input';
  inp.placeholder='Project name';
  const finish=async(save)=>{
    if(save&&inp.value.trim()){
      const color=PROJECT_COLORS[_allProjects.length%PROJECT_COLORS.length];
      await api('/api/projects/create',{method:'POST',body:JSON.stringify({name:inp.value.trim(),color})});
      await renderSessionList();
      showToast('Project created');
    }else{
      inp.replaceWith(addBtn);
    }
  };
  inp.onkeydown=(e)=>{
    if(e.key==='Enter'){e.preventDefault();finish(true);}
    if(e.key==='Escape'){e.preventDefault();finish(false);}
  };
  inp.onblur=()=>finish(false);
  addBtn.replaceWith(inp);
  setTimeout(()=>inp.focus(),10);
}

function _startProjectRename(proj, chip){
  const inp=document.createElement('input');
  inp.className='project-create-input';
  inp.value=proj.name;
  const finish=async(save)=>{
    if(save&&inp.value.trim()&&inp.value.trim()!==proj.name){
      await api('/api/projects/rename',{method:'POST',body:JSON.stringify({project_id:proj.project_id,name:inp.value.trim()})});
      await renderSessionList();
      showToast('Project renamed');
    }else{
      renderSessionListFromCache();
    }
  };
  inp.onkeydown=(e)=>{
    if(e.key==='Enter'){e.preventDefault();finish(true);}
    if(e.key==='Escape'){e.preventDefault();finish(false);}
  };
  inp.onblur=()=>finish(false);
  inp.onclick=(e)=>e.stopPropagation();
  chip.replaceWith(inp);
  setTimeout(()=>{inp.focus();inp.select();},10);
}

async function _confirmDeleteProject(proj){
  const ok=await showConfirmDialog({
    message:'Delete project "'+proj.name+'"? Sessions will be unassigned but not deleted.',
    confirmLabel:t('delete_title'),
    danger:true
  });
  if(!ok){return;}
  await api('/api/projects/delete',{method:'POST',body:JSON.stringify({project_id:proj.project_id})});
  if(_activeProject===proj.project_id) _activeProject=null;
  await renderSessionList();
  showToast('Project deleted');
}
