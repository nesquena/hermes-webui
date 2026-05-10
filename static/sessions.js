// ── Session action icons (SVG, monochrome, inherit currentColor) ──
const ICONS={
  stop:'<svg width="14" height="14" viewBox="0 0 16 16" fill="currentColor" stroke="none"><rect x="4" y="4" width="8" height="8" rx="1.5"/></svg>',
  pin:'<svg width="14" height="14" viewBox="0 0 16 16" fill="currentColor" stroke="none"><polygon points="8,1.5 9.8,5.8 14.5,6.2 11,9.4 12,14 8,11.5 4,14 5,9.4 1.5,6.2 6.2,5.8"/></svg>',
  unpin:'<svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.3"><polygon points="8,2 9.8,6.2 14.2,6.2 10.7,9.2 12,13.8 8,11 4,13.8 5.3,9.2 1.8,6.2 6.2,6.2"/></svg>',
  folder:'<svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.3"><path d="M2 4.5h4l1.5 1.5H14v7H2z"/></svg>',
  archive:'<svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.3"><rect x="1.5" y="2" width="13" height="3" rx="1"/><path d="M2.5 5v8h11V5"/><line x1="6" y1="8.5" x2="10" y2="8.5"/></svg>',
  unarchive:'<svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.3"><rect x="1.5" y="2" width="13" height="3" rx="1"/><path d="M2.5 5v8h11V5"/><polyline points="6.5,7 8,5.5 9.5,7"/></svg>',
  dup:'<svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.3"><rect x="4.5" y="4.5" width="8.5" height="8.5" rx="1.5"/><path d="M3 11.5V3h8.5"/></svg>',
  trash:'<svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.3"><path d="M3.5 4.5h9M6.5 4.5V3h3v1.5M4.5 4.5v8.5h7v-8.5"/><line x1="7" y1="7" x2="7" y2="11"/><line x1="9" y1="7" x2="9" y2="11"/></svg>',
  more:'<svg width="14" height="14" viewBox="0 0 16 16" fill="currentColor" stroke="none"><circle cx="8" cy="3" r="1.25"/><circle cx="8" cy="8" r="1.25"/><circle cx="8" cy="13" r="1.25"/></svg>',
  edit:'<svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" stroke-linejoin="round"><path d="M11.5 2.5l2 2L5 13H3v-2z"/><path d="M10 4l2 2"/></svg>',
};

// Tracks which session_id is currently being loaded. Used to discard stale
// responses from in-flight requests when the user switches sessions again
// before the first request completes (#1060).
let _loadingSessionId = null;

// ── Composer draft persistence ────────────────────────────────────────────────

// Debounced save — prevents hammering the server on every keystroke.
let _draftSaveTimer = null;
const _DRAFT_SAVE_DELAY_MS = 400;

function _saveComposerDraft(sid, text, files) {
  if (!sid) return;
  clearTimeout(_draftSaveTimer);
  _draftSaveTimer = setTimeout(() => {
    api('/api/session/draft', {
      method: 'POST',
      body: JSON.stringify({ session_id: sid, text: text || '', files: files || [] }),
    }).catch(() => {});
  }, _DRAFT_SAVE_DELAY_MS);
}

// Fire-and-forget immediate save (used before session switches).
function _saveComposerDraftNow(sid, text, files) {
  if (!sid) return;
  clearTimeout(_draftSaveTimer);
  api('/api/session/draft', {
    method: 'POST',
    body: JSON.stringify({ session_id: sid, text: text || '', files: files || [] }),
  }).catch(() => {});
}

// Restore composer draft from server onto #msg textarea.
// Only restores if there's actual text (skip empty/None drafts).
// Guards against double-restore when rapidly switching sessions.
function _restoreComposerDraft(draft, targetSid) {
  const ta = $('msg');
  if (!ta) return;
  // targetSid is the session that was requested — if it no longer matches
  // _loadingSessionId, a newer session switch has already begun, so skip.
  if (targetSid && _loadingSessionId !== null && _loadingSessionId !== targetSid) return;
  const text = (draft && typeof draft.text === 'string') ? draft.text : '';
  const files = (draft && Array.isArray(draft.files)) ? draft.files : [];
  // If there's no text and no files, clear the textarea (a previous session's
  // draft may still be sitting there from a cross-session switch).
  if (!text && !files.length) {
    if (ta.value) {
      ta.value = '';
      if (typeof autoResize === 'function') autoResize();
      if (typeof updateSendBtn === 'function') updateSendBtn();
    }
    return;
  }
  // Only update if different to avoid cursor jumps on unrelated session switches.
  const current = ta.value || '';
  if (current !== text) {
    ta.value = text;
    if (typeof autoResize === 'function') autoResize();
    if (typeof updateSendBtn === 'function') updateSendBtn();
  }
  // Files restoration is skipped for now (requires S.pendingFiles plumbing).
}

// Clear the saved draft for a session (called when message is sent).
function _clearComposerDraft(sid) {
  if (!sid) return;
  clearTimeout(_draftSaveTimer);
  api('/api/session/draft', {
    method: 'POST',
    body: JSON.stringify({ session_id: sid, text: '' }),
  }).catch(() => {});
}

const SESSION_VIEWED_COUNTS_KEY = 'hermes-session-viewed-counts';
const SESSION_COMPLETION_UNREAD_KEY = 'hermes-session-completion-unread';
const SESSION_OBSERVED_STREAMING_KEY = 'hermes-session-observed-streaming';
let _sessionViewedCounts = null;
let _sessionCompletionUnread = null;
let _sessionObservedStreaming = null;
const _sessionStreamingById = new Map();
const _sessionListSnapshotById = new Map();

function _formatSessionModelWithGateway(s){
  if(!s||!s.model)return'';
  const routing=(typeof _latestGatewayRoutingForSession==='function')?_latestGatewayRoutingForSession(s):(s.gateway_routing||null);
  if(typeof _formatGatewayModelLabel==='function'){
    return _formatGatewayModelLabel(s.model,s.model,routing)||s.model;
  }
  return s.model;
}

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

function _getSessionCompletionUnread() {
  if (_sessionCompletionUnread !== null) return _sessionCompletionUnread;
  try {
    const parsed = JSON.parse(localStorage.getItem(SESSION_COMPLETION_UNREAD_KEY) || '{}');
    _sessionCompletionUnread = parsed && typeof parsed === 'object' && !Array.isArray(parsed) ? parsed : {};
  } catch (_){
    _sessionCompletionUnread = {};
  }
  return _sessionCompletionUnread;
}

function _saveSessionCompletionUnread() {
  try {
    localStorage.setItem(SESSION_COMPLETION_UNREAD_KEY, JSON.stringify(_getSessionCompletionUnread()));
  } catch (_){
    // Ignore localStorage write failures.
  }
}

function _markSessionCompletionUnread(sid, messageCount = 0) {
  if (!sid) return;
  const unread = _getSessionCompletionUnread();
  const count = Number.isFinite(messageCount) ? Number(messageCount) : 0;
  unread[sid] = {message_count: count, completed_at: Date.now()};
  _saveSessionCompletionUnread();
}

function _clearSessionCompletionUnread(sid) {
  if (!sid) return;
  const unread = _getSessionCompletionUnread();
  if (!Object.prototype.hasOwnProperty.call(unread, sid)) return;
  delete unread[sid];
  _saveSessionCompletionUnread();
}

function _hasSessionCompletionUnread(sid) {
  if (!sid) return false;
  return Object.prototype.hasOwnProperty.call(_getSessionCompletionUnread(), sid);
}

function _getSessionObservedStreaming() {
  if (_sessionObservedStreaming !== null) return _sessionObservedStreaming;
  try {
    const parsed = JSON.parse(localStorage.getItem(SESSION_OBSERVED_STREAMING_KEY) || '{}');
    _sessionObservedStreaming = parsed && typeof parsed === 'object' && !Array.isArray(parsed) ? parsed : {};
  } catch (_){
    _sessionObservedStreaming = {};
  }
  return _sessionObservedStreaming;
}

function _saveSessionObservedStreaming() {
  try {
    localStorage.setItem(SESSION_OBSERVED_STREAMING_KEY, JSON.stringify(_getSessionObservedStreaming()));
  } catch (_){
    // Ignore localStorage write failures.
  }
}

function _rememberObservedStreamingSession(s) {
  if (!s || !s.session_id) return;
  const observed = _getSessionObservedStreaming();
  observed[s.session_id] = {
    message_count: Number(s.message_count || 0),
    last_message_at: Number(s.last_message_at || 0),
    observed_at: Date.now(),
  };
  _saveSessionObservedStreaming();
}

function _forgetObservedStreamingSession(sid) {
  if (!sid) return;
  const observed = _getSessionObservedStreaming();
  if (!Object.prototype.hasOwnProperty.call(observed, sid)) return;
  delete observed[sid];
  _saveSessionObservedStreaming();
}

function _hasUnreadForSession(s) {
  if (!s || !s.session_id) return false;
  if (_hasSessionCompletionUnread(s.session_id)) return true;
  const counts = _getSessionViewedCounts();
  if (!Object.prototype.hasOwnProperty.call(counts, s.session_id)) {
    _setSessionViewedCount(s.session_id, Number(s.message_count || 0));
    return false;
  }
  if (!Number.isFinite(s.message_count)) return false;
  return s.message_count > Number(counts[s.session_id] || 0);
}

function _isSessionActivelyViewedForList(sid) {
  if (!sid || !S.session || S.session.session_id !== sid) return false;
  if (typeof _loadingSessionId !== 'undefined' && _loadingSessionId && _loadingSessionId !== sid) return false;
  if (typeof document !== 'undefined' && document.visibilityState && document.visibilityState !== 'visible') return false;
  if (typeof document !== 'undefined' && typeof document.hasFocus === 'function' && !document.hasFocus()) return false;
  return true;
}

function _isSessionLocallyStreaming(s) {
  if (!s || !s.session_id) return false;
  const isActive = S.session && s.session_id === S.session.session_id;
  return Boolean(
    (isActive && S.busy)
    || (typeof INFLIGHT === 'object' && INFLIGHT && INFLIGHT[s.session_id])
  );
}

function _isSessionEffectivelyStreaming(s) {
  return Boolean(s && (s.is_streaming || _isSessionLocallyStreaming(s)));
}

function _rememberRenderedStreamingState(s, isStreaming) {
  if (!s || !s.session_id || !isStreaming) return;
  _sessionStreamingById.set(s.session_id, true);
  _rememberObservedStreamingSession(s);
}

function _rememberRenderedSessionSnapshot(s) {
  if (!s || !s.session_id) return;
  const previous = _sessionListSnapshotById.get(s.session_id);
  if (previous) return;
  _sessionListSnapshotById.set(s.session_id, {
    message_count: Number(s.message_count || 0),
    last_message_at: Number(s.last_message_at || 0),
  });
}

function _markSessionCompletedInList(session, previousSid = null) {
  if (!session || !Array.isArray(_allSessions)) return;
  const finalSid = session.session_id || previousSid;
  if (!finalSid) return;
  const idx = _allSessions.findIndex(s => s && (s.session_id === finalSid || s.session_id === previousSid));
  if (idx < 0) return;
  const {messages: _messages, tool_calls: _toolCalls, ...sessionMeta} = session;
  const messageCount = Number(
    session.message_count != null
      ? session.message_count
      : (Array.isArray(session.messages) ? session.messages.length : (_allSessions[idx].message_count || 0))
  );
  const lastMessageAt = Number(session.last_message_at || session.updated_at || _allSessions[idx].last_message_at || 0);
  _allSessions[idx] = {
    ..._allSessions[idx],
    ...sessionMeta,
    session_id: finalSid,
    message_count: messageCount,
    last_message_at: lastMessageAt,
    active_stream_id: null,
    pending_user_message: null,
    pending_started_at: null,
    is_streaming: false,
  };
  _sessionStreamingById.set(finalSid, false);
  _forgetObservedStreamingSession(finalSid);
  if (previousSid && previousSid !== finalSid) {
    _sessionStreamingById.delete(previousSid);
    _forgetObservedStreamingSession(previousSid);
    _sessionListSnapshotById.delete(previousSid);
  }
  _sessionListSnapshotById.set(finalSid, {
    message_count: messageCount,
    last_message_at: lastMessageAt,
  });
  renderSessionListFromCache();
}

function _markPollingCompletionUnreadTransitions(sessions) {
  if (!Array.isArray(sessions)) return;
  const seen = new Set();
  for (const s of sessions) {
    if (!s || !s.session_id) continue;
    const sid = s.session_id;
    seen.add(sid);
    const wasStreaming = _sessionStreamingById.get(sid);
    const isStreaming = _isSessionEffectivelyStreaming(s);
    const previousSnapshot = _sessionListSnapshotById.get(sid);
    const observedStreaming = _getSessionObservedStreaming()[sid];
    const messageCount = Number(s.message_count || 0);
    const lastMessageAt = Number(s.last_message_at || 0);
    const completedObservedStream = wasStreaming === true && !isStreaming;
    const completedWithNewMessages = Boolean(
      (previousSnapshot || observedStreaming)
      && !isStreaming
      && (
        messageCount > Number((previousSnapshot || observedStreaming).message_count || 0)
        || lastMessageAt > Number((previousSnapshot || observedStreaming).last_message_at || 0)
      )
    );
    const completedPersistedObservedStream = Boolean(observedStreaming && !isStreaming);
    if ((completedObservedStream || completedPersistedObservedStream || completedWithNewMessages) && !_isSessionActivelyViewedForList(sid)) {
      _markSessionCompletionUnread(sid, s.message_count);
    }
    _sessionStreamingById.set(sid, isStreaming);
    if (isStreaming) {
      _rememberObservedStreamingSession(s);
    } else {
      _forgetObservedStreamingSession(sid);
    }
    _sessionListSnapshotById.set(sid, {
      message_count: messageCount,
      last_message_at: lastMessageAt,
    });
  }
  for (const sid of Array.from(_sessionStreamingById.keys())) {
    if (!seen.has(sid)) _sessionStreamingById.delete(sid);
  }
  for (const sid of Array.from(_sessionListSnapshotById.keys())) {
    if (!seen.has(sid)) _sessionListSnapshotById.delete(sid);
  }
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
  const modelSel=$('modelSelect');
  const selectedDefaultModel=window._defaultModel||(modelSel&&modelSel.value)||'';
  let defaultApplied=false;
  if(window._defaultModel&&modelSel&&typeof _applyModelToDropdown==='function'){
    defaultApplied=!!_applyModelToDropdown(window._defaultModel,modelSel,window._activeProvider||null);
  }
  const canQualify=!window._defaultModel||defaultApplied||(modelSel&&modelSel.value===selectedDefaultModel);
  const newModelState=(canQualify&&typeof _modelStateForSelect==='function')
    ? _modelStateForSelect(modelSel,selectedDefaultModel)
    : {model:selectedDefaultModel,model_provider:null};
  const reqBody={
    model:newModelState.model,
    model_provider:newModelState.model_provider||null,
    workspace:inheritWs,
    profile:S.activeProfile||'default',
  };
  if(_activeProject&&_activeProject!==NO_PROJECT_FILTER) reqBody.project_id=_activeProject;
  const data=await api('/api/session/new',{method:'POST',body:JSON.stringify(reqBody)});
  S.session=data.session;S.messages=data.session.messages||[];
  S.lastUsage={...(data.session.last_usage||{})};
  if(flash)S.session._flash=true;
  localStorage.setItem('hermes-webui-session',S.session.session_id);
  _setActiveSessionUrl(S.session.session_id);
  _setSessionViewedCount(S.session.session_id, S.session.message_count || 0);
  // Sync chat-header dropdown to the session's model so the UI reflects
  // the default model the server actually used (#872).
  if(S.session.model && S.session.model!==$('modelSelect').value && typeof _applyModelToDropdown==='function'){
    _applyModelToDropdown(S.session.model,$('modelSelect'),S.session.model_provider||null);
    if(typeof syncModelChip==='function') syncModelChip();
  }
  // Reset per-session visual state: a fresh chat is idle even if another
  // conversation is still streaming in the background.
  S.busy=false;
  S.activeStreamId=null;
  updateSendBtn();
  setStatus('');
  setComposerStatus('');
  updateQueueBadge(S.session.session_id);
  syncTopbar();renderMessages();loadDir('.');
  // don't call renderSessionList here - callers do it when needed
}

async function loadSession(sid){
  const currentSid = S.session ? S.session.session_id : null;
  // Clicking the already-open session in the sidebar is a no-op. Reloading it
  // tears down active pane state and can reset the long-session scroll window
  // to the top even though the user did not navigate anywhere.
  if(currentSid===sid) return;
  // Mark this session as the in-flight load. Subsequent loadSession() calls
  // will overwrite this; stale awaits use the mismatch to bail out (#1060).
  _loadingSessionId = sid;
  stopApprovalPolling();hideApprovalCard();
  _yoloEnabled=false;_updateYoloPill();
  if(typeof stopClarifyPolling==='function') stopClarifyPolling();
  if(typeof hideClarifyCard==='function') hideClarifyCard();
  // Show loading indicator immediately for responsiveness.
  // Cleared by renderMessages() once full session data arrives.
  // Persist the current composer draft before switching away so it can be
  // restored when the user switches back (#1060). Save to server now so the
  // draft survives page refresh and syncs across clients.
  if (currentSid && currentSid !== sid) {
    _saveComposerDraftNow(currentSid, ($('msg') || {}).value || '', S.pendingFiles ? [...S.pendingFiles] : []);
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
        // If this 404 was for the saved active-session ID (not a click-into request),
        // wipe the stale localStorage value and rethrow so boot can fall through to
        // the empty-state instead of sticking to a broken "Session not available" view.
        if(!currentSid&&localStorage.getItem('hermes-webui-session')===sid){
          localStorage.removeItem('hermes-webui-session');
          if (_loadingSessionId === sid) _loadingSessionId = null;
          throw e;
        }
      } else {
        _msgInner.innerHTML='<div style="display:flex;align-items:center;justify-content:center;height:100%;color:var(--text-muted);font-size:14px;padding:40px;text-align:center;">Failed to load session. Try switching sessions or refreshing.</div>';
        if(typeof showToast==='function') showToast('Failed to load session',3000,'error');
      }
    }
    if (_loadingSessionId === sid) _loadingSessionId = null;
    return;
  }
  // Guard: api() may have redirected (401) and returned undefined; in that case
  // the browser is already navigating away, so abort the rest of this flow.
  if (!data) {
    if (_loadingSessionId === sid) _loadingSessionId = null;
    return;
  }
  // Stale response? A newer loadSession() call has already started (#1060).
  if (_loadingSessionId !== sid) return;
  S.session=data.session;
  S.session._modelResolutionDeferred=true;
  S.lastUsage={...(data.session.last_usage||{})};
  // Reset scroll-direction tracker on session switch so the new chat's
  // first scroll doesn't compare against the previous chat's scrollTop
  // and false-trigger an unpin (#1731 follow-up — Opus stage-302 SHOULD-FIX).
  if (typeof window !== 'undefined' && typeof window._resetScrollDirectionTracker === 'function') {
    try { window._resetScrollDirectionTracker(); } catch (_) {}
  }
  // Sync workspace display immediately so the chip label reflects the new session's workspace
  // before any async message-loading begins (mirrors how model is handled).
  if(typeof syncTopbar==='function') syncTopbar();
  _setSessionViewedCount(S.session.session_id, Number(data.session.message_count || 0));
  _clearSessionCompletionUnread(S.session.session_id);
  localStorage.setItem('hermes-webui-session',S.session.session_id);
  _setActiveSessionUrl(S.session.session_id);

  const activeStreamId=S.session.active_stream_id||null;
  // If the server says the session is idle, discard any browser-side inflight
  // cache left behind by a crashed/restarted stream. Otherwise the UI can keep
  // showing a permanent thinking/running state even though active_streams=0.
  if(!activeStreamId&&INFLIGHT[sid]){
    delete INFLIGHT[sid];
    if(typeof clearInflightState==='function') clearInflightState(sid);
    S.activeStreamId=null;
    S.busy=false;
  }

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
    // appendLiveToolCard() is guarded by S.activeStreamId; restore it before
    // replaying persisted live tools so the compact Activity count survives
    // switching away from and back to an active chat (#1715).
    S.activeStreamId=activeStreamId;
    syncTopbar();renderMessages();appendThinking();loadDir('.');
    clearLiveToolCards();
    if(typeof placeLiveToolCardsHost==='function') placeLiveToolCardsHost();
    for(const tc of (S.toolCalls||[])){
      if(tc&&tc.name) appendLiveToolCard(tc);
    }
    setBusy(true);setComposerStatus('');
    startApprovalPolling(sid);
    if(typeof startClarifyPolling==='function') startClarifyPolling(sid);
    if(typeof _fetchYoloState==='function') _fetchYoloState(sid);
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
      setStatus('');
      setComposerStatus('');
      syncTopbar();renderMessages();appendThinking();loadDir('.');
      updateQueueBadge(sid);
      startApprovalPolling(sid);
      if(typeof startClarifyPolling==='function') startClarifyPolling(sid);
      if(typeof _fetchYoloState==='function') _fetchYoloState(sid);
      if(typeof attachLiveStream==='function') attachLiveStream(sid, activeStreamId, S.session.pending_attachments||[], {reconnecting:true});
      else if(typeof watchInflightSession==='function') watchInflightSession(sid, activeStreamId);
    }else{
      S.busy=false;
      S.activeStreamId=null;
      updateSendBtn();
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
  if(typeof _renderPendingPromptsForActiveSession==='function') _renderPendingPromptsForActiveSession();

  // Restore server-persisted composer draft (synced across clients + survives refresh).
  // Pass sid so _restoreComposerDraft can skip if this session is mid-load (guards
  // against stale writes from slow responses racing to restore the previous draft).
  const _draft = S.session && S.session.composer_draft;
  if (_draft && (typeof _restoreComposerDraft === 'function')) {
    _restoreComposerDraft(_draft, sid);
  }

  _resolveSessionModelForDisplaySoon(sid);
  // Clear the in-flight session marker now that this load has completed (#1060).
  if (_loadingSessionId === sid) _loadingSessionId = null;

  // ── Cross-channel handoff hint ──
  // After session fully loaded, check if this is a messaging session with
  // enough conversation rounds to warrant a handoff hint bar.
  if (S.session && _isMessagingSession(S.session)) {
    _checkAndShowHandoffHint(sid);
  } else {
    _hideHandoffHint();
  }
}

// ── Handoff hint logic ──────────────────────────────────────────────────────

const _HANDOFF_THRESHOLD = 10;  // conversation rounds
const _HANDOFF_STORAGE_PREFIX = 'handoff:';
const _HANDOFF_SUFFIX_DISMISSED_AT = 'dismissed_at';
const _HANDOFF_SUFFIX_SUMMARY_HANDLED_AT = 'summary_handled_at';
const _MESSAGING_RAW_SOURCES = new Set(['weixin', 'telegram', 'discord', 'slack']);
const _MESSAGING_SOURCE_LABELS = {
  weixin: 'WeChat',
  telegram: 'Telegram',
  discord: 'Discord',
  slack: 'Slack',
};

function _isMessagingSession(session) {
  if (!session) return false;
  // session_source is set by PR #1294 source normalization
  if (session.session_source === 'messaging') return true;
  // Fallback: check raw_source directly
  const raw = (session.raw_source || session.source_tag || session.source || '').toLowerCase();
  return _MESSAGING_RAW_SOURCES.has(raw);
}

function _isReadOnlySession(session) {
  return !!(session && (session.read_only || session.is_read_only));
}

function _sourceKeyForSession(session) {
  return (session && (session.raw_source || session.source_tag || session.source || '') || '').toLowerCase();
}

function _isCliSession(session) {
  if (!session) return false;
  // session_source is set by upstream normalization for CLI sessions as 'cli'
  if (session.session_source === 'cli') return true;
  // Legacy payloads often use raw/source tags to convey the source.
  const raw = (
    session.raw_source
    || session.source_tag
    || session.source
    || session.source_label
    || ''
  ).toLowerCase();
  if (raw === 'cli') return true;
  // If messaging-like, don't classify as legacy CLI even when is_cli_session is true.
  if (_isMessagingSession(session)) return false;
  return session.is_cli_session === true;
}

function _normalizeMessageForCliImportComparison(message) {
  if (!message || typeof message !== 'object') return message;
  const clone = { ...message };
  delete clone.timestamp;
  delete clone._ts;
  return clone;
}

function _isCliImportRefreshPrefixMatch(localMessages, freshMessages) {
  if (!Array.isArray(localMessages) || !Array.isArray(freshMessages)) return false;
  if (localMessages.length > freshMessages.length) return false;
  for (let i = 0; i < localMessages.length; i += 1) {
    if (JSON.stringify(_normalizeMessageForCliImportComparison(localMessages[i])) !== JSON.stringify(_normalizeMessageForCliImportComparison(freshMessages[i]))) {
      return false;
    }
  }
  return true;
}

function _handoffStorageKey(sid) {
  return `${_HANDOFF_STORAGE_PREFIX}${sid}:`;
}

function _getHandoffStorageValue(sid, suffix) {
  try {
    const raw = localStorage.getItem(_handoffStorageKey(sid) + suffix);
    return raw ? parseFloat(raw) : null;
  } catch { return null; }
}

function _setHandoffStorageValue(sid, suffix, ts) {
  const key = _handoffStorageKey(sid) + suffix;
  try {
    if (!Number.isFinite(ts)) {
      localStorage.removeItem(key);
      return;
    }
    localStorage.setItem(key, String(ts));
  } catch {}
}

function _clearHandoffStorageForSession(sid) {
  if (!sid) return;
  try {
    _setHandoffStorageValue(sid, _HANDOFF_SUFFIX_DISMISSED_AT, null);
    _setHandoffStorageValue(sid, _HANDOFF_SUFFIX_SUMMARY_HANDLED_AT, null);
  } catch {}
}

function _getHandoffDismissedAt(sid) {
  return _getHandoffStorageValue(sid, _HANDOFF_SUFFIX_DISMISSED_AT);
}

function _setHandoffDismissedAt(sid, ts) {
  _setHandoffStorageValue(sid, _HANDOFF_SUFFIX_DISMISSED_AT, ts);
}

function _getHandoffSummaryHandledAt(sid) {
  return _getHandoffStorageValue(sid, _HANDOFF_SUFFIX_SUMMARY_HANDLED_AT);
}

function _setHandoffSummaryHandledAt(sid, ts) {
  _setHandoffStorageValue(sid, _HANDOFF_SUFFIX_SUMMARY_HANDLED_AT, ts);
}

function _getHandoffSince(sid) {
  const dismissedAt = _getHandoffDismissedAt(sid);
  const summaryHandledAt = _getHandoffSummaryHandledAt(sid);
  if (Number.isFinite(dismissedAt) && Number.isFinite(summaryHandledAt)) return Math.max(dismissedAt, summaryHandledAt);
  if (Number.isFinite(dismissedAt)) return dismissedAt;
  if (Number.isFinite(summaryHandledAt)) return summaryHandledAt;
  return null;
}

function _handoffMessagesEl() {
  return document.getElementById('messages');
}

function _handoffIsMessagesNearBottom(el) {
  if (!el) return false;
  return el.scrollHeight - el.scrollTop - el.clientHeight < 150;
}

function _syncHandoffDockSpace(open) {
  const messages = _handoffMessagesEl();
  if (!messages) return;
  const wasNearBottom = _handoffIsMessagesNearBottom(messages);
  if (!open) {
    messages.classList.remove('handoff-dock-visible');
    messages.style.removeProperty('--handoff-dock-height');
    if (wasNearBottom && typeof scrollToBottom === 'function') requestAnimationFrame(scrollToBottom);
    return;
  }
  messages.classList.add('handoff-dock-visible');
  const measure = () => {
    const container = $('handoffHintContainer');
    const h = container && container.getBoundingClientRect().height;
    if (h > 0) messages.style.setProperty('--handoff-dock-height', Math.ceil(h + 24) + 'px');
    if (wasNearBottom && typeof scrollToBottom === 'function') scrollToBottom();
  };
  requestAnimationFrame(measure);
  setTimeout(measure, 360);
}

function _getChannelLabel(session) {
  if (!session) return '';
  // Use source_label from PR #1294 if available
  if (session.source_label) return session.source_label;
  const raw = (session.raw_source || session.source_tag || session.source || '').toLowerCase();
  return _MESSAGING_SOURCE_LABELS[raw] || raw || '';
}

async function _checkAndShowHandoffHint(sid) {
  try {
    const since = _getHandoffSince(sid);
    const body = { session_id: sid };
    if (since != null) body.since = since;

    const result = await api('/api/session/conversation-rounds', {
      method: 'POST',
      body: JSON.stringify(body),
    });
    // Stale? Session switched while we were fetching.
    if (!S.session || S.session.session_id !== sid) return;

    if (result && result.ok && result.should_show) {
      _showHandoffHint(sid, result.rounds);
    } else {
      const container = $('handoffHintContainer');
      const isSameVisibleSession = !!(
        container &&
        container.classList.contains('is-visible') &&
        container.dataset.sessionId === String(sid)
      );
      if (!isSameVisibleSession) _hideHandoffHint();
    }
  } catch (e) {
    console.warn('Handoff hint check failed:', e);
    _hideHandoffHint();
  }
}

function _showHandoffHint(sid, rounds) {
  const container = $('handoffHintContainer');
  if (!container) return;

  // Clear any existing content.
  container.innerHTML = '';
  container.style.display = '';
  container.classList.add('is-visible');
  container.dataset.sessionId = String(sid);

  const channel = _getChannelLabel(S.session);
  const hintText = channel
    ? `${channel} handoff`
    : `Conversation handoff`;
  const hintMeta = `${rounds} new conversation rounds`;

  const bar = document.createElement('div');
  bar.className = 'handoff-hint-bar';
  bar.id = 'handoffHintBar';
  bar.innerHTML = `
    <div class="handoff-hint-text">
      <span class="handoff-hint-dot" aria-hidden="true"></span>
      <span class="handoff-hint-label">${esc(hintText)}</span>
      <span class="handoff-hint-meta">${esc(hintMeta)}</span>
    </div>
    <div class="handoff-hint-actions">
      <button class="handoff-hint-action" type="button">View summary</button>
      <button class="handoff-hint-dismiss" type="button" onclick="event.stopPropagation(); _dismissHandoffHint('${esc(sid)}')" title="Dismiss">
        Close
      </button>
    </div>
  `;

  // Click on the bar (not the explicit close button) triggers summary generation.
  bar.addEventListener('click', (e) => {
    if (e.target.closest('.handoff-hint-dismiss')) return;
    _generateHandoffSummary(sid, rounds);
  });

  container.appendChild(bar);
  _syncHandoffDockSpace(true);
}

function _hideHandoffHint() {
  const container = $('handoffHintContainer');
  if (container) {
    container.innerHTML = '';
    container.style.display = 'none';
    container.classList.remove('is-visible');
    delete container.dataset.sessionId;
  }
  _syncHandoffDockSpace(false);
}

function _dismissHandoffHint(sid) {
  _setHandoffDismissedAt(sid, Date.now() / 1000);
  _hideHandoffHint();
}

function _buildHandoffSummaryToolMessage(summary, channel, rounds, fallback) {
  const generatedAt = Date.now() / 1000;
  return {
    role: 'tool',
    tool_call_id: '',
    name: 'handoff_summary',
    timestamp: generatedAt,
    _ts: generatedAt,
    content: JSON.stringify({
      _handoff_summary_card: true,
      session_id: sidValue(),
      summary: String(summary || '').trim(),
      channel: (typeof channel === 'string' && channel.trim()) ? channel.trim() : null,
      rounds: Number.isFinite(rounds) ? rounds : null,
      fallback: !!fallback,
      generated_at: generatedAt,
    }),
  };
}

function sidValue() {
  return S && S.session && S.session.session_id ? S.session.session_id : null;
}

function _extractHandoffSummaryPayload(content){
  if(!content) return null;
  if(typeof content!=='string') return null;
  try {
    const parsed=JSON.parse(content);
    return parsed&&typeof parsed==='object'&&parsed._handoff_summary_card===true?parsed:null;
  } catch (e) {
    return null;
  }
}

async function _generateHandoffSummary(sid, rounds) {
  // Treat handoff like a slash-command result: the composer dock entry
  // disappears and the transient summary card renders in the transcript.
  _hideHandoffHint();
  const channel = _getChannelLabel(S.session);
  if (typeof setHandoffUi === 'function') {
    setHandoffUi({
      sessionId: sid,
      phase: 'running',
      channel,
      rounds,
    });
  }

  try {
    const since = _getHandoffSince(sid);
    const body = { session_id: sid };
    if (since != null) body.since = since;

    const result = await api('/api/session/handoff-summary', {
      method: 'POST',
      body: JSON.stringify(body),
    });
    const isSuccess = result && result.ok && result.summary;
    if (isSuccess) {
      _setHandoffSummaryHandledAt(sid, Date.now() / 1000);
      _setHandoffDismissedAt(sid, null);
      const marker=_buildHandoffSummaryToolMessage(result.summary, channel, result.rounds || rounds, !!result.fallback);
      if (S.session && S.session.session_id === sid) {
        S.messages = [...S.messages, marker];
        if (typeof renderMessages === 'function') renderMessages();
      }
      if (typeof setHandoffUi === 'function') {
        setHandoffUi(null);
      }
    } else if (S.session && S.session.session_id === sid && typeof setHandoffUi === 'function') {
      // Keep transient card while the user can retry the action.
      setHandoffUi({
        sessionId: sid,
        phase: 'error',
        channel,
        rounds,
        errorText: 'Could not generate summary. Please try again.',
      });
    } else {
      // Stale session response path: only record success baseline.
    }
  } catch (e) {
    console.warn('Handoff summary failed:', e);
    if (S.session && S.session.session_id === sid && typeof setHandoffUi === 'function') {
      setHandoffUi({
        sessionId: sid,
        phase: 'error',
        channel,
        rounds,
        errorText: 'Summary generation failed: ' + e.message,
      });
    }
  }

  // If generation succeeds, set a baseline so only new activity after that time
  // can re-trigger handoff prompts. Failures keep the hint active so users can
  // retry.
}

function _resolveSessionModelForDisplaySoon(sid){
  if(!sid) return;
  setTimeout(async()=>{
    try{
      const data=await api(`/api/session?session_id=${encodeURIComponent(sid)}&messages=0&resolve_model=1`);
      const model=data&&data.session&&data.session.model;
      const provider=data&&data.session&&data.session.model_provider;
      if(!model||!S.session||S.session.session_id!==sid) return;
      S.session.model=model;
      S.session.model_provider=provider||null;
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
  // Guard: api() may have redirected (401) and returned undefined.
  if (!data || !data.session) return;
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
// Generation token bumped every time S.messages is wholesale-replaced
// (rather than incrementally extended). _loadOlderMessages snapshots it
// before its `await` and re-checks after, so a late-resolving prefetch
// does not prepend onto a transcript that was rebuilt under it
// (e.g. by _ensureAllMessagesLoaded after a Start-jump). See #1937.
let _messagesGeneration = 0;
function _bumpMessagesGeneration() {
  // Wrap to keep the counter bounded; the only operation that matters is
  // strict inequality between the snapshot and the post-await read, so any
  // monotonic bump is sufficient.
  _messagesGeneration = (_messagesGeneration + 1) | 0;
  return _messagesGeneration;
}

async function _loadOlderMessages() {
  if (_loadingOlder || !_messagesTruncated) return;
  const sid = S.session ? S.session.session_id : null;
  if (!sid || !S.messages.length) return;
  if (_oldestIdx <= 0) { _messagesTruncated = false; return; }
  _loadingOlder = true;
  // Snapshot the generation BEFORE we await. If S.messages is wholesale
  // replaced while the request is in flight, the post-await check below
  // bails out so we never prepend stale older messages onto a freshly
  // rebuilt transcript (#1937).
  const startGeneration = _messagesGeneration;
  try {
    const data = await api(`/api/session?session_id=${encodeURIComponent(sid)}&messages=1&resolve_model=0&msg_before=${_oldestIdx}&msg_limit=${_INITIAL_MSG_LIMIT}`);
    // Guard: api() may have redirected (401) and returned undefined.
    if (!data || !data.session) { _loadingOlder = false; return; }
    //  - response shape sane
    //  - the active session is still the one we issued the request for.
    //    Compare against S.session.session_id, NOT _loadingSessionId — the
    //    latter is null between session loads, leaving a window where a
    //    stale response could prepend onto the new session's S.messages.
    if (!data || !data.session) return;
    if (!S.session || S.session.session_id !== sid) return;
    if (_loadingSessionId !== null && _loadingSessionId !== sid) return;
    // Generation guard: another code path (typically jumpToSessionStart →
    // _ensureAllMessagesLoaded) may have replaced S.messages while we were
    // awaiting. Prepending older messages onto that replacement would
    // duplicate the head of the transcript. Detect via the generation
    // counter and abort cleanly. _oldestIdx and _messagesTruncated were
    // already reset by the wholesale-replace path, so no rollback needed.
    if (_messagesGeneration !== startGeneration) return;
    const olderMsgs = (data.session.messages || []).filter(m => m && m.role);
    if (!olderMsgs.length) { _messagesTruncated = false; return; }
    // Prepend older messages
    // Use $('messages') — the scrollable container (#msgInner is not scrollable).
    const container = $('messages');
    const prevScrollH = container ? container.scrollHeight : 0;
    S.messages = [...olderMsgs, ...S.messages];
    // renderMessages() windows long transcripts from the end. If we do not
    // expand that window before rendering, the newly prepended page stays
    // hidden and the "hidden" counter rises while the viewport appears stuck.
    // Count roughly by the same visible-message rules used by renderMessages().
    const addedRenderable = olderMsgs.filter(m=>{
      if(!m||!m.role||m.role==='tool') return false;
      if(typeof _isContextCompactionMessage==='function'&&_isContextCompactionMessage(m)) return false;
      if(typeof _isPreservedCompressionTaskListMessage==='function'&&_isPreservedCompressionTaskListMessage(m)) return false;
      const hasTc=Array.isArray(m.tool_calls)&&m.tool_calls.length>0;
      const hasTu=Array.isArray(m.content)&&m.content.some(p=>p&&p.type==='tool_use');
      return !!(msgContent(m)||m._statusCard||m.attachments?.length||(m.role==='assistant'&&(hasTc||hasTu||(typeof _messageHasReasoningPayload==='function'&&_messageHasReasoningPayload(m)))));
    }).length;
    _messageRenderWindowSize=_currentMessageRenderWindowSize()+Math.max(addedRenderable, MESSAGE_RENDER_WINDOW_DEFAULT);
    _messagesTruncated = !!data.session._messages_truncated;
    _oldestIdx = data.session._messages_offset || 0;
    renderMessages({ preserveScroll: true });
    if (container) {
      // Prepending older messages must not teleport the reader. Preserve the
      // currently visible viewport by adding the inserted height to scrollTop.
      const oldTop = container.scrollTop;
      const newScrollH = container.scrollHeight;
      const addedHeight = Math.max(0, newScrollH - prevScrollH);
      _programmaticScroll = true;
      container.scrollTop = oldTop + addedHeight;
      requestAnimationFrame(()=>{ _programmaticScroll = false; });
    }
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
//
// Race-safety (#1937): with the endless-scroll opt-in, _loadOlderMessages
// may be in flight when this runs (e.g. user scrolled near the top, then
// hit the Start jump pill). Two coordinated guards prevent the prefetch
// from prepending duplicate messages onto our wholesale replacement:
//   1. Hold the _loadingOlder mutex around the body so a NEW prefetch
//      cannot start mid-replace (entry-gate check at line ~1003 returns
//      early). The mutex is also self-protecting against concurrent
//      ensure-all calls from rapid double-clicks on Start.
//   2. Bump _messagesGeneration before mutating S.messages so any
//      in-flight prefetch's post-await generation check bails out.
async function _ensureAllMessagesLoaded() {
  if (!_messagesTruncated || !S.session) return;
  if (_loadingOlder) {
    // A prefetch is mid-flight (between the `_loadingOlder = true` line
    // and its post-await guards). Bumping the generation token now
    // poisons that prefetch's continuation, but we still need to claim
    // the mutex AFTER it releases. Yield until the prefetch finishes
    // (its finally-block clears _loadingOlder) before fetching the full
    // history ourselves. The generation bump below ensures any other
    // future race against this same continuation also fails closed.
    _bumpMessagesGeneration();
    while (_loadingOlder) {
      await new Promise(resolve => setTimeout(resolve, 16));
    }
    if (!_messagesTruncated || !S.session) return;
  }
  _loadingOlder = true;
  try {
    const sid = S.session.session_id;
    const data = await api(`/api/session?session_id=${encodeURIComponent(sid)}&messages=1&resolve_model=0`);
    // Guard: api() may have redirected (401) and returned undefined.
    if (!data || !data.session) return;
    // Session may have been switched while we awaited. Bail rather than
    // overwrite the new session's messages.
    if (!S.session || S.session.session_id !== sid) return;
    if (_loadingSessionId !== null && _loadingSessionId !== sid) return;
    const msgs = (data.session.messages || []).filter(m => m && m.role);
    // Bump the generation BEFORE the wholesale replace so any racing
    // prefetch (whose snapshot was taken before this call's mutex
    // acquisition) sees the new value and aborts.
    _bumpMessagesGeneration();
    S.messages = msgs;
    _messagesTruncated = false;
    _oldestIdx = 0;
    if (S.session && S.session.session_id === sid) {
      S.session.message_count = Number(data.session.message_count || msgs.length);
    }
  } finally {
    _loadingOlder = false;
  }
}

let _allSessions = [];  // cached for search filter
let _renamingSid = null;  // session_id currently being renamed (blocks list re-renders)
let _showArchived = false;  // toggle to show archived sessions
let _sessionSelectMode = false;  // batch select mode
const _selectedSessions = new Set();  // selected session IDs
let _allProjects = [];  // cached project list
// Sentinel value for the _activeProject state when filtering to sessions
// that have no project_id assigned. Distinct from real project IDs so the
// equality check below can branch cleanly on it. The literal string is
// not user-visible (the chip renders the localized label) — it just has
// to be something a user-created project_id can never collide with, which
// double-underscore prefixes provide.
const NO_PROJECT_FILTER = '__none__';
let _activeProject = null;  // project_id filter (null = show all, NO_PROJECT_FILTER = unassigned only)
let _showAllProfiles = false;  // false = filter to active profile only
let _otherProfileCount = 0;       // count of sessions from other profiles (server-reported)
let _sessionActionMenu = null;
let _sessionActionAnchor = null;
let _sessionActionSessionId = null;
const _expandedChildSessionKeys = new Set();
const _expandedLineageKeys = new Set();
let _sessionVisibleSidebarIds = [];
const SESSION_VIRTUAL_ROW_HEIGHT = 52;
const SESSION_VIRTUAL_BUFFER_ROWS = 12;
const SESSION_VIRTUAL_THRESHOLD_ROWS = 80;
let _sessionVirtualScrollList = null;
let _sessionVirtualScrollRaf = 0;

function _sessionIdFromLocation(){
  if(typeof window==='undefined'||!window.location) return null;
  const marker='/session/';
  const path=window.location.pathname||'';
  const idx=path.indexOf(marker);
  if(idx>=0){
    const raw=path.slice(idx+marker.length).split('/')[0];
    if(raw){try{return decodeURIComponent(raw);}catch(_e){return raw;}}
  }
  try{
    const qs=new URLSearchParams(window.location.search||'');
    return qs.get('session')||null;
  }catch(_e){return null;}
}
function _sessionUrlForSid(sid){
  const encoded=encodeURIComponent(sid);
  let base;
  try{base=new URL(`session/${encoded}`, document.baseURI||window.location.origin+'/');}
  catch(_e){base=new URL(`/session/${encoded}`, window.location.origin);}
  try{
    const current=new URL(window.location.href);
    current.searchParams.delete('session');
    base.search=current.searchParams.toString();
    base.hash=current.hash;
  }catch(_e){}
  return base.pathname+base.search+base.hash;
}
function _setActiveSessionUrl(sid){
  if(typeof window==='undefined'||!window.history||!sid) return;
  const next=_sessionUrlForSid(sid);
  if(next && next!==(window.location.pathname+window.location.search+window.location.hash)){
    window.history.pushState({session_id:sid},'',next);
  }
}

// ── Batch select mode ──
function toggleSessionSelectMode(){
  _sessionSelectMode=!_sessionSelectMode;
  _selectedSessions.clear();
  renderSessionListFromCache();
}
function exitSessionSelectMode(){
  _sessionSelectMode=false;
  _selectedSessions.clear();
  const bar=$('batchActionBar');
  if(bar) bar.style.display='none';
  renderSessionListFromCache();
}
function toggleSessionSelect(sid){
  if(_selectedSessions.has(sid)) _selectedSessions.delete(sid);
  else _selectedSessions.add(sid);
  _updateBatchActionBar();
  const cb=document.querySelector('.session-select-cb[data-sid="'+sid+'"]');
  const item=cb?cb.closest('.session-item'):null;
  if(item){item.classList.toggle('selected',_selectedSessions.has(sid));if(cb)cb.checked=_selectedSessions.has(sid);}
}
function setSessionSelected(sid, selected){
  if(selected) _selectedSessions.add(sid);
  else _selectedSessions.delete(sid);
  _updateBatchActionBar();
  const cb=document.querySelector('.session-select-cb[data-sid="'+sid+'"]');
  const item=cb?cb.closest('.session-item'):null;
  if(item){item.classList.toggle('selected',_selectedSessions.has(sid));if(cb)cb.checked=_selectedSessions.has(sid);}
}
function selectAllSessions(){
  _selectedSessions.clear();
  const ids=Array.isArray(_sessionVisibleSidebarIds)&&_sessionVisibleSidebarIds.length
    ? _sessionVisibleSidebarIds
    : Array.from(document.querySelectorAll('.session-select-cb')).map(cb=>cb.dataset.sid).filter(Boolean);
  ids.forEach(sid=>_selectedSessions.add(sid));
  document.querySelectorAll('.session-select-cb').forEach(cb=>{
    const sid=cb.dataset.sid;
    if(sid){cb.checked=_selectedSessions.has(sid);const item=cb.closest('.session-item');if(item)item.classList.toggle('selected',_selectedSessions.has(sid));}
  });
  _updateBatchActionBar();
}
function deselectAllSessions(){
  _selectedSessions.clear();
  document.querySelectorAll('.session-select-cb').forEach(cb=>{cb.checked=false;const item=cb.closest('.session-item');if(item)item.classList.remove('selected');});
  _updateBatchActionBar();
}
function _updateBatchActionBar(){
  const bar=$('batchActionBar');if(!bar)return;
  const count=_selectedSessions.size;
  if(count>0){_renderBatchActionBar();}
  else{bar.style.display='none';}
}
function _renderBatchActionBar(){
  const bar=$('batchActionBar');if(!bar)return;
  bar.innerHTML='';bar.style.display=_selectedSessions.size>0?'flex':'none';
  const countBadge=document.createElement('span');countBadge.className='batch-count';
  countBadge.textContent=t('session_selected_count',_selectedSessions.size);bar.appendChild(countBadge);
  // Archive
  const archiveBtn=document.createElement('button');archiveBtn.className='batch-action-btn';
  archiveBtn.textContent=t('session_batch_archive');
  archiveBtn.onclick=async()=>{
    const ids=[..._selectedSessions];
    const ok=await showConfirmDialog({message:t('session_batch_archive_confirm',ids.length),confirmLabel:t('session_batch_archive'),danger:true});
    if(!ok)return;
    try{await Promise.all(ids.map(sid=>api('/api/session/archive',{method:'POST',body:JSON.stringify({session_id:sid,archived:true})})));
      showToast(t('session_archived'));exitSessionSelectMode();await renderSessionList();
    }catch(e){showToast('Archive failed: '+(e.message||e));}
  };bar.appendChild(archiveBtn);
  // Move
  const moveBtn=document.createElement('button');moveBtn.className='batch-action-btn';
  moveBtn.textContent=t('session_batch_move');
  moveBtn.onclick=(e)=>{e.stopPropagation();_showBatchProjectPicker();};bar.appendChild(moveBtn);
  // Delete
  const deleteBtn=document.createElement('button');deleteBtn.className='batch-action-btn batch-action-btn-danger';
  deleteBtn.textContent=t('session_batch_delete');
  deleteBtn.onclick=async()=>{
    const ids=[..._selectedSessions];
    const ok=await showConfirmDialog({message:t('session_batch_delete_confirm',ids.length),confirmLabel:t('delete_title'),danger:true});
    if(!ok)return;
    try{
      await Promise.all(ids.map(sid=>api('/api/session/delete',{method:'POST',body:JSON.stringify({session_id:sid})})));
      ids.forEach(_clearHandoffStorageForSession);
      if(S.session&&ids.includes(S.session.session_id)){
        S.session=null;S.messages=[];S.entries=[];localStorage.removeItem('hermes-webui-session');
        const remaining=await api('/api/sessions');
        if(remaining.sessions&&remaining.sessions.length){await loadSession(remaining.sessions[0].session_id);}
        else{$('msgInner').innerHTML='';$('emptyState').style.display='';}
      }
      showToast(t('session_delete')+' ('+ids.length+')');exitSessionSelectMode();await renderSessionList();
    }catch(e){showToast('Delete failed: '+(e.message||e));}
  };bar.appendChild(deleteBtn);
}
function _showBatchProjectPicker(){
  const ids=[..._selectedSessions];if(!ids.length)return;
  const bar=$('batchActionBar');if(!bar)return;
  bar.querySelectorAll('.batch-project-picker').forEach(p=>p.remove());
  const picker=document.createElement('div');picker.className='project-picker batch-project-picker';
  const none=document.createElement('div');none.className='project-picker-item';none.textContent='No project';
  none.onclick=async()=>{picker.remove();
    try{await Promise.all(ids.map(sid=>api('/api/session/move',{method:'POST',body:JSON.stringify({session_id:sid,project_id:null})})));
      showToast('Removed from project');exitSessionSelectMode();await renderSessionList();
    }catch(e){showToast('Move failed: '+(e.message||e));}
  };picker.appendChild(none);
  for(const p of(_allProjects||[])){
    const item=document.createElement('div');item.className='project-picker-item';
    if(p.color){const dot=document.createElement('span');dot.className='color-dot';
      dot.style.cssText='width:6px;height:6px;border-radius:50%;background:'+p.color+';flex-shrink:0;';item.appendChild(dot);}
    const name=document.createElement('span');name.textContent=p.name;item.appendChild(name);
    item.onclick=async()=>{picker.remove();
      try{await Promise.all(ids.map(sid=>api('/api/session/move',{method:'POST',body:JSON.stringify({session_id:sid,project_id:p.project_id})})));
        showToast('Moved to '+p.name);exitSessionSelectMode();await renderSessionList();
      }catch(e){showToast('Move failed: '+(e.message||e));}
    };picker.appendChild(item);
  }
  bar.appendChild(picker);
  const close=(e)=>{if(!picker.contains(e.target)){picker.remove();document.removeEventListener('click',close);}};
  setTimeout(()=>document.addEventListener('click',close),0);
}

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

function _appendSessionDuplicateAction(menu, session){
  menu.appendChild(_buildSessionAction(
    t('session_duplicate'),
    t('session_duplicate_desc'),
    ICONS.dup,
    async()=>{
      closeSessionActionMenu();
      try{
        const res=await api('/api/session/duplicate',{method:'POST',body:JSON.stringify({session_id:session.session_id})});
        if(res.session){
          await loadSession(res.session.session_id);
          await renderSessionList();
          showToast(t('session_duplicated'));
        }
      }catch(err){showToast(t('session_duplicate_failed')+err.message);}
    }
  ));
}

function _openSessionActionMenu(session, anchorEl){
  if(_isReadOnlySession(session)){ if(typeof showToast==='function') showToast('Read-only imported sessions cannot be modified.',3000); return; }
  if(_sessionActionMenu && _sessionActionSessionId===session.session_id && _sessionActionAnchor===anchorEl){
    closeSessionActionMenu();
    return;
  }
  closeSessionActionMenu();
  const isMessagingSession = _isMessagingSession(session);
  const isCliSession = _isCliSession(session);
  const isExternalSession = isMessagingSession || isCliSession;
  const menu=document.createElement('div');
  menu.className='session-action-menu open';
  // Rename — first menu item by request (#1764). Double-click rename is
  // timing-sensitive: the first click frequently registers as "open the
  // chat" before the second click arrives, so users open the conversation
  // when they meant to rename it. Putting Rename in the menu eliminates
  // the timing entirely. Only shown for sessions that support rename
  // (read-only imported sessions skip it; same gate as startRename's
  // _isReadOnlySession check).
  if(!_isReadOnlySession(session)){
    menu.appendChild(_buildSessionAction(
      t('session_rename'),
      t('session_rename_desc'),
      ICONS.edit,
      ()=>{
        closeSessionActionMenu();
        // Find the row for this session and call its attached startRename.
        // Falls back to a no-op toast if the row isn't currently rendered
        // (e.g. archived-and-hidden) — extremely rare since the menu only
        // opens from a visible row's three-dot button.
        const row=document.querySelector('.session-item[data-sid="'+session.session_id+'"]');
        if(row && typeof row._startRename === 'function'){
          row._startRename();
        } else if(typeof showToast==='function'){
          showToast(t('session_rename_failed_no_row')||'Could not start rename — row not found.', 3000, 'error');
        }
      }
    ));
  }
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
  if(!isExternalSession){
    _appendSessionDuplicateAction(menu, session);
  }
  if(session.active_stream_id){
    menu.appendChild(_buildSessionAction(
      t('session_stop_response'),
      t('session_stop_response_desc'),
      ICONS.stop,
      async()=>{
        closeSessionActionMenu();
        await cancelSessionStream(session);
        showToast(t('stream_stopped'));
      }
    ));
  }
  if(!isExternalSession){
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
  }
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

// Generation counter to discard stale API responses (issue #1430).
// Multiple callers (message send, rename, session switch) fire renderSessionList()
// concurrently. Without this guard, a slower older response can overwrite _allSessions
// with stale data, causing sessions to vanish from the sidebar.
let _renderSessionListGen = 0;

function _isOptimisticFirstTurnSessionRow(s){
  if(!s||!s.session_id||s.archived) return false;
  const messageCount=Number(s.message_count||0);
  if(messageCount<=0&&!s.pending_user_message) return false;
  return Boolean(
    s.is_streaming||
    s.active_stream_id||
    s.pending_user_message||
    s.pending_started_at||
    _isSessionLocallyStreaming(s)||
    _sessionStreamingById.get(s.session_id)===true
  );
}

function _mergeOptimisticFirstTurnSessions(fetchedSessions){
  const merged=Array.isArray(fetchedSessions)?[...fetchedSessions]:[];
  const bySid=new Map();
  merged.forEach((s,idx)=>{if(s&&s.session_id) bySid.set(s.session_id,idx);});
  for(const local of Array.isArray(_allSessions)?_allSessions:[]){
    if(!_isOptimisticFirstTurnSessionRow(local)) continue;
    const sid=local.session_id;
    const idx=bySid.has(sid)?bySid.get(sid):-1;
    if(idx>=0){
      const fetched=merged[idx]||{};
      const localCount=Number(local.message_count||0);
      const fetchedCount=Number(fetched.message_count||0);
      const localTs=Number(local.last_message_at||local.updated_at||0);
      const fetchedTs=Number(fetched.last_message_at||fetched.updated_at||0);
      merged[idx]={
        ...local,
        ...fetched,
        message_count:Math.max(localCount,fetchedCount),
        last_message_at:Math.max(localTs,fetchedTs),
        updated_at:Math.max(Number(local.updated_at||0),Number(fetched.updated_at||0),localTs,fetchedTs),
        active_stream_id:fetched.active_stream_id||local.active_stream_id||null,
        pending_user_message:fetched.pending_user_message||local.pending_user_message||null,
        pending_started_at:fetched.pending_started_at||local.pending_started_at||null,
        is_streaming:Boolean(fetched.is_streaming||local.is_streaming||_isSessionLocallyStreaming(local)),
      };
    }else{
      merged.push({...local,is_streaming:true});
      bySid.set(sid,merged.length-1);
    }
  }
  return merged;
}

async function renderSessionList(){
  const _gen = ++_renderSessionListGen;
  try{
    if(!($('sessionSearch').value||'').trim()) _contentSearchResults = [];
    const allProfilesQS = _showAllProfiles ? '?all_profiles=1' : '';
    const [sessData, projData] = await Promise.all([
      api('/api/sessions' + allProfilesQS),
      api('/api/projects' + allProfilesQS),
    ]);
    // Discard stale response — a newer renderSessionList() call superseded us.
    if (_gen !== _renderSessionListGen) return;
    // Server's other_profile_count tells us how many sessions exist outside the
    // active profile so the "Show N from other profiles" toggle can render
    // without a second round-trip. Stashed on the module for renderSessionListFromCache.
    _otherProfileCount = sessData.other_profile_count || 0;
    _allSessions = _mergeOptimisticFirstTurnSessions(sessData.sessions||[]);
    _allProjects = projData.projects||[];
    // Capture server clock for clock-skew compensation (issue #1144).
    // server_time is epoch seconds from the server's time.time().
    // _serverTimeDelta = client - server, so (Date.now() - _serverTimeDelta)
    // gives an approximation of the current server time.
    if (typeof sessData.server_time === 'number' && sessData.server_time > 0) {
      _serverTimeDelta = Date.now() - (sessData.server_time * 1000);
    }
    if (typeof sessData.server_tz === 'string') {
      _serverTz = sessData.server_tz;
    }
    _markPollingCompletionUnreadTransitions(_allSessions);
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
    const resp = await fetch(new URL('api/sessions/gateway/stream?probe=1', document.baseURI || location.href).href, { credentials:'same-origin' });
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
                    const next = res.session.messages.filter(m => m && m.role);
                    if (next.length < prev) return;
                    if (prev > 0 && !_isCliImportRefreshPrefixMatch(S.messages, next)) return;
                    S.messages = next;
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
      if(_gatewaySSE){
        _gatewaySSE.close();
        _gatewaySSE = null;
      }
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
let _serverTimeDelta = 0;       // ms offset: client clock - server clock (for clock-skew compensation)
let _serverTz = '';              // server timezone offset string (e.g. "+0800", "+0000", "-0500")

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

function _serverNowMs() {
  // Compensate for clock skew between client and server (issue #1144).
  // Returns an approximation of the current server time in ms.
  return Date.now() - _serverTimeDelta;
}

function _serverTzOptions() {
  // Build a timeZone option from _serverTz (e.g. "+0800" → "Etc/GMT-8").
  // Falls back to undefined (uses browser timezone) when:
  //   - _serverTz is not set or is UTC (no offset to apply)
  //   - _serverTz is malformed
  //   - _serverTz has a fractional-hour component (India +0530, Iran +0330,
  //     Newfoundland -0330, Nepal +0545, etc.) — IANA Etc/GMT zones cannot
  //     express half/quarter-hour offsets; use _formatInServerTz() instead
  //     for correct fractional-offset formatting.
  if (!_serverTz || _serverTz === '+0000' || _serverTz === '-0000') return undefined;
  const m = _serverTz.match(/^([+-])(\d{2})(\d{2})$/);
  if (!m) return undefined;
  if (m[3] !== '00') return undefined;  // fractional offset — caller must use _formatInServerTz
  // IANA Etc/GMT uses inverted sign: UTC+8 → "Etc/GMT-8"
  const sign = m[1] === '+' ? '-' : '+';
  return { timeZone: `Etc/GMT${sign}${parseInt(m[2])}` };
}

function _formatInServerTz(date, options) {
  // Format `date` in the server's wall-clock timezone, including correct
  // handling of fractional-hour offsets that Etc/GMT cannot express.
  //
  // Strategy: shift the timestamp by the server's offset, then format with
  // timeZone:'UTC' so no further conversion is applied — the formatted
  // output reads as the wall-clock time in the server's timezone.
  //
  // Falls back to plain `date.toLocaleString(undefined, options)` (browser
  // timezone) when _serverTz is absent, UTC, or malformed.
  if (!_serverTz || _serverTz === '+0000' || _serverTz === '-0000') {
    return date.toLocaleString(undefined, options);
  }
  const m = _serverTz.match(/^([+-])(\d{2})(\d{2})$/);
  if (!m) return date.toLocaleString(undefined, options);
  const sign = m[1] === '+' ? 1 : -1;
  const offsetMin = sign * (parseInt(m[2]) * 60 + parseInt(m[3]));
  const adjusted = new Date(date.getTime() + offsetMin * 60 * 1000);
  return adjusted.toLocaleString(undefined, { ...options, timeZone: 'UTC' });
}

function _localDayOrdinal(timestampMs) {
  const date = new Date(timestampMs);
  return Math.floor(Date.UTC(date.getFullYear(), date.getMonth(), date.getDate()) / 86400000);
}

function _sessionCalendarBoundaries(nowMs) {
  nowMs = nowMs || _serverNowMs();
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

function _formatSessionDate(timestampMs, nowMs) {
  nowMs = nowMs || _serverNowMs();
  const date = new Date(timestampMs);
  const now = new Date(nowMs);
  const options = {month:'short', day:'numeric'};
  if (date.getFullYear() !== now.getFullYear()) options.year = 'numeric';
  return date.toLocaleDateString(undefined, options);
}

function _formatRelativeSessionTime(timestampMs, nowMs) {
  if (!timestampMs) return t('session_time_unknown');
  nowMs = nowMs || _serverNowMs();
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

function _sessionTimeBucketLabel(timestampMs, nowMs) {
  if (!timestampMs) return t('session_time_bucket_older');
  nowMs = nowMs || _serverNowMs();
  const {startOfToday, startOfYesterday, startOfWeek, startOfLastWeek} = _sessionCalendarBoundaries(nowMs);
  if (timestampMs >= startOfToday) return t('session_time_bucket_today');
  if (timestampMs >= startOfYesterday) return t('session_time_bucket_yesterday');
  if (timestampMs >= startOfWeek) return t('session_time_bucket_this_week');
  if (timestampMs >= startOfLastWeek) return t('session_time_bucket_last_week');
  return t('session_time_bucket_older');
}

function _isChildSession(s){
  return !!(s&&s.parent_session_id&&s.relationship_type==='child_session');
}

function _sessionLineageKey(s, sessionIdsInList){
  if(!s||!s.session_id) return null;
  if(_isChildSession(s)) return null;
  const lineageKey=s._lineage_root_id||s.lineage_root_id||null;
  if(lineageKey) return lineageKey;
  // If parent_session_id points to another session in the current list,
  // this is a subagent/fork child without compression metadata — don't
  // collapse it into lineage (#494). Compression continuations carry an
  // explicit lineage root, even when stale optimistic rows leave parent
  // segments in the browser cache during active compression.
  if(s.parent_session_id && sessionIdsInList && sessionIdsInList.has(s.parent_session_id)){
    return null;
  }
  return s.parent_session_id || null;
}

function _sessionLineageContainsSession(s, sid){
  if(!s||!sid) return false;
  if(s.session_id===sid) return true;
  if(Array.isArray(s._lineage_segments)&&s._lineage_segments.some(seg=>seg&&seg.session_id===sid)) return true;
  if(Array.isArray(s._child_sessions)&&s._child_sessions.some(child=>child&&child.session_id===sid)) return true;
  return false;
}

function _sessionSegmentCount(s){
  if(!s) return 0;
  const counts=[];
  if(typeof s._lineage_collapsed_count==='number') counts.push(s._lineage_collapsed_count);
  if(typeof s._compression_segment_count==='number') counts.push(s._compression_segment_count);
  if(Array.isArray(s._lineage_segments)) counts.push(s._lineage_segments.length);
  const count=Math.max(0,...counts.map(n=>Number.isFinite(n)?n:0));
  return count>1?count:0;
}

function _sidebarLineageKeyForRow(s){
  if(!s) return null;
  return s._lineage_key||s._lineage_root_id||s.lineage_root_id||s.parent_session_id||s.session_id||null;
}

function _truncatedSessionId(sid){
  sid=String(sid||'').trim();
  if(!sid) return '';
  if(sid.length<=16) return sid;
  return sid.slice(0,12)+'...';
}

function _sessionTitleForForkParent(parentSid){
  if(!parentSid||!Array.isArray(_allSessions)) return '';
  const parent=_allSessions.find(item=>item&&item.session_id===parentSid);
  const title=parent&&String(parent.title||'').trim();
  if(!title||title==='Untitled') return '';
  return title;
}

function _attachChildSessionsToSidebarRows(collapsedRows, rawSessions){
  const rows=(collapsedRows||[]).filter(s=>!_isChildSession(s)).map(s=>({...s}));
  const visibleBySid=new Map();
  const visibleBySegmentSid=new Map();
  const visibleByLineageKey=new Map();
  for(const row of rows){
    if(row&&row.session_id) visibleBySid.set(row.session_id,row);
    const lineageKey=_sidebarLineageKeyForRow(row);
    if(lineageKey&&!visibleByLineageKey.has(lineageKey)) visibleByLineageKey.set(lineageKey,row);
    for(const seg of (Array.isArray(row._lineage_segments)?row._lineage_segments:[])){
      if(seg&&seg.session_id) visibleBySegmentSid.set(seg.session_id,{row,seg});
    }
  }
  const orphans=[];
  for(const child of rawSessions||[]){
    if(!_isChildSession(child)) continue;
    if(child._cross_surface_child_session){
      orphans.push({...child,_orphan_child_session:true});
      continue;
    }
    const parentSid=child.parent_session_id;
    let parentRow=visibleBySid.get(parentSid);
    let parentSegment=null;
    if(!parentRow&&visibleBySegmentSid.has(parentSid)){
      const resolved=visibleBySegmentSid.get(parentSid);
      parentRow=resolved.row;
      parentSegment=resolved.seg;
    }
    if(!parentRow&&child._parent_lineage_root_id){
      parentRow=visibleByLineageKey.get(child._parent_lineage_root_id)||null;
    }
    if(parentRow){
      if(!Array.isArray(parentRow._child_sessions)) parentRow._child_sessions=[];
      const childCopy={...child};
      if(parentSegment){
        childCopy._parent_segment_id=parentSegment.session_id;
        childCopy._parent_segment_title=parentSegment.title||child.parent_title||'Untitled';
      }
      parentRow._child_sessions.push(childCopy);
      parentRow._child_session_count=parentRow._child_sessions.length;
    } else {
      orphans.push({...child,_orphan_child_session:true});
    }
  }
  return [...rows,...orphans];
}

function _syncSidebarExpansionForActiveSession(rows, activeSid){
  if(!activeSid) return;
  for(const row of rows||[]){
    const key=_sidebarLineageKeyForRow(row);
    if(!key) continue;
    if(Array.isArray(row._child_sessions)&&row._child_sessions.some(child=>child&&child.session_id===activeSid)){
      _expandedChildSessionKeys.add(key);
    }
    if(Array.isArray(row._lineage_segments)&&row._lineage_segments.some(seg=>seg&&seg.session_id===activeSid&&seg.session_id!==row.session_id)){
      _expandedLineageKeys.add(key);
    }
  }
}

function _collapseSessionLineageForSidebar(sessions){
  const result=[];
  const sessionIdsInList=new Set((sessions||[]).map(s=>s.session_id));
  const groups=new Map();
  for(const s of sessions||[]){
    const key=_sessionLineageKey(s, sessionIdsInList);
    if(!key){result.push(s);continue;}
    if(!groups.has(key)) groups.set(key,[]);
    groups.get(key).push(s);
  }
  for(const [key,items] of groups.entries()){
    if(items.length<=1){result.push(items[0]);continue;}
    const sorted=[...items].sort((a,b)=>{
      const bSeg=Number(b&&b._compression_segment_count||0);
      const aSeg=Number(a&&a._compression_segment_count||0);
      if(bSeg||aSeg){
        if(bSeg!==aSeg) return bSeg-aSeg;
      }
      return _sessionTimestampMs(b)-_sessionTimestampMs(a);
    });
    const chosen=sorted[0];
    result.push({...chosen,_lineage_key:key,_lineage_collapsed_count:items.length,_lineage_segments:sorted});
  }
  return result;
}

function _activeSessionIdForSidebar(){
  if(S.session&&S.session.session_id) return S.session.session_id;
  if(typeof _sessionIdFromLocation==='function') return _sessionIdFromLocation();
  return null;
}

function upsertActiveSessionForLocalTurn({title='', messageCount=0, timestampMs=Date.now()}={}){
  if(!S.session||!S.session.session_id) return;
  const sid=S.session.session_id;
  const nowSec=Math.floor((Number(timestampMs)||Date.now())/1000);
  const localCount=Array.isArray(S.messages)?S.messages.length:0;
  const count=Math.max(Number(S.session.message_count||0),Number(messageCount||0),localCount,1);
  S.session.message_count=count;
  S.session.last_message_at=nowSec;
  S.session.updated_at=nowSec;
  if((S.session.title==='Untitled'||!S.session.title)&&title){
    S.session.title=title;
  }
  const existingIdx=_allSessions.findIndex(s=>s&&s.session_id===sid);
  const row={
    ...S.session,
    session_id:sid,
    title:S.session.title||title||'New chat',
    message_count:count,
    last_message_at:nowSec,
    updated_at:nowSec,
    profile:S.session.profile||S.activeProfile||'default',
    is_streaming:true,
  };
  if(existingIdx>=0) _allSessions[existingIdx]={..._allSessions[existingIdx],...row};
  else _allSessions.unshift(row);
  renderSessionListFromCache();
}

function clearOptimisticSessionStreaming(sid){
  sid=sid||(S.session&&S.session.session_id)||'';
  if(!sid) return;
  if(S.session&&S.session.session_id===sid){
    S.session.active_stream_id=null;
    S.activeStreamId=null;
  }
  if(Array.isArray(_allSessions)){
    const idx=_allSessions.findIndex(s=>s&&s.session_id===sid);
    if(idx>=0){
      _allSessions[idx]={
        ..._allSessions[idx],
        active_stream_id:null,
        pending_user_message:null,
        pending_started_at:null,
        is_streaming:false,
      };
    }
  }
  if(typeof _sessionStreamingById!=='undefined'&&_sessionStreamingById&&typeof _sessionStreamingById.set==='function'){
    _sessionStreamingById.set(sid,false);
  }
  if(typeof _forgetObservedStreamingSession==='function') _forgetObservedStreamingSession(sid);
  renderSessionListFromCache();
}


function _sessionVirtualWindow(opts){
  const total=Math.max(0, Number(opts&&opts.total)||0);
  const threshold=Math.max(1, Number(opts&&opts.threshold)||SESSION_VIRTUAL_THRESHOLD_ROWS);
  const itemHeight=Math.max(1, Number(opts&&opts.itemHeight)||SESSION_VIRTUAL_ROW_HEIGHT);
  const buffer=Math.max(0, Number(opts&&opts.buffer)||SESSION_VIRTUAL_BUFFER_ROWS);
  const viewportHeight=Math.max(itemHeight, Number(opts&&opts.viewportHeight)||itemHeight*10);
  const visibleRows=Math.max(1, Math.ceil(viewportHeight/itemHeight));
  if(total<=threshold){
    return {virtualized:false,start:0,end:total,topPad:0,bottomPad:0,itemHeight,total};
  }
  let start=Math.floor((Number(opts&&opts.scrollTop)||0)/itemHeight)-buffer;
  start=Math.max(0, Math.min(start, Math.max(0,total-visibleRows)));
  let end=Math.min(total, start+visibleRows+(buffer*2));
  const activeIndex=Number.isFinite(Number(opts&&opts.activeIndex))?Number(opts.activeIndex):-1;
  if(activeIndex>=0&&activeIndex<total&&(activeIndex<start||activeIndex>=end)){
    start=Math.max(0, Math.min(activeIndex-buffer, Math.max(0,total-visibleRows-(buffer*2))));
    end=Math.min(total, start+visibleRows+(buffer*2));
  }
  return {
    virtualized:true,
    start,
    end,
    topPad:start*itemHeight,
    bottomPad:Math.max(0,(total-end)*itemHeight),
    itemHeight,
    total,
  };
}

function _sessionVirtualSpacer(height, where){
  const spacer=document.createElement('div');
  spacer.className='session-virtual-spacer';
  spacer.dataset.virtualSpacer=where||'gap';
  spacer.setAttribute('aria-hidden','true');
  spacer.style.height=Math.max(0,Math.round(height||0))+'px';
  spacer.style.flex='0 0 auto';
  return spacer;
}

function _scheduleSessionVirtualizedRender(){
  if(_renamingSid||_sessionVirtualScrollRaf) return;
  // Skip the re-render if the list is below the virtualization threshold —
  // there's no virtual window to recompute, and re-rendering would just
  // rebuild the whole DOM on every scroll tick. Without this guard, the
  // unconditional scroll listener (attached for any list) caused
  // user-facing scroll jumps on small lists. (#1669 follow-up)
  const list=_sessionVirtualScrollList;
  if(list){
    const total=Number(list.dataset.sessionVirtualTotal||0);
    if(total>0&&total<=SESSION_VIRTUAL_THRESHOLD_ROWS) return;
  }
  _sessionVirtualScrollRaf=requestAnimationFrame(()=>{_sessionVirtualScrollRaf=0;renderSessionListFromCache();});
}

function _ensureSessionVirtualScrollHandler(list){
  if(!list||_sessionVirtualScrollList===list) return;
  if(_sessionVirtualScrollList){
    _sessionVirtualScrollList.removeEventListener('scroll', _scheduleSessionVirtualizedRender);
  }
  _sessionVirtualScrollList=list;
  list.addEventListener('scroll', _scheduleSessionVirtualizedRender, {passive:true});
}

function renderSessionListFromCache(){
  // Don't re-render while user is actively renaming a session (would destroy the input)
  if(_renamingSid) return;
  closeSessionActionMenu();
  const q=($('sessionSearch').value||'').toLowerCase();
  const activeSidForSidebar=_activeSessionIdForSidebar();
  const titleMatches=q?_allSessions.filter(s=>(s.title||'Untitled').toLowerCase().includes(q)):_allSessions;
  // Merge content matches (deduped): content matches appended after title matches
  const titleIds=new Set(titleMatches.map(s=>s.session_id));
  const allMatched=q?[...titleMatches,..._contentSearchResults.filter(s=>!titleIds.has(s.session_id))]:titleMatches;
  // Never surface ephemeral 0-message sessions in the sidebar — they only become
  // real once the first message is sent. The server already filters them, but this
  // guard ensures a brand-new active session doesn't flash into the list while
  // _allSessions is stale from a prior render (#1171).
  const withMessages=allMatched.filter(s=>
    (s.message_count||0)>0 ||
    _isSessionEffectivelyStreaming(s) ||
    !!s.active_stream_id ||
    !!s.pending_user_message ||
    (activeSidForSidebar&&s.session_id===activeSidForSidebar) ||
    (S.session&&s.session_id===S.session.session_id&&(S.session.message_count||0)>0)
  );
  // The server is authoritative for profile scoping (#1611): it filters by
  // active profile when no query param is set, and returns the aggregate when
  // we send ?all_profiles=1. The renamed-root cross-alias (a row tagged
  // 'default' matching active 'kinni' when kinni.is_default) lives server-side
  // in _profiles_match, and a strict-equality client filter would reject those
  // rows incorrectly. So we trust the wire data and skip the redundant client
  // filter entirely.
  const profileFiltered=withMessages;
  // Filter by active project. NO_PROJECT_FILTER sentinel asks for sessions
  // with no project_id; otherwise filter to the matching project_id, or
  // pass through when no filter is active.
  const projectFiltered=
    _activeProject===NO_PROJECT_FILTER
      ?profileFiltered.filter(s=>!s.project_id)
      :(_activeProject?profileFiltered.filter(s=>s.project_id===_activeProject):profileFiltered);
  // Filter archived unless toggle is on
  const sessionsRaw=_showArchived?projectFiltered:projectFiltered.filter(s=>!s.archived);
  const sessions=_attachChildSessionsToSidebarRows(_collapseSessionLineageForSidebar(sessionsRaw), sessionsRaw);
  _syncSidebarExpansionForActiveSession(sessions, activeSidForSidebar);
  const archivedCount=projectFiltered.filter(s=>s.archived).length;
  const list=$('sessionList');
  const listScrollTopBeforeRender=list.scrollTop||0;
  list.innerHTML='';
  // Batch select bar (when in select mode)
  if(_sessionSelectMode){
    const selectBar=document.createElement('div');selectBar.className='session-select-bar';
    const exitBtn=document.createElement('button');exitBtn.className='batch-exit-btn';
    exitBtn.textContent='\u2715';exitBtn.title='Exit select mode';
    exitBtn.onclick=(e)=>{e.stopPropagation();exitSessionSelectMode();};
    selectBar.appendChild(exitBtn);
    const selectAllBtn=document.createElement('button');selectAllBtn.className='batch-select-all-btn';
    selectAllBtn.textContent=t('session_select_all');
    selectAllBtn.onclick=(e)=>{e.stopPropagation();selectAllSessions();};
    selectBar.appendChild(selectAllBtn);
    list.appendChild(selectBar);
  }
  // Ensure batch action bar exists in DOM
  let batchBar=$('batchActionBar');
  if(!batchBar){batchBar=document.createElement('div');batchBar.id='batchActionBar';batchBar.className='batch-action-bar';}
  list.appendChild(batchBar);
  if(_sessionSelectMode&&_selectedSessions.size>0){batchBar.style.display='flex';_renderBatchActionBar();}
  else{batchBar.style.display='none';}
  // Project filter bar — show when there are real projects OR there are
  // unassigned sessions (so the Unassigned chip has something to filter to).
  const hasUnprojected=profileFiltered.some(s=>!s.project_id);
  if(_allProjects.length>0||hasUnprojected){
    const bar=document.createElement('div');
    bar.className='project-bar';
    // "All" chip
    const allChip=document.createElement('span');
    allChip.className='project-chip'+(!_activeProject?' active':'');
    allChip.textContent='All';
    allChip.onclick=()=>{_activeProject=null;renderSessionListFromCache();};
    bar.appendChild(allChip);
    // "Unassigned" chip — only when there are sessions with no project to
    // filter to. Hidden in the common case where every session is already
    // organized, to keep the chip bar uncluttered.
    if(hasUnprojected){
      const noneChip=document.createElement('span');
      noneChip.className='project-chip no-project'+(_activeProject===NO_PROJECT_FILTER?' active':'');
      noneChip.textContent='Unassigned';
      noneChip.title='Show conversations not yet assigned to a project';
      noneChip.onclick=()=>{_activeProject=NO_PROJECT_FILTER;renderSessionListFromCache();};
      bar.appendChild(noneChip);
    }
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
  // Profile filter toggle (show sessions from other profiles).
  // Cross-profile rows live SERVER-SIDE behind ?all_profiles=1, so the toggle
  // must trigger a refetch — there's no client-cached aggregate to slice through.
  // The server is authoritative for the count (renamed-root cross-alias is
  // server-side). A naive strict-equality client fallback would mis-count.
  const otherProfileCount = _otherProfileCount;
  if(otherProfileCount>0&&!_showAllProfiles){
    const pfToggle=document.createElement('div');
    pfToggle.style.cssText='font-size:10px;padding:4px 10px;color:var(--muted);cursor:pointer;text-align:center;opacity:.7;';
    pfToggle.textContent='Show '+otherProfileCount+' from other profiles';
    pfToggle.onclick=()=>{_showAllProfiles=true;renderSessionList();};
    list.appendChild(pfToggle);
  } else if(_showAllProfiles){
    const pfToggle=document.createElement('div');
    pfToggle.style.cssText='font-size:10px;padding:4px 10px;color:var(--muted);cursor:pointer;text-align:center;opacity:.7;';
    pfToggle.textContent='Show active profile only';
    pfToggle.onclick=()=>{_showAllProfiles=false;renderSessionList();};
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
    empty.textContent=_activeProject===NO_PROJECT_FILTER?'No unassigned sessions.':'No sessions in this project yet.';
    list.appendChild(empty);
  }
  const orderedSessions=[...sessions].sort((a,b)=>_sessionTimestampMs(b)-_sessionTimestampMs(a));
  // Separate pinned from unpinned
  const pinned=orderedSessions.filter(s=>s.pinned);
  const unpinned=orderedSessions.filter(s=>!s.pinned);
  // Date grouping: Pinned / Today / Yesterday / This week / Last week / Older
  const now=_serverNowMs();
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
  const flatSessionRows=[];
  for(const g of groups){
    if(_groupCollapsed[g.label]) continue;
    for(const s of g.items){ flatSessionRows.push({group:g,session:s}); }
  }
  _sessionVisibleSidebarIds=flatSessionRows.map(row=>row.session&&row.session.session_id).filter(Boolean);
  _ensureSessionVirtualScrollHandler(list);
  const activeIndex=flatSessionRows.findIndex(row=>_sessionLineageContainsSession(row.session,activeSidForSidebar));
  const shouldAnchorActive=activeSidForSidebar&&activeIndex>=0&&(
    list.dataset.sessionVirtualActiveAnchor!==activeSidForSidebar||
    list.dataset.sessionVirtualFilter!==q
  );
  let virtualWindow=_sessionVirtualWindow({
    total:flatSessionRows.length,
    scrollTop:listScrollTopBeforeRender,
    viewportHeight:list.clientHeight||520,
    itemHeight:SESSION_VIRTUAL_ROW_HEIGHT,
    buffer:SESSION_VIRTUAL_BUFFER_ROWS,
    threshold:SESSION_VIRTUAL_THRESHOLD_ROWS,
    activeIndex:shouldAnchorActive?activeIndex:-1,
  });
  let virtualAnchorScrollTop=null;
  if(shouldAnchorActive&&virtualWindow.virtualized){
    list.dataset.sessionVirtualActiveAnchor=activeSidForSidebar;
    virtualAnchorScrollTop=virtualWindow.topPad;
  }else if(activeSidForSidebar){
    list.dataset.sessionVirtualActiveAnchor=activeSidForSidebar;
  }else{
    delete list.dataset.sessionVirtualActiveAnchor;
  }
  list.dataset.sessionVirtualTotal=String(flatSessionRows.length);
  list.dataset.sessionVirtualFilter=q;
  list.dataset.sessionVirtualStart=String(virtualWindow.start);
  list.dataset.sessionVirtualEnd=String(virtualWindow.end);
  // Render groups with collapsible headers. Large sidebars render only the
  // current session-row window plus top/bottom spacers inside each group body;
  // headers remain real DOM so pin/archive/date grouping and clicks survive.
  let globalSessionRowIndex=0;
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
    const isGroupCollapsed=Boolean(_groupCollapsed[g.label]);
    if(isGroupCollapsed){body.style.display='none';caret.classList.add('collapsed');}
    hdr.onclick=()=>{
      const isCollapsed=body.style.display==='none';
      body.style.display=isCollapsed?'':'none';
      caret.classList.toggle('collapsed',!isCollapsed);
      _groupCollapsed[g.label]=!isCollapsed;
      _saveCollapsed();
      renderSessionListFromCache();
    };
    wrapper.appendChild(hdr);
    let groupTopPad=0;
    let groupBottomPad=0;
    for(const s of g.items){
      if(isGroupCollapsed) continue;
      const rowIndex=globalSessionRowIndex++;
      const inWindow=!virtualWindow.virtualized||(rowIndex>=virtualWindow.start&&rowIndex<virtualWindow.end);
      if(inWindow){ body.appendChild(_renderOneSession(s, Boolean(g.isPinned))); }
      else if(rowIndex<virtualWindow.start){ groupTopPad+=virtualWindow.itemHeight; }
      else { groupBottomPad+=virtualWindow.itemHeight; }
    }
    if(groupTopPad>0){ body.insertBefore(_sessionVirtualSpacer(groupTopPad,'before'), body.firstChild); }
    if(groupBottomPad>0){ body.appendChild(_sessionVirtualSpacer(groupBottomPad,'after')); }
    wrapper.appendChild(body);
    list.appendChild(wrapper);
  }
  if(virtualAnchorScrollTop!==null){
    list.scrollTop=virtualAnchorScrollTop;
  }else if(listScrollTopBeforeRender>0){
    // Always restore the user's scroll position after re-render, regardless
    // of whether the virtualization window applies. Lists below the
    // virtualization threshold (≤80 rows) still have their DOM rebuilt by
    // every renderSessionListFromCache() call, and without this restore the
    // scrollTop drops to 0 — producing a "scroll keeps jumping back" feel
    // when the list scrolls naturally. Fixed for #1669 follow-up.
    list.scrollTop=listScrollTopBeforeRender;
  }
  // Select mode toggle button (only when NOT in select mode)
  if(!_sessionSelectMode){
    const toggleBtn=document.createElement('div');toggleBtn.className='session-select-toggle';
    toggleBtn.textContent=t('session_select_mode');
    toggleBtn.onclick=(e)=>{e.stopPropagation();toggleSessionSelectMode();};
    list.appendChild(toggleBtn);
  }
  // Note: declared after the groups loop but available via function hoisting.
  function _renderOneSession(s, isPinnedGroup=false){
    const el=document.createElement('div');
    const isActive=_sessionLineageContainsSession(s,activeSidForSidebar);
    const isStreaming=_isSessionEffectivelyStreaming(s);
    _rememberRenderedStreamingState(s, isStreaming);
    _rememberRenderedSessionSnapshot(s);
    const hasUnread=_hasUnreadForSession(s)&&!isActive;
    const readOnly=_isReadOnlySession(s);
    el.className='session-item'+(isActive?' active':'')+(isActive&&S.session&&S.session._flash?' new-flash':'')+(s.archived?' archived':'')+(isStreaming?' streaming':'')+(hasUnread?' unread':'');
    if(s.is_cli_session){
      el.classList.add('cli-session');
      el.dataset.source=_getChannelLabel(s)||'CLI';
      el.dataset.sourceKey=_sourceKeyForSession(s)||'cli';
    }
    if(readOnly) el.classList.add('read-only-session');
    if(isActive&&S.session&&S.session._flash)delete S.session._flash;
    const rawTitle=s.title||'Untitled';
    const tags=(rawTitle.match(/#[\w-]+/g)||[]);
    let cleanTitle=tags.length?rawTitle.replace(/#[\w-]+/g,'').trim():rawTitle;
    // Guard: system prompt content must never surface as a visible session title
    if(cleanTitle.startsWith('[SYSTEM:')){
      cleanTitle='Session';
    }
    // Checkbox for batch select mode
    if(_sessionSelectMode&&!readOnly){
      const cbWrapper=document.createElement('label');cbWrapper.className='session-select-cb-wrapper';
      const cb=document.createElement('input');cb.type='checkbox';cb.className='session-select-cb';
      cb.dataset.sid=s.session_id;cb.checked=_selectedSessions.has(s.session_id);
      cb.onchange=(e)=>{e.stopPropagation();setSessionSelected(s.session_id,cb.checked);};
      cb.onclick=(e)=>{e.stopPropagation();};
      cb.onpointerup=(e)=>{e.stopPropagation();};
      cbWrapper.onpointerup=(e)=>{e.stopPropagation();};
      cbWrapper.onclick=(e)=>{e.stopPropagation();};
      cbWrapper.appendChild(cb);
      el.classList.toggle('selected',_selectedSessions.has(s.session_id));
      el.appendChild(cbWrapper);
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
    // Parent session indicator for forked/branched sessions (#465)
    if(s.parent_session_id){
      const branchInd=document.createElement('span');
      branchInd.className='session-branch-indicator';
      branchInd.innerHTML=li('git-branch',12);
      const parentLabel=_sessionTitleForForkParent(s.parent_session_id)||_truncatedSessionId(s.parent_session_id);
      branchInd.title=(typeof t==='function'?t('forked_from'):'Forked from')+' '+parentLabel;
      titleRow.appendChild(branchInd);
    }
    const title=document.createElement('span');
    title.className='session-title';
    title.textContent=cleanTitle||'Untitled';
    title.title=readOnly?'Read-only imported session':'Double-click to rename';
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
    const lineageKey=_sidebarLineageKeyForRow(s);
    const segmentCount=_sessionSegmentCount(s);
    const lineageSegments=Array.isArray(s._lineage_segments)?s._lineage_segments.filter(seg=>seg&&seg.session_id&&seg.session_id!==s.session_id):[];
    const canExpandLineageSegments=Boolean(lineageKey&&segmentCount>1&&lineageSegments.length>0);
    const lineageSegmentsExpanded=canExpandLineageSegments&&_expandedLineageKeys.has(lineageKey);
    if(segmentCount>0){
      const segmentCountEl=document.createElement('span');
      segmentCountEl.className='session-lineage-count'+(canExpandLineageSegments?' expandable':'');
      const segmentLabel=t('session_meta_segments', segmentCount);
      segmentCountEl.textContent=segmentLabel;
      segmentCountEl.title=segmentLabel;
      if(canExpandLineageSegments){
        segmentCountEl.setAttribute('role','button');
        segmentCountEl.setAttribute('tabindex','0');
        segmentCountEl.setAttribute('aria-expanded',lineageSegmentsExpanded?'true':'false');
        ['pointerdown','pointerup','click'].forEach(ev=>segmentCountEl.addEventListener(ev,e=>e.stopPropagation()));
        const toggleLineageSegments=(e)=>{
          e.preventDefault();
          e.stopPropagation();
          if(_expandedLineageKeys.has(lineageKey)) _expandedLineageKeys.delete(lineageKey);
          else _expandedLineageKeys.add(lineageKey);
          renderSessionListFromCache();
        };
        segmentCountEl.onclick=toggleLineageSegments;
        segmentCountEl.onkeydown=(e)=>{
          if(e.key==='Enter'||e.key===' '){toggleLineageSegments(e);}
        };
      }
      titleRow.appendChild(segmentCountEl);
    }
    const childCount=typeof s._child_session_count==='number'?s._child_session_count:(Array.isArray(s._child_sessions)?s._child_sessions.length:0);
    if(childCount>0){
      const childCountEl=document.createElement('span');
      childCountEl.className='session-child-count';
      const childLabel=t('session_meta_children', childCount);
      childCountEl.textContent=childLabel;
      childCountEl.title=childLabel;
      ['pointerdown','pointerup','click'].forEach(ev=>childCountEl.addEventListener(ev,e=>e.stopPropagation()));
      childCountEl.onclick=(e)=>{
        e.stopPropagation();
        const key=_sidebarLineageKeyForRow(s);
        if(_expandedChildSessionKeys.has(key)) _expandedChildSessionKeys.delete(key);
        else _expandedChildSessionKeys.add(key);
        renderSessionListFromCache();
      };
      titleRow.appendChild(childCountEl);
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
      if(childCount>0) metaBits.push(t('session_meta_children', childCount));
      const modelMeta=_formatSessionModelWithGateway(s);
      if(modelMeta) metaBits.push(modelMeta);
      const sourceLabel=_getChannelLabel(s);
      if(s.is_cli_session&&sourceLabel) metaBits.push(sourceLabel);
      if(readOnly) metaBits.push('read-only');
      if(_showAllProfiles&&s.profile) metaBits.push(s.profile);
      const meta=document.createElement('div');
      meta.className='session-meta';
      meta.textContent=metaBits.join(' · ');
      sessionText.appendChild(meta);
    }
    if(lineageSegmentsExpanded){
      const lineageList=document.createElement('div');
      lineageList.className='session-lineage-segments';
      ['pointerdown','pointerup','click'].forEach(ev=>lineageList.addEventListener(ev,e=>e.stopPropagation()));
      const sortedSegments=[...lineageSegments].sort((a,b)=>{
        const bSeg=Number(b&&b._compression_segment_count||0);
        const aSeg=Number(a&&a._compression_segment_count||0);
        if(bSeg||aSeg){
          if(bSeg!==aSeg) return bSeg-aSeg;
        }
        return _sessionTimestampMs(b)-_sessionTimestampMs(a);
      });
      for(const seg of sortedSegments){
        const row=document.createElement('button');
        row.type='button';
        row.className='session-lineage-segment'+(activeSidForSidebar&&seg.session_id===activeSidForSidebar?' active':'');
        const segTitle=seg.title||t('session_lineage_segment_untitled');
        const segTime=_formatRelativeSessionTime(_sessionTimestampMs(seg));
        row.textContent=`-> ${segTitle} - ${segTime}`;
        row.title=t('session_lineage_segment_open');
        row.onclick=async(e)=>{
          e.stopPropagation();
          if(seg.is_cli_session){
            try{await api('/api/session/import_cli',{method:'POST',body:JSON.stringify({session_id:seg.session_id})});}
            catch(_e){ /* read-only fallback */ }
          }
          await loadSession(seg.session_id);
          renderSessionListFromCache();
        };
        lineageList.appendChild(row);
      }
      sessionText.appendChild(lineageList);
    }
    if(childCount>0&&Array.isArray(s._child_sessions)&&_expandedChildSessionKeys.has(lineageKey)){
      const childList=document.createElement('div');
      childList.className='session-child-sessions';
      ['pointerdown','pointerup','click'].forEach(ev=>childList.addEventListener(ev,e=>e.stopPropagation()));
      const sortedChildren=[...s._child_sessions].sort((a,b)=>_sessionTimestampMs(b)-_sessionTimestampMs(a));
      for(const child of sortedChildren){
        const row=document.createElement('button');
        row.type='button';
        row.className='session-child-session'+(activeSidForSidebar&&child.session_id===activeSidForSidebar?' active':'');
        const childTitle=child.title||'Untitled child session';
        const childTime=_formatRelativeSessionTime(_sessionTimestampMs(child));
        const parentNote=child._parent_segment_title?` via ${child._parent_segment_title}`:'';
        row.textContent=`-> ${childTitle}${parentNote} - ${childTime}`;
        row.title='Open child session';
        row.onclick=async(e)=>{
          e.stopPropagation();
          if(child.is_cli_session){
            try{await api('/api/session/import_cli',{method:'POST',body:JSON.stringify({session_id:child.session_id})});}
            catch(_e){ /* read-only fallback */ }
          }
          await loadSession(child.session_id);
          renderSessionListFromCache();
        };
        childList.appendChild(row);
      }
      sessionText.appendChild(childList);
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
      if(_isReadOnlySession(s)){ if(typeof showToast==='function') showToast('Read-only imported sessions cannot be renamed.',3000); return; }
      // Guard: prevent renaming if session is currently being loaded
      if (_loadingSessionId && _loadingSessionId !== s.session_id) return;

      closeSessionActionMenu();
      _renamingSid = s.session_id;
      const oldTitle=s.title||'Untitled';
      const inp=document.createElement('input');
      inp.className='session-title-input';
      inp.value=oldTitle;
      ['click','mousedown','dblclick','pointerdown'].forEach(ev=>
        inp.addEventListener(ev, e2=>e2.stopPropagation())
      );
      const applyTitle=(nextTitle, updateDom=true)=>{
        if(updateDom) title.textContent=nextTitle;
        s.title=nextTitle;
        const cached=_allSessions.find(item=>item&&item.session_id===s.session_id);
        if(cached) cached.title=nextTitle;
        if(S.session&&S.session.session_id===s.session_id){S.session.title=nextTitle;syncTopbar();}
      };
      let finishDone=false;
      const finish=async(save)=>{
        if(finishDone) return;
        finishDone=true;
        const releaseRename=()=>{
          _renamingSid = null;
          if(inp.isConnected) inp.replaceWith(title);
          // Allow list re-renders again after DOM cleanup has completed.
          setTimeout(()=>{ if(_renamingSid===null) renderSessionListFromCache(); },50);
        };
        if(!save){
          applyTitle(oldTitle,false);
          releaseRename();
          return;
        }
        const newTitle=inp.value.trim()||'Untitled';
        try{
          if(newTitle!==oldTitle){
            await api('/api/session/rename',{method:'POST',body:JSON.stringify({session_id:s.session_id,title:newTitle})});
          }
          applyTitle(newTitle);
        }catch(err){
          applyTitle(oldTitle,false);
          const msg='Rename failed: '+(err&&err.message?err.message:String(err));
          setStatus(msg);
          if(typeof showToast==='function') showToast(msg,3000,'error');
        }finally{
          releaseRename();
        }
      };
      inp.onkeydown=e2=>{
        if(e2.key==='Enter'){
          if(window._isImeEnter&&window._isImeEnter(e2)){return;}
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
    // Expose the rename closure on the row so the three-dot action menu
    // (`_openSessionActionMenu`, defined elsewhere) can trigger it without
    // needing a separate DOM hunt or a duplicate copy of all this state
    // (oldTitle / applyTitle / finish / _renamingSid bookkeeping). The
    // double-click path on this element still calls startRename() directly.
    el._startRename = startRename;
    el.dataset.sid = s.session_id;

    // (Project dot is appended above, between title and timestamp, so it
    // sits outside the truncating title span and stays visible.)
    el.appendChild(sessionText);
    const state=document.createElement('span');
    state.className='session-attention-indicator session-state-indicator'+(isStreaming?' is-streaming':(hasUnread?' is-unread':''));
    state.setAttribute('aria-hidden','true');
    el.appendChild(state);
    // Single trigger button that opens a shared dropdown menu
    let actions=null;
    if(!readOnly){
      actions=document.createElement('div');
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
    }

    // Use pointerup + manual double-tap detection instead of onclick/ondblclick.
    // onclick/ondblclick are unreliable on touch devices (iPad Safari especially):
    // hover-triggered layout shifts, ghost clicks, and 300ms delay all break
    // single-tap navigation. pointerup fires immediately on both mouse & touch.
    // Mouse clicks are instant; touch presses need a 300ms delay to distinguish
    // a tap from a scroll-drag gesture on mobile.
    // Drag detection (pointermove > 5px) cancels the pending tap on release.
    let _lastTapTime=0;
    let _tapTimer=null;
    let _pointerDownX=0;
    let _pointerDownY=0;
    let _pointerActive=false;
    let _isDragging=false;
    let _clearDragTimer=null;
    const _clearPointerDragState=()=>{
      _pointerActive=false;
      if(_isDragging){
        _isDragging=false;
        if(_clearDragTimer){clearTimeout(_clearDragTimer);_clearDragTimer=null;}
        _clearDragTimer=setTimeout(()=>{el.classList.remove('dragging');_clearDragTimer=null;},50);
      }
    };
    el.onpointerdown=(e)=>{
      if(e.pointerType==='mouse' && e.button!==0) return;
      _pointerActive=true;
      _pointerDownX=e.clientX;
      _pointerDownY=e.clientY;
      _isDragging=false;
      if(_clearDragTimer){clearTimeout(_clearDragTimer);_clearDragTimer=null;}
      el.classList.remove('dragging');
    };
    el.onpointermove=(e)=>{
      // Plain hover also dispatches pointermove. Only mark a row as dragging
      // after an actual press starts on this row; otherwise hovered rows stay
      // faded until the next sidebar rerender clears their DOM nodes.
      if(!_pointerActive) return;
      if(_isDragging) return;
      const dx=Math.abs(e.clientX-_pointerDownX);
      const dy=Math.abs(e.clientY-_pointerDownY);
      if(dx>5||dy>5){
        _isDragging=true;
        el.classList.add('dragging');
        // Cancel any pending drag-clear so we don't flash hover mid-drag
        if(_clearDragTimer){clearTimeout(_clearDragTimer);_clearDragTimer=null;}
      }
    };
    el.onpointercancel=_clearPointerDragState;
    el.onpointerleave=()=>{ if(_pointerActive) _clearPointerDragState(); };
    el.onpointerup=(e)=>{
      if(e.pointerType==='mouse' && e.button!==0) return;  // ignore right/middle click
      _pointerActive=false;
      if(_renamingSid) return;
      if(actions&&actions.contains(e.target)) return;
      if(e.target&&e.target.closest&&e.target.closest('.session-child-count,.session-child-sessions,.session-child-session,.session-lineage-count,.session-lineage-segments,.session-lineage-segment')) return;
      if(_sessionSelectMode){e.stopPropagation();if(!readOnly)toggleSessionSelect(s.session_id);return;}
      // If the pointer moved enough to be a drag, cancel any pending tap
      if(_isDragging){clearTimeout(_tapTimer);_tapTimer=null;_lastTapTime=0;_clearDragTimer=setTimeout(()=>{el.classList.remove('dragging');_clearDragTimer=null;},50);return;}
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
      // then navigate. Mouse is instant; touch needs delay to suppress
      // accidental navigation during scroll-drag lifts.
      clearTimeout(_tapTimer);
      const delay=e.pointerType==='mouse'?0:300;
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
      }, delay);
    };
    // Add ondblclick for more reliable double-click detection
    el.ondblclick=(e)=>{
      if(e.pointerType==='mouse' && e.button!==0) return;
      if(_renamingSid) return;
      if(actions&&actions.contains(e.target)) return;
      if(_sessionSelectMode){e.stopPropagation();if(!readOnly)toggleSessionSelect(s.session_id);return;}
      // Guard: prevent renaming if session is currently being loaded
      if (_loadingSessionId && _loadingSessionId !== s.session_id) return;
      startRename();
    };
    return el;
  }
}

async function _handleActiveSessionStorageEvent(e){
  if(!e || e.key !== 'hermes-webui-session') return;
  // Do not treat localStorage as a global active-session bus. Each tab owns its
  // active conversation via its URL (/session/<id>), so another tab switching
  // sessions must not force this tab to navigate away from an in-flight turn.
  if(typeof renderSessionListFromCache==='function') renderSessionListFromCache();
}

if(typeof window!=='undefined'){
  window.addEventListener('storage', (e) => { void _handleActiveSessionStorageEvent(e); });
  window.addEventListener('popstate', () => {
    const sid=(typeof _sessionIdFromLocation==='function')?_sessionIdFromLocation():null;
    if(!sid || (S.session && S.session.session_id===sid)) return;
    // Refuse to switch sessions mid-stream — same UX guard the storage-event
    // handler had. A user mid-turn who hits browser Back should NOT lose the
    // active stream. They can hit Back again once the turn ends.
    if(S.busy){
      if(typeof showToast==='function') showToast('Finish the current turn before switching sessions.',3000);
      return;
    }
    void loadSession(sid);
  });
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
    _clearHandoffStorageForSession(sid);
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
      if(window._isImeEnter&&window._isImeEnter(e)){return;}
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
      if(window._isImeEnter&&window._isImeEnter(e)){return;}
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
  renameItem.onmouseenter=()=>renameItem.style.background='var(--hover-bg)';
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
  delItem.onmouseenter=()=>delItem.style.background='var(--hover-bg)';
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

// Global Escape handler for batch select mode
document.addEventListener('keydown',(e)=>{
  if(e.key==='Escape'&&_sessionSelectMode) exitSessionSelectMode();
});
