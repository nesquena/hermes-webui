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

// Tracks which session_id is currently being loaded. Used to discard stale
// responses from in-flight requests when the user switches sessions again
// before the first request completes (#1060).
let _loadingSessionId = null;

const SESSION_VIEWED_COUNTS_KEY = 'hermes-session-viewed-counts';
let _sessionViewedCounts = null;

function _getSessionViewedCounts() {
  if (_sessionViewedCounts !== null) return _sessionViewedCounts;
  try {
    const parsed = JSON.parse(localStorage.getItem(SESSION_VIEWED_COUNTS_KEY) || '{}');
    _sessionViewedCounts = parsed && typeof parsed === 'object' && !Array.isArray(parsed) ? parsed : {};
  } catch (_){
    _sessionViewedCounts = {};
  }
  return _sessionViewedCounts;
}

function _saveSessionViewedCounts() {
  try {
    localStorage.setItem(SESSION_VIEWED_COUNTS_KEY, JSON.stringify(_getSessionViewedCounts()));
  } catch (_){
    // Ignore localStorage write failures.
  }
}

function _setSessionViewedCount(sid, messageCount = 0) {
  if (!sid) return;
  const counts = _getSessionViewedCounts();
  const next = Number.isFinite(messageCount) ? Number(messageCount) : 0;
  counts[sid] = next;
  _saveSessionViewedCounts();
}

function _hasUnreadForSession(s) {
  if (!s || !s.session_id) return false;
  const counts = _getSessionViewedCounts();
  if (!Object.prototype.hasOwnProperty.call(counts, s.session_id)) {
    _setSessionViewedCount(s.session_id, Number(s.message_count || 0));
    return false;
  }
  if (!Number.isFinite(s.message_count)) return false;
  return s.message_count > Number(counts[s.session_id] || 0);
}

async function newSession(flash){
  updateQueueBadge();
  S.toolCalls=[];
  clearLiveToolCards();
  // One-shot profile-switch workspace: applied to the first new session after a profile
  // switch, then cleared.  Use a dedicated flag so S._profileDefaultWorkspace (the
  // persistent boot/settings default) is not consumed and remains available for the
  // blank-page display on all subsequent returns to the empty state (#823).
  const switchWs=S._profileSwitchWorkspace;
  S._profileSwitchWorkspace=null;
  const inheritWs=switchWs||(S.session?S.session.workspace:null)||(S._profileDefaultWorkspace||null);
  // Use the saved default model for new sessions (#872). The user's saved
  // default_model (from Settings) takes priority over the chat-header dropdown
  // value, which reflects the *previous* session's model. Fall back to the
  // dropdown value only when no default_model is configured.
  const newModel=window._defaultModel||$('modelSelect').value;
  const data=await api('/api/session/new',{method:'POST',body:JSON.stringify({model:newModel,workspace:inheritWs,profile:S.activeProfile||'default'})});
  S.session=data.session;S.messages=data.session.messages||[];
  S.lastUsage={...(data.session.last_usage||{})};
  if(flash)S.session._flash=true;
  localStorage.setItem('hermes-webui-session',S.session.session_id);
  _setSessionViewedCount(S.session.session_id, S.session.message_count || 0);
  // Sync chat-header dropdown to the session's model so the UI reflects
  // the default model the server actually used (#872).
  if(S.session.model && S.session.model!==$('modelSelect').value && typeof _applyModelToDropdown==='function'){
    _applyModelToDropdown(S.session.model,$('modelSelect'));
    if(typeof syncModelChip==='function') syncModelChip();
  }
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
  // Mark this session as the in-flight load. Subsequent loadSession() calls
  // will overwrite this; stale awaits use the mismatch to bail out (#1060).
  _loadingSessionId = sid;
  stopApprovalPolling();hideApprovalCard();
  if(typeof stopClarifyPolling==='function') stopClarifyPolling();
  if(typeof hideClarifyCard==='function') hideClarifyCard();
  // Show loading indicator immediately for responsiveness.
  // Cleared by renderMessages() once full session data arrives.
  const currentSid = S.session ? S.session.session_id : null;
  // Persist the current composer draft before switching away so it can be
  // restored when the user switches back (#1060).
  if (currentSid && currentSid !== sid) {
    if (!S.composerDrafts) S.composerDrafts = {};
    const draft = { text: ($('msg') || {}).value || '', files: S.pendingFiles ? [...S.pendingFiles] : [] };
    if (draft.text || draft.files.length) S.composerDrafts[currentSid] = draft;
  }
  if (currentSid !== sid) {
    S.messages = [];
    S.toolCalls = [];
    _messagesTruncated = false;
    _oldestIdx = 0;
    _loadingOlder = false;
    const _msgInner = $('msgInner');
    if (_msgInner) _msgInner.innerHTML = '<div style="display:flex;align-items:center;justify-content:center;height:100%;color:var(--text-muted);font-size:14px;padding:40px;text-align:center;">Loading conversation...</div>';
  }
  // Phase 1: Load metadata only (~1KB) for fast session switching.
  // Guard against network/server failures to prevent a permanently stuck loading state.
  let data;
  try {
    data = await api(`/api/session?session_id=${encodeURIComponent(sid)}&messages=0&resolve_model=0`);
  } catch(e) {
    const _msgInner = $('msgInner');
    if(_msgInner){
      if(e.status===404){
        _msgInner.innerHTML='<div style="display:flex;align-items:center;justify-content:center;height:100%;color:var(--text-muted);font-size:14px;padding:40px;text-align:center;">Session not available in web UI.</div>';
      } else {
        _msgInner.innerHTML='<div style="display:flex;align-items:center;justify-content:center;height:100%;color:var(--text-muted);font-size:14px;padding:40px;text-align:center;">Failed to load session. Try switching sessions or refreshing.</div>';
        if(typeof showToast==='function') showToast('Failed to load session',3000,'error');
      }
    }
    if (_loadingSessionId === sid) _loadingSessionId = null;
    return;
  }
  // Stale response? A newer loadSession() call has already started (#1060).
  if (_loadingSessionId !== sid) return;
  S.session=data.session;
  S.session._modelResolutionDeferred=true;
  S.lastUsage={...(data.session.last_usage||{})};
  _setSessionViewedCount(S.session.session_id, Number(data.session.message_count || 0));
  localStorage.setItem('hermes-webui-session',S.session.session_id);

  const activeStreamId=S.session.active_stream_id||null;

  // Phase 2a: If session is streaming, restore from INFLIGHT cache before
  // loading full messages (INFLIGHT state is self-contained and sufficient).
  if(!INFLIGHT[sid]&&activeStreamId&&typeof loadInflightState==='function'){
    const stored=loadInflightState(sid, activeStreamId);
    if(stored){
      INFLIGHT[sid]={
        messages:Array.isArray(stored.messages)&&stored.messages.length?stored.messages:[],
        uploaded:Array.isArray(stored.uploaded)?stored.uploaded:[],
        toolCalls:Array.isArray(stored.toolCalls)?stored.toolCalls:[],
        reattach:true,
      };
    }
  }

  if(INFLIGHT[sid]){
    // Streaming session: use cached INFLIGHT messages (already has pending assistant output).
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
    if(typeof startClarifyPolling==='function') startClarifyPolling(sid);
    S.activeStreamId=activeStreamId;
    const _cb=$('btnCancel');if(_cb&&activeStreamId)_cb.style.display='inline-flex';
    if(INFLIGHT[sid].reattach&&activeStreamId&&typeof attachLiveStream==='function'){
      INFLIGHT[sid].reattach=false;
      if (_loadingSessionId !== sid) return;
      attachLiveStream(sid, activeStreamId, S.session.pending_attachments||[], {reconnecting:true});
    }
  }else{
    // Phase 2b: Idle session — load full messages lazily for rendering.
    // _ensureMessagesLoaded is idempotent; it skips if S.messages already populated.
    try {
      await _ensureMessagesLoaded(sid);
    } catch (e) {
      // Network errors, server failures, or SSE drops (Chrome error codes 4/5)
      // can cause _ensureMessagesLoaded to throw. Without a try/catch here the
      // "Loading conversation..." div injected at the top of loadSession would
      // persist forever with no recovery path.
      const _msgInner = $('msgInner');
      if (_msgInner) {
        _msgInner.innerHTML = '<div style="display:flex;align-items:center;justify-content:center;height:100%;color:var(--text-muted);font-size:14px;padding:40px;text-align:center;">Failed to load messages. Try switching sessions or refreshing.</div>';
      }
      if (typeof showToast === 'function') showToast('Failed to load conversation messages', 3000, 'error');
      if (_loadingSessionId === sid) _loadingSessionId = null;
      return;
    }
    // Stale? A newer loadSession() call has already started (#1060).
    if (_loadingSessionId !== sid) return;

    // Restore any queued message that survived page refresh via sessionStorage.
    if(typeof queueSessionMessage==='function'){
      try{
        const _storedQ=sessionStorage.getItem('hermes-queue-'+sid);
        if(_storedQ){
          const _entries=JSON.parse(_storedQ);
          if(Array.isArray(_entries)&&_entries.length){
            const _lastMsg=S.messages.slice().reverse()
              .find(m=>m&&m.role==='assistant');
            const _lastAsst=_lastMsg?(_lastMsg.timestamp||_lastMsg._ts||0)*1000:0;
            const _fresh=_entries.filter(e=>!e._queued_at||e._queued_at>_lastAsst);
            if(_fresh.length){
              const _first=_fresh[0];
              const _msg=$&&$('msg');
              if(_msg&&_first.text&&!_msg.value){
                _msg.value=_first.text||'';
                if(typeof autoResize==='function') autoResize();
                if(typeof showToast==='function') showToast((_fresh.length>1?`${_fresh.length} queued messages restored (showing first)`:'Queued message restored')+' — review and send when ready');
              }
              sessionStorage.removeItem('hermes-queue-'+sid);
            } else {
              sessionStorage.removeItem('hermes-queue-'+sid);
            }
          } else {
            sessionStorage.removeItem('hermes-queue-'+sid);
          }
        }
      }catch(_){sessionStorage.removeItem('hermes-queue-'+sid);}
    }

    // Reconstruct tool calls from message metadata, or fall back to session-level summary.
    // (hasMessageToolMetadata already computed inside _ensureMessagesLoaded; S.toolCalls set there.)
    updateQueueBadge(sid);

    // Attach pending user message if one is queued.
    const pendingMsg=typeof getPendingSessionMessage==='function'?getPendingSessionMessage(S.session):null;
    if(pendingMsg) S.messages.push(pendingMsg);

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
      if(typeof startClarifyPolling==='function') startClarifyPolling(sid);
      if(typeof attachLiveStream==='function') attachLiveStream(sid, activeStreamId, S.session.pending_attachments||[], {reconnecting:true});
      else if(typeof watchInflightSession==='function') watchInflightSession(sid, activeStreamId);
    }else{
      S.busy=false;
      S.activeStreamId=null;
      updateSendBtn();
      const _cb=$('btnCancel');if(_cb)_cb.style.display='none';
      setStatus('');
      setComposerStatus('');
      updateQueueBadge(sid);
      syncTopbar();renderMessages();
      // Kick off loadDir first (issues network requests), then highlight code.
      // The fetch is dispatched before the CPU-bound Prism pass begins.
      const _dirP=loadDir('.');
      highlightCode();
      await _dirP;
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
  _resolveSessionModelForDisplaySoon(sid);
  // Clear the in-flight session marker now that this load has completed (#1060).
  if (_loadingSessionId === sid) _loadingSessionId = null;
}

function _resolveSessionModelForDisplaySoon(sid){
  if(!sid) return;
  setTimeout(async()=>{
    try{
      const data=await api(`/api/session?session_id=${encodeURIComponent(sid)}&messages=0&resolve_model=1`);
      const model=data&&data.session&&data.session.model;
      if(!model||!S.session||S.session.session_id!==sid) return;
      S.session.model=model;
      S.session._modelResolutionDeferred=false;
      syncTopbar();
    }catch(_){
      // Keep session switching non-blocking; the next load can try again.
    }
  },0);
}

// Tracks whether the current session has older messages that were not
// loaded during the initial paginated fetch (msg_limit window).
// When true, scrolling to the top triggers _loadOlderMessages().
let _messagesTruncated = false;

// Load session messages if not already present.
// Called after loadSession fetches metadata (messages=0).
// Idempotent: if messages are already in S.messages, resolves immediately.
// Handles streaming sessions specially: restores from INFLIGHT cache or API.
// msg_limit (default 30): only fetch the last N messages for fast switching.
// Older messages are loaded on-demand via _loadOlderMessages().
const _INITIAL_MSG_LIMIT = 30;

async function _ensureMessagesLoaded(sid) {
  // Already have messages? (e.g. from INFLIGHT restore path, already set)
  if (S.messages && S.messages.length > 0 && S.messages[0] && S.messages[0].role) {
    return;
  }
  // Fetch session messages with a tail window for fast initial load.
  const data = await api(`/api/session?session_id=${encodeURIComponent(sid)}&messages=1&resolve_model=0&msg_limit=${_INITIAL_MSG_LIMIT}`);
  _messagesTruncated = !!data.session._messages_truncated;
  _oldestIdx = data.session._messages_offset || 0;
  const msgs = (data.session.messages || []).filter(m => m && m.role);
  // Check for tool-call metadata on messages (for tool-call card rendering)
  const hasMessageToolMetadata = msgs.some(m => {
    if (!m || m.role !== 'assistant') return false;
    const hasTc = Array.isArray(m.tool_calls) && m.tool_calls.length > 0;
    const hasTu = Array.isArray(m.content) && m.content.some(p => p && p.type === 'tool_use');
    return hasTc || hasTu;
  });
  if (!hasMessageToolMetadata && data.session.tool_calls && data.session.tool_calls.length) {
    S.toolCalls = data.session.tool_calls.map(tc => ({...tc, done: true}));
  } else {
    S.toolCalls = [];
  }
  clearLiveToolCards();
  S.messages = msgs;
  if(S.session&&S.session.session_id===sid){
    S.session.message_count=Number(data.session.message_count || msgs.length);
    S.lastUsage={...(data.session.last_usage||S.lastUsage||{})};
    _setSessionViewedCount(sid, Number(S.session.message_count || msgs.length));
  }
}

// Load older messages when the user scrolls to the top of the conversation.
// Prepends them to S.messages and re-renders, preserving scroll position.
let _loadingOlder = false;
// _oldestIdx tracks the index (in the server's full message array) of the
// oldest message currently loaded in S.messages. Starts at 0 when all
// messages are loaded, or > 0 when truncated by msg_limit.
let _oldestIdx = 0;

async function _loadOlderMessages() {
  if (_loadingOlder || !_messagesTruncated) return;
  const sid = S.session ? S.session.session_id : null;
  if (!sid || !S.messages.length) return;
  if (_oldestIdx <= 0) { _messagesTruncated = false; return; }
  _loadingOlder = true;
  try {
    const data = await api(`/api/session?session_id=${encodeURIComponent(sid)}&messages=1&resolve_model=0&msg_before=${_oldestIdx}&msg_limit=${_INITIAL_MSG_LIMIT}`);
    if (!data || !data.session || (_loadingSessionId !== null && _loadingSessionId !== sid)) return;
    const olderMsgs = (data.session.messages || []).filter(m => m && m.role);
    if (!olderMsgs.length) { _messagesTruncated = false; return; }
    // Prepend older messages
    // Use $('messages') — the scrollable container (#msgInner is not scrollable).
    const container = $('messages');
    const prevScrollH = container ? container.scrollHeight : 0;
    S.messages = [...olderMsgs, ...S.messages];
    _messagesTruncated = !!data.session._messages_truncated;
    _oldestIdx = data.session._messages_offset || 0;
    renderMessages();
    // Restore scroll position so the user stays at the same message.
    // renderMessages() calls scrollToBottom() at the end, so we must
    // counter-scroll to where the user was before loading older messages.
    if (container) {
      const newScrollH = container.scrollHeight;
      container.scrollTop = newScrollH - prevScrollH;
    }
    // renderMessages() called scrollToBottom() which set _scrollPinned=true.
    // We just restored the user's scroll position, so mark as not pinned.
    _scrollPinned = false;
  } catch(e) {
    console.warn('_loadOlderMessages failed:', e);
  } finally {
    // Always clear the loading lock. If the user switched sessions while
    // this request was in flight, loadSession() already set _loadingOlder=false
    // (see line ~122), so this is a harmless double-reset.
    _loadingOlder = false;
  }
}

// Ensure the full message history is loaded (for undo, export, etc).
// If the session was loaded with msg_limit, this fetches all messages.
async function _ensureAllMessagesLoaded() {
  if (!_messagesTruncated || !S.session) return;
  const sid = S.session.session_id;
  const data = await api(`/api/session?session_id=${encodeURIComponent(sid)}&messages=1&resolve_model=0`);
  const msgs = (data.session.messages || []).filter(m => m && m.role);
  S.messages = msgs;
  _messagesTruncated = false;
  if(S.session && S.session.session_id === sid){
    S.session.message_count = Number(data.session.message_count || msgs.length);
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
    session.pinned?t('session_unpin'):t('session_pin'),
    session.pinned?t('session_unpin_desc'):t('session_pin_desc'),
    session.pinned?ICONS.pin:ICONS.unpin,
    async()=>{
      closeSessionActionMenu();
      const newPinned=!session.pinned;
      try{
        await api('/api/session/pin',{method:'POST',body:JSON.stringify({session_id:session.session_id,pinned:newPinned})});
        session.pinned=newPinned;
        if(S.session&&S.session.session_id===session.session_id) S.session.pinned=newPinned;
        renderSessionList();
      }catch(err){showToast(t('session_pin_failed')+err.message);}
    },
    session.pinned?'is-active':''
  ));
  menu.appendChild(_buildSessionAction(
    t('session_move_project'),
    session.project_id?t('session_move_project_desc_has'):t('session_move_project_desc_none'),
    ICONS.folder,
    async()=>{
      closeSessionActionMenu();
      _showProjectPicker(session, anchorEl);
    }
  ));
  menu.appendChild(_buildSessionAction(
    session.archived?t('session_restore'):t('session_archive'),
    session.archived?t('session_restore_desc'):t('session_archive_desc'),
    session.archived?ICONS.unarchive:ICONS.archive,
    async()=>{
      closeSessionActionMenu();
      try{
        await api('/api/session/archive',{method:'POST',body:JSON.stringify({session_id:session.session_id,archived:!session.archived})});
        session.archived=!session.archived;
        if(S.session&&S.session.session_id===session.session_id) S.session.archived=session.archived;
        await renderSessionList();
        showToast(session.archived?t('session_archived'):t('session_restored'));
      }catch(err){showToast(t('session_archive_failed')+err.message);}
    }
  ));
  menu.appendChild(_buildSessionAction(
    t('session_duplicate'),
    t('session_duplicate_desc'),
    ICONS.dup,
    async()=>{
      closeSessionActionMenu();
      try{
        const res=await api('/api/session/new',{method:'POST',body:JSON.stringify({workspace:session.workspace,model:session.model})});
        if(res.session){
          await api('/api/session/rename',{method:'POST',body:JSON.stringify({session_id:res.session.session_id,title:(session.title||'Untitled')+' (copy)'})});
          await loadSession(res.session.session_id);
          await renderSessionList();
          showToast(t('session_duplicated'));
        }
      }catch(err){showToast(t('session_duplicate_failed')+err.message);}
    }
  ));
  menu.appendChild(_buildSessionAction(
    t('session_delete'),
    t('session_delete_desc'),
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
    const isStreaming = _allSessions.some(s => Boolean(s && s.is_streaming));
    if (isStreaming) {
      startStreamingPoll();
    } else {
      stopStreamingPoll();
    }
    ensureSessionTimeRefreshPoll();
    renderSessionListFromCache();  // no-ops if rename is in progress
  }catch(e){console.warn('renderSessionList',e);}
}

// ── Gateway session SSE (real-time sync for agent sessions) ──
let _gatewaySSE = null;
let _gatewayPollTimer = null;
let _gatewayProbeInFlight = false;
let _gatewaySSEWarningShown = false;
const _gatewayFallbackPollMs = 30000;
const _streamingPollMs = 5000;
const _sessionTimeRefreshMs = 60000;
let _streamingPollTimer = null;
let _sessionTimeRefreshTimer = null;

function startStreamingPoll(){
  if(_streamingPollTimer) return;
  _streamingPollTimer = setInterval(() => {
    void renderSessionList();
  }, _streamingPollMs);
}

function stopStreamingPoll(){
  if(!_streamingPollTimer) return;
  clearInterval(_streamingPollTimer);
  _streamingPollTimer = null;
}

function ensureSessionTimeRefreshPoll(){
  if(_sessionTimeRefreshTimer) return;
  _sessionTimeRefreshTimer = setInterval(() => {
    renderSessionListFromCache();
  }, _sessionTimeRefreshMs);
}

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
  }catch(e){
    // Network error during probe — server may be unreachable.
    // Start fallback polling as a safe default; it will self-cancel
    // when the SSE connection recovers and sessions_changed fires.
    startGatewayPollFallback(_gatewayFallbackPollMs);
    renderSessionList();
  }finally{
    _gatewayProbeInFlight = false;
  }
}

function startGatewaySSE(){
  stopGatewaySSE();
  if(!window._showCliSessions) return;
  try{
    _gatewaySSE = new EventSource('api/sessions/gateway/stream');
    _gatewaySSE.addEventListener('sessions_changed', (ev) => {
      try{
        const data = JSON.parse(ev.data);
        if(data.sessions){
          stopGatewayPollFallback();
          _gatewaySSEWarningShown = false;
          renderSessionList(); // re-fetch and re-render
          // If the active session received new gateway messages, refresh the conversation view.
          // S.busy check prevents stomping on an in-progress WebUI response.
          // is_cli_session check ensures we only poll import_cli for CLI-originated sessions.
          if(S.session && !S.busy && S.session.is_cli_session){
            const changedIds = new Set((data.sessions||[]).map(s=>s.session_id));
            if(changedIds.has(S.session.session_id)){
              // Capture active session ID before async fetch — race guard.
              // If the user switches sessions while the fetch is in-flight, discard the result.
              const activeSid = S.session.session_id;
              api('/api/session/import_cli',{method:'POST',body:JSON.stringify({session_id:activeSid})})
                .then(res=>{
                  if(!S.session || S.session.session_id !== activeSid) return;
                  if(res && res.session && Array.isArray(res.session.messages)){
                    const prev = S.messages.length;
                    S.messages = res.session.messages.filter(m=>m&&m.role);
                    if(S.messages.length !== prev){
                      renderMessages();
                      if(typeof highlightCode==='function') highlightCode();
                    }
                  }
                })
                .catch(()=>{ /* ignore — next poll will retry */ });
            }
          }
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
  const raw = Number(session && (session.last_message_at || session.updated_at || session.created_at || 0));
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
    if (diffMs < minute) return t('session_time_minutes_ago', 1);
    if (diffMs < hour) {
      const minutes = Math.floor(diffMs / minute);
      return t('session_time_minutes_ago', minutes);
    }
    const hours = Math.floor(diffMs / hour);
    return t('session_time_hours_ago', hours);
  }
  if (timestampMs >= startOfYesterday) return t('session_time_days_ago', 1);
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
      let _pClickTimer=null;
      chip.onclick=(e)=>{
        clearTimeout(_pClickTimer);
        _pClickTimer=setTimeout(()=>{_pClickTimer=null;_activeProject=p.project_id;renderSessionListFromCache();},220);
      };
      chip.ondblclick=(e)=>{e.stopPropagation();clearTimeout(_pClickTimer);_pClickTimer=null;_startProjectRename(p,chip);};
      chip.oncontextmenu=(e)=>{e.preventDefault();_showProjectContextMenu(e,p,chip);};
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
    caret.textContent='\u25BE'; // down when expanded; rotated right when collapsed
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
    for(const s of g.items){ body.appendChild(_renderOneSession(s, Boolean(g.isPinned))); }
    wrapper.appendChild(body);
    list.appendChild(wrapper);
  }
  // ── Render session items (extracted for group body use) ──
  // Note: declared after the groups loop but available via function hoisting.
  function _renderOneSession(s, isPinnedGroup=false){
    const el=document.createElement('div');
    const isActive=S.session&&s.session_id===S.session.session_id;
    const isLocalStreaming=Boolean(
      s.session_id
      && (
        (isActive&&S.busy)
        || (typeof INFLIGHT==='object'&&INFLIGHT&&INFLIGHT[s.session_id])
      )
    );
    const isStreaming=Boolean(s.is_streaming||isLocalStreaming);
    const hasUnread=_hasUnreadForSession(s)&&!isActive;
    el.className='session-item'+(isActive?' active':'')+(isActive&&S.session&&S.session._flash?' new-flash':'')+(s.archived?' archived':'')+(isStreaming?' streaming':'')+(hasUnread?' unread':'');
    if(isActive&&S.session&&S.session._flash)delete S.session._flash;
    const rawTitle=s.title||'Untitled';
    const tags=(rawTitle.match(/#[\w-]+/g)||[]);
    let cleanTitle=tags.length?rawTitle.replace(/#[\w-]+/g,'').trim():rawTitle;
    // Guard: system prompt content must never surface as a visible session title
    if(cleanTitle.startsWith('[SYSTEM:')){
      cleanTitle='Session';
    }
    const sessionText=document.createElement('div');
    sessionText.className='session-text';
    const titleRow=document.createElement('div');
    titleRow.className='session-title-row';
    if(s.pinned&&!isPinnedGroup){
      const pinInd=document.createElement('span');
      pinInd.className='session-pin-indicator';
      pinInd.innerHTML=ICONS.pin;
      titleRow.appendChild(pinInd);
    }
    const title=document.createElement('span');
    title.className='session-title';
    title.textContent=cleanTitle||'Untitled';
    title.title='Double-click to rename';
    const tsMs=_sessionTimestampMs(s);
    const ts=document.createElement('span');
    const hasAttentionState=isStreaming||hasUnread;
    ts.className='session-time'+(hasAttentionState?' is-hidden':'');
    ts.textContent=hasAttentionState?'':_formatRelativeSessionTime(tsMs);
    titleRow.appendChild(title);
    // Project color dot: placed BETWEEN title and timestamp, not inside the
    // title span. Inside the title span it would be clipped by the ellipsis
    // truncation, becoming invisible exactly when the title is long enough
    // to need the project marker. As a flex-flow sibling it stays visible
    // regardless of title length and sits next to the timestamp on the right.
    if(s.project_id){
      const proj=_allProjects.find(p=>p.project_id===s.project_id);
      if(proj){
        const dot=document.createElement('span');
        dot.className='session-project-dot';
        dot.style.background=proj.color||'var(--blue)';
        dot.title=proj.name;
        titleRow.appendChild(dot);
      }
    }
    titleRow.appendChild(ts);
    sessionText.appendChild(titleRow);
    const density=(window._sidebarDensity==='detailed'?'detailed':'compact');
    if(density==='detailed'){
      const metaBits=[];
      const msgCount=typeof s.message_count==='number'?s.message_count:0;
      const msgLabel=(typeof t==='function')
        ? t('session_meta_messages', msgCount)
        : `${msgCount} msg${msgCount===1?'':'s'}`;
      metaBits.push(msgLabel);
      if(s.model) metaBits.push(s.model);
      if(_showAllProfiles&&s.profile) metaBits.push(s.profile);
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
        if(e2.key==='Enter'){
          if(e2.isComposing){return;}
          e2.preventDefault();
          e2.stopPropagation();
          finish(true);
        }
        if(e2.key==='Escape'){e2.preventDefault();e2.stopPropagation();finish(false);}
      };
      // onblur: cancel only -- no accidental saves
      inp.onblur=()=>{ if(_renamingSid===s.session_id) finish(false); };
      title.replaceWith(inp);
      setTimeout(()=>{inp.focus();inp.select();},10);
    };

    // (Project dot is appended above, between title and timestamp, so it
    // sits outside the truncating title span and stays visible.)
    el.appendChild(sessionText);
    const state=document.createElement('span');
    state.className='session-attention-indicator session-state-indicator'+(isStreaming?' is-streaming':(hasUnread?' is-unread':''));
    state.setAttribute('aria-hidden','true');
    el.appendChild(state);
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

    // Use pointerup + manual double-tap detection instead of onclick/ondblclick.
    // onclick/ondblclick are unreliable on touch devices (iPad Safari especially):
    // hover-triggered layout shifts, ghost clicks, and 300ms delay all break
    // single-tap navigation. pointerup fires immediately on both mouse & touch.
    let _lastTapTime=0;
    let _tapTimer=null;
    el.onpointerup=(e)=>{
      if(e.pointerType==='mouse' && e.button!==0) return;  // ignore right/middle click
      if(_renamingSid) return;
      if(actions.contains(e.target)) return;
      const now=Date.now();
      if(now-_lastTapTime<350){
        // Double-tap: rename
        clearTimeout(_tapTimer);
        _tapTimer=null;
        _lastTapTime=0;
        startRename();
        return;
      }
      _lastTapTime=now;
      // Single tap: wait to ensure it's not the first of a double-tap,
      // then navigate
      clearTimeout(_tapTimer);
      _tapTimer=setTimeout(async()=>{
        _tapTimer=null;
        _lastTapTime=0;
        if(_renamingSid) return;
        // For CLI sessions, import into WebUI store first (idempotent)
        if(s.is_cli_session){
          try{
            await api('/api/session/import_cli',{method:'POST',body:JSON.stringify({session_id:s.session_id})});
          }catch(e){ /* import failed -- fall through to read-only view */ }
        }
        await loadSession(s.session_id);renderSessionListFromCache();
        if(typeof closeMobileSidebar==='function')closeMobileSidebar();
      }, 300);
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
      const _tt=$('topbarTitle');if(_tt)_tt.textContent=window._botName||'Hermes';
      const _tm=$('topbarMeta');if(_tm)_tm.textContent='Start a new conversation';
      $('msgInner').innerHTML='';
      $('emptyState').style.display='';
      $('fileTree').innerHTML='';
      if(typeof S!=='undefined') S.session=null;
      if(typeof syncAppTitlebar==='function') syncAppTitlebar();
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

// Resize a .project-create-input to fit its current value (or placeholder).
// Bounded by the CSS min-width:40px / max-width:180px on the same class so
// the input is never comically tiny nor wider than the project bar.
// Uses a hidden span sized with the same font/padding to measure text width.
function _resizeProjectInput(inp){
  const sizer=document.createElement('span');
  const cs=getComputedStyle(inp);
  // Read font from the live element so the sizer stays calibrated if CSS changes.
  // Horizontal padding only (0 vertical) — we're measuring width, not height.
  sizer.style.cssText='position:absolute;visibility:hidden;white-space:pre;';
  sizer.style.fontSize=cs.fontSize;
  sizer.style.fontFamily=cs.fontFamily;
  sizer.style.padding='0 '+cs.paddingRight;
  sizer.textContent=inp.value||inp.placeholder||' ';
  document.body.appendChild(sizer);
  const w=Math.min(180,Math.max(40,sizer.offsetWidth+2));
  document.body.removeChild(sizer);
  inp.style.width=w+'px';
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
    if(e.key==='Enter'){
      if(e.isComposing){return;}
      e.preventDefault();
      finish(true);
    }
    if(e.key==='Escape'){e.preventDefault();finish(false);}
  };
  inp.onblur=()=>finish(false);
  inp.addEventListener('input',()=>_resizeProjectInput(inp));
  addBtn.replaceWith(inp);
  _resizeProjectInput(inp);
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
    if(e.key==='Enter'){
      if(e.isComposing){return;}
      e.preventDefault();
      finish(true);
    }
    if(e.key==='Escape'){e.preventDefault();finish(false);}
  };
  inp.onblur=()=>finish(false);
  inp.onclick=(e)=>e.stopPropagation();
  inp.addEventListener('input',()=>_resizeProjectInput(inp));
  chip.replaceWith(inp);
  _resizeProjectInput(inp);
  setTimeout(()=>{inp.focus();inp.select();},10);
}

function _showProjectContextMenu(e, proj, chip){
  document.querySelectorAll('.project-ctx-menu').forEach(el=>el.remove());
  const menu=document.createElement('div');
  menu.className='project-ctx-menu';
  // background: var(--surface) — fully-opaque theme variable (not var(--panel),
  // which is undefined in this codebase and falls back to transparent, letting
  // the session list show through the menu). Same variable used by
  // .session-action-menu and other floating popovers.
  menu.style.cssText='position:fixed;background:var(--surface);border:1px solid var(--border);border-radius:8px;padding:6px 0;z-index:9999;min-width:140px;box-shadow:0 4px 16px rgba(0,0,0,.35);';
  menu.style.left=e.clientX+'px';
  menu.style.top=e.clientY+'px';

  // Rename option
  const renameItem=document.createElement('div');
  renameItem.textContent='Rename';
  renameItem.style.cssText='padding:7px 14px;cursor:pointer;font-size:13px;color:var(--text);';
  renameItem.onmouseenter=()=>renameItem.style.background='var(--hover)';
  renameItem.onmouseleave=()=>renameItem.style.background='';
  renameItem.onclick=()=>{menu.remove();_startProjectRename(proj,chip);};
  menu.appendChild(renameItem);

  // Color picker row
  const colorRow=document.createElement('div');
  colorRow.style.cssText='display:flex;gap:5px;padding:7px 14px;align-items:center;';
  PROJECT_COLORS.forEach(hex=>{
    const dot=document.createElement('span');
    dot.style.cssText=`width:16px;height:16px;border-radius:50%;background:${hex};cursor:pointer;display:inline-block;flex-shrink:0;`;
    if(hex===(proj.color||'')) dot.style.outline='2px solid var(--text)';
    dot.onclick=async()=>{
      menu.remove();
      await api('/api/projects/rename',{method:'POST',body:JSON.stringify({project_id:proj.project_id,name:proj.name,color:hex})});
      await renderSessionList();
      showToast('Color updated');
    };
    colorRow.appendChild(dot);
  });
  menu.appendChild(colorRow);

  // Divider + Delete
  const sep=document.createElement('hr');
  sep.style.cssText='border:none;border-top:1px solid var(--border);margin:4px 0;';
  menu.appendChild(sep);
  const delItem=document.createElement('div');
  delItem.textContent='Delete';
  delItem.style.cssText='padding:7px 14px;cursor:pointer;font-size:13px;color:var(--error,#e94560);';
  delItem.onmouseenter=()=>delItem.style.background='var(--hover)';
  delItem.onmouseleave=()=>delItem.style.background='';
  delItem.onclick=()=>{menu.remove();_confirmDeleteProject(proj);};
  menu.appendChild(delItem);

  document.body.appendChild(menu);
  const dismiss=()=>{menu.remove();document.removeEventListener('click',dismiss);};
  setTimeout(()=>document.addEventListener('click',dismiss),0);
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
