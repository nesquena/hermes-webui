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
  spark:'<svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" stroke-linejoin="round"><path d="M8 1.8l1.1 3.1 3.1 1.1-3.1 1.1L8 10.2 6.9 7.1 3.8 6l3.1-1.1z"/><path d="M12.5 9.5l.5 1.5 1.5.5-1.5.5-.5 1.5-.5-1.5-1.5-.5 1.5-.5z"/></svg>',
  link:'<svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" stroke-linejoin="round"><path d="M6.7 9.3a3 3 0 0 1 0-4.2l1.7-1.7a3 3 0 0 1 4.2 4.2l-1 1"/><path d="M9.3 6.7a3 3 0 0 1 0 4.2l-1.7 1.7a3 3 0 0 1-4.2-4.2l1-1"/></svg>',
};

// Tracks which session_id is currently being loaded. Used to discard stale
// responses from in-flight requests when the user switches sessions again
// before the first request completes (#1060).
let _loadingSessionId = null;
// #3306: Snapshot of S.messages captured by loadSession() right before it
// clears them on a force-reload of the active session. Consumed by
// _ensureMessagesLoaded() when calling _carryForwardEphemeralTurnFields so
// ephemeral fields (_turnUsage, _turnDuration, _turnTps, _gatewayRouting,
// _statusCard) survive the wholesale replace. null when there is nothing
// to carry forward (initial load, switch-to-different-session, etc.).
let _pendingCarryForwardSnapshot = null;

// ── Composer draft persistence ────────────────────────────────────────────────

// Debounced save — prevents hammering the server on every keystroke.
let _draftSaveTimer = null;
const _DRAFT_SAVE_DELAY_MS = 400;
const NEW_CHAT_DRAFT_SESSION_KEY = 'hermes-new-chat-draft-session';

function _profileMatchesActiveProfile(profile, activeProfile){
  const eventName = (typeof profile === 'string' && profile.trim()) ? profile.trim() : 'default';
  const activeName = (typeof activeProfile === 'string' && activeProfile.trim()) ? activeProfile.trim() : 'default';
  if(eventName === activeName) return true;
  return eventName === 'default' && !!S.activeProfileIsDefault;
}

function _sessionEventProfilesMatch(eventProfile, activeProfile){
  if(!(typeof eventProfile === 'string' && eventProfile.trim())) return true;
  return _profileMatchesActiveProfile(eventProfile, activeProfile);
}

function _isRestorableNewChatDraftSession(session, requireDraft=false) {
  if (!session || !session.session_id) return false;
  const messageCount = Number(session.message_count || 0);
  if (messageCount !== 0) return false;
  if (session.active_stream_id || session.pending_user_message || session.worktree_path) return false;
  const title = session.title || 'Untitled';
  if (title !== 'Untitled' && title !== 'New Chat') return false;
  const activeProfile = S.activeProfile || 'default';
  const sessionProfile = session.profile || 'default';
  if (!_profileMatchesActiveProfile(sessionProfile, activeProfile)) return false;
  if (!requireDraft) return true;
  const draft = session.composer_draft || {};
  const text = (typeof draft.text === 'string') ? draft.text : '';
  const files = Array.isArray(draft.files) ? draft.files : [];
  return !!(text || files.length);
}

function _rememberNewChatDraftSession(session) {
  if (!_isRestorableNewChatDraftSession(session)) return;
  try { localStorage.setItem(NEW_CHAT_DRAFT_SESSION_KEY, session.session_id); } catch (_) {}
}

function _clearRememberedNewChatDraftSession(sid) {
  if (!sid) return;
  try {
    if (localStorage.getItem(NEW_CHAT_DRAFT_SESSION_KEY) === sid) {
      localStorage.removeItem(NEW_CHAT_DRAFT_SESSION_KEY);
    }
  } catch (_) {}
}

async function _restoreRememberedNewChatDraftSession() {
  let sid = '';
  try { sid = localStorage.getItem(NEW_CHAT_DRAFT_SESSION_KEY) || ''; } catch (_) { sid = ''; }
  if (!sid || (S.session && S.session.session_id === sid)) return false;
  try {
    const data = await api(`/api/session?session_id=${encodeURIComponent(sid)}&messages=0&resolve_model=0`);
    const session = data && data.session;
    if (!_isRestorableNewChatDraftSession(session, true)) {
      _clearRememberedNewChatDraftSession(sid);
      return false;
    }
    await loadSession(sid, {skipLineageResolve:true});
    return !!(S.session && S.session.session_id === sid);
  } catch (_) {
    _clearRememberedNewChatDraftSession(sid);
    return false;
  }
}

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

// Immediate save used before session switches.
function _saveComposerDraftNow(sid, text, files) {
  if (!sid) return;
  clearTimeout(_draftSaveTimer);
  return api('/api/session/draft', {
    method: 'POST',
    body: JSON.stringify({ session_id: sid, text: text || '', files: files || [] }),
  }).catch(() => {});
}

// Restore composer draft from server onto #msg textarea.
// Only restores if there's actual text (skip empty/None drafts).
// Guards against double-restore when rapidly switching sessions.
function _restoreComposerDraft(draft, targetSid, opts={}) {
  const ta = $('msg');
  if (!ta) return;
  // targetSid is the session that was requested — if it no longer matches
  // _loadingSessionId, a newer session switch has already begun, so skip.
  if (targetSid && _loadingSessionId !== null && _loadingSessionId !== targetSid) return;
  const text = (draft && typeof draft.text === 'string') ? draft.text : '';
  const files = (draft && Array.isArray(draft.files)) ? draft.files : [];
  const current = ta.value || '';
  const preserveActiveInput = !!(opts && opts.preserveActiveInput);

  // Same-session force refreshes are driven by external state changes and may
  // finish seconds after the user continued typing. In that case the local
  // composer is the authoritative in-progress draft; never replace non-empty
  // local input with an older server draft. Cross-session switches still restore
  // normally so the previous session's composer contents do not leak forward.
  if (preserveActiveInput && current && current !== text) return;

  // If there's no text and no files, clear the textarea (a previous session's
  // draft may still be sitting there from a cross-session switch).
  if (!text && !files.length) {
    if (current) {
      ta.value = '';
      if (typeof autoResize === 'function') autoResize();
      if (typeof updateSendBtn === 'function') updateSendBtn();
    }
    return;
  }
  // Only update if different to avoid cursor jumps on unrelated session switches.
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
  _clearRememberedNewChatDraftSession(sid);
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
let _sessionListPointerActive = false;
let _sessionListLastScrollAt = 0;
let _pendingSessionListPayload = null;
let _pendingSessionListApplyTimer = 0;
const SESSION_LIST_INTERACTION_IDLE_MS = 700;
const SESSION_SWIPE_DURATION_MS = 500;
const SESSION_SWIPE_REFLOW_LEAD_MS = 220;
const SESSION_REFLOW_TIMEOUT_MS = 420;
const SESSION_LIST_FLIP_TIMEOUT_MS = 460;
const SESSION_LONG_PRESS_DELAY_MS = 400;
const SESSION_ARCHIVE_SWIPE_THRESHOLD_PX = 128;
const SESSION_DELETE_SWIPE_THRESHOLD_PX = 128;
const SESSION_SWIPE_CANCEL_RATIO = 0.75;

function _formatSessionModelWithGateway(s){
  if(!s||!s.model)return'';
  const routing=(typeof _latestGatewayRoutingForSession==='function')?_latestGatewayRoutingForSession(s):(s.gateway_routing||null);
  if(typeof _formatGatewayModelLabel==='function'){
    return _formatGatewayModelLabel(s.model,s.model,routing)||getModelLabel(s.model);
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
  // If the viewed count is now current, any prior completion-unread marker is
  // stale — clear it so _hasUnreadForSession doesn't short-circuit (#3020).
  _clearSessionCompletionUnread(sid);
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

function _clearSessionViewedCount(sid) {
  if (!sid) return;
  const counts = _getSessionViewedCounts();
  if (!Object.prototype.hasOwnProperty.call(counts, sid)) return;
  delete counts[sid];
  _saveSessionViewedCounts();
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
  // For the active session, rely on S.busy to indicate an ongoing stream.
  // INFLIGHT entries for non-active sessions are artifacts of interrupted
  // streams (page refresh, network disconnect, gateway restart) where
  // `delete INFLIGHT[sid]` was never reached — they should NOT cause the
  // sidebar spinner to appear on completed sessions. (#2066)
  return isActive && Boolean(S.busy);
}

function _isSessionEffectivelyStreaming(s) {
  return Boolean(s && (s.is_streaming || _isSessionLocallyStreaming(s)));
}

function _isServerIdleSessionRow(s) {
  return Boolean(s && s.session_id && !s.is_streaming && !s.active_stream_id && !s.pending_user_message);
}

function _reconcileActiveSessionIdleStateFromList(serverRows) {
  if (!S || !S.session || !S.session.session_id) return false;
  if (typeof _sendInProgress !== 'undefined' && _sendInProgress) return false;
  if (!Array.isArray(serverRows)) return false;
  const sid=S.session.session_id;
  const serverRow=serverRows.find(s=>s&&s.session_id===sid);
  if (!serverRow) return false;
  if (!_isServerIdleSessionRow(serverRow)) return false;
  let changed=false;
  if (S.busy) { S.busy=false; changed=true; }
  if (S.activeStreamId) { S.activeStreamId=null; changed=true; }
  if (INFLIGHT&&INFLIGHT[sid]) {
    delete INFLIGHT[sid];
    if (typeof clearInflightState==='function') clearInflightState(sid);
    changed=true;
  }
  if (S.session) {
    S.session.active_stream_id=null;
    S.session.pending_user_message=null;
  }
  _sessionStreamingById.set(sid, false);
  _forgetObservedStreamingSession(sid);
  if (typeof hideApprovalCard==='function') hideApprovalCard(true);
  if (typeof hideLiveRunStatus==='function') hideLiveRunStatus(sid);
  if (typeof clearLiveToolCards==='function') clearLiveToolCards();
  if (changed&&typeof updateSendBtn==='function') updateSendBtn();
  if (changed&&typeof _scheduleActiveSessionIdleReload==='function') _scheduleActiveSessionIdleReload(sid);
  return changed;
}

function _scheduleActiveSessionIdleReload(sid) {
  if(!sid) return;
  setTimeout(async () => {
    if(!S||!S.session||S.session.session_id !== sid) return;
    if(S.busy || S.activeStreamId) return;
    try{
      await loadSession(sid, {force:true, externalRefreshReason:'idle-reconcile'});
    }catch(_){}
  },0);
}

function _purgeStaleInflightEntries() {
  // Clean up INFLIGHT entries for sessions the server confirms are NOT
  // streaming. This prevents the in-memory cache from growing unbounded
  // when streams end abnormally. (#2066)  Additionally, any INFLIGHT entry
  // whose session id is no longer present in the current _allSessions list
  // (deleted / archived / filtered out) is also removed so that ghost entries
  // from deleted sessions do not accumulate. (#2092)
  if (typeof INFLIGHT !== 'object' || !INFLIGHT) return;
  const sessionsById = new Map();
  if (Array.isArray(_allSessions)) {
    for (const s of _allSessions) {
      if (s && s.session_id) sessionsById.set(s.session_id, s);
    }
  }
  for (const sid of Object.keys(INFLIGHT)) {
    if (typeof _sendInProgress !== 'undefined' && _sendInProgress && sid === _sendInProgressSid) {
      continue;
    }
    if (!sessionsById.has(sid)) {
      // Session is absent from _allSessions — it was deleted / archived /
      // filtered and can never stream again, so drop the entry.
      delete INFLIGHT[sid];
      if (typeof clearInflightState === 'function') clearInflightState(sid);
      continue;
    }
    const s = sessionsById.get(sid);
    if (!s.is_streaming) {
      // Session exists but is not streaming — purge it.
      delete INFLIGHT[sid];
      if (typeof clearInflightState === 'function') clearInflightState(sid);
    }
    // Sessions that exist and are still streaming are preserved.
  }
}

function _rememberRenderedStreamingState(s, isStreaming) {
  if (!s || !s.session_id || !isStreaming) return;
  _sessionStreamingById.set(s.session_id, true);
  _rememberObservedStreamingSession(s);
}

function _inflightHasVisibleLiveState(inflight) {
  if (!inflight || typeof inflight !== 'object') return false;
  if (String(inflight.lastAssistantText || '').trim()) return true;
  if (String(inflight.lastReasoningText || '').trim()) return true;
  if (String(inflight.liveTurnHtml || '').trim()) return true;
  if (Array.isArray(inflight.toolCalls) && inflight.toolCalls.length) return true;
  if (Array.isArray(inflight.activityBurstAnchors) && inflight.activityBurstAnchors.length) return true;
  if (Array.isArray(inflight.messages)) {
    return inflight.messages.some((msg) => {
      if (!msg || msg.role !== 'assistant') return false;
      const content = msg.content;
      if (typeof content === 'string') return content.trim();
      if (Array.isArray(content)) return content.length > 0;
      return Boolean(content);
    });
  }
  return false;
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
  const finalIdx = _allSessions.findIndex(s => s && s.session_id === finalSid);
  const previousIdx = previousSid ? _allSessions.findIndex(s => s && s.session_id === previousSid) : -1;
  const idx = finalIdx >= 0 ? finalIdx : previousIdx;
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
    for (let i = _allSessions.length - 1; i >= 0; i--) {
      if (i !== idx && _allSessions[i] && _allSessions[i].session_id === previousSid) {
        _allSessions.splice(i, 1);
      }
    }
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
    if (completedObservedStream || completedPersistedObservedStream || completedWithNewMessages) {
      if (!_isSessionActivelyViewedForList(sid)) {
        _markSessionCompletionUnread(sid, s.message_count);
      } else {
        // Sync viewed count so we don't flag stale unread on tab switch (#3020)
        _setSessionViewedCount(sid, messageCount);
      }
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

let _newSessionInFlight=null;
const _newSessionPendingText=()=>t('new_session_creating')||'Creating new conversation…';
function _setNewSessionPending(pending){
  const ids=['btnNewChat','btnTitlebarNewChat'];
  for (let i=0;i<ids.length;i++){
    const btn=$(ids[i]);
    if(!btn) continue;
    btn.disabled=!!pending;
    btn.setAttribute('aria-busy',pending?'true':'false');
  }
  const statusEl=$('composerStatus');
  const pendingText=_newSessionPendingText();
  if(pending){
    setComposerStatus(pendingText);
  }else if(statusEl&&statusEl.textContent===pendingText){
    setComposerStatus('');
  }
}

async function newSession(flash, options={}){
  if(_newSessionInFlight){
    if(typeof showToast==='function') showToast(_newSessionPendingText(),1500);
    return _newSessionInFlight;
  }
  _setNewSessionPending(true);
  _newSessionInFlight=(async()=>{
    updateQueueBadge();
    S.toolCalls=[];
    _messagesTruncated=false;
    _oldestIdx=0;
    clearLiveToolCards();
    // One-shot profile-switch workspace: applied to the first new session after a profile
    // switch, then cleared.  Use a dedicated flag so S._profileDefaultWorkspace (the
    // persistent boot/settings default) is not consumed and remains available for the
    // blank-page display on all subsequent returns to the empty state (#823).
    const switchWs=S._profileSwitchWorkspace;
    S._profileSwitchWorkspace=null;
    const inheritWs=switchWs||(S.session?S.session.workspace:null)||(S._profileDefaultWorkspace||null);
    const reqBody={
      workspace:inheritWs,
      profile:S.activeProfile||'default',
    };
    if(S.session&&S.session.session_id) reqBody.prev_session_id=S.session.session_id;
    if(options&&options.worktree) reqBody.worktree=true;
    if(_activeProject&&_activeProject!==NO_PROJECT_FILTER) reqBody.project_id=_activeProject;
    // Carry the visible picker selection into the new session. Without this,
    // /api/session/new falls back to config.yaml defaults (e.g. gpt-5.5) even
    // when the user already chose cursor/composer-2.5 in the composer chip.
    const modelSelForNew=$('modelSelect');
    let newModelState=null;
    if(modelSelForNew&&modelSelForNew.value&&typeof _modelStateForSelect==='function'){
      newModelState=_modelStateForSelect(modelSelForNew,modelSelForNew.value);
    }else if(typeof _readPersistedModelState==='function'){
      newModelState=_readPersistedModelState();
    }
    if(newModelState&&newModelState.model){
      reqBody.model=newModelState.model;
      // Cold-start / picker-without-provider fallback: when the dropdown option's
      // data-provider is empty/'default' or the persisted state predates provider
      // tracking, newModelState.model_provider is null. POST /api/session/new's
      // fast path in _resolve_compatible_session_model_state requires both model
      // and a truthy model_provider; without it, the request falls into
      // get_available_models() and a 3-4s cold catalog rebuild. window._activeProvider
      // is hydrated at boot (ui.js) and on config refresh (panels.js), so it's a
      // safe default that matches the user's configured route. S.session.model_provider
      // is the previous-session fallback when the dropdown is unhydrated.
      //
      // Guard: a slash-qualified model (e.g. "gemini/gemini-2.5") or an
      // @provider:model string already carries a foreign provider namespace from
      // a previous session that was served by a different backend. Attaching
      // the current _activeProvider to such a slug would let the server's fast
      // path pass it through without consulting the catalog, silently
      // re-pointing the new session at the wrong backend (the very case the
      // slow-path normalization in _resolve_compatible_session_model_state is
      // designed to fix — see routes.py docstring around line 1891-1894). For
      // those models we leave the wire shape with model_provider=null so the
      // slow path's cross-provider repair still runs. Closes the open
      // follow-up from #2518.
      const _bareModel=!/[/]/.test(newModelState.model)&&!newModelState.model.startsWith('@');
      // Second guard (#3410-followup): even a bare model can carry a known
      // family prefix (gpt→openai, claude→anthropic, gemini→google). If that
      // family maps to a DIFFERENT provider than the fallback we'd attach, the
      // server fast path passes the pair through verbatim (no validation) and
      // silently routes to the wrong backend — so leave model_provider=null and
      // let the slow-path family repair run (mirrors routes.py _normalize_provider_id).
      const _fallbackProvider=_bareModel?(window._activeProvider||(S.session&&S.session.model_provider)||''):'';
      const _familyProvider=(m=>{const s=String(m||'').toLowerCase();
        if(s.startsWith('gpt'))return 'openai';if(s.startsWith('claude'))return 'anthropic';
        if(s.startsWith('gemini'))return 'google';return '';})(newModelState.model);
      const _normProv=p=>{const s=String(p||'').toLowerCase();
        if(s.startsWith('openai'))return 'openai';if(s.startsWith('anthropic')||s.startsWith('claude'))return 'anthropic';
        if(s.startsWith('google')||s.startsWith('gemini'))return 'google';return s;};
      const _familyMismatch=_familyProvider&&_fallbackProvider&&_normProv(_fallbackProvider)!==_familyProvider;
      const _fallbackIsNamedCustom=String(_fallbackProvider||'').toLowerCase().startsWith('custom:');
      reqBody.model_provider=newModelState.model_provider
        ||((_bareModel&&!_familyMismatch&&!_fallbackIsNamedCustom)?(_fallbackProvider||null):null)
        ||null;
    }
    const data=await api('/api/session/new',{method:'POST',body:JSON.stringify(reqBody)});
    S.session=data.session;S.messages=data.session.messages||[];
    if(_sessionSourceFilter==='cli') _sessionSourceFilter='webui';
    if(typeof _hydrateTodosFromSession==='function') _hydrateTodosFromSession(S.session);
    S.lastUsage={...(data.session.last_usage||{})};
    if(!(options&&options.worktree)) _rememberNewChatDraftSession(S.session);
    if(flash)S.session._flash=true;
    try{localStorage.setItem('hermes-webui-session',S.session.session_id);}catch(_){}
    _setActiveSessionUrl(S.session.session_id);
    if(typeof startSessionStream==='function') startSessionStream(S.session.session_id);
    _setSessionViewedCount(S.session.session_id, S.session.message_count || 0);
    // Sync chat-header dropdown to the session's model/provider so the UI reflects
    // the default route the server actually used (#872). Compare provider state too:
    // duplicate model ids can exist under several providers, and a stale persisted
    // picker selection with the same model id should not mask the new session's
    // configured default provider.
    const modelSel=$('modelSelect');
    if(S.session.model && modelSel && typeof _applyModelToDropdown==='function'){
      const currentModelState=(typeof _modelStateForSelect==='function')
        ? _modelStateForSelect(modelSel,modelSel.value)
        : {model:modelSel.value,model_provider:null};
      const sessionProvider=S.session.model_provider||null;
      const currentProvider=currentModelState.model_provider||null;
      if(S.session.model!==modelSel.value || sessionProvider !== currentProvider){
        let sessionModelApplied=_applyModelToDropdown(S.session.model,modelSel,sessionProvider);
        if(!sessionModelApplied){
          const opt=document.createElement('option');
          opt.value=S.session.model;
          opt.textContent=typeof getModelLabel==='function'?getModelLabel(S.session.model):S.session.model;
          opt.dataset.custom='1';
          opt.dataset.provider=sessionProvider||'';
          modelSel.appendChild(opt);
          sessionModelApplied=_applyModelToDropdown(S.session.model,modelSel,sessionProvider);
        }
        if(sessionModelApplied&&typeof syncModelChip==='function') syncModelChip();
      }
    }
    // Reset per-session visual state: a fresh chat is idle even if another
    // conversation is still streaming in the background.
    S.busy=false;
    S.activeStreamId=null;
    updateSendBtn();
    setStatus('');
    setComposerStatus('');
    if(typeof _setLiveAssistantTps==='function') _setLiveAssistantTps(null);
    if(typeof _syncCtxIndicator==='function'){
      _syncCtxIndicator({
        input_tokens:data.session.input_tokens||0,
        output_tokens:data.session.output_tokens||0,
        estimated_cost:data.session.estimated_cost||0,
        cache_read_tokens:data.session.cache_read_tokens||0,
        cache_write_tokens:data.session.cache_write_tokens||0,
        cache_hit_percent:data.session.cache_hit_percent,
        context_length:data.session.context_length||0,
        last_prompt_tokens:data.session.last_prompt_tokens||0,
        threshold_tokens:data.session.threshold_tokens||0,
      });
    }
    updateQueueBadge(S.session.session_id);
    syncTopbar();renderMessages();
    const dirLoad=loadDir('.');
    // loadDir('.') is fire-and-forget while the workspace panel is closed:
    // waiting would block new-chat/profile-switch flow for users who never open
    // the file tree. When visible, wait so the file list lands with the session.
    if(options&&options.awaitWorkspaceLoad) await dirLoad;
    // don't call renderSessionList here - callers do it when needed
  })();
  try{
    return await _newSessionInFlight;
  }finally{
    _newSessionInFlight=null;
    _setNewSessionPending(false);
  }
}

// #2971 (Greptile P1 r3377162160): loadSession() tears down the live
// per-session SSE at the top via stopSessionStream() (line ~754), but only the
// success path re-arms it via startSessionStream() (line ~875). Every
// early-return exit (fetch error, auth-redirect undefined) — and the
// same-session no-op guard, which returns BEFORE the teardown — could leave
// the session the user actually remains on with a permanently null
// EventSource, silently dropping bg_task_complete delivery until a full page
// reload or a forced loadSession. This helper re-arms the stream for whatever
// session is currently on screen (S.session). startSessionStream() is
// idempotent — it no-ops when already live for that sid (top guard
// `_sessionStreamSessionId === sid && _sessionEventSource`) — so this never
// double-arms the success path, which arms the *newly assigned* S.session
// only after this point.
function _rearmActiveSessionStream(){
  if(typeof startSessionStream!=='function') return;
  const activeSid = S.session ? S.session.session_id : null;
  if(activeSid) startSessionStream(activeSid);
}

async function loadSession(sid){
  const opts = arguments[1] || {};
  if(!opts.skipLineageResolve && typeof _resolveSessionIdFromSidebarLineage==='function'){
    const resolvedSid=_resolveSessionIdFromSidebarLineage(sid);
    if(resolvedSid&&resolvedSid!==sid) sid=resolvedSid;
  }
  const forceReload = !!opts.force;
  const currentSid = S.session ? S.session.session_id : null;
  const sameSessionForceReload = forceReload && currentSid===sid;
  // Clicking the already-open session in the sidebar is a no-op. Reloading it
  // tears down active pane state and can reset the long-session scroll window
  // to the top even though the user did not navigate anywhere. Explicit
  // refresh paths pass {force:true} when external state.db changes arrive.
  // Do not no-op a same-session click while another load is in flight: the
  // previous transcript may already have been cleared for the pending switch.
  // Static force-reload invariant: if(currentSid===sid && !forceReload) return;
  // #2971: idempotent re-arm before the no-op guard revives a stream a prior
  // failed loadSession killed; no-ops on real switches.
  _rearmActiveSessionStream();
  if(currentSid===sid && !forceReload && !_loadingSessionId) return;
  // Mark this session as the in-flight load. Subsequent loadSession() calls
  // will overwrite this; stale awaits use the mismatch to bail out (#1060).
  _loadingSessionId = sid;
  stopApprovalPolling();hideApprovalCard(forceReload);
  if(typeof stopSessionStream==='function') stopSessionStream();
  _yoloEnabled=false;_updateYoloPill();
  if(typeof stopClarifyPolling==='function') stopClarifyPolling();
  if(typeof hideClarifyCard==='function') hideClarifyCard(forceReload, forceReload?'external-refresh':'dismissed');
  // Show loading indicator immediately for responsiveness.
  // Cleared by renderMessages() once full session data arrives.
  // Persist the current composer draft before switching away so it can be
  // restored when the user switches back (#1060). Save to server now so the
  // draft survives page refresh and syncs across clients.
  if (currentSid && currentSid !== sid) {
    await _saveComposerDraftNow(currentSid, ($('msg') || {}).value || '', S.pendingFiles ? [...S.pendingFiles] : []);
    // The awaited draft save above yields the event loop. If another
    // loadSession() started for a different session while we were waiting
    // (rapid switch B→C), _loadingSessionId now points at that newer load —
    // bail out before the destructive state-clearing block below so this stale
    // continuation can't wipe S.messages / write the loading placeholder /
    // close streams for the session the user actually landed on (#1060 guard,
    // extended to cover the new pre-switch await).
    if (_loadingSessionId !== sid) return;
  }
  if (currentSid !== sid || forceReload) {
    // #3306: When force-reloading the currently-active session (e.g. external
    // poll triggering a refresh), snapshot the existing messages BEFORE we
    // clear them. _ensureMessagesLoaded() runs the ephemeral-field
    // carry-forward (_turnUsage, _turnDuration, _turnTps, _gatewayRouting,
    // _statusCard) against S.messages, but by the time the API fetch returns
    // S.messages has already been reset to [] here and the carry-forward is a
    // no-op. The visible symptom is the token-usage badge vanishing ~10s
    // after each assistant turn completes. Stash the snapshot so the
    // carry-forward call can consume it.
    _pendingCarryForwardSnapshot = (currentSid === sid && forceReload)
      ? (S.messages || []).slice()
      : null;
    // #3239: also capture a reload-width hint BEFORE clearing so the
    // authoritative reload preserves the already-loaded transcript width
    // instead of collapsing a long session back to the default tail window.
    if (sameSessionForceReload) _captureSameSessionForceReloadHint(sid);
    else _clearSameSessionForceReloadHint();
    S.messages = [];
    S.toolCalls = [];
    _messagesTruncated = false;
    _oldestIdx = 0;
    // Close live SSE streams from the session we're leaving. The error
    // handler checks _isSessionActivelyViewed() and won't auto-reconnect
    // for a backgrounded session, preventing leaked connections that would
    // pump token events into an orphaned closure, freezing the main thread.
    if (currentSid && currentSid !== sid && typeof closeOtherLiveStreams === 'function') {
      closeOtherLiveStreams(sid);
    }
    _loadingOlder = false;
    const _msgInner = $('msgInner');
    if (_msgInner && currentSid !== sid) _msgInner.innerHTML = '<div style="display:flex;align-items:center;justify-content:center;height:100%;color:var(--text-muted);font-size:14px;padding:40px;text-align:center;">Loading conversation...</div>';
  }
  // Phase 1: Load metadata only (~1KB) for fast session switching. Keep model
  // resolution out of the first-paint path; old provider-shaped model IDs are
  // repaired by the deferred resolver after S.session is assigned.
  // Guard against network/server failures to prevent a permanently stuck loading state.
  let data;
  try {
    data = await api(`/api/session?session_id=${encodeURIComponent(sid)}&messages=0&resolve_model=0`);
  } catch(e) {
    const _msgInner = $('msgInner');
    if(_msgInner){
      if(e.status===404){
        _msgInner.innerHTML='<div style="display:flex;align-items:center;justify-content:center;height:100%;color:var(--text-muted);font-size:14px;padding:40px;text-align:center;">Session not available in web UI.</div>';
        // Self-heal (clear saved id + strip /session/<id> URL) only when the
        // 404'd id is the one we are activating: a boot-time restore
        // (!currentSid, #2798) or a mid-session reload of the *current* session
        // whose sidecar was deleted server-side (#2782). A click into a
        // *different* dead session (currentSid && currentSid!==sid) must not run
        // it: localStorage and the URL still point at the live session (both are
        // only updated on a successful load), so wiping them would log the user
        // out of a healthy session. The URL strip is needed in the self-heal
        // case because _sessionIdFromLocation() re-injects the id on reload.
        // Only the rethrow stays gated on !currentSid: boot rethrows to fall
        // through to empty-state; mid-session there is no boot path to reach.
        if(!currentSid || currentSid===sid){
          try{ localStorage.removeItem('hermes-webui-session'); }catch(_){ }
          try{ history.replaceState(null,'',_appRootPath()); }catch(_){ }
          if (_loadingSessionId === sid) _loadingSessionId = null;
          if(!currentSid){
            throw e;
          }
        }
      } else {
        _msgInner.innerHTML='<div style="display:flex;align-items:center;justify-content:center;height:100%;color:var(--text-muted);font-size:14px;padding:40px;text-align:center;">Failed to load session. Try switching sessions or refreshing.</div>';
        if(typeof showToast==='function') showToast('Failed to load session',3000,'error');
      }
    }
    _clearSameSessionForceReloadHint(sid);
    // Capture whether this failure self-healed away the current session (a
    // 404 on the *current* session whose sidecar was deleted server-side).
    // In that case there is no live session left to stream for, so we must
    // NOT restart — doing so would spin the SSE reconnect loop against a dead
    // session_id.
    const _selfHealedCurrent = (e.status===404) && (currentSid===sid);
    if (_loadingSessionId === sid) _loadingSessionId = null;
    // The session stream was stopped unconditionally at the top of this load
    // (mirroring stopApprovalPolling). On the happy path it's restarted ~120
    // lines below, but this failure exit never reaches that point — leaving
    // the session still on screen permanently silenced. bg_task_complete
    // events (the new feature's primary delivery path) would be dropped until
    // the user explicitly navigates to a session again. Restart the stream for
    // the session that remains on screen. Skip when a newer load is already in
    // flight (_loadingSessionId !== null after the reset above): that load owns
    // the stream and starts its own. Skip the self-healed-current case (no live
    // session to stream).
    // #2971: this fetch-error path keeps its bespoke guarded restart (rather
    // than the shared _rearmActiveSessionStream helper used on the other
    // early-returns) because only here can the current session have just
    // self-healed away — re-arming a 404'd/deleted session_id would spin the
    // SSE reconnect loop against a dead session.
    if (currentSid && !_selfHealedCurrent && _loadingSessionId === null
        && typeof startSessionStream === 'function') {
      startSessionStream(currentSid);
    }
    return;
  }
  // Guard: api() may have redirected (401) and returned undefined; in that case
  // the browser is already navigating away, so abort the rest of this flow.
  if (!data) {
    _clearSameSessionForceReloadHint(sid);
    if (_loadingSessionId === sid) _loadingSessionId = null;
    // #2971: re-arm the still-displayed session's stream (defensive — harmless
    // if the 401 redirect is already tearing the page down). Idempotent.
    _rearmActiveSessionStream();
    return;
  }
  // Stale response? A newer loadSession() call has already started (#1060).
  if (_loadingSessionId !== sid) {
    // #2971: a newer in-flight load owns the final stream arming, but until it
    // assigns S.session and reaches startSessionStream() the currently-shown
    // session must not be left stream-dead by our top-of-function teardown.
    // Re-arm the genuinely-displayed S.session (idempotent — no-ops once the
    // newer load arms its own sid).
    _rearmActiveSessionStream();
    return;
  }
  S.session=data.session;
  if(typeof _hydrateTodosFromSession==='function') _hydrateTodosFromSession(S.session);
  S.session._modelResolutionDeferred=true;
  S.lastUsage={...(data.session.last_usage||{})};
  // Reset scroll-direction tracker on session switch so the new chat's
  // first scroll doesn't compare against the previous chat's scrollTop
  // and false-trigger an unpin (#1731 follow-up — Opus stage-302 SHOULD-FIX).
  if (typeof window !== 'undefined' && typeof window._resetScrollDirectionTracker === 'function') {
    try { window._resetScrollDirectionTracker(); } catch (_) {}
  }
  if(typeof _applyPendingSessionModelForSession==='function') _applyPendingSessionModelForSession(sid);
  _resolveSessionModelForDisplaySoon(sid);
  // Sync workspace display immediately so the chip label reflects the new session's workspace
  // before any async message-loading begins (mirrors how model is handled).
  if(typeof syncTopbar==='function') syncTopbar();
  _setSessionViewedCount(S.session.session_id, Number(data.session.message_count || 0));
  _clearSessionCompletionUnread(S.session.session_id);
  try{localStorage.setItem('hermes-webui-session',S.session.session_id);}catch(_){}
  _setActiveSessionUrl(S.session.session_id);
  if(typeof startSessionStream==='function') startSessionStream(S.session.session_id);

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

  function _mergePendingSessionMessage(session,messages){
    if(!Array.isArray(messages)) return false;
    const pendingMsg=typeof getPendingSessionMessage==='function'?getPendingSessionMessage(session,messages):null;
    if(!pendingMsg) return false;
    if(messages.some(existing=>_sameTranscriptMessage(existing,pendingMsg))) return false;
    const liveAssistantIdx=messages.findIndex(m=>m&&m.role==='assistant'&&m._live);
    if(liveAssistantIdx>=0) messages.splice(liveAssistantIdx,0,pendingMsg);
    else messages.push(pendingMsg);
    return true;
  }

  // Phase 2a: If session is streaming, restore the persisted transcript first,
  // then merge the local INFLIGHT live tail. INFLIGHT is a recovery tail, not a
  // complete transcript; treating it as the full source makes long sessions look
  // like they lost history after switching away and back.
  if(!INFLIGHT[sid]&&activeStreamId&&typeof loadInflightState==='function'){
    const stored=loadInflightState(sid, activeStreamId);
    if(stored){
      INFLIGHT[sid]={
        messages:Array.isArray(stored.messages)&&stored.messages.length?stored.messages:[],
        uploaded:Array.isArray(stored.uploaded)?stored.uploaded:[],
        toolCalls:Array.isArray(stored.toolCalls)?stored.toolCalls:[],
        // Phase 2: restore the live todo snapshot from persisted INFLIGHT
        // so the panel does not flicker to empty when a mid-stream
        // browser reload reattaches before the next `todo_state` event
        // fires.  Both fields are optional; missing values fall back to
        // cold-load via session.todo_state.
        todos:Array.isArray(stored.todos)?stored.todos:null,
        todoStateMeta:stored.todoStateMeta||null,
        reattach:true,
        lastAssistantText:String(stored.lastAssistantText||''),
        lastReasoningText:String(stored.lastReasoningText||''),
        lastRunJournalSeq:Number(stored.lastRunJournalSeq||0)||0,
        journalReplayFromStart:!!stored.journalReplayFromStart,
        currentActivityBurstId:Number(stored.currentActivityBurstId||0)||0,
        currentLiveSegmentSeq:Number(stored.currentLiveSegmentSeq||0)||0,
        activityBurstAnchors:Array.isArray(stored.activityBurstAnchors)?stored.activityBurstAnchors:[],
      };
    }
  }

  if(INFLIGHT[sid]&&INFLIGHT[sid].journalReplayFromStart&&activeStreamId){
    delete INFLIGHT[sid];
    if(typeof clearInflightState==='function') clearInflightState(sid);
  }

  if(activeStreamId&&INFLIGHT[sid]&&!_inflightHasVisibleLiveState(INFLIGHT[sid])){
    // A stale cursor-only INFLIGHT entry is worse than no cache: replay would
    // resume after lastRunJournalSeq while the pane has no prose/tool DOM to
    // preserve, making a session switch look like the live turn vanished.
    delete INFLIGHT[sid];
    if(typeof clearInflightState==='function') clearInflightState(sid);
  }

  if(INFLIGHT[sid]){
    _ensureInflightLiveAssistantMessage(INFLIGHT[sid]);
    const inflightMessages=_projectInflightMessagesForActivityBursts(INFLIGHT[sid]);
    S.toolCalls=[];
    try {
      await _ensureMessagesLoaded(sid);
    } catch(e) {
      S.messages=inflightMessages;
    }
    const liveTailPrepared=_prepareRunningLiveTail(S.messages,inflightMessages);
    if(liveTailPrepared){
      S.messages=_dropCurrentTurnAssistantMessages(S.messages);
    }
    S.messages=_mergeInflightTailMessages(S.messages,inflightMessages);
    S.toolCalls=(INFLIGHT[sid].toolCalls||[]);
    if(_mergePendingSessionMessage(S.session,S.messages)&&inflightMessages===(INFLIGHT[sid].messages||[])){
      INFLIGHT[sid].messages=S.messages;
    }
    // Refresh todos from cold-load or persisted INFLIGHT before painting.
    if(typeof _hydrateTodosFromSession==='function') _hydrateTodosFromSession(S.session);
    S.busy=true;
    // appendLiveToolCard() is guarded by S.activeStreamId; restore it before
    // replaying persisted live tools so the compact Activity count survives
    // switching away from and back to an active chat (#1715).
    S.activeStreamId=activeStreamId;
    const liveToolReplayId=(tc)=>String(tc&&(tc.tid||tc.id||tc.tool_call_id||tc.tool_use_id||tc.call_id||'')||'').trim();
    const replayPersistedLiveToolCards=(opts)=>{
      const liveToolCalls=Array.isArray(S.toolCalls)
        ? S.toolCalls
        : (Array.isArray(INFLIGHT[sid]&&INFLIGHT[sid].toolCalls)?INFLIGHT[sid].toolCalls:[]);
      const skipUnkeyedRestoredDuplicates=!!(opts&&opts.skipUnkeyedRestoredDuplicates);
      const restoredLiveTurn=skipUnkeyedRestoredDuplicates?document.getElementById('liveAssistantTurn'):null;
      const hasRestoredLiveToolRows=!!(restoredLiveTurn&&restoredLiveTurn.querySelector('.tool-card-row'));
      for(const tc of (liveToolCalls||[])){
        if(skipUnkeyedRestoredDuplicates&&hasRestoredLiveToolRows&&!liveToolReplayId(tc)) continue;
        if(tc&&tc.name) appendLiveToolCard(tc,{sessionId:sid,streamId:activeStreamId});
      }
    };
    let didReconnect=false;
    if(INFLIGHT[sid].reattach&&activeStreamId&&typeof attachLiveStream==='function'){
      INFLIGHT[sid].reattach=false;
      if (_loadingSessionId !== sid) return;
      didReconnect=true;
      attachLiveStream(sid, activeStreamId, S.session.pending_attachments||[], {reconnecting:true});
    }
    syncTopbar();renderMessages(sameSessionForceReload?{preserveScroll:true}:undefined);
    if(typeof ensureRunActivityForCurrentTurn==='function') ensureRunActivityForCurrentTurn();
    const hasStructuredLiveState=!!(INFLIGHT[sid]&&(
      String(INFLIGHT[sid].lastAssistantText||'').trim()||
      String(INFLIGHT[sid].lastReasoningText||'').trim()||
      (Array.isArray(INFLIGHT[sid].activityBurstAnchors)&&INFLIGHT[sid].activityBurstAnchors.length)||
      (Array.isArray(INFLIGHT[sid].toolCalls)&&INFLIGHT[sid].toolCalls.length)
    ));
    let restoredLiveTurn=false;
    if(typeof restoreLiveTurnHtmlForSession==='function'){
      if(!hasStructuredLiveState){
        restoredLiveTurn=restoreLiveTurnHtmlForSession(sid);
      }else{
        const liveTurn=document.getElementById('liveAssistantTurn');
        const hasCurrentWorklogContent=!!(liveTurn&&liveTurn.querySelector(
          '.live-worklog[data-live-worklog-shell="1"] .tool-card-row,'+
          '.live-worklog[data-live-worklog-shell="1"] .wl-reason,'+
          '.tool-call-group[data-live-tool-worklog-group="1"] .tool-card-row,'+
          '.tool-call-group[data-live-tool-worklog-group="1"] .wl-reason,'+
          '.tool-call-group[data-live-tool-call-group="1"] .tool-card-row,'+
          '.tool-call-group[data-live-tool-call-group="1"] .wl-reason'
        ));
        if(hasCurrentWorklogContent) restoredLiveTurn=true;
        else restoredLiveTurn=restoreLiveTurnHtmlForSession(sid);
      }
    }
    if(restoredLiveTurn&&didReconnect){
      replayPersistedLiveToolCards({skipUnkeyedRestoredDuplicates:true});
    }
    if(!restoredLiveTurn){
      clearLiveToolCards();
      if(typeof placeLiveToolCardsHost==='function') placeLiveToolCardsHost();
      if(typeof ensureLiveWorklogShell==='function') ensureLiveWorklogShell();
      else appendThinking();
      replayPersistedLiveToolCards();
    }
    if(typeof ensureLiveWorklogShell==='function'){
      const liveTurn=document.getElementById('liveAssistantTurn');
      if(!liveTurn||!liveTurn.querySelector('.tool-call-group[data-tool-worklog-group="1"]')) ensureLiveWorklogShell();
    }
    loadDir('.');
    setBusy(true);setComposerStatus('');
    startApprovalPolling(sid);
    if(typeof startClarifyPolling==='function') startClarifyPolling(sid);
    if(typeof _fetchYoloState==='function') _fetchYoloState(sid);
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

    // Restore any queued message that survived page refresh or tab restore.
    if(typeof queueSessionMessage==='function'){
      try{
        const _entries=typeof _readPersistedSessionQueue==='function'
          ? _readPersistedSessionQueue(sid)
          : [];
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
          }
          if(typeof _clearPersistedSessionQueue==='function') _clearPersistedSessionQueue(sid);
        }
      }catch(_){if(typeof _clearPersistedSessionQueue==='function') _clearPersistedSessionQueue(sid);}
    }

    // Reconstruct tool calls from message metadata, or fall back to session-level summary.
    // (hasMessageToolMetadata already computed inside _ensureMessagesLoaded; S.toolCalls set there.)
    updateQueueBadge(sid);

    // Attach pending user message if one is queued.
    _mergePendingSessionMessage(S.session,S.messages);

    if(activeStreamId){
      S.busy=true;
      S.activeStreamId=activeStreamId;
      if(typeof attachLiveStream==='function') attachLiveStream(sid, activeStreamId, S.session.pending_attachments||[], {reconnecting:true});
      else if(typeof watchInflightSession==='function') watchInflightSession(sid, activeStreamId);
      updateSendBtn();
      setStatus('');
      setComposerStatus('');
      // syncTopbar();renderMessages();appendThinking();loadDir('.');
      syncTopbar();renderMessages(sameSessionForceReload?{preserveScroll:true}:undefined);
      if(typeof ensureLiveWorklogShell==='function') ensureLiveWorklogShell();
      else appendThinking();
      loadDir('.');
      updateQueueBadge(sid);
      startApprovalPolling(sid);
      if(typeof startClarifyPolling==='function') startClarifyPolling(sid);
      if(typeof _fetchYoloState==='function') _fetchYoloState(sid);
    }else{
      S.busy=false;
      S.activeStreamId=null;
      updateSendBtn();
      setStatus('');
      setComposerStatus('');
      updateQueueBadge(sid);
      syncTopbar();renderMessages(sameSessionForceReload?{preserveScroll:true}:undefined);
      if(typeof resumeManualCompressionForSession==='function') resumeManualCompressionForSession(sid);
      const _dirP=loadDir('.');
      // Workspace refresh is guarded by session id inside loadDir(); do not
      // block session-load completion, draft restore, or model resolution on
      // file-tree IO for users focused on the chat.
      if(_dirP&&typeof _dirP.catch==='function') _dirP.catch(()=>{});
    }
  }

  // Sync context usage indicator from session data
  const _s=S.session;
  if(_s&&typeof _syncCtxIndicator==='function'){
    const u=S.lastUsage||{};
    const _pick=(latest,stored,dflt=0)=>latest!=null?latest:(stored!=null?stored:dflt);
    const _pickPositive=(latest,stored,dflt=0)=>Number(latest)>0?latest:(Number(stored)>0?stored:dflt);
    _syncCtxIndicator({
      input_tokens:      _pick(u.input_tokens,      _s.input_tokens),
      output_tokens:     _pick(u.output_tokens,     _s.output_tokens),
      estimated_cost:    _pick(u.estimated_cost,    _s.estimated_cost),
      cache_read_tokens: _pick(u.cache_read_tokens, _s.cache_read_tokens),
      cache_write_tokens:_pick(u.cache_write_tokens,_s.cache_write_tokens),
      cache_hit_percent: _pick(u.cache_hit_percent, _s.cache_hit_percent, null),
      context_length:    _pickPositive(u.context_length, _s.context_length),
      last_prompt_tokens:_pick(u.last_prompt_tokens,_s.last_prompt_tokens),
      threshold_tokens:  _pick(_s.threshold_tokens,  u.threshold_tokens),
    });
  }
  if(typeof _renderPendingPromptsForActiveSession==='function') _renderPendingPromptsForActiveSession();

  // Restore server-persisted composer draft (synced across clients + survives refresh).
  // Pass sid so _restoreComposerDraft can skip if this session is mid-load (guards
  // against stale writes from slow responses racing to restore the previous draft).
  const _draft = S.session && S.session.composer_draft;
  if (_draft && (typeof _restoreComposerDraft === 'function')) {
    _restoreComposerDraft(_draft, sid, {preserveActiveInput:currentSid===sid&&forceReload});
  }

  // Clear the in-flight session marker now that this load has completed (#1060).
  if (_loadingSessionId === sid) _loadingSessionId = null;

  if(typeof renderSessionArtifacts==='function') renderSessionArtifacts();

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
const _MESSAGING_RAW_SOURCES = new Set(['weixin', 'telegram', 'discord', 'slack', 'email', 'wecom', 'wecom_callback']);
const _MESSAGING_SOURCE_LABELS = {
  weixin: 'WeChat',
  telegram: 'Telegram',
  discord: 'Discord',
  slack: 'Slack',
  email: 'Email',
  wecom: 'WeCom',
  wecom_callback: 'WeCom Callback',
};

function _isMessagingSession(session) {
  if (!session) return false;
  // session_source is set by PR #1294 source normalization
  if (session.session_source === 'messaging') return true;
  // Fallback: check raw_source directly
  const raw = (session.raw_source || session.source_tag || session.source || '').toLowerCase();
  return _MESSAGING_RAW_SOURCES.has(raw);
}

/**
 * Returns true when a session originates from an external channel (CLI bridge,
 * Discord, Telegram, Slack, etc.) and therefore needs a server-side import
 * before the WebUI can read or send messages into it.
 * Covers both legacy CLI sessions and messaging-source sessions.
 */
function _isExternalSession(session) {
  return !!(session && (session.is_cli_session || _isMessagingSession(session)));
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

function _sessionSourceLabel(filter, count) {
  const n = Number(count) || 0;
  return filter === 'cli' ? `CLI sessions (${n})` : `WebUI sessions (${n})`;
}

function _setSessionSourceFilter(filter) {
  const next = filter === 'cli' ? 'cli' : 'webui';
  if (_sessionSourceFilter === next) return;
  _sessionSourceFilter = next;
  _activeProject = null;
  _selectedSessions.clear();
  _sessionSelectMode = false;
  try { localStorage.setItem('hermes-session-source-filter', next); } catch (_e) {}
  renderSessionListFromCache();
}

function _restoreSessionSourceFilter() {
  try {
    const raw = localStorage.getItem('hermes-session-source-filter');
    if (raw === 'cli' || raw === 'webui') _sessionSourceFilter = raw;
  } catch (_e) {}
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
  // Session deletion should also prune per-session tracking maps. Otherwise
  // heavy users accumulate one localStorage entry per deleted session forever,
  // which increases quota pressure and can make future UI persistence fail.
  try { _clearSessionViewedCount(sid); } catch {}
  try { _clearSessionCompletionUnread(sid); } catch {}
  try { _forgetObservedStreamingSession(sid); } catch {}
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
      const resolvedContextLength=data.session.context_length||S.session.context_length||0;
      S.session.context_length=resolvedContextLength;
      S.session.threshold_tokens=data.session.threshold_tokens||0;
      S.session.last_prompt_tokens=data.session.last_prompt_tokens||0;
      S.session._modelResolutionDeferred=false;
      syncTopbar();
      if(typeof _syncCtxIndicator==='function'){
        const u=S.lastUsage||{};
        const _pick=(latest,stored,dflt=0)=>latest!=null?latest:(stored!=null?stored:dflt);
        _syncCtxIndicator({
          input_tokens:_pick(u.input_tokens,S.session.input_tokens),
          output_tokens:_pick(u.output_tokens,S.session.output_tokens),
          estimated_cost:_pick(u.estimated_cost,S.session.estimated_cost),
          cache_read_tokens:_pick(u.cache_read_tokens,S.session.cache_read_tokens),
          cache_write_tokens:_pick(u.cache_write_tokens,S.session.cache_write_tokens),
          cache_hit_percent:_pick(u.cache_hit_percent,S.session.cache_hit_percent,null),
          context_length:resolvedContextLength||u.context_length||0,
          last_prompt_tokens:_pick(u.last_prompt_tokens,S.session.last_prompt_tokens),
          threshold_tokens:data.session.threshold_tokens||0,
        });
      }
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
let _sameSessionForceReloadHint = null;

function _currentLoadedRenderableMessageCount(){
  if(typeof _messageRenderableMessageCount==='function'){
    try{return Math.max(0,Number(_messageRenderableMessageCount())||0);}
    catch(_){}
  }
  let count=0;
  for(const m of (S.messages||[])){
    if(m&&m.role&&m.role!=='tool') count++;
  }
  return count;
}

function _captureSameSessionForceReloadHint(sid){
  const loadedRenderableCount=_currentLoadedRenderableMessageCount();
  const loadedMessageCount=Array.isArray(S.messages)?S.messages.length:0;
  const knownMessageCount=Number(S.session&&S.session.session_id===sid&&S.session.message_count)||loadedMessageCount;
  if(!sid || (loadedRenderableCount<=0 && loadedMessageCount<=0)){
    _sameSessionForceReloadHint=null;
    return;
  }
  _sameSessionForceReloadHint={
    session_id:sid,
    loaded_renderable_count:loadedRenderableCount,
    loaded_message_count:loadedMessageCount,
    message_count:knownMessageCount,
    truncated:!!_messagesTruncated,
  };
}

function _clearSameSessionForceReloadHint(sid){
  if(!_sameSessionForceReloadHint) return;
  if(!sid || _sameSessionForceReloadHint.session_id===sid) _sameSessionForceReloadHint=null;
}

function _messageReloadLimitForSession(sid){
  const hint=_sameSessionForceReloadHint;
  if(hint&&hint.session_id===sid){
    const loadedRenderableCount=Math.max(0,Number(hint.loaded_renderable_count)||0);
    const loadedMessageCount=Math.max(0,Number(hint.loaded_message_count)||0);
    if(loadedRenderableCount>0 || loadedMessageCount>0){
      if(!hint.truncated) return null;
      const previousMessageCount=Math.max(0,Number(hint.message_count)||0);
      const currentMessageCount=Math.max(0,Number(S.session&&S.session.session_id===sid&&S.session.message_count)||0);
      const appendedMessageCount=Math.max(0,currentMessageCount-previousMessageCount);
      return Math.max(_INITIAL_MSG_LIMIT,loadedRenderableCount,loadedMessageCount+appendedMessageCount);
    }
  }
  return _INITIAL_MSG_LIMIT;
}

function _syncToolCallsForLoadedMessages(messages, sessionToolCalls){
  const msgs=Array.isArray(messages)?messages:[];
  // During active streaming, skip — clearing S.toolCalls would lose Activity
  // and the renderMessages fallback is blocked by S.busy=true.
  if(S.busy||S.activeStreamId) return;
  const hasMessageToolMetadata=msgs.some(m=>{
    if(!m) return false;
    const hasTc=Array.isArray(m.tool_calls)&&m.tool_calls.length>0;
    // `_partial_tool_calls` are emitted by interrupted/partial turns and must also
    // anchor rendering to the owning assistant message, so we can reconstruct
    // settled tool cards from the message history when available.
    const hasPartialTc=Array.isArray(m._partial_tool_calls)&&m._partial_tool_calls.length>0;
    const hasTu=Array.isArray(m.content)&&m.content.some(p=>p&&p.type==='tool_use');
    return hasTc||hasPartialTc||hasTu;
  });
  if(!hasMessageToolMetadata&&Array.isArray(sessionToolCalls)&&sessionToolCalls.length){
    S.toolCalls=sessionToolCalls.map(tc=>({...tc,done:true}));
  }else{
    S.toolCalls=[];
  }
}

async function _ensureMessagesLoaded(sid) {
  // Already have messages? (e.g. from INFLIGHT restore path, already set)
  if (S.messages && S.messages.length > 0 && S.messages[0] && S.messages[0].role) {
    _clearSameSessionForceReloadHint(sid);
    return;
  }
  // Fetch session messages with a tail window for fast initial load.
  const reloadLimit = _messageReloadLimitForSession(sid); // defaults to _INITIAL_MSG_LIMIT
  const reloadLimitParam = reloadLimit ? `&msg_limit=${reloadLimit}` : '';
  // expand_renderable=1 is sent ONLY here, on the initial cold load: it tells
  // the server to expand the tail window backward until it holds ~msg_limit
  // *renderable* rows so a tool-heavy session doesn't open showing 1-2 visible
  // messages (#3790). The "Load earlier" path (_loadOlderMessages) deliberately
  // omits it to keep its raw transport cap.
  const expandParam = reloadLimit ? '&expand_renderable=1' : '';
  let data;
  try {
    data = await api(`/api/session?session_id=${encodeURIComponent(sid)}&messages=1&resolve_model=0${reloadLimitParam}${expandParam}`);
  } finally {
    _clearSameSessionForceReloadHint(sid);
  }
  // Guard: api() may have redirected (401) and returned undefined.
  if (!data || !data.session) return;
  _messagesTruncated = !!data.session._messages_truncated;
  _oldestIdx = data.session._messages_offset || 0;
  // #3162: `msgs` is reassigned below by the #3018 ephemeral-field carry-forward,
  // so it must be `let`, not `const`. The `const` form threw a TypeError inside
  // _ensureMessagesLoaded() that surfaced as a "Failed to load conversation messages"
  // toast on every mobile message (SSE/visibility events trigger this reload path
  // more aggressively on mobile).
  let msgs = (data.session.messages || []).filter(m => m && m.role);
  // Skip _syncToolCalls when INFLIGHT exists — the INFLIGHT restore path
  // (loadSession line ~871) will overwrite S.toolCalls from INFLIGHT[sid].toolCalls.
  // Clearing here and then overwriting is wasteful, and if S.busy becomes true
  // before the next render, the fallback can't re-derive from messages.
  if(!(typeof INFLIGHT !== 'undefined' && INFLIGHT && INFLIGHT[sid])){
    _syncToolCallsForLoadedMessages(msgs, data.session.tool_calls);
  }
  clearLiveToolCards();
  // #3018: preserve client-side ephemeral turn fields (_turnUsage, _turnDuration,
  // _turnTps, _gatewayRouting, _statusCard) across the loadSession replace.
  if(typeof window._carryForwardEphemeralTurnFields==='function'){
    // #3306: Prefer the pre-clear snapshot stashed by loadSession() on a
    // force-reload of the active session; S.messages was reset to [] there
    // and would otherwise yield an empty carry-forward.
    const _prev = (Array.isArray(_pendingCarryForwardSnapshot) && _pendingCarryForwardSnapshot.length)
      ? _pendingCarryForwardSnapshot
      : (S.messages || []);
    msgs=window._carryForwardEphemeralTurnFields(_prev, msgs);
    _pendingCarryForwardSnapshot = null;
  }
  if(typeof clearVisibleMessageRowCache==='function') clearVisibleMessageRowCache();
  S.messages = msgs;
  // Expand render window to cover all loaded messages so the next
  // renderMessages() doesn't hide most of them behind a tiny window.
  if(typeof _messageRenderableMessageCount==='function'&&typeof _currentMessageRenderWindowSize==='function'){
    _messageRenderWindowSize=Math.max(_currentMessageRenderWindowSize(), _messageRenderableMessageCount());
  }
  if(S.session&&S.session.session_id===sid){
    S.session.message_count=Number(data.session.message_count || msgs.length);
    S.lastUsage={...(data.session.last_usage||S.lastUsage||{})};
    // Phase 2: the messages=1 response carries the canonical cold-load
    // `todo_state` snapshot, derived server-side from the FULL untruncated
    // message list (api/routes.py + api/todo_state.py). The earlier
    // messages=0 fetch in loadSession() does not include this field —
    // attach_todo_state is gated on `load_messages`. Without applying it
    // here, long sessions whose latest todo write falls outside the
    // _INITIAL_MSG_LIMIT tail would lose the panel on refresh: the
    // legacy reverse-scan in _legacyTodosFromMessages() can only see the
    // tail S.messages, while the authoritative snapshot was already
    // computed by the server and is sitting in this very response.
    // _hydrateTodosFromSession is idempotent and picks newer of
    // cold-load vs INFLIGHT by timestamp, so calling it again here is
    // safe even when an INFLIGHT snapshot was already restored.
    if(data.session.todo_state !== undefined){
      S.session.todo_state = data.session.todo_state;
    }else{
      delete S.session.todo_state;
    }
    if(typeof _hydrateTodosFromSession === 'function'){
      _hydrateTodosFromSession(S.session);
    }
    if(typeof scheduleTodosRefresh === 'function'){
      scheduleTodosRefresh();
    }
    _setSessionViewedCount(sid, Number(S.session.message_count || msgs.length));
    if(typeof syncTopbar==='function') syncTopbar();
  }
}

function _messageComparableText(m){
  if(!m) return '';
  if(typeof msgContent==='function'){
    try{return String(msgContent(m)||'').trim();}
    catch(_){}
  }
  return String(m.content||'').trim();
}

function _stripAttachedFilesMarker(text){
  return String(text||'').replace(/\n\n\[Attached files: [^\]]+\]$/,'').trim();
}

function _sameTranscriptMessage(a,b){
  if(!(a&&b)) return false;
  const role=String(a.role||'');
  if(role!==String(b.role||'')) return false;
  const aText=_messageComparableText(a);
  const bText=_messageComparableText(b);
  if(aText===bText) return true;
  if(role==='user'){
    return _stripAttachedFilesMarker(aText)===_stripAttachedFilesMarker(bText);
  }
  return false;
}

function _currentTurnAssistantText(messages){
  const list=Array.isArray(messages)?messages:[];
  let start=-1;
  for(let i=list.length-1;i>=0;i--){
    if(list[i]&&list[i].role==='user'){start=i;break;}
  }
  const parts=[];
  for(let i=start+1;i<list.length;i++){
    const msg=list[i];
    if(!msg||msg.role!=='assistant'||msg._live) continue;
    const text=_messageComparableText(msg);
    if(text) parts.push(text);
  }
  return parts.join('\n\n').trim();
}

function _compactTranscriptText(text){
  return String(text||'').replace(/\s+/g,' ').trim();
}

function _dropCurrentTurnAssistantMessages(messages){
  const list=Array.isArray(messages)?messages:[];
  let start=-1;
  for(let i=list.length-1;i>=0;i--){
    if(list[i]&&list[i].role==='user'){start=i;break;}
  }
  if(start<0) return list;
  return list.filter((msg,idx)=>idx<=start||!(msg&&msg.role==='assistant'));
}

function _ensureInflightLiveAssistantMessage(inflight){
  if(!inflight) return false;
  const text=String(inflight.lastAssistantText||'').trim();
  const reasoning=String(inflight.lastReasoningText||'').trim();
  if(!text&&!reasoning) return false;
  if(!Array.isArray(inflight.messages)) inflight.messages=[];
  let live=null;
  for(let i=inflight.messages.length-1;i>=0;i--){
    const msg=inflight.messages[i];
    if(msg&&msg.role==='assistant'&&msg._live){live=msg;break;}
  }
  if(live){
    const liveText=_messageComparableText(live);
    if(text&&(!liveText||text.startsWith(liveText)||text.length>liveText.length)){
      live.content=text;
    }
    if(reasoning&&!live.reasoning) live.reasoning=reasoning;
    return true;
  }
  inflight.messages.push({
    role:'assistant',
    content:text,
    reasoning:reasoning||undefined,
    _live:true,
    _ts:Date.now()/1000,
  });
  return true;
}

function _projectInflightMessagesForActivityBursts(inflight){
  const messages=Array.isArray(inflight&&inflight.messages)?inflight.messages:[];
  const anchors=Array.isArray(inflight&&inflight.activityBurstAnchors)?inflight.activityBurstAnchors:[];
  if(!anchors.length) return messages;
  let liveIdx=-1;
  for(let i=messages.length-1;i>=0;i--){
    const msg=messages[i];
    if(msg&&msg.role==='assistant'&&msg._live){liveIdx=i;break;}
  }
  if(liveIdx<0) return messages;
  let liveTailStartIdx=liveIdx;
  while(liveTailStartIdx>0){
    const prev=messages[liveTailStartIdx-1];
    if(!(prev&&prev.role==='assistant'&&prev._live)) break;
    liveTailStartIdx-=1;
  }
  const live=messages[liveIdx];
  const text=_messageComparableText(live);
  if(!text) return messages;
  const priorLiveTexts=messages.slice(liveTailStartIdx,liveIdx)
    .filter(m=>m&&m.role==='assistant'&&m._live)
    .map(m=>_messageComparableText(m))
    .filter(Boolean);
  const liveTailIsAccumulator=priorLiveTexts.length>0&&priorLiveTexts.every(part=>
    _compactTranscriptText(text).includes(_compactTranscriptText(part))
  );
  const replaceStartIdx=liveTailIsAccumulator?liveTailStartIdx:liveIdx;
  if(priorLiveTexts.length&&!liveTailIsAccumulator) return messages;
  const cleanAnchors=anchors
    .map(a=>({id:Number(a&&a.id),textEnd:Number(a&&a.textEnd)}))
    .filter(a=>Number.isFinite(a.id)&&Number.isFinite(a.textEnd)&&a.textEnd>0)
    .sort((a,b)=>a.textEnd-b.textEnd||a.id-b.id);
  const aliasBurstIds=new Map();
  const fallbackBurstId = Number(inflight.currentActivityBurstId||0)||0;
  aliasBurstIds.set(0,fallbackBurstId);
  let lastVisibleBurstId=null;
  let lastVisibleTextEnd=0;
  const visibleAnchors=[];
  for(const anchor of cleanAnchors){
    const end=Math.min(text.length,anchor.textEnd);
    if(end<=lastVisibleTextEnd){
      if(lastVisibleBurstId!==null) aliasBurstIds.set(anchor.id,lastVisibleBurstId);
      continue;
    }
    visibleAnchors.push(anchor);
    lastVisibleBurstId=anchor.id;
    lastVisibleTextEnd=end;
  }

  if(visibleAnchors.length&&Number.isFinite(visibleAnchors[0].id)) aliasBurstIds.set(0,visibleAnchors[0].id);
  if(!visibleAnchors.length){
    const firstVisibleBurstId=Number(cleanAnchors[0]&&cleanAnchors[0].id);
    const fallbackAnchorId=Number.isFinite(firstVisibleBurstId)?firstVisibleBurstId:fallbackBurstId;
    if(fallbackAnchorId!==fallbackBurstId) aliasBurstIds.set(0,fallbackAnchorId);
    const projected=[{...live,content:text,_activityBurstId:fallbackAnchorId}];

    const baselineSeq=Number(inflight.currentLiveSegmentSeq);
    const existingSeqs=messages
      .filter(m=>m&&m._live&&Number.isFinite(Number(m._liveSegmentSeq)))
      .map(m=>Number(m._liveSegmentSeq));
    const baseFromMessages=existingSeqs.length
      ? existingSeqs.reduce((acc,n)=>Math.max(acc,n),-Infinity)
      : 0;
    const firstSeq=(Number.isFinite(baselineSeq)&&baselineSeq>0)
      ? baselineSeq
      : (Number.isFinite(baseFromMessages)&&baseFromMessages>0)
        ? baseFromMessages
        : 1;
    projected.forEach((seg,i)=>{
      seg._liveSegmentSeq=i===0?firstSeq:1+i;
    });
    if(Array.isArray(inflight.toolCalls)){
      const segmentSeqByBurstId=new Map();
      segmentSeqByBurstId.set(String(fallbackAnchorId),firstSeq);
      projected.forEach(seg=>{
        const bid=Number(seg&&seg._activityBurstId);
        const seq=Number(seg&&seg._liveSegmentSeq);
        if(!Number.isFinite(bid)||!Number.isFinite(seq)) return;
        const key=String(bid);
        const current=segmentSeqByBurstId.get(key);
        if(current===undefined||seq>current) segmentSeqByBurstId.set(key,seq);
      });

      const validSeqs=new Set(segmentSeqByBurstId.values());
      const canonicalBurstId=(value)=>{
        const bid=Number(value);
        if(!Number.isFinite(bid)) return null;
        if(aliasBurstIds.has(bid)) return aliasBurstIds.get(bid);
        return bid;
      };

      inflight.toolCalls.forEach(tc=>{
        if(!tc) return;
        if(tc.activityBurstId!==undefined&&tc.activityBurstId!==null){
          const current=Number(tc.activityBurstId);
          if(aliasBurstIds.has(current)) tc.activityBurstId=aliasBurstIds.get(current);
        }
        const segSeq=Number(tc.activitySegmentSeq);
        if(Number.isFinite(segSeq)&&validSeqs.has(segSeq)) return;
        const canonical=canonicalBurstId(tc.activityBurstId);
        if(!Number.isFinite(canonical)){
          if(Number.isFinite(segSeq)) tc.activitySegmentSeq=undefined;
          return;
        }
        const mappedSeq=segmentSeqByBurstId.get(String(canonical));
        if(Number.isFinite(mappedSeq)) tc.activitySegmentSeq=mappedSeq;
        else if(Number.isFinite(segSeq)) tc.activitySegmentSeq=undefined;
      });
    }
    return [...messages.slice(0,replaceStartIdx),...projected,...messages.slice(liveIdx+1)];
  }
  const projected=[];
  let prev=0;
  for(let i=0;i<visibleAnchors.length;i++){
    const anchor=visibleAnchors[i];
    const end=Math.max(prev,Math.min(text.length,anchor.textEnd));
    const part=text.slice(prev,end).trim();
    if(part) projected.push({...live,content:part,_activityBurstId:anchor.id});
    else{
      const fallbackAnchor = visibleAnchors[i+1] || visibleAnchors[i-1];
      if(fallbackAnchor && Number.isFinite(anchor.id)&&Number.isFinite(fallbackAnchor.id)){
        aliasBurstIds.set(anchor.id,fallbackAnchor.id);
      }
    }
    prev=end;
  }
  const tail=text.slice(prev).trim();
  if(tail) projected.push({...live,content:tail,_activityBurstId:Number(inflight.currentActivityBurstId||0)||0});
  if(!projected.length) return messages;

  const baselineSeq=Number(inflight.currentLiveSegmentSeq);
  const existingSeqs=messages
    .filter(m=>m&&m._live&&Number.isFinite(Number(m._liveSegmentSeq)))
    .map(m=>Number(m._liveSegmentSeq));
  const baseFromMessages=existingSeqs.length
    ? existingSeqs.reduce((acc,n)=>Math.max(acc,n),-Infinity)
    : 0;
  const endSeq=(Number.isFinite(baselineSeq)&&baselineSeq>0)
    ? baselineSeq
    : (Number.isFinite(baseFromMessages)&&baseFromMessages>0)
      ? baseFromMessages
      : projected.length;
  let firstSeq=endSeq-projected.length+1;
  if(!Number.isFinite(firstSeq)||firstSeq<1) firstSeq=1;
  projected.forEach((seg,i)=>{
    const seq=firstSeq+i;
    seg._liveSegmentSeq=seq;
  });
  if(Number.isFinite(firstSeq) && projected.length){
    inflight.currentLiveSegmentSeq=projected[projected.length-1]._liveSegmentSeq;
  }

  const segmentSeqByBurstId=new Map();
  projected.forEach(seg=>{
    const bid=Number(seg&&seg._activityBurstId);
    const seq=Number(seg&&seg._liveSegmentSeq);
    if(!Number.isFinite(bid)||!Number.isFinite(seq)) return;
    const key=String(bid);
    const current=segmentSeqByBurstId.get(key);
    if(current===undefined||seq>current) segmentSeqByBurstId.set(key,seq);
  });

  const canonicalBurstId = (value)=>{
    const bid=Number(value);
    if(!Number.isFinite(bid)) return null;
    if(aliasBurstIds.has(bid)) return aliasBurstIds.get(bid);
    return bid;
  };

  const validSeqs=new Set(segmentSeqByBurstId.values());

  if(Array.isArray(inflight.toolCalls)){
    inflight.toolCalls.forEach(tc=>{
      if(!tc) return;
      if(tc.activityBurstId!==undefined&&tc.activityBurstId!==null){
        const current=Number(tc.activityBurstId);
        if(aliasBurstIds.has(current)) tc.activityBurstId=aliasBurstIds.get(current);
      }
      const segSeq=Number(tc.activitySegmentSeq);
      if(Number.isFinite(segSeq)&&validSeqs.has(segSeq)) return;
      const canonical=canonicalBurstId(tc.activityBurstId);
      if(!Number.isFinite(canonical)){
        if(Number.isFinite(segSeq)) tc.activitySegmentSeq=undefined;
        return;
      }
      const mappedSeq=segmentSeqByBurstId.get(String(canonical));
      if(Number.isFinite(mappedSeq)) tc.activitySegmentSeq=mappedSeq;
      else if(Number.isFinite(segSeq)) tc.activitySegmentSeq=undefined;
    });
  }
  return [...messages.slice(0,replaceStartIdx),...projected,...messages.slice(liveIdx+1)];
}

function _prepareRunningLiveTail(baseMessages,inflightMessages){
  const inflight=Array.isArray(inflightMessages)?inflightMessages:[];
  const liveMessages=inflight.filter(m=>m&&m.role==='assistant'&&m._live);
  if(liveMessages.length>1) return liveMessages.some(m=>!!_messageComparableText(m));
  const live=liveMessages[0]||null;
  if(!live) return false;
  const liveText=_messageComparableText(live);
  const persistedText=_currentTurnAssistantText(baseMessages);
  if(persistedText){
    const compactPersisted=_compactTranscriptText(persistedText);
    const compactLive=_compactTranscriptText(liveText);
    if(!liveText || persistedText.startsWith(liveText)){
      live.content=persistedText;
    }else if(liveText.startsWith(persistedText)){
      const extra=liveText.slice(persistedText.length).trim();
      if(extra&&compactPersisted.includes(_compactTranscriptText(extra))){
        live.content=persistedText;
      }
    }else if(compactPersisted===compactLive){
      live.content=persistedText;
    }
  }
  return !!_messageComparableText(live);
}

function _mergeInflightTailMessages(baseMessages, inflightMessages){
  const base=Array.isArray(baseMessages)?baseMessages:[];
  const inflight=Array.isArray(inflightMessages)?inflightMessages:[];
  let firstLiveIdx=-1;
  for(let i=0;i<inflight.length;i++){
    if(inflight[i]&&inflight[i]._live){firstLiveIdx=i;break;}
  }
  if(firstLiveIdx<0) return base;
  let start=firstLiveIdx;
  if(firstLiveIdx>0&&inflight[firstLiveIdx-1]&&inflight[firstLiveIdx-1].role==='user') start=firstLiveIdx-1;
  const tail=inflight.slice(start).filter(m=>m&&m.role);
  const merged=[...base];
  for(const msg of tail){
    let candidate=msg;
    if(!candidate) continue;
    const duplicate=merged.slice(-Math.max(5,tail.length+2)).some(existing=>_sameTranscriptMessage(existing,candidate));
    if(!duplicate) merged.push(candidate);
  }
  return merged;
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
    // Ask the server for a larger authoritative tail window instead of a
    // separate msg_before page. The same /api/session contract handles both —
    // post-#2716 the backend always runs the full append-only merge, so a
    // larger msg_limit on the same call produces the same merged transcript
    // we'd get by stitching pages, but without client-side index bookkeeping.
    // Cumulative growth: each "load more" asks for currentLoaded + 30, and the
    // newly exposed head is what we expose to the user.
    const requestedLimit = Math.max(_INITIAL_MSG_LIMIT, (S.messages || []).length + _INITIAL_MSG_LIMIT);
    const data = await api(`/api/session?session_id=${encodeURIComponent(sid)}&messages=1&resolve_model=0&msg_limit=${requestedLimit}`);
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
    let responseSession = data.session;
    let expandedMsgs = (responseSession.messages || []).filter(m => m && m.role);
    const currentMsgs = (S.messages || []).filter(m => m && m.role);
    const currentLen = currentMsgs.length;
    // Suffix-continuity check: the cumulative tail is only safe to wholesale-
    // replace when our currently-displayed messages are still its suffix. If
    // the server appended new messages (or merge filtered something) while we
    // were awaiting, the suffix won't line up — fall back to the legacy
    // msg_before page so we never drop visible older messages on the floor.
    let tailMatches = expandedMsgs.length >= currentLen;
    if (tailMatches && currentLen > 0) {
      const start = expandedMsgs.length - currentLen;
      for (let i = 0; i < currentLen; i++) {
        if (!_sameTranscriptMessage(expandedMsgs[start + i], currentMsgs[i])) {
          tailMatches = false;
          break;
        }
      }
    }
    let olderCount = Math.max(0, expandedMsgs.length - currentLen);
    let olderMsgs = expandedMsgs.slice(0, olderCount);
    let nextMessages = expandedMsgs;
    if (!tailMatches) {
      // Race fallback: keep the legacy index-page request as the
      // correctness-preserving alternative. Same guards reapplied because
      // we just awaited again.
      const fallback = await api(`/api/session?session_id=${encodeURIComponent(sid)}&messages=1&resolve_model=0&msg_before=${_oldestIdx}&msg_limit=${_INITIAL_MSG_LIMIT}`);
      if (!fallback || !fallback.session) { _loadingOlder = false; return; }
      if (!S.session || S.session.session_id !== sid) return;
      if (_loadingSessionId !== null && _loadingSessionId !== sid) return;
      if (_messagesGeneration !== startGeneration) return;
      responseSession = fallback.session;
      olderMsgs = (responseSession.messages || []).filter(m => m && m.role);
      nextMessages = [...olderMsgs, ...S.messages];
    }
    if (!olderMsgs.length) { _messagesTruncated = !!responseSession._messages_truncated; return; }
    // Replace with the larger tail window and preserve scroll as if older
    // messages were prepended. When the suffix check fails, nextMessages
    // already encodes the legacy prepend fallback so the visible behavior
    // matches the old msg_before page path exactly.
    // Use $('messages') — the scrollable container (#msgInner is not scrollable).
    const container = $('messages');
    const prevScrollH = container ? container.scrollHeight : 0;
    // Carry forward ephemeral turn fields (_turnUsage/_turnDuration/_turnTps/
    // _gatewayRouting/_statusCard) before the wholesale replace so the badge
    // does not briefly appear and disappear during older-message expansion.
    if (typeof window._carryForwardEphemeralTurnFields === 'function') {
      nextMessages = window._carryForwardEphemeralTurnFields(S.messages || [], nextMessages);
    }
    S.messages = nextMessages;
    _syncToolCallsForLoadedMessages(nextMessages, responseSession.tool_calls);
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
    _messagesTruncated = !!responseSession._messages_truncated;
    _oldestIdx = responseSession._messages_offset || 0;
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
    // #3306: Same ephemeral-field carry-forward as _ensureMessagesLoaded.
    // Loading older messages also does a wholesale replace of S.messages
    // and would otherwise drop _turnUsage/_turnDuration/_turnTps/
    // _gatewayRouting/_statusCard on the existing turns.
    let _msgsToAssign = msgs;
    if (typeof window._carryForwardEphemeralTurnFields === 'function') {
      _msgsToAssign = window._carryForwardEphemeralTurnFields(S.messages || [], msgs);
    }
    S.messages = _msgsToAssign;
    _messagesTruncated = false;
    _oldestIdx = 0;
    _syncToolCallsForLoadedMessages(msgs, data.session.tool_calls);
    if (S.session && S.session.session_id === sid) {
      S.session.message_count = Number(data.session.message_count || msgs.length);
    }
  } finally {
    _loadingOlder = false;
  }
}

let _allSessions = [];  // cached for search filter
let _sessionAttentionSoundPrimed = false;
const _sessionAttentionSoundState = new Map();
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
let _sessionSourceFilter = 'webui';  // 'webui' keeps WebUI chats separate from read-only CLI sessions
_restoreSessionSourceFilter();
let _sessionActionMenu = null;
let _sessionActionAnchor = null;
let _sessionActionSessionId = null;
const _expandedChildSessionKeys = new Set();
const _expandedLineageKeys = new Set();
const _lineageReportCache = new Map();
const _lineageReportInflight = new Map();
let _lineageReportCacheGeneration = 0;
let _sessionVisibleSidebarIds = [];
let _pendingSessionReflowPositions = null;
const _optimisticallyRemovedSessionIds = new Set();
const _sessionSwipeReturnOffsets = new Map();

function _captureSessionReflowPositions(){
  const list=$('sessionList');
  if(!list) return null;
  const positions=new Map();
  list.querySelectorAll('.session-item[data-sid]').forEach(row=>{
    positions.set(row.dataset.sid,row.getBoundingClientRect().top);
  });
  return positions;
}

function _waitForSessionMotion(ms){
  return new Promise(resolve=>setTimeout(resolve,ms));
}

function _playSessionRowsReflowFromPositions(before, timeoutMs, prefersReducedMotion){
  if(!before||!before.size) return;
  if(prefersReducedMotion&&prefersReducedMotion()) return;
  const list=$('sessionList');
  if(!list) return;
  const movingRows=[];
  list.querySelectorAll('.session-item[data-sid]').forEach(row=>{
    const oldTop=before.get(row.dataset.sid);
    if(oldTop===undefined) return;
    const delta=oldTop-row.getBoundingClientRect().top;
    if(Math.abs(delta)<1) return;
    movingRows.push({row,delta});
  });
  if(!movingRows.length) return;
  movingRows.forEach(({row,delta})=>{
    row.style.transition='none';
    row.style.setProperty('--session-reflow-offset',delta+'px');
    row.classList.add('session-reflowing');
  });
  list.getBoundingClientRect();
  movingRows.forEach(({row})=>{
    let reflowCleared=false;
    const clearReflow=()=>{
      if(reflowCleared) return;
      reflowCleared=true;
      row.classList.remove('session-reflowing');
      row.style.removeProperty('--session-reflow-offset');
      row.removeEventListener('transitionend',onReflowEnd);
    };
    const onReflowEnd=(event)=>{
      if(event.propertyName==='transform') clearReflow();
    };
    row.addEventListener('transitionend',onReflowEnd);
    row.style.removeProperty('transition');
    requestAnimationFrame(()=>requestAnimationFrame(()=>{
      if(!reflowCleared) row.style.setProperty('--session-reflow-offset','0px');
    }));
    setTimeout(clearReflow,timeoutMs);
  });
}

function _sessionPrefersReducedMotion(){
  try{
    return Boolean(window.matchMedia&&window.matchMedia('(prefers-reduced-motion: reduce)').matches);
  }catch(_){
    return false;
  }
}

function _makeSessionSwipeAffordance(side, icon, label){
  const affordance=document.createElement('div');
  affordance.className='session-swipe-affordance session-swipe-affordance-'+side;
  affordance.setAttribute('aria-hidden','true');
  const stack=document.createElement('span');
  stack.className='session-swipe-action-stack';
  const badge=document.createElement('span');
  badge.className='session-swipe-badge';
  badge.innerHTML=li(icon,18);
  const text=document.createElement('span');
  text.className='session-swipe-label';
  text.textContent=label;
  stack.append(badge,text);
  affordance.append(stack);
  return affordance;
}
const SESSION_VIRTUAL_ROW_HEIGHT = 52;
const SESSION_VIRTUAL_BUFFER_ROWS = 12;
const SESSION_VIRTUAL_THRESHOLD_ROWS = 80;
let _sessionVirtualScrollList = null;
let _sessionVirtualScrollRaf = 0;

function _sessionSnapshotById(sid){
  if(!sid)return null;
  if(S.session&&S.session.session_id===sid) return S.session;
  return (_allSessions||[]).find(s=>s&&s.session_id===sid)||null;
}
function _pinnedSessionCount(){
  return (_allSessions||[]).filter(s=>s&&s.pinned&&!s.archived).length;
}
function _getPinnedSessionsLimit(){
  const limit=parseInt(window._pinnedSessionsLimit||3,10);
  return (Number.isFinite(limit)&&limit>0)?limit:3;
}
function _pinnedSessionsLimitMessage(){
  const limit=_getPinnedSessionsLimit();
  return `Only ${limit} conversations can be pinned. Unpin one before pinning another.`;
}
function _worktreeSessionCount(ids){
  return (ids||[]).reduce((count,sid)=>{
    const session=_sessionSnapshotById(sid);
    return count+(session&&session.worktree_path?1:0);
  },0);
}
function _sessionResponseRetainsWorktree(response, session){
  if(response&&typeof response.worktree_retained==='boolean') return response.worktree_retained;
  return !!(session&&session.worktree_path);
}
function _worktreeResponseCount(results){
  return (results||[]).reduce((count,result)=>{
    return count+(_sessionResponseRetainsWorktree(result&&result.response,result&&result.session)?1:0);
  },0);
}
function _sessionArchiveDescription(session){
  return session&&session.worktree_path?t('session_archive_worktree_desc'):t('session_archive_desc');
}
function _sessionArchiveToast(response, session){
  return _sessionResponseRetainsWorktree(response,session)?t('session_archived_worktree'):t('session_archived');
}
function _sessionDeleteDescription(session){
  return session&&session.worktree_path?t('session_delete_worktree_desc'):t('session_delete_desc');
}
function _optimisticallyArchiveSessionInList(sid, archived){
  if(!sid||!Array.isArray(_allSessions)) return;
  let changed=false;
  _allSessions=_allSessions.map(s=>{
    if(!s||s.session_id!==sid) return s;
    changed=true;
    return {...s,archived:!!archived};
  });
  if(changed) renderSessionListFromCache();
}
function _optimisticallyRemoveSessionFromList(sid){
  if(!sid||!Array.isArray(_allSessions)) return;
  const before=_allSessions.length;
  _allSessions=_allSessions.filter(s=>!s||s.session_id!==sid);
  if(_selectedSessions&&_selectedSessions.has(sid)) _selectedSessions.delete(sid);
  if(typeof _dropStaleOptimisticSessionRow==='function') _dropStaleOptimisticSessionRow(sid);
  if(_allSessions.length!==before) renderSessionListFromCache();
}

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
    return qs.get('session')||qs.get('session_id')||null;
  }catch(_e){return null;}
}
function _appRootPath(){
  try{
    const base = new URL(document.baseURI||window.location.origin+'/', window.location.origin);
    return base.pathname || '/';
  }catch(_e){return '/';}
}
function _sessionUrlForSid(sid){
  const encoded=encodeURIComponent(sid);
  let base;
  try{base=new URL(`session/${encoded}`, document.baseURI||window.location.origin+'/');}
  catch(_e){base=new URL(`/session/${encoded}`, window.location.origin);}
  try{
    const current=new URL(window.location.href);
    current.searchParams.delete('session');
    current.searchParams.delete('session_id');
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
    const wtCount=_worktreeSessionCount(ids);
    const sessionsById=new Map(ids.map(sid=>[sid,_sessionSnapshotById(sid)]));
    const ok=await showConfirmDialog({
      message:wtCount?t('session_batch_archive_worktree_confirm',ids.length,wtCount):t('session_batch_archive_confirm',ids.length),
      confirmLabel:t('session_batch_archive'),
      danger:true
    });
    if(!ok)return;
    try{
      const results=await Promise.all(ids.map(async sid=>{
        const response=await api('/api/session/archive',{method:'POST',body:JSON.stringify({session_id:sid,archived:true})});
        return {response,session:sessionsById.get(sid)||null};
      }));
      const retainedCount=_worktreeResponseCount(results);
      showToast(retainedCount?t('session_archived_worktree'):t('session_archived'));exitSessionSelectMode();await renderSessionList();
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
    const wtCount=_worktreeSessionCount(ids);
    const sessionsById=new Map(ids.map(sid=>[sid,_sessionSnapshotById(sid)]));
    const ok=await showConfirmDialog({
      message:wtCount?t('session_batch_delete_worktree_confirm',ids.length,wtCount):t('session_batch_delete_confirm',ids.length),
      confirmLabel:t('delete_title'),
      danger:true
    });
    if(!ok)return;
    try{
      const results=await Promise.all(ids.map(async sid=>{
        const response=await api('/api/session/delete',{method:'POST',body:JSON.stringify({session_id:sid})});
        return {response,session:sessionsById.get(sid)||null};
      }));
      const retainedCount=_worktreeResponseCount(results);
      ids.forEach(_clearHandoffStorageForSession);
      if(S.session&&ids.includes(S.session.session_id)){
        S.session=null;S.messages=[];S.entries=[];localStorage.removeItem('hermes-webui-session');
        if(typeof _hydrateTodosFromSession==='function') _hydrateTodosFromSession(null);
        const remaining=await api('/api/sessions');
        if(remaining.sessions&&remaining.sessions.length){await loadSession(remaining.sessions[0].session_id);}
        else{$('msgInner').innerHTML='';$('emptyState').style.display='';}
      }
      showToast((retainedCount?t('session_deleted_worktree'):t('session_delete'))+' ('+ids.length+')');exitSessionSelectMode();await renderSessionList();
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
    if(_sessionActionAnchor.classList&&_sessionActionAnchor.classList.contains('session-actions-trigger')){
      _sessionActionAnchor.classList.remove('active');
    }
    const row=_sessionActionAnchor.closest('.session-item');
    if(row) row.classList.remove('menu-open','long-pressing');
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
  // Reset any prior clamp so we measure the menu's natural height.
  _sessionActionMenu.style.maxHeight='';
  const menuH=_sessionActionMenu.offsetHeight || 0;
  const margin=8;
  const maxAvail=window.innerHeight-margin*2;
  let top=rect.bottom+6;
  // Prefer flipping above the row when the menu would overflow the bottom and
  // there's room above.
  if(top+menuH>window.innerHeight-margin && rect.top>menuH+12){
    top=rect.top-menuH-6;
  }
  // If the menu is taller than the viewport, or still overflows after the flip
  // attempt (e.g. a top-anchored row with a tall menu and no room above), cap
  // its height to the viewport and let it scroll instead of clipping off-screen.
  if(menuH>maxAvail){
    _sessionActionMenu.style.maxHeight=maxAvail+'px';
    top=margin;
  } else {
    // Clamp vertically so the whole menu stays on-screen at both edges.
    if(top+menuH>window.innerHeight-margin) top=window.innerHeight-margin-menuH;
    if(top<margin) top=margin;
  }
  _sessionActionMenu.style.top=top+'px';
}

function _buildSessionAction(label, meta, icon, onSelect, extraClass=''){
  const opt=document.createElement('button');
  opt.type='button';
  opt.className='ws-opt session-action-opt'+(extraClass?` ${extraClass}`:'');
  // Compact context-menu shape (#3223 redesign, Nathan 2026-06-01): show only
  // icon + label, matching VS Code / browser / ChatGPT conversation menus. The
  // descriptive `meta` is preserved as a hover tooltip (title=) so the
  // information stays discoverable without consuming permanent vertical space —
  // this also keeps the menu short enough to avoid viewport clipping.
  if(meta) opt.title=meta;
  opt.innerHTML=
    `<span class="ws-opt-action">`
      + `<span class="ws-opt-icon">${icon}</span>`
      + `<span class="session-action-copy">`
        + `<span class="ws-opt-name">${esc(label)}</span>`
      + `</span>`
    + `</span>`;
  opt.onclick=async(e)=>{
    e.preventDefault();
    e.stopPropagation();
    await onSelect();
  };
  return opt;
}

function _sessionMarkdownLabel(session){
  const sid=session&&session.session_id?String(session.session_id):'';
  const title=String((session&&(session.title||session.name))||'Conversation').replace(/\s+/g,' ').trim()||'Conversation';
  const shortSid=sid?sid.slice(0,12):'';
  const label=shortSid?`${title} (${shortSid})`:title;
  return label.replace(/([\\\[\]])/g,'\\$1').slice(0,120);
}

function _sessionMarkdownUrlSid(sid){
  return encodeURIComponent(String(sid||'')).replace(/[()]/g, ch => ch==='('?'%28':'%29');
}

function _sessionInternalReferenceForSession(session){
  const sid=session&&session.session_id;
  if(!sid) return '';
  return `[${_sessionMarkdownLabel(session)}](session://${_sessionMarkdownUrlSid(sid)})`;
}

async function _copyTextToClipboard(text){
  if(navigator&&navigator.clipboard&&typeof navigator.clipboard.writeText==='function'){
    await navigator.clipboard.writeText(text);
    return true;
  }
  const ta=document.createElement('textarea');
  ta.value=text;
  ta.setAttribute('readonly','');
  ta.style.position='fixed';
  ta.style.left='-9999px';
  ta.style.top='0';
  document.body.appendChild(ta);
  ta.select();
  try{return document.execCommand('copy');}
  finally{ta.remove();}
}

async function _copySessionLink(session){
  const sid=session&&session.session_id;
  if(!sid) return;
  const ref=_sessionInternalReferenceForSession(session);
  try{
    await _copyTextToClipboard(ref);
    showToast(t('session_link_copied'));
  }catch(err){
    showToast(t('session_link_copy_failed')+(err&&err.message?err.message:err));
  }
}

function _mountSessionActionMenu(menu, session, anchorEl){
  document.body.appendChild(menu);
  _sessionActionMenu = menu;
  _sessionActionAnchor = anchorEl;
  _sessionActionSessionId = session.session_id;
  if(anchorEl.classList&&anchorEl.classList.contains('session-actions-trigger')) anchorEl.classList.add('active');
  const row=anchorEl.closest('.session-item');
  if(row) row.classList.add('menu-open');
  _positionSessionActionMenu(anchorEl);
  _playSessionActionMenuEntrance(menu);
}

function _appendSessionCopyLinkAction(menu, session){
  menu.appendChild(_buildSessionAction(
    t('session_copy_link'),
    t('session_copy_link_desc'),
    ICONS.link,
    async()=>{
      closeSessionActionMenu();
      await _copySessionLink(session);
    }
  ));
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

function _playSessionActionMenuEntrance(menu){
  if(!menu) return;
  const reduce=_sessionPrefersReducedMotion();
  if(reduce) return;
  if(typeof menu.animate==='function'){
    try{
      const anim=menu.animate(
        [
          {opacity:0, transform:'translate3d(0,-4px,0) scale(.985)'},
          {opacity:1, transform:'translate3d(0,0,0) scale(1)'}
        ],
        {duration:450, easing:'cubic-bezier(.2,.8,.2,1)'}
      );
      if(anim&&anim.finished) anim.finished.catch(()=>{});
      return;
    }catch(_){}
  }
  menu.classList.add('open-animated');
}

async function _archiveSession(session, archived=true, beforeListRender=null){
  if(_isReadOnlySession(session)){ if(typeof showToast==='function') showToast('Read-only imported sessions cannot be modified.',3000); return false; }
  const reflowPositions=_captureSessionReflowPositions();
  const renderHold=beforeListRender?Promise.resolve().then(beforeListRender):null;
  try{
    const response=await api('/api/session/archive',{method:'POST',body:JSON.stringify({session_id:session.session_id,archived})});
    session.archived=archived;
    const cached=(_allSessions||[]).find(s=>s&&s.session_id===session.session_id);
    if(cached) cached.archived=archived;
    if(S.session&&S.session.session_id===session.session_id) S.session.archived=archived;
    showToast(session.archived?_sessionArchiveToast(response,session):t('session_restored'));
    if(renderHold) await renderHold;
    if(_showArchived&&!_sessionPrefersReducedMotion()) _sessionSwipeReturnOffsets.set(session.session_id,'0px');
    _pendingSessionReflowPositions=reflowPositions;
    renderSessionListFromCache();
    void renderSessionList();
    return true;
  }catch(err){if(renderHold) await renderHold.catch(()=>{});_pendingSessionReflowPositions=null;showToast(t('session_archive_failed')+err.message);return false;}
}

function _openSessionActionMenu(session, anchorEl){
  const isReadOnly = _isReadOnlySession(session);
  if(_sessionActionMenu && _sessionActionSessionId===session.session_id && _sessionActionAnchor===anchorEl){
    closeSessionActionMenu();
    return;
  }
  closeSessionActionMenu();
  const isMessagingSession = _isMessagingSession(session);
  const isCliSession = _isCliSession(session);
  const isExternalSession = isMessagingSession || isCliSession;
  const menu=document.createElement('div');
  menu.className='session-action-menu';
  _appendSessionCopyLinkAction(menu, session);
  if(isReadOnly){
    _mountSessionActionMenu(menu, session, anchorEl);
    return;
  }
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
      }catch(err){
        showToast(t('session_pin_failed')+err.message);
        await renderSessionList();
      }
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
    session.archived?t('session_restore_desc'):_sessionArchiveDescription(session),
    session.archived?ICONS.unarchive:ICONS.archive,
    async()=>{
      closeSessionActionMenu();
      await _archiveSession(session,!session.archived);
    }
  ));
  if(isExternalSession && !session.archived){
    menu.appendChild(_buildSessionAction(
      t('session_hide_external'),
      t('session_hide_external_desc'),
      ICONS.archive,
      async()=>{
        closeSessionActionMenu();
        try{
          await api('/api/session/archive',{method:'POST',body:JSON.stringify({session_id:session.session_id,archived:true})});
          _optimisticallyArchiveSessionInList(session.session_id,true);
          session.archived=true;
          if(S.session&&S.session.session_id===session.session_id) S.session.archived=true;
          void renderSessionList();
          showToast(t('session_hidden'));
        }catch(err){showToast(t('session_archive_failed')+err.message);}
      }
    ));
  }
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
  // Title regeneration matches the backend guard (api/routes.py rejects
  // read_only OR is_imported with 403). read_only sessions already bailed at
  // the isReadOnly early-return above; skip imported sessions here so the
  // action is hidden rather than failing with a 403 toast. This keeps the
  // is_imported gate scoped to regenerate instead of broadening the shared
  // _isReadOnlySession() helper (which gates rename/pin/archive/etc.).
  if(!session.is_imported){
    menu.appendChild(_buildSessionAction(
      t('session_title_regenerate'),
      t('session_title_regenerate_desc'),
      ICONS.spark,
      async()=>{
        closeSessionActionMenu();
        try{
          if(typeof showToast==='function') showToast(t('session_title_regenerating'), 1600);
          const response=await api('/api/session/title/regenerate',{method:'POST',body:JSON.stringify({session_id:session.session_id})});
          const nextTitle=(response&&response.title)||(response&&response.session&&response.session.title)||'';
          if(nextTitle){
            session.title=nextTitle;
            const cached=(_allSessions||[]).find(item=>item&&item.session_id===session.session_id);
            if(cached) cached.title=nextTitle;
            if(S.session&&S.session.session_id===session.session_id){S.session.title=nextTitle;syncTopbar();}
            renderSessionListFromCache();
          }
          if(typeof showToast==='function') showToast(t('session_title_regenerated', nextTitle||t('untitled')), 2400);
        }catch(err){
          const msg=t('session_title_regenerate_failed')+(err&&err.message?err.message:String(err));
          setStatus(msg);
          if(typeof showToast==='function') showToast(msg,3000,'error');
        }
      }
    ));
  }
  if(!isExternalSession){
    if(session.worktree_path){
      menu.appendChild(_buildSessionAction(
        t('session_worktree_remove'),
        t('session_worktree_remove_desc', session.worktree_path),
        ICONS.trash,
        async()=>{
          closeSessionActionMenu();
          await removeWorktree(session);
        },
        'danger'
      ));
    }
    menu.appendChild(_buildSessionAction(
      t('session_delete'),
      _sessionDeleteDescription(session),
      ICONS.trash,
      async()=>{
        closeSessionActionMenu();
        await deleteSession(session.session_id);
      },
      'danger'
    ));
  }
  _mountSessionActionMenu(menu, session, anchorEl);
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
let _renderSessionListInFlight = null;
let _renderSessionListQueuedRequest = null;
let _sessionListRefreshAnimationPending = false;
let _sessionListFirstRenderAnimated = false;
let _sessionListEnterAllAnimationPending = false;

function animateNextSessionListRefresh(options={}){
  _sessionListRefreshAnimationPending = true;
  if(options&&options.enterAll) _sessionListEnterAllAnimationPending = true;
}

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

function _shouldKeepLocalOnlyOptimisticSessionRow(local){
  if(!_isOptimisticFirstTurnSessionRow(local)) return false;
  const sid=local.session_id;
  if(typeof _sendInProgress!=='undefined'&&_sendInProgress&&sid===_sendInProgressSid) return true;
  const activeSid=S&&S.session&&S.session.session_id;
  const isActive=Boolean(activeSid&&activeSid===sid);
  const hasRuntimeConfirmation=Boolean(local.active_stream_id||local.pending_user_message||local.pending_started_at);
  if(isActive&&S.busy&&hasRuntimeConfirmation) return true;
  const localTs=Number(local.last_message_at||local.updated_at||0);
  const ageMs=localTs>0?Date.now()-(localTs*1000):Infinity;
  return Boolean(isActive&&S.busy&&ageMs>=0&&ageMs<5000);
}

function _dropStaleOptimisticSessionRow(sid){
  if(!sid) return;
  if(INFLIGHT&&INFLIGHT[sid]){
    delete INFLIGHT[sid];
    if(typeof clearInflightState==='function') clearInflightState(sid);
  }
  if(typeof _sessionStreamingById!=='undefined'&&_sessionStreamingById&&typeof _sessionStreamingById.set==='function'){
    _sessionStreamingById.set(sid,false);
  }
  if(typeof _forgetObservedStreamingSession==='function') _forgetObservedStreamingSession(sid);
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
      const fetchedIsServerIdle=_isServerIdleSessionRow(fetched);
      const keepLocalOptimistic=fetchedIsServerIdle?false:_shouldKeepLocalOnlyOptimisticSessionRow(local);
      const localCount=Number(local.message_count||0);
      const fetchedCount=Number(fetched.message_count||0);
      const localTs=Number(local.last_message_at||local.updated_at||0);
      const fetchedTs=Number(fetched.last_message_at||fetched.updated_at||0);
      if(!keepLocalOptimistic&&typeof _dropStaleOptimisticSessionRow==='function') _dropStaleOptimisticSessionRow(sid);
      merged[idx]={
        ...local,
        ...fetched,
        title:keepLocalOptimistic?(local.title||fetched.title):fetched.title,
        message_count:keepLocalOptimistic?Math.max(localCount,fetchedCount):fetchedCount,
        last_message_at:keepLocalOptimistic?Math.max(localTs,fetchedTs):fetchedTs,
        updated_at:keepLocalOptimistic?Math.max(Number(local.updated_at||0),Number(fetched.updated_at||0),localTs,fetchedTs):Number(fetched.updated_at||fetchedTs||0),
        active_stream_id:fetchedIsServerIdle?null:(keepLocalOptimistic?(fetched.active_stream_id||local.active_stream_id||null):null),
        pending_user_message:fetchedIsServerIdle?null:(keepLocalOptimistic?(fetched.pending_user_message||local.pending_user_message||null):null),
        pending_started_at:fetchedIsServerIdle?null:(keepLocalOptimistic?(fetched.pending_started_at||local.pending_started_at||null):null),
        is_streaming:fetchedIsServerIdle?false:Boolean(fetched.is_streaming||(keepLocalOptimistic&&(local.is_streaming||_isSessionLocallyStreaming(local)))),
      };
    }else{
      if(_shouldKeepLocalOnlyOptimisticSessionRow(local)){
        merged.push({...local,is_streaming:true});
        bySid.set(sid,merged.length-1);
      }else{
        _dropStaleOptimisticSessionRow(sid);
      }
    }
  }
  return merged;
}

function _isSessionListUserInteracting(){
  const now=Date.now();
  const list=$('sessionList');
  const pointerOverList=Boolean(list&&(list.matches(':hover')||list.matches(':focus-within')));
  return Boolean(
    _sessionListPointerActive ||
    pointerOverList ||
    (_sessionListLastScrollAt && now-_sessionListLastScrollAt<SESSION_LIST_INTERACTION_IDLE_MS)
  );
}

function _schedulePendingSessionListApply(){
  if(_pendingSessionListApplyTimer) clearTimeout(_pendingSessionListApplyTimer);
  _pendingSessionListApplyTimer=setTimeout(()=>{
    _pendingSessionListApplyTimer=0;
    if(!_pendingSessionListPayload) return;
    if(_isSessionListUserInteracting()){
      _schedulePendingSessionListApply();
      return;
    }
    const payload=_pendingSessionListPayload;
    _pendingSessionListPayload=null;
    if(payload.gen!==_renderSessionListGen) return;
    _applySessionListPayload(payload.sessData,payload.projData);
  }, Math.max(120, SESSION_LIST_INTERACTION_IDLE_MS));
}


function _sessionAttentionSoundSignature(s){
  const attention=s&&s.attention&&typeof s.attention==='object'?s.attention:null;
  const count=Number(attention&&attention.count);
  if(!attention||!attention.kind||!Number.isFinite(count)||count<=0)return null;
  const kind=String(attention.kind)==='approval'?'approval':(String(attention.kind)==='clarify'?'clarify':'attention');
  return `${kind}:${Math.max(1,count||1)}`;
}

function _syncSessionAttentionSoundState(sessions){
  const next=new Map();
  for(const s of Array.isArray(sessions)?sessions:[]){
    if(!s||!s.session_id)continue;
    const sig=_sessionAttentionSoundSignature(s);
    if(sig) next.set(s.session_id,sig);
  }
  if(!_sessionAttentionSoundPrimed){
    _sessionAttentionSoundPrimed=true;
    _sessionAttentionSoundState.clear();
    next.forEach((sig,sid)=>_sessionAttentionSoundState.set(sid,sig));
    return;
  }
  next.forEach((sig,sid)=>{
    const prev=_sessionAttentionSoundState.get(sid);
    if(prev!==sig){
      const [kind,countRaw]=String(sig).split(':');
      const count=Number(countRaw)||1;
      const s=(Array.isArray(sessions)?sessions:[]).find(item=>item&&item.session_id===sid)||{session_id:sid};
      const playKey=typeof _attentionSoundKey==='function'?_attentionSoundKey(s.session_id,kind,count):`${s.session_id}:${sig}`;
      if(playKey&&typeof playAttentionSound==='function') playAttentionSound(playKey);
    }
  });
  _sessionAttentionSoundState.clear();
  next.forEach((sig,sid)=>_sessionAttentionSoundState.set(sid,sig));
}

function _applySessionListPayload(sessData, projData){
  // Server's other_profile_count tells us how many sessions exist outside the
  // active profile so the "Show N from other profiles" toggle can render
  // without a second round-trip. Stashed on the module for renderSessionListFromCache.
  _otherProfileCount = sessData.other_profile_count || 0;
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
  const serverSessions=_optimisticallyRemovedSessionIds.size
    ? (sessData.sessions||[]).filter(s=>s&&!_optimisticallyRemovedSessionIds.has(s.session_id))
    : (sessData.sessions||[]);
  _reconcileActiveSessionIdleStateFromList(serverSessions);
  _allSessions = _mergeOptimisticFirstTurnSessions(serverSessions);
  _syncSessionAttentionSoundState(_allSessions);
  _clearLineageReportCache();
  _allProjects = projData.projects||[];
  _markPollingCompletionUnreadTransitions(_allSessions);
  const isStreaming = _allSessions.some(s => Boolean(s && s.is_streaming));
  if (isStreaming) {
    startStreamingPoll();
  } else {
    stopStreamingPoll();
  }
  ensureSessionTimeRefreshPoll();
  ensureActiveSessionExternalRefreshPoll();
  if(!_sessionListFirstRenderAnimated&&Array.isArray(_allSessions)&&_allSessions.length){
    animateNextSessionListRefresh({enterAll:true});
    _sessionListFirstRenderAnimated=true;
  }
  ensureSessionEventsSSE();
  renderSessionListFromCache();  // no-ops if rename is in progress
}

function _mergeRenderSessionListOptions(prev, next){
  const merged={...(prev||{}),...(next||{})};
  // Immediate refreshes must not be downgraded by a later passive polling tick.
  if((prev&&prev.deferWhileInteracting===false)||(next&&next.deferWhileInteracting===false)){
    merged.deferWhileInteracting=false;
  }
  return merged;
}

async function _runRenderSessionListRefresh(opts, _gen){
  const deferWhileInteracting=Boolean(opts&&opts.deferWhileInteracting);
  if(!deferWhileInteracting) _pendingSessionListPayload=null;
  try{
    if(!($('sessionSearch').value||'').trim()) _contentSearchResults = [];
    const allProfilesQS = _showAllProfiles ? '?all_profiles=1' : '';
    const [sessData, projData] = await Promise.all([
      api('/api/sessions' + allProfilesQS,{timeoutToast:false}),
      api('/api/projects' + allProfilesQS,{timeoutToast:false}),
    ]);
    // Discard stale response — a newer renderSessionList() call superseded us.
    if (_gen !== _renderSessionListGen) return;
    if(deferWhileInteracting&&_isSessionListUserInteracting()){
      _pendingSessionListPayload={gen:_gen,sessData,projData};
      _schedulePendingSessionListApply();
      return;
    }
    _applySessionListPayload(sessData,projData);
  }catch(e){console.warn('renderSessionList',e);}
}

async function _drainRenderSessionListQueue(initialRequest){
  let request=initialRequest;
  try{
    while(request){
      await _runRenderSessionListRefresh(request.opts, request.gen);
      request=_renderSessionListQueuedRequest;
      _renderSessionListQueuedRequest=null;
    }
  }finally{
    _renderSessionListInFlight=null;
    if(_renderSessionListQueuedRequest){
      const next=_renderSessionListQueuedRequest;
      _renderSessionListQueuedRequest=null;
      _renderSessionListInFlight=_drainRenderSessionListQueue(next);
    }
  }
}

async function renderSessionList(opts={}){
  const request={opts:opts||{},gen:++_renderSessionListGen};
  if(_renderSessionListInFlight){
    _renderSessionListQueuedRequest={
      opts:_mergeRenderSessionListOptions(_renderSessionListQueuedRequest&&_renderSessionListQueuedRequest.opts, request.opts),
      gen:request.gen,
    };
    return _renderSessionListInFlight;
  }
  _renderSessionListInFlight=_drainRenderSessionListQueue(request);
  return _renderSessionListInFlight;
}

// ── Gateway session SSE (real-time sync for agent sessions) ──
let _gatewaySSE = null;
let _gatewayPollTimer = null;
let _gatewayProbeInFlight = false;
let _gatewaySSEWarningShown = false;
const _gatewayFallbackPollMs = 30000;
const _streamingPollMs = 5000;
const _sessionTimeRefreshMs = 60000;
// #3107: the active-session "is it externally updated?" poll used to fire
// every 5 s. On long sessions this caused visible scroll jitter and a
// noticeable network/CPU floor because the SSE session-events stream
// already pushes invalidations in real time; this poll exists only as a
// fallback for the case where SSE is broken/unavailable. Bump to 30 s
// to keep the safety net without turning it into a primary refresh path.
const _activeSessionExternalRefreshMs = 30000;
let _streamingPollTimer = null;
let _sessionTimeRefreshTimer = null;
let _activeSessionExternalRefreshTimer = null;
let _activeSessionExternalRefreshInFlight = false;
let _sessionEventsSSE = null;
let _sessionEventsRefreshTimer = 0;
let _sessionEventsReconnectTimer = 0;
let _sessionEventsNeedsRefreshOnOpen = false;
let _sessionEventsReconnectAttempt = 0;
const _sessionEventsReconnectBaseMs = 5000;
const _sessionEventsReconnectMaxMs = 30000;

function _sessionEventsReconnectDelayMs(){
  const attempt = Math.max(0, Number(_sessionEventsReconnectAttempt || 0));
  const base = Math.min(_sessionEventsReconnectMaxMs, _sessionEventsReconnectBaseMs * Math.pow(2, attempt));
  const jitter = Math.floor(Math.random() * Math.max(1, Math.floor(base * 0.35)));
  return Math.min(_sessionEventsReconnectMaxMs, Math.floor(base * 0.75) + jitter);
}
let _sessionListRefreshInFlight = false;
let _sessionListRefreshPendingReason = '';

function startStreamingPoll(){
  if(_streamingPollTimer) return;
  _streamingPollTimer = setInterval(() => {
    void renderSessionList({deferWhileInteracting:true});
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

async function refreshActiveSessionIfExternallyUpdated(reason){
  if(_activeSessionExternalRefreshInFlight) return;
  if(!S.session || !S.session.session_id) return;
  if(S.busy || S.activeStreamId) return;
  // Cooldown: don't force-reload immediately after streaming ends — the
  // "done" event already delivered the final messages. Reloading here would
  // clear S.toolCalls and lose Activity.
  if(typeof window !== 'undefined' && window._streamJustFinished) return;
  if(typeof document !== 'undefined' && document.hidden) return;
  const sid = S.session.session_id;
  const localCount = Number(S.session.message_count || (Array.isArray(S.messages)?S.messages.length:0) || 0);
  const localLast = Number(S.session.last_message_at || S.session.updated_at || 0);
  _activeSessionExternalRefreshInFlight = true;
  try{
    const data = await api(`/api/session?session_id=${encodeURIComponent(sid)}&messages=0&resolve_model=0`,{timeoutToast:false});
    if(!data || !data.session) return;
    if(!S.session || S.session.session_id !== sid) return;
    if(S.busy || S.activeStreamId) return;
    const remoteCount = Number(data.session.message_count || 0);
    const remoteLast = Number(data.session.last_message_at || data.session.updated_at || 0);
    if(remoteCount > localCount || remoteLast > localLast){
      await loadSession(sid, {force:true, externalRefreshReason:reason||'poll'});
      if(typeof renderSessionList==='function') void renderSessionList();
    }
  }catch(e){
    // Ignore transient refresh failures; the next poll/focus event will retry.
  }finally{
    _activeSessionExternalRefreshInFlight = false;
  }
}

function ensureActiveSessionExternalRefreshPoll(){
  if(_activeSessionExternalRefreshTimer) return;
  _activeSessionExternalRefreshTimer = setInterval(() => {
    void refreshActiveSessionIfExternallyUpdated('poll');
  }, _activeSessionExternalRefreshMs);
  if(typeof document !== 'undefined' && !document._hermesExternalRefreshVisibilityHook){
    document.addEventListener('visibilitychange', () => {
      if(!document.hidden) void refreshActiveSessionIfExternallyUpdated('visible');
    });
    document._hermesExternalRefreshVisibilityHook = true;
  }
  if(typeof window !== 'undefined' && !window._hermesExternalRefreshFocusHook){
    window.addEventListener('focus', () => { void refreshActiveSessionIfExternallyUpdated('focus'); });
    window._hermesExternalRefreshFocusHook = true;
  }
}

async function refreshSessionList(reason='manual', opts={}){
  const force = !!(opts && opts.force);
  const refreshActive = !!(opts && opts.refreshActive);
  if(!force && typeof document !== 'undefined' && document.hidden) return;
  if(_sessionListRefreshInFlight){
    _sessionListRefreshPendingReason = reason || 'session-list';
    return;
  }
  _sessionListRefreshInFlight = true;
  try{
    await renderSessionList({deferWhileInteracting:!force});
    if(refreshActive) await refreshActiveSessionIfExternallyUpdated(reason||'session-list');
  }finally{
    _sessionListRefreshInFlight = false;
    const pendingReason = _sessionListRefreshPendingReason;
    _sessionListRefreshPendingReason = '';
    if(pendingReason) _scheduleSessionEventsRefresh(pendingReason);
  }
}

function _scheduleSessionEventsRefresh(reason){
  if(_sessionEventsRefreshTimer) return;
  _sessionEventsRefreshTimer = setTimeout(() => {
    _sessionEventsRefreshTimer = 0;
    void refreshSessionList(reason||'event');
  }, 300);
}

function _closeSessionEventsSSE(){
  if(_sessionEventsSSE){
    _sessionEventsSSE.close();
    _sessionEventsSSE = null;
  }
}

function ensureSessionEventsSSE(){
  if(typeof document !== 'undefined' && !document._hermesSessionEventsVisibilityHook){
    document.addEventListener('visibilitychange', () => {
      if(document.hidden){
        _closeSessionEventsSSE();
      }else{
        ensureSessionEventsSSE();
        void refreshSessionList('visible');
      }
    });
    document._hermesSessionEventsVisibilityHook = true;
  }
  if(typeof EventSource==='undefined') return;
  if(typeof document !== 'undefined' && document.hidden) return;
  if(_sessionEventsSSE) return;
  try{
    // Same-origin relative URL preserves subpath mounts and normal WebUI cookies.
    _sessionEventsSSE = new EventSource('api/sessions/events');
    _sessionEventsSSE.onopen = () => {
      _sessionEventsReconnectAttempt = 0;
      if(!_sessionEventsNeedsRefreshOnOpen) return;
      _sessionEventsNeedsRefreshOnOpen = false;
      void refreshSessionList('reconnect');
    };
    _sessionEventsSSE.addEventListener('sessions_changed', (ev) => {
      const activeProfile = S.activeProfile || 'default';
      try {
        const payload = typeof ev?.data === 'string' ? JSON.parse(ev.data) : {};
        const eventProfile = payload && typeof payload.profile === 'string' ? payload.profile : '';
        if (!_sessionEventProfilesMatch(eventProfile, activeProfile)) {
          return;
        }
      } catch (_err) {
        // Non-JSON payload (or transient malformed event). Keep legacy behavior:
        // refresh once event was seen.
      }
      _scheduleSessionEventsRefresh('event');
    });
    _sessionEventsSSE.onerror = () => {
      _sessionEventsNeedsRefreshOnOpen = true;
      _closeSessionEventsSSE();
      if(_sessionEventsReconnectTimer) return;
      const delayMs = _sessionEventsReconnectDelayMs();
      _sessionEventsReconnectAttempt = Math.min(_sessionEventsReconnectAttempt + 1, 6);
      _sessionEventsReconnectTimer = setTimeout(() => {
        _sessionEventsReconnectTimer = 0;
        ensureSessionEventsSSE();
      }, delayMs);
    };
  }catch(e){
    _closeSessionEventsSSE();
  }
}

if(typeof window!=='undefined') window.refreshSessionList = refreshSessionList;

function startGatewayPollFallback(ms){
  const intervalMs = Math.max(5000, Number(ms) || _gatewayFallbackPollMs);
  if(_gatewayPollTimer) clearInterval(_gatewayPollTimer);
  _gatewayPollTimer = setInterval(() => { renderSessionList({deferWhileInteracting:true}); }, intervalMs);
}

function stopGatewayPollFallback(){
  if(_gatewayPollTimer){
    clearInterval(_gatewayPollTimer);
    _gatewayPollTimer = null;
  }
}

function _gatewaySessionSnapshotKey(sessions){
  return (Array.isArray(sessions)?sessions:[])
    .filter(s=>s&&s.session_id)
    .map(s=>`${s.session_id}:${s.updated_at||0}:${s.message_count||0}`)
    .sort()
    .join('|');
}

function _isGatewaySessionForSnapshot(session){
  if(!session) return false;
  if(typeof _isCliSession==='function'&&_isCliSession(session)) return true;
  if(typeof _isMessagingSession==='function'&&_isMessagingSession(session)) return true;
  const source=String(session.session_source||session.raw_source||session.source_tag||session.source||'').toLowerCase();
  return !!source&&source!=='webui';
}

function _isDuplicateGatewaySessionSnapshot(sessions){
  const incoming=(Array.isArray(sessions)?sessions:[]).filter(_isGatewaySessionForSnapshot);
  const currentGatewaySessions=(Array.isArray(_allSessions)?_allSessions:[]).filter(_isGatewaySessionForSnapshot);
  if(!incoming.length&&!currentGatewaySessions.length) return true;
  return _gatewaySessionSnapshotKey(incoming)===_gatewaySessionSnapshotKey(currentGatewaySessions);
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
      renderSessionList({deferWhileInteracting:true});
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
    renderSessionList({deferWhileInteracting:true});
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
          if(!_isDuplicateGatewaySessionSnapshot(data.sessions)){
            renderSessionList({deferWhileInteracting:true}); // re-fetch and re-render
          }
          // If the active session received new gateway messages, refresh the conversation view.
          // S.busy check prevents stomping on an in-progress WebUI response.
          // _isExternalSession covers CLI-originated and messaging-source sessions
          // that need a server-side import before WebUI can read them.
          if(S.session && !S.busy && _isExternalSession(S.session)){
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
                    // Carry forward ephemeral turn fields (_turnUsage/
                    // _turnDuration/_turnTps/_gatewayRouting/_statusCard) so
                    // gateway-driven CLI refreshes do not drop the badge.
                    let _nextToAssign = next;
                    if (typeof window._carryForwardEphemeralTurnFields === 'function') {
                      _nextToAssign = window._carryForwardEphemeralTurnFields(S.messages || [], next);
                    }
                    S.messages = _nextToAssign;
                    if(S.session && S.session.session_id === activeSid){
                      S.session.message_count = next.length;
                      const newest = next.length ? next[next.length - 1] : null;
                      const newestTs = Number((newest && (newest.timestamp || newest._ts)) || 0);
                      if(newestTs){
                        S.session.last_message_at = newestTs;
                        S.session.updated_at = newestTs;
                      }
                    }
                    if(S.messages.length !== prev){
                      renderMessages({preserveScroll:true});
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
      if(typeof recordClientSSEError==='function') recordClientSSEError('gateway-sessions',{ready_state:_gatewaySSE?_gatewaySSE.readyState:null,reason:'gateway EventSource.onerror'});
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
let _lastSessionSearchQuery = '';
let _hideSearchPreviewsAfterSelect = false;
let _serverTimeDelta = 0;       // ms offset: client clock - server clock (for clock-skew compensation)
let _serverTz = '';              // server timezone offset string (e.g. "+0800", "+0000", "-0500")

function _sessionSearchRanges(text, query){
  const source=String(text||'');
  const q=String(query||'').trim();
  if(!source||!q) return [];
  const lower=source.toLowerCase();
  const full=q.toLowerCase();
  const ranges=[];
  const collect=(needle)=>{
    if(!needle) return;
    let from=0;
    while(from<lower.length){
      const idx=lower.indexOf(needle,from);
      if(idx<0) break;
      const end=idx+needle.length;
      if(!ranges.some(r=>idx<r.end&&end>r.start)) ranges.push({start:idx,end});
      from=Math.max(end,idx+1);
    }
  };
  collect(full);
  if(!ranges.length&&/\s/.test(full)){
    const seen=new Set();
    full.split(/\s+/).filter(Boolean).sort((a,b)=>b.length-a.length).forEach(token=>{
      if(seen.has(token)) return;
      seen.add(token);
      collect(token);
    });
  }
  return ranges.sort((a,b)=>a.start-b.start);
}

function _appendHighlightedText(parent, text, query, highlightClass){
  const source=String(text||'');
  const ranges=_sessionSearchRanges(source,query);
  if(!ranges.length){
    parent.appendChild(document.createTextNode(source));
    return ranges;
  }
  let pos=0;
  for(const r of ranges){
    if(r.start>pos) parent.appendChild(document.createTextNode(source.slice(pos,r.start)));
    const mark=document.createElement('span');
    mark.className=highlightClass||'session-search-hit';
    mark.textContent=source.slice(r.start,r.end);
    parent.appendChild(mark);
    pos=r.end;
  }
  if(pos<source.length) parent.appendChild(document.createTextNode(source.slice(pos)));
  return ranges;
}

function _sessionSearchContentPreview(session, query){
  if(!session||!query||_hideSearchPreviewsAfterSelect) return '';
  if(session.match_type!=='content') return '';
  const preview=String(session.match_preview||'').replace(/\s+/g,' ').trim();
  return preview||'';
}

function _sessionSearchAddIdCandidate(candidates, seen, value){
  const raw=String(value||'').trim();
  if(!raw) return;
  const add=(candidate)=>{
    const sid=String(candidate||'').trim();
    if(!sid||seen.has(sid)) return;
    seen.add(sid);
    candidates.push(sid);
  };
  add(raw);
  try{add(decodeURIComponent(raw));}catch(_e){}
}

function _sessionSearchCleanUrlToken(token){
  let value=String(token||'').trim();
  value=value.replace(/[\],.;]+$/g,'');
  while(value.endsWith(')')&&value.indexOf('(')<0) value=value.slice(0,-1);
  return value;
}

function _sessionSearchSessionIdCandidates(query){
  const source=String(query||'').trim();
  const candidates=[];
  const seen=new Set();
  if(!source) return candidates;
  _sessionSearchAddIdCandidate(candidates,seen,source);

  const inspectUrl=(token)=>{
    const cleaned=_sessionSearchCleanUrlToken(token);
    if(!cleaned) return;
    try{
      const url=new URL(cleaned,'http://webui.local');
      const parts=url.pathname.split('/').filter(Boolean);
      const sessionIdx=parts.findIndex(p=>p.toLowerCase()==='session');
      if(sessionIdx>=0&&parts[sessionIdx+1]) _sessionSearchAddIdCandidate(candidates,seen,parts[sessionIdx+1]);
      for(const key of ['session_id','session','sid']){
        const value=url.searchParams.get(key);
        if(value) _sessionSearchAddIdCandidate(candidates,seen,value);
      }
    }catch(_e){}
  };

  const markdownLinkRe=/\]\(([^\s)]+)\)/g;
  let match;
  while((match=markdownLinkRe.exec(source))) inspectUrl(match[1]);

  const sessionSchemeRe=/session:\/\/([^\s)>\]]+)/gi;
  while((match=sessionSchemeRe.exec(source))) _sessionSearchAddIdCandidate(candidates,seen,match[1]);

  const urlRe=/(?:https?:\/\/[^\s<>\]]+|\/session\/[^\s<>\]]+|\?[^\s<>\]]+)/gi;
  while((match=urlRe.exec(source))) inspectUrl(match[0]);

  const queryParamRe=/(?:^|[?&\s])(session_id|session|sid)=([^&#\s)]+)/gi;
  while((match=queryParamRe.exec(source))) _sessionSearchAddIdCandidate(candidates,seen,match[2]);
  return candidates;
}

function _sessionSearchDirectSessionMatches(sessions, query){
  const candidates=_sessionSearchSessionIdCandidates(query);
  if(!candidates.length) return [];
  const candidateIds=new Set(candidates.map(s=>String(s)));
  return (sessions||[]).filter(s=>s&&candidateIds.has(String(s.session_id||'')));
}

function _sessionSearchDirectAndTitleMatches(sessions, query){
  const source=String(query||'').trim();
  if(!source) return sessions||[];
  const q=source.toLowerCase();
  const titleMatches=(sessions||[]).filter(s=>_sessionDisplayTitle(s).toLowerCase().includes(q));
  const directSessionMatches=_sessionSearchDirectSessionMatches(sessions,source);
  const directSessionIds=new Set(directSessionMatches.map(s=>s.session_id));
  return [...directSessionMatches,...titleMatches.filter(s=>!directSessionIds.has(s.session_id))];
}

function _sessionSearchMergeMatches(sessions, query, contentResults){
  const source=String(query||'').trim();
  if(!source) return sessions||[];
  const directAndTitleMatches=_sessionSearchDirectAndTitleMatches(sessions,source);
  const directOrTitleIds=new Set(directAndTitleMatches.map(s=>s.session_id));
  return [
    ...directAndTitleMatches,
    ...(contentResults||[]).filter(s=>s&&s.match_type==='content'&&!directOrTitleIds.has(s.session_id))
  ];
}

function syncSessionSearchClear(){
  const input=$('sessionSearch');
  const clear=$('sessionSearchClear');
  if(!input||!clear) return;
  clear.hidden=!Boolean(input.value);
}

function clearSessionSearch(focusInput=true){
  const input=$('sessionSearch');
  if(!input) return;
  if(input.value){
    input.value='';
    filterSessions();
  }else{
    syncSessionSearchClear();
  }
  if(focusInput) input.focus();
}

function filterSessions(){
  // Immediate client-side title filter (no flicker)
  // Debounced content search via API for message text
  syncSessionSearchClear();
  const q = ($('sessionSearch').value || '').trim();
  if(q!==_lastSessionSearchQuery){
    _lastSessionSearchQuery=q;
    _hideSearchPreviewsAfterSelect=false;
  }
  renderSessionListFromCache();
  clearTimeout(_searchDebounceTimer);
  if (!q) { _contentSearchResults = []; return; }
  _searchDebounceTimer = setTimeout(async () => {
    const requestedQ = q;
    try {
      const data = await api(`/api/sessions/search?q=${encodeURIComponent(requestedQ)}&content=1&depth=5`);
      const currentQ = ($('sessionSearch').value || '').trim();
      if(currentQ!==requestedQ) return;
      const directAndTitleMatches=_sessionSearchDirectAndTitleMatches(_allSessions,currentQ);
      const directOrTitleIds=new Set(directAndTitleMatches.map(s=>s.session_id));
      _contentSearchResults = (data.sessions||[]).filter(s => s.match_type === 'content' && !directOrTitleIds.has(s.session_id));
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

function _sessionLineageKey(s, sessionIdsInList, sessionsById){
  if(!s||!s.session_id) return null;
  if(_isChildSession(s)) return null;
  if(s.session_source==='fork') return null;
  const lineageKey=s._lineage_root_id||s.lineage_root_id||null;
  if(lineageKey) return lineageKey;
  // WebUI-native context compression may only persist parent_session_id:
  // the preserved parent snapshot is marked pre_compression_snapshot while
  // the new continuation points at it.  When both rows are in the sidebar
  // payload, still collapse them into one conversation (#2489).
  const parent=s.parent_session_id&&sessionsById?sessionsById.get(s.parent_session_id):null;
  if(s.pre_compression_snapshot||parent&&parent.pre_compression_snapshot){
    let root=s;
    const seen=new Set();
    while(root&&root.parent_session_id&&sessionsById&&sessionsById.has(root.parent_session_id)&&!seen.has(root.parent_session_id)){
      const next=sessionsById.get(root.parent_session_id);
      if(!next||_isChildSession(next)||next.session_source==='fork'||!(root.pre_compression_snapshot||next.pre_compression_snapshot)) break;
      seen.add(root.session_id);
      root=next;
    }
    return root&&root.session_id||s.parent_session_id||s.session_id;
  }
  // If parent_session_id points to another session in the current list,
  // this is a subagent/fork child without compression metadata — don't
  // collapse it into lineage (#494).
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

function _resolveSessionIdFromSidebarLineage(sid){
  sid=String(sid||'').trim();
  if(!sid||!Array.isArray(_allSessions)||!_allSessions.length) return sid||null;
  const visibleRows=_collapseSessionLineageForSidebar(_allSessions).filter(row=>row&&!_isChildSession(row));
  if(visibleRows.some(row=>row&&row.session_id===sid)) return sid;
  const candidates=[];
  for(const row of visibleRows){
    if(!row||!row.session_id) continue;
    if(row.session_source==='fork'||row.relationship_type==='child_session') continue;
    const lineageLike=!!(
      row._lineage_key||row._lineage_root_id||row.lineage_root_id||
      row._compression_segment_count||row.pre_compression_snapshot||
      (Array.isArray(row._lineage_segments)&&row._lineage_segments.length>1)
    );
    if(!lineageLike) continue;
    const key=_sidebarLineageKeyForRow(row);
    if(key===sid||row.parent_session_id===sid||row._lineage_root_id===sid||row.lineage_root_id===sid||_sessionLineageContainsSession(row,sid)){
      candidates.push(row);
    }
  }
  if(!candidates.length) return sid;
  candidates.sort((a,b)=>{
    const bSeg=Number(b&&b._compression_segment_count||b&&b._lineage_collapsed_count||0);
    const aSeg=Number(a&&a._compression_segment_count||a&&a._lineage_collapsed_count||0);
    if(bSeg!==aSeg) return bSeg-aSeg;
    const bSnapshot=!!(b&&b.pre_compression_snapshot);
    const aSnapshot=!!(a&&a.pre_compression_snapshot);
    if(bSnapshot!==aSnapshot) return aSnapshot-bSnapshot;
    return _sessionTimestampMs(b)-_sessionTimestampMs(a);
  });
  return candidates[0].session_id||sid;
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

function _clearLineageReportCache(){
  _lineageReportCache.clear();
  _lineageReportInflight.clear();
  _lineageReportCacheGeneration++;
}

function _lineageReportCacheKey(s,lineageKey){
  return lineageKey||_sidebarLineageKeyForRow(s)||null;
}

function _lineageLocalSegmentCount(s){
  if(!s) return 0;
  if(Array.isArray(s._lineage_segments)) return s._lineage_segments.length;
  return s.session_id?1:0;
}

function _lineageReportNeedsFetch(s,lineageKey,segmentCount){
  const key=_lineageReportCacheKey(s,lineageKey);
  if(!s||!s.session_id||!key) return false;
  if(_lineageReportCache.has(key)||_lineageReportInflight.has(key)) return false;
  return Number(segmentCount||0)>_lineageLocalSegmentCount(s);
}

function _lineageSegmentsForRender(s,lineageKey){
  const segments=[];
  const seen=new Set();
  const currentSid=s&&s.session_id;
  const addSegment=(seg)=>{
    if(!seg||!seg.session_id||seg.session_id===currentSid||seen.has(seg.session_id)) return;
    if(seg.role==='child_session') return;
    seen.add(seg.session_id);
    segments.push({...seg});
  };
  for(const seg of (Array.isArray(s&&s._lineage_segments)?s._lineage_segments:[])) addSegment(seg);
  const cached=_lineageReportCache.get(_lineageReportCacheKey(s,lineageKey));
  if(cached&&Array.isArray(cached.segments)){
    for(const seg of cached.segments) addSegment(seg);
  }
  return segments;
}

function _fetchLineageReportForRow(s,lineageKey){
  const key=_lineageReportCacheKey(s,lineageKey);
  if(!s||!s.session_id||!key) return Promise.resolve(null);
  if(_lineageReportCache.has(key)) return Promise.resolve(_lineageReportCache.get(key));
  if(_lineageReportInflight.has(key)) return _lineageReportInflight.get(key);
  const generation=_lineageReportCacheGeneration;
  let request;
  request=api('/api/session/lineage/report?session_id='+encodeURIComponent(s.session_id))
    .then(report=>{
      if(generation===_lineageReportCacheGeneration){
        _lineageReportCache.set(key,(report&&report.found!==false)?report:{error:true});
      }
      return report;
    })
    .catch(err=>{
      console.warn('lineage report',err);
      if(generation===_lineageReportCacheGeneration) _lineageReportCache.set(key,{error:true});
      return null;
    })
    .finally(()=>{
      if(_lineageReportInflight.get(key)===request) _lineageReportInflight.delete(key);
    });
  _lineageReportInflight.set(key,request);
  return request;
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

function _sessionFullTitleTooltip(rawTitle, cleanTitle, session){
  const fallback=String(cleanTitle||'Untitled').trim()||'Untitled';
  const full=String(rawTitle||fallback).trim()||fallback;
  const title=full.startsWith('[SYSTEM:') ? fallback : full;
  if(typeof t==='function'&&_isReadOnlySession(session)) return t('session_readonly_title_hint', title);
  return title;
}

function _sessionForkTooltip(parentLabel){
  const parent=String(parentLabel||'').trim()||'unknown parent';
  // Preserve the localized "Forked from" base (the catalog key exists in all
  // locales) rather than hardcoding English — the only regression risk in the
  // tooltip rework was dropping t('forked_from') here.
  const prefix=(typeof t==='function'?t('forked_from'):'Forked from');
  return `${prefix}: ${parent}`;
}

function _sessionLineageBadgeTooltip(label, canExpand){
  const base=String(label||'Prior turns').trim()||'Prior turns';
  if(typeof t==='function'){
    return canExpand
      ? t('session_lineage_toggle_hint', base)
      : t('session_lineage_static_hint', base);
  }
  return base;
}

function _sessionChildBadgeTooltip(label){
  const base=String(label||'Child sessions').trim()||'Child sessions';
  if(typeof t==='function') return t('session_child_toggle_hint', base);
  return base;
}

function _sessionStateTooltip({isStreaming=false,hasUnread=false}={}){
  if(isStreaming) return 'Conversation is running';
  if(hasUnread) return 'Unread completion';
  return '';
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
        childCopy._parent_segment_title=_sessionDisplayTitle(parentSegment)||child.parent_title||'Untitled';
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
  const sessionsById=new Map((sessions||[]).filter(s=>s&&s.session_id).map(s=>[s.session_id,s]));
  const groups=new Map();
  for(const s of sessions||[]){
    const key=_sessionLineageKey(s, sessionIdsInList, sessionsById);
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
      // Preserved pre-compression parents can share the same backend segment
      // count as the continuation. Prefer the non-snapshot tip before falling
      // back to timestamps, otherwise a recently-polled parent reopens the
      // older transcript and makes the active continuation look lost.
      const bSnapshot=!!(b&&b.pre_compression_snapshot);
      const aSnapshot=!!(a&&a.pre_compression_snapshot);
      if(bSnapshot!==aSnapshot) return aSnapshot-bSnapshot;
      return _sessionTimestampMs(b)-_sessionTimestampMs(a);
    });
    const chosen=sorted[0];
    result.push({...chosen,_lineage_key:key,_lineage_collapsed_count:items.length,_lineage_segments:sorted});
  }
  return result;
}

function _sessionDisplayTitle(s){
  const rawTitle=String((s&&(s.display_title||s._state_db_title||s.title))||'Untitled').trim();
  const strip=(typeof _stripAttachedFilesMarker==='function')
    ? _stripAttachedFilesMarker
    : (text)=>String(text||'').replace(/\n\n\[Attached files: [^\]]+\]$/,'').trim();
  const title=strip(rawTitle);
  return title||'Untitled';
}

function _sessionTitleIsDefaultWebUI(rawTitle){
  const title=String(rawTitle||'').replace(/\s+/g,' ').trim();
  return title==='Hermes WebUI'||/^Hermes WebUI #\d+$/.test(title);
}

function _sessionTitleTags(rawTitle){
  if(_sessionTitleIsDefaultWebUI(rawTitle)) return [];
  return String(rawTitle||'').match(/#[\w-]+/g)||[];
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

function _sessionRowsWithActiveEphemeralSession(rows){
  rows=Array.isArray(rows)?rows:[];
  if(!S.session||!S.session.session_id) return rows;
  const sid=S.session.session_id;
  if(rows.some(s=>s&&s.session_id===sid)) return rows;
  const nowSec=Math.floor(Date.now()/1000);
  const activeRow={
    ...S.session,
    session_id:sid,
    title:S.session.title||'New Chat',
    display_title:S.session.display_title||S.session.title||'New Chat',
    message_count:0,
    last_message_at:S.session.last_message_at||S.session.updated_at||nowSec,
    updated_at:S.session.updated_at||S.session.last_message_at||nowSec,
    profile:S.session.profile||S.activeProfile||'default',
    is_streaming:false,
  };
  return [activeRow,...rows];
}

function _ensureActiveSessionRowPresent(rows, sourceRows){
  rows=Array.isArray(rows)?rows:[];
  const activeSid=_activeSessionIdForSidebar();
  if(!activeSid||rows.some(s=>s&&s.session_id===activeSid)) return rows;
  const activeRow=(Array.isArray(sourceRows)?sourceRows:[]).find(s=>s&&s.session_id===activeSid);
  // Only re-inject the active FRESHLY-CREATED 0-message ephemeral chat. An active
  // conversation that already has messages and was filtered out by the search
  // query must stay filtered — re-adding it here would pollute unrelated search
  // results with the current chat (#3408 review, Codex).
  if(activeRow && Number(activeRow.message_count||0)<=0){
    return [activeRow,...rows];
  }
  return rows;
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
  _sessionListLastScrollAt=Date.now();
  if(_renamingSid||_sessionVirtualScrollRaf) return;
  const list=_sessionVirtualScrollList;
  const total=Number(list&&list.dataset&&list.dataset.sessionVirtualTotal||0);
  // Skip the re-render if the list is below the virtualization threshold —
  // there's no virtual window to recompute, and re-rendering would just
  // rebuild the whole DOM on every scroll tick. Without this guard, the
  // unconditional scroll listener (attached for any list) caused
  // user-facing scroll jumps on small lists. (#1669 follow-up)
  if(total>0&&total<=SESSION_VIRTUAL_THRESHOLD_ROWS) return;
  _sessionVirtualScrollRaf=requestAnimationFrame(()=>{
    _sessionVirtualScrollRaf=0;
    const liveList=_sessionVirtualScrollList;
    const liveTotal=Number(liveList&&liveList.dataset&&liveList.dataset.sessionVirtualTotal||0);
    if(liveList&&liveTotal>SESSION_VIRTUAL_THRESHOLD_ROWS){
      const nextWindow=_sessionVirtualWindow({
        total:liveTotal,
        scrollTop:liveList.scrollTop||0,
        viewportHeight:liveList.clientHeight||520,
        itemHeight:SESSION_VIRTUAL_ROW_HEIGHT,
        buffer:SESSION_VIRTUAL_BUFFER_ROWS,
        threshold:SESSION_VIRTUAL_THRESHOLD_ROWS,
        activeIndex:-1,
      });
      const currentStart=Number(liveList.dataset.sessionVirtualStart||0);
      const currentEnd=Number(liveList.dataset.sessionVirtualEnd||0);
      if(nextWindow.virtualized&&nextWindow.start===currentStart&&nextWindow.end===currentEnd) return;
    }
    renderSessionListFromCache();
  });
}

function _ensureSessionVirtualScrollHandler(list){
  if(!list) return;
  if(_sessionVirtualScrollList===list) return;
  if(_sessionVirtualScrollList){
    _sessionVirtualScrollList.removeEventListener('scroll', _scheduleSessionVirtualizedRender);
    _sessionVirtualScrollList.removeEventListener('pointerdown', _markSessionListPointerDown);
    _sessionVirtualScrollList.removeEventListener('pointerup', _markSessionListPointerUp);
    _sessionVirtualScrollList.removeEventListener('pointercancel', _markSessionListPointerUp);
    _sessionVirtualScrollList.removeEventListener('pointerleave', _markSessionListPointerUp);
  }
  _sessionVirtualScrollList=list;
  list.addEventListener('scroll', _scheduleSessionVirtualizedRender, {passive:true});
  list.addEventListener('pointerdown', _markSessionListPointerDown, {passive:true});
  list.addEventListener('pointerup', _markSessionListPointerUp, {passive:true});
  list.addEventListener('pointercancel', _markSessionListPointerUp, {passive:true});
  list.addEventListener('pointerleave', _markSessionListPointerUp, {passive:true});
}

function _markSessionListPointerDown(){
  _sessionListPointerActive=true;
  _sessionListLastScrollAt=Date.now();
}

function _markSessionListPointerUp(){
  _sessionListPointerActive=false;
  _sessionListLastScrollAt=Date.now();
  if(_pendingSessionListPayload) _schedulePendingSessionListApply();
}

let _sessionVirtualResyncRaf = 0;
function _resyncSessionVirtualWindowAfterRender(list, expectedScrollTop, virtualWindow){
  if(!list||!virtualWindow||!virtualWindow.virtualized) return;
  expectedScrollTop=Number(expectedScrollTop)||0;
  if(expectedScrollTop<=0) return;
  if(_sessionVirtualResyncRaf) cancelAnimationFrame(_sessionVirtualResyncRaf);
  _sessionVirtualResyncRaf=requestAnimationFrame(()=>{
    _sessionVirtualResyncRaf=0;
    if(_renamingSid) return;
    const actualScrollTop=Number(list.scrollTop)||0;
    const tolerance=Math.max(2, Number(virtualWindow.itemHeight||SESSION_VIRTUAL_ROW_HEIGHT)/2);
    if(Math.abs(actualScrollTop-expectedScrollTop)<=tolerance) return;
    renderSessionListFromCache();
  });
}

// Top-level so BOTH the sidebar visibility predicate (_sidebarRowHasVisibleMessages,
// reached via renderSessionListFromCache -> _partitionSidebarSessionRows) and the
// per-row renderer (_renderOneSession, nested in renderSessionListFromCache) can call
// it. It was previously declared INSIDE renderSessionListFromCache and relied on
// function hoisting — but hoisting is scoped to the enclosing function, so the
// top-level _sidebarRowHasVisibleMessages threw "ReferenceError: _sessionAttentionState
// is not defined" on every cache render, crashing the sidebar (#3696, regressed in
// #3672 when _sidebarRowHasVisibleMessages was extracted to top level). Pure function
// (only its arg `s` plus the i18n global `t`), so hoisting it is safe.
function _sessionAttentionState(s){
  const attention=s&&s.attention&&typeof s.attention==='object'?s.attention:null;
  if(!attention||!attention.kind||!Number.isFinite(Number(attention.count))||Number(attention.count)<=0)return null;
  const kind=String(attention.kind)==='approval'?'approval':(String(attention.kind)==='clarify'?'clarify':'attention');
  const count=Math.max(1,Number(attention.count)||1);
  const labelKey=kind==='approval'?'session_attention_approval':(kind==='clarify'?'session_attention_clarify':'session_attention_generic');
  const titleKey=kind==='approval'?'session_attention_approval_title':(kind==='clarify'?'session_attention_clarify_title':'session_attention_generic_title');
  const fallback=kind==='approval'?(count===1?'Approval':`${count} approvals`):(kind==='clarify'?(count===1?'Question':`${count} questions`):(count===1?'Attention':`${count} items`));
  const titleFallback=kind==='approval'?'Waiting for permission decision':(kind==='clarify'?'Waiting for your answer':'Waiting for user action');
  const label=(typeof t==='function')?t(labelKey,count):fallback;
  const title=(typeof t==='function')?t(titleKey,count):titleFallback;
  return {kind,count,severity:String(attention.severity||''),label,title};
}

function _sidebarRowHasVisibleMessages(s, activeSidForSidebar){
  return (s.message_count||0)>0 ||
    _sessionAttentionState(s) ||
    _isSessionEffectivelyStreaming(s) ||
    !!s.active_stream_id ||
    !!s.pending_user_message ||
    (activeSidForSidebar&&s.session_id===activeSidForSidebar) ||
    (S.session&&s.session_id===S.session.session_id&&(S.session.message_count||0)>0);
}

function _partitionSidebarSessionRows(allMatched, activeSidForSidebar){
  let webuiSessionCount=0;
  let cliSessionCount=0;
  for(const s of allMatched){
    if(!_sidebarRowHasVisibleMessages(s, activeSidForSidebar)) continue;
    if(_isCliSession(s)) cliSessionCount++;
    else webuiSessionCount++;
  }
  if(_sessionSourceFilter==='cli' && !window._showCliSessions && cliSessionCount===0){
    _sessionSourceFilter='webui';
  }
  const showCliOnly=_sessionSourceFilter==='cli';
  const profileFiltered=[];
  const sessionsRaw=[];
  let archivedCount=0;
  for(const s of allMatched){
    if(!_sidebarRowHasVisibleMessages(s, activeSidForSidebar)) continue;
    const isCli=_isCliSession(s);
    if(showCliOnly ? !isCli : isCli) continue;
    if(s.default_hidden&&!(_activeProject&&_activeProject!==NO_PROJECT_FILTER&&s.project_id===_activeProject)) continue;
    profileFiltered.push(s);
    if(_activeProject===NO_PROJECT_FILTER){
      if(s.project_id) continue;
    } else if(_activeProject){
      if(s.project_id!==_activeProject) continue;
    }
    if(s.archived) archivedCount++;
    if(!_showArchived&&s.archived) continue;
    sessionsRaw.push(s);
  }
  return {
    webuiSessionCount,
    cliSessionCount,
    profileFiltered,
    sessionsRaw,
    archivedCount,
  };
}

function renderSessionListFromCache(){
  // Don't re-render while user is actively renaming a session (would destroy the input)
  if(_renamingSid) return;
  // Keep the per-conversation actions menu stable while the user is trying to
  // click it. Sidebar syncs, stream/unread updates, and panel-resync repairs can
  // all call this while the fixed-position menu is open; rebuilding the row DOM
  // here removes the anchor and makes the menu feel unclickable.
  if(_sessionActionMenu) return;
  closeSessionActionMenu();
  // Purge stale INFLIGHT entries for sessions the server confirms are NOT
  // streaming. This runs on every list refresh to prevent memory leaks from
  // interrupted streams. (#2066)
  _purgeStaleInflightEntries();
  const searchQueryRaw=($('sessionSearch').value||'').trim();
  const q=searchQueryRaw.toLowerCase();
  const activeSidForSidebar=_activeSessionIdForSidebar();
  const sidebarRows=_sessionRowsWithActiveEphemeralSession(_allSessions);
  // Merge direct session-id/link matches, title matches, then content matches (deduped).
  // Direct matches must not disable content search: if a user pasted the same
  // session id into another conversation, that content hit should still appear.
  const searchMatches=_sessionSearchMergeMatches(sidebarRows,searchQueryRaw,_contentSearchResults);
  const allMatched=_ensureActiveSessionRowPresent(searchMatches,sidebarRows);
  const {
    webuiSessionCount,
    cliSessionCount,
    profileFiltered,
    sessionsRaw,
    archivedCount,
  }=_partitionSidebarSessionRows(allMatched, activeSidForSidebar);
  const sessions=_attachChildSessionsToSidebarRows(_collapseSessionLineageForSidebar(sessionsRaw), sessionsRaw);
  _syncSidebarExpansionForActiveSession(sessions, activeSidForSidebar);
  const list=$('sessionList');
  const animateRefresh=_sessionListRefreshAnimationPending;
  _sessionListRefreshAnimationPending=false;
  const enterAllAnimatedRows=animateRefresh&&_sessionListEnterAllAnimationPending;
  _sessionListEnterAllAnimationPending=false;
  const flipBefore=animateRefresh?_captureSessionReflowPositions():null;
  const committedSwipeDuration=_sessionPrefersReducedMotion()?0:SESSION_SWIPE_DURATION_MS;
  const committedSwipeReflowDelay=Math.max(0,committedSwipeDuration-SESSION_SWIPE_REFLOW_LEAD_MS);
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
  if(window._showCliSessions || cliSessionCount>0){
    const sourceTabs=document.createElement('div');
    sourceTabs.className='session-source-tabs';
    for(const filter of ['webui','cli']){
      const count=filter==='cli'?cliSessionCount:webuiSessionCount;
      const btn=document.createElement('button');
      btn.type='button';
      btn.className='session-source-tab'+(_sessionSourceFilter===filter?' active':'');
      btn.textContent=_sessionSourceLabel(filter,count);
      btn.setAttribute('aria-pressed', _sessionSourceFilter===filter?'true':'false');
      btn.onclick=()=>_setSessionSourceFilter(filter);
      sourceTabs.appendChild(btn);
    }
    list.appendChild(sourceTabs);
  }
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
      // Touch long-press → context menu (mobile UX: project chips can only be
      // deleted via the right-click menu, which has no touch equivalent).
      let _lpTimer=null;
      let _lpHandled=false;
      let _lpStartX=0,_lpStartY=0;
      chip.addEventListener('touchstart',(e)=>{
        const t=e.changedTouches&&e.changedTouches[0];
        if(!t) return;
        // Clear any in-flight timer before scheduling a new one, mirroring the
        // session-item long-press path (_clearLongPressTimer). Without this a
        // second finger / stray touchstart orphans the prior timer, which then
        // fires unsuppressed ~500ms later and pops the menu after the gesture
        // was cancelled.
        if(_lpTimer){clearTimeout(_lpTimer);_lpTimer=null;}
        _lpHandled=false;_lpStartX=t.clientX;_lpStartY=t.clientY;
        chip.classList.add('long-pressing');
        _lpTimer=setTimeout(()=>{
          _lpTimer=null;
          if(_lpHandled) return;  // already consumed by another gesture — stale fire is a no-op
          _lpHandled=true;
          chip.classList.remove('long-pressing');
          clearTimeout(_pClickTimer);_pClickTimer=null;
          const syn={clientX:t.clientX,clientY:t.clientY,preventDefault:()=>{}};
          _showProjectContextMenu(syn,p,chip);
        },500);
      },{passive:true});
      chip.addEventListener('touchmove',(e)=>{
        if(!_lpTimer) return;
        const t=e.changedTouches&&e.changedTouches[0];
        if(!t) return;
        if(Math.abs(t.clientX-_lpStartX)>10||Math.abs(t.clientY-_lpStartY)>10){
          clearTimeout(_lpTimer);_lpTimer=null;
          chip.classList.remove('long-pressing');
        }
      },{passive:true});
      chip.addEventListener('touchend',(e)=>{
        clearTimeout(_lpTimer);_lpTimer=null;
        chip.classList.remove('long-pressing');
        if(_lpHandled){e.preventDefault();e.stopPropagation();}
      },{passive:false});
      chip.addEventListener('touchcancel',()=>{
        clearTimeout(_lpTimer);_lpTimer=null;_lpHandled=false;
        chip.classList.remove('long-pressing');
      },{passive:true});
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
  if(_sessionSourceFilter==='cli'&&sessions.length===0){
    const empty=document.createElement('div');
    empty.className='session-empty-note';
    empty.textContent=window._showCliSessions?'No CLI sessions found.':'Enable Show agent sessions in Settings to list CLI sessions here.';
    list.appendChild(empty);
  } else if(_activeProject&&sessions.length===0){
    const empty=document.createElement('div');
    empty.className='session-empty-note';
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
  const virtualWindowBeforeActiveAnchor=_sessionVirtualWindow({
    total:flatSessionRows.length,
    scrollTop:listScrollTopBeforeRender,
    viewportHeight:list.clientHeight||520,
    itemHeight:SESSION_VIRTUAL_ROW_HEIGHT,
    buffer:SESSION_VIRTUAL_BUFFER_ROWS,
    threshold:SESSION_VIRTUAL_THRESHOLD_ROWS,
    activeIndex:-1,
  });
  const activeWasAlreadyVisible=activeIndex>=virtualWindowBeforeActiveAnchor.start&&activeIndex<virtualWindowBeforeActiveAnchor.end;
  const shouldMoveSidebarToActive=shouldAnchorActive&&!activeWasAlreadyVisible;
  let virtualWindow=_sessionVirtualWindow({
    total:flatSessionRows.length,
    scrollTop:listScrollTopBeforeRender,
    viewportHeight:list.clientHeight||520,
    itemHeight:SESSION_VIRTUAL_ROW_HEIGHT,
    buffer:SESSION_VIRTUAL_BUFFER_ROWS,
    threshold:SESSION_VIRTUAL_THRESHOLD_ROWS,
    activeIndex:shouldMoveSidebarToActive?activeIndex:-1,
  });
  let virtualAnchorScrollTop=null;
  if(shouldMoveSidebarToActive&&virtualWindow.virtualized){
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
    _resyncSessionVirtualWindowAfterRender(list, listScrollTopBeforeRender, virtualWindow);
  }
  // Select mode toggle button (only when NOT in select mode)
  if(!_sessionSelectMode){
    const toggleBtn=document.createElement('div');toggleBtn.className='session-select-toggle';
    toggleBtn.textContent=t('session_select_mode');
    toggleBtn.onclick=(e)=>{e.stopPropagation();toggleSessionSelectMode();};
    list.appendChild(toggleBtn);
  }
  // Refresh FLIP and queued archive/delete reflow both drive
  // --session-reflow-offset. Refresh wins so one render has one transform writer.
  const reflowBefore=animateRefresh?flipBefore:_pendingSessionReflowPositions;
  const reflowTimeout=animateRefresh?SESSION_LIST_FLIP_TIMEOUT_MS:SESSION_REFLOW_TIMEOUT_MS;
  _pendingSessionReflowPositions=null;
  _playSessionRowsReflowFromPositions(reflowBefore,reflowTimeout,_sessionPrefersReducedMotion);

  function _renderOneSession(s, isPinnedGroup=false){
    const el=document.createElement('div');
    const isActive=_sessionLineageContainsSession(s,activeSidForSidebar);
    const isStreaming=_isSessionEffectivelyStreaming(s);
    _rememberRenderedStreamingState(s, isStreaming);
    _rememberRenderedSessionSnapshot(s);
    const hasUnread=_hasUnreadForSession(s)&&!isActive;
    const attention=_sessionAttentionState(s);
    const attentionClass=attention?(attention.kind==='approval'?' attention-approval':(attention.kind==='clarify'?' attention-clarify':' attention-attention')):'';
    const readOnly=_isReadOnlySession(s);
    el.className='session-item'+(isActive?' active':'')+(isActive&&S.session&&S.session._flash?' new-flash':'')+(s.archived?' archived':'')+(isStreaming?' streaming':'')+(hasUnread?' unread':'')+(attention?' needs-attention':'')+attentionClass;
    const swipeReturnOffset=_sessionSwipeReturnOffsets.get(s.session_id);
    if(swipeReturnOffset!==undefined){
      _sessionSwipeReturnOffsets.delete(s.session_id);
      el.style.setProperty('--session-swipe-return-offset',swipeReturnOffset);
      el.classList.add('session-swipe-returning');
      el.addEventListener('animationend',()=>{
        el.classList.remove('session-swipe-returning');
        el.style.removeProperty('--session-swipe-return-offset');
      },{once:true});
    }
    if(animateRefresh&&(enterAllAnimatedRows||!(flipBefore&&flipBefore.has(s.session_id)))){
      el.classList.add('session-list-flip-enter');
    }
    if(s.is_cli_session||_isMessagingSession(s)){
      el.classList.add('cli-session');
      el.dataset.source=_getChannelLabel(s)||'CLI';
      el.dataset.sourceKey=_sourceKeyForSession(s)||'cli';
    }
    if(readOnly) el.classList.add('read-only-session');
    if(isActive&&S.session&&S.session._flash)delete S.session._flash;
    const rawTitle=_sessionDisplayTitle(s);
    const tags=_sessionTitleTags(rawTitle);
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
    if(s.worktree_path){
      const wtInd=document.createElement('span');
      wtInd.className='session-worktree-indicator';
      wtInd.innerHTML=li('git-branch',12);
      const wtLabel=(typeof t==='function'?t('session_worktree_badge'):'Worktree');
      wtInd.title=`${wtLabel}: ${s.worktree_branch||s.worktree_path}`;
      titleRow.appendChild(wtInd);
    }
    // Parent session indicator for forked/branched sessions (#465)
    if(s.parent_session_id){
      const branchInd=document.createElement('span');
      branchInd.className='session-branch-indicator';
      branchInd.innerHTML=li('git-branch',12);
      const parentLabel=_sessionTitleForForkParent(s.parent_session_id)||_truncatedSessionId(s.parent_session_id);
      branchInd.title=_sessionForkTooltip(parentLabel);
      titleRow.appendChild(branchInd);
    }
    const title=document.createElement('span');
    title.className='session-title';
    const displayTitle=cleanTitle||'Untitled';
    const titleMatched=Boolean(searchQueryRaw&&displayTitle.toLowerCase().includes(searchQueryRaw.toLowerCase()));
    if(titleMatched) _appendHighlightedText(title,displayTitle,searchQueryRaw,'session-search-hit');
    else title.textContent=displayTitle;
    title.title=_sessionFullTitleTooltip(rawTitle,cleanTitle,s);
    const tsMs=_sessionTimestampMs(s);
    const ts=document.createElement('span');
    const hasAttentionState=isStreaming||hasUnread||Boolean(attention);
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
    const density=(window._sidebarDensity==='detailed'?'detailed':'compact');
    const showLineageMetadata=density==='detailed';
    const lineageKey=_sidebarLineageKeyForRow(s);
    const segmentCount=showLineageMetadata?_sessionSegmentCount(s):0;
    const lineageSegments=showLineageMetadata?_lineageSegmentsForRender(s,lineageKey):[];
    const needsLineageReport=showLineageMetadata?_lineageReportNeedsFetch(s,lineageKey,segmentCount):false;
    const lineageReportKey=showLineageMetadata?_lineageReportCacheKey(s,lineageKey):null;
    const canExpandLineageSegments=showLineageMetadata&&Boolean(lineageKey&&segmentCount>1&&(lineageSegments.length>0||needsLineageReport||_lineageReportInflight.has(lineageReportKey)));
    const lineageSegmentsExpanded=canExpandLineageSegments&&_expandedLineageKeys.has(lineageKey);
    if(segmentCount>0){
      const segmentCountEl=document.createElement('span');
      segmentCountEl.className='session-lineage-count'+(canExpandLineageSegments?' expandable':'');
      const segmentLabel=t('session_meta_segments', segmentCount);
      segmentCountEl.textContent=segmentLabel;
      segmentCountEl.title=_sessionLineageBadgeTooltip(segmentLabel,canExpandLineageSegments);
      if(canExpandLineageSegments){
        segmentCountEl.setAttribute('role','button');
        segmentCountEl.setAttribute('tabindex','0');
        segmentCountEl.setAttribute('aria-expanded',lineageSegmentsExpanded?'true':'false');
        ['pointerdown','pointerup','click'].forEach(ev=>segmentCountEl.addEventListener(ev,e=>e.stopPropagation()));
        const toggleLineageSegments=(e)=>{
          e.preventDefault();
          e.stopPropagation();
          if(_expandedLineageKeys.has(lineageKey)) _expandedLineageKeys.delete(lineageKey);
          else {
            _expandedLineageKeys.add(lineageKey);
            if(needsLineageReport) _fetchLineageReportForRow(s,lineageKey).then(()=>renderSessionListFromCache());
          }
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
      childCountEl.title=_sessionChildBadgeTooltip(childLabel);
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
    if(s.is_cli_session||_isMessagingSession(s)){
      const chipLabel=_getChannelLabel(s)||'CLI';
      const chip=document.createElement('span');
      chip.className='session-source-chip';
      chip.textContent=chipLabel;
      chip.dataset.sourceKey=_sourceKeyForSession(s)||'cli';
      titleRow.appendChild(chip);
    }
    titleRow.appendChild(ts);
    sessionText.appendChild(titleRow);
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
      if(sourceLabel&&(s.is_cli_session||_isMessagingSession(s))) metaBits.push(sourceLabel);
      if(readOnly) metaBits.push('read-only');
      if(_showAllProfiles&&s.profile) metaBits.push(s.profile);
      const meta=document.createElement('div');
      meta.className='session-meta';
      meta.textContent=metaBits.join(' · ');
      sessionText.appendChild(meta);
    }
    const contentPreview=titleMatched?'':_sessionSearchContentPreview(s,searchQueryRaw);
    if(contentPreview){
      const preview=document.createElement('div');
      preview.className='session-search-preview';
      preview.title=contentPreview;
      _appendHighlightedText(preview,contentPreview,searchQueryRaw,'session-search-hit session-search-hit-preview');
      sessionText.appendChild(preview);
    }
    if(lineageSegmentsExpanded){
      const lineageList=document.createElement('div');
      lineageList.className='session-lineage-segments';
      ['pointerdown','pointerup','click'].forEach(ev=>lineageList.addEventListener(ev,e=>e.stopPropagation()));
      const sortedSegments=[...lineageSegments].sort((a,b)=>_sessionTimestampMs(b)-_sessionTimestampMs(a));
      for(const seg of sortedSegments){
        const row=document.createElement('button');
        row.type='button';
        row.className='session-lineage-segment'+(activeSidForSidebar&&seg.session_id===activeSidForSidebar?' active':'');
        const segTitle=_sessionDisplayTitle(seg)||t('session_lineage_segment_untitled');
        const segTime=_formatRelativeSessionTime(_sessionTimestampMs(seg));
        row.textContent=`-> ${segTitle} - ${segTime}`;
        row.title=t('session_lineage_segment_open');
        row.onclick=async(e)=>{
          e.stopPropagation();
          if(_isExternalSession(seg)){
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
        const childTitle=_sessionDisplayTitle(child)||'Untitled child session';
        const childTime=_formatRelativeSessionTime(_sessionTimestampMs(child));
        const parentNote=child._parent_segment_title?` via ${child._parent_segment_title}`:'';
        row.textContent=`-> ${childTitle}${parentNote} - ${childTime}`;
        row.title='Open child session';
        row.onclick=async(e)=>{
          e.stopPropagation();
          if(_isExternalSession(child)){
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
      // onblur: save on blur — Escape explicitly cancels. The old cancel-on-blur
      // behavior broke rename on mobile (iPhone "Done" dismisses the keyboard,
      // triggering blur) and was less natural on desktop too (typing a name then
      // clicking elsewhere should save, not discard).
      inp.onblur=()=>{ if(_renamingSid===s.session_id) finish(true); };
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
    const attentionDotClass=attention?(attention.kind==='approval'?' is-attention-approval':(attention.kind==='clarify'?' is-attention-clarify':' is-attention-generic')):'';
    state.className='session-attention-indicator session-state-indicator'+(isStreaming?' is-streaming':(hasUnread?' is-unread':''))+attentionDotClass;
    state.setAttribute('aria-hidden','true');
    // Tooltip precedence: a localized attention title (pending approval/clarify,
    // from the attention-indicator feature) is more specific and actionable than
    // the generic running/unread state tooltip, so it wins. Fall back to the state
    // tooltip only when there is no attention title AND the state tooltip is
    // non-empty — never blank an otherwise-meaningful tooltip.
    const _stateTip=_sessionStateTooltip({isStreaming,hasUnread});
    if(attention&&attention.title) state.title=attention.title;
    else if(_stateTip) state.title=_stateTip;
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
      const stopMenuPointer=(e)=>e.stopPropagation();
      menuBtn.onpointerdown=stopMenuPointer;
      menuBtn.onpointerup=stopMenuPointer;
      menuBtn.onclick=(e)=>{
        e.stopPropagation();
        e.preventDefault();
        _openSessionActionMenu(s, menuBtn);
      };
      actions.appendChild(menuBtn);
      el.appendChild(actions);
    }
    el.oncontextmenu=(e)=>{
      if(readOnly) return;
      e.preventDefault();
      if(e.pointerType==='touch'||e.pointerType==='pen') return;
      e.stopPropagation();
      clearTimeout(_tapTimer);
      _tapTimer=null;
      _lastTapTime=0;
      _clearPointerDragState();
      _openSessionActionMenu(s, actions||el);
    };

    if(!readOnly){
      el.append(
        _makeSessionSwipeAffordance('right',s.archived?'undo':'archive',s.archived?'Restore':t('session_batch_archive')),
        _makeSessionSwipeAffordance('left','trash-2',t('session_batch_delete')),
      );
    }

    // Use release events + manual double-tap detection instead of onclick/ondblclick.
    // onclick/ondblclick are unreliable on touch devices (iPad Safari especially):
    // hover-triggered layout shifts, ghost clicks, and 300ms delay all break
    // single-tap navigation.
    // Mouse clicks are instant; touch presses need a 300ms delay to distinguish
    // a tap from a scroll-drag gesture on mobile.
    // Movement promotes pressing into dragging; drag release cancels a pending tap.
    let _lastTapTime=0;
    let _tapTimer=null;
    let _pointerDownX=0;
    let _pointerDownY=0;
    let _gestureState='idle'; // idle | pressing | dragging | committed
    let _clearDragTimer=null;
    let _longPressTimer=null;
    let _longPressMenuOpened=false;
    let _swipeTracking=false;
    let _pointerX=0;
    let _pointerY=0;
    let _gesturePointerType='';
    const _clearLongPressTimer=()=>{
      if(_longPressTimer){clearTimeout(_longPressTimer);_longPressTimer=null;}
      if(!_longPressMenuOpened) el.classList.remove('long-pressing');
    };
    const _beginSessionGesture=(clientX,clientY,pointerType='')=>{
      _gesturePointerType=pointerType;
      _pointerDownX=clientX;
      _pointerDownY=clientY;
      _pointerX=clientX;
      _pointerY=clientY;
      _gestureState='pressing';
      _swipeTracking=false;
      _longPressMenuOpened=false;
      if(_clearDragTimer){clearTimeout(_clearDragTimer);_clearDragTimer=null;}
      el.classList.remove('dragging','swipe-committed','swipe-removing');
      el.style.removeProperty('height');
      el.style.removeProperty('min-height');
    };
    const _scheduleSessionLongPressMenu=()=>{
      _clearLongPressTimer();
      el.classList.add('long-pressing');
      _longPressTimer=setTimeout(()=>{
        if(_gestureState!=='pressing'||_renamingSid||_sessionSelectMode||readOnly) return;
        _longPressMenuOpened=true;
        clearTimeout(_tapTimer);
        _tapTimer=null;
        _lastTapTime=0;
        _openSessionActionMenu(s, el);
      },SESSION_LONG_PRESS_DELAY_MS);
    };
    const _isSessionSwipeTarget=()=>{
      return _gesturePointerType!=='mouse'&&!readOnly&&!_renamingSid&&!_sessionSelectMode;
    };
    const _isSessionActionTarget=(target)=>{
      return !!(actions&&target&&actions.contains(target));
    };
    const _trackHorizontalSwipe=(dx,dy)=>{
      if(dx>8&&dx>dy*1.1) _swipeTracking=true;
    };
    const _promoteSessionDrag=(dx,dy)=>{
      if(_gestureState!=='pressing'||(dx<=5&&dy<=5)) return;
      if(dy>8||dx>10) _clearLongPressTimer();
      _gestureState='dragging';
      el.classList.add('dragging');
      if(_clearDragTimer){clearTimeout(_clearDragTimer);_clearDragTimer=null;}
    };
    const _updateSessionGesture=(clientX,clientY)=>{
      if(_gestureState==='idle') return false;
      _pointerX=clientX;
      _pointerY=clientY;
      const signedDx=clientX-_pointerDownX;
      const signedDy=clientY-_pointerDownY;
      const dx=Math.abs(signedDx);
      const dy=Math.abs(signedDy);
      _promoteSessionDrag(dx,dy);
      _trackHorizontalSwipe(dx,dy);
      if(_isSessionSwipeTarget()&&(_swipeTracking||dx>dy)) _paintSessionSwipe(signedDx);
      return _swipeTracking;
    };
    const _canSwipeDeleteSession=()=>{
      return _isSessionSwipeTarget()&&!_isMessagingSession(s)&&!_isCliSession(s);
    };
    const _paintSessionSwipe=(signedDx)=>{
      const rawOffset=signedDx*.55;
      const revealedOffset=Math.max(-72,Math.min(72,rawOffset));
      const overshoot=Math.max(0,Math.abs(rawOffset)-72);
      const offset=Math.sign(rawOffset)*(Math.abs(revealedOffset)+Math.sqrt(overshoot)*5);
      const progress=Math.min(1,Math.abs(revealedOffset)/72);
      const reveal=Math.abs(offset);
      const actionRevealScale=1.15;
      const iconScale=Math.min(1,Math.max(.01,progress*actionRevealScale));
      const badgeSize=34*iconScale;
      const iconSize=18*iconScale;
      const labelScale=Math.min(1,Math.max(.01,progress*actionRevealScale));
      const actionOpacity=Math.min(1,Math.max(.01,progress*actionRevealScale));
      const actionInset=6;
      const tileGap=6;
      const stretchStart=72/actionRevealScale;
      const stretchProgress=Math.max(0,reveal-stretchStart);
      const badgeStretch=Math.min(Math.max(0,reveal-34),stretchProgress*1.15,Math.max(0,reveal-badgeSize-actionInset-tileGap));
      el.style.setProperty('--session-swipe-offset',offset+'px');
      el.style.setProperty('--session-swipe-reveal',reveal+'px');
      el.style.setProperty('--session-swipe-badge-size',badgeSize+'px');
      el.style.setProperty('--session-swipe-icon-size',iconSize+'px');
      el.style.setProperty('--session-swipe-label-scale',labelScale);
      el.style.setProperty('--session-swipe-badge-stretch',badgeStretch+'px');
      el.style.setProperty('--session-swipe-progress',actionOpacity);
      el.classList.toggle('swiping-right',offset>0);
      el.classList.toggle('swiping-left',offset<0);
    };
    const _clearSessionSwipePaint=()=>{
      el.style.removeProperty('--session-swipe-offset');
      el.style.removeProperty('--session-swipe-reveal');
      el.style.removeProperty('--session-swipe-badge-size');
      el.style.removeProperty('--session-swipe-icon-size');
      el.style.removeProperty('--session-swipe-label-scale');
      el.style.removeProperty('--session-swipe-badge-stretch');
      el.style.removeProperty('--session-swipe-progress');
      el.style.removeProperty('height');
      el.style.removeProperty('min-height');
      el.classList.remove('swiping-right','swiping-left','swipe-committed','swipe-removing');
    };
    const _settleSessionSwipePaint=()=>{
      el.classList.remove('dragging');
      requestAnimationFrame(()=>requestAnimationFrame(_clearSessionSwipePaint));
    };
    const _completeSessionSwipePaint=(signedDx)=>{
      el.classList.remove('dragging');
      el.classList.add('swipe-committed');
      el.style.setProperty('--session-swipe-progress','0');
      el.style.setProperty('--session-swipe-offset',(signedDx>0?1:-1)*window.innerWidth+'px');
      const rect=el.getBoundingClientRect();
      el.style.height=rect.height+'px';
      el.style.minHeight=rect.height+'px';
      requestAnimationFrame(()=>el.classList.add('swipe-removing'));
    };
    const _handleSessionSwipe=(signedDx,signedDy)=>{
      if(_gestureState==='committed'||!_isSessionSwipeTarget()) return false;
      const actionThreshold=signedDx>0?SESSION_ARCHIVE_SWIPE_THRESHOLD_PX:SESSION_DELETE_SWIPE_THRESHOLD_PX;
      if(Math.abs(signedDx)<actionThreshold) return false;
      if(Math.abs(signedDy)>Math.abs(signedDx)*SESSION_SWIPE_CANCEL_RATIO) return false;
      _gestureState='committed';
      _clearLongPressTimer();
      clearTimeout(_tapTimer);
      _tapTimer=null;
      _lastTapTime=0;
      if(signedDx>0){
        if(s.archived){
          _settleSessionSwipePaint();
          _archiveSession(s,false,()=>_waitForSessionMotion(committedSwipeDuration)).then((restored)=>{
            if(!restored) _settleSessionSwipePaint();
          });
        }else if(_showArchived){
          _settleSessionSwipePaint();
          _archiveSession(s,true,()=>_waitForSessionMotion(committedSwipeDuration)).then((archived)=>{
            if(!archived) _settleSessionSwipePaint();
          });
        }else{
          _completeSessionSwipePaint(signedDx);
          _archiveSession(s,true,()=>_waitForSessionMotion(committedSwipeReflowDelay)).then((archived)=>{
            if(!archived) _settleSessionSwipePaint();
          });
        }
      }else if(_canSwipeDeleteSession()){
        el.classList.remove('dragging');
        deleteSession(s.session_id,async()=>{
          _completeSessionSwipePaint(signedDx);
          await _waitForSessionMotion(committedSwipeReflowDelay);
        }).then((deleted)=>{
          if(!deleted) _settleSessionSwipePaint();
        });
      }else if(typeof showToast==='function'){
        showToast('Imported sessions cannot be deleted here.',3000);
        _gestureState='dragging';
        _settleSessionSwipePaint();
      }
      return true;
    };
    const _commitSessionSwipe=()=>{
      return _handleSessionSwipe(_pointerX-_pointerDownX,_pointerY-_pointerDownY);
    };
    const _clearPointerDragState=()=>{
      if(_gestureState==='committed'){
        _clearLongPressTimer();
        return;
      }
      const wasDragging=_gestureState==='dragging'||_swipeTracking;
      _gestureState='idle';
      _clearLongPressTimer();
      if(wasDragging){
        if(_clearDragTimer){clearTimeout(_clearDragTimer);_clearDragTimer=null;}
        _clearDragTimer=setTimeout(()=>{_settleSessionSwipePaint();_clearDragTimer=null;},50);
      }
    };
    const _finishSessionGesture=(clientX,clientY,target,pointerType)=>{
      const wasDragging=_gestureState==='dragging'||_swipeTracking;
      _clearLongPressTimer();
      if(_renamingSid){_gestureState='idle';return false;}
      if(_isSessionActionTarget(target)){_gestureState='idle';return false;}
      _pointerX=clientX;
      _pointerY=clientY;
      _commitSessionSwipe();
      if(_longPressMenuOpened){_gestureState='idle';return true;}
      if(_gestureState==='committed') return true;
      if(_sessionActionMenu&&!_sessionActionMenu.contains(target)){
        closeSessionActionMenu();
        return true;
      }
      if(target&&target.closest&&target.closest('.session-child-count,.session-child-sessions,.session-child-session,.session-lineage-count,.session-lineage-segments,.session-lineage-segment')) return false;
      if(_sessionSelectMode){if(!readOnly)toggleSessionSelect(s.session_id);return true;}
      if(wasDragging){
        clearTimeout(_tapTimer);_tapTimer=null;_lastTapTime=0;
        _gestureState='idle';
        _clearDragTimer=setTimeout(()=>{_settleSessionSwipePaint();_clearDragTimer=null;},50);
        return false;
      }
      _gestureState='idle';
      const now=Date.now();
      if(now-_lastTapTime<350){
        clearTimeout(_tapTimer);
        _tapTimer=null;
        _lastTapTime=0;
        el.classList.remove('loading');
        startRename();
        return false;
      }
      _lastTapTime=now;
      clearTimeout(_tapTimer);
      const delay=pointerType==='mouse'?0:300;
      if(pointerType!=='mouse') el.classList.add('loading');
      _tapTimer=setTimeout(async()=>{
        _tapTimer=null;
        _lastTapTime=0;
        if(_renamingSid) return;
        // For external sessions (CLI, Discord, Telegram, Slack), import into
        // WebUI store first so /api/chat/start finds a persisted session.
        if(_isExternalSession(s)){
          try{
            await api('/api/session/import_cli',{method:'POST',body:JSON.stringify({session_id:s.session_id})});
          }catch(e){ /* import failed -- fall through to read-only view */ }
        }
        try{
          if(($('sessionSearch').value||'').trim()) _hideSearchPreviewsAfterSelect=true;
          await loadSession(s.session_id);renderSessionListFromCache();
          if(typeof closeMobileSidebar==='function')closeMobileSidebar();
        }finally{
          el.classList.remove('loading');
        }
      }, delay);
      return false;
    };
    el.onpointerdown=(e)=>{
      if(e.pointerType==='touch') return;
      if(e.pointerType==='mouse' && e.button!==0) return;
      if(_isSessionActionTarget(e.target)) return;
      _beginSessionGesture(e.clientX,e.clientY,e.pointerType||'');
      if(e.pointerType==='pen'){
        _scheduleSessionLongPressMenu();
      }
    };
    el.onpointermove=(e)=>{
      if(e.pointerType==='touch') return;
      // Plain hover also dispatches pointermove. Only mark a row as dragging
      // after an actual press starts on this row; otherwise hovered rows stay
      // faded until the next sidebar rerender clears their DOM nodes.
      _updateSessionGesture(e.clientX,e.clientY);
    };
    el.onpointercancel=(e)=>{
      if(e.pointerType==='touch') return;
      _clearPointerDragState();
    };
    el.onpointerleave=()=>{
      if(_gesturePointerType==='mouse'&&_gestureState!=='idle') _clearPointerDragState();
    };
    el.onpointerup=(e)=>{
      if(e.pointerType==='touch') return;
      if(e.pointerType==='mouse' && e.button!==0) return;  // ignore right/middle click
      if(_finishSessionGesture(e.clientX,e.clientY,e.target,e.pointerType)) e.stopPropagation();
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
    el.addEventListener('touchstart',(e)=>{
      if(_isSessionActionTarget(e.target)) return;
      const touch=e.changedTouches&&e.changedTouches[0];
      if(!touch) return;
      _beginSessionGesture(touch.clientX,touch.clientY,'touch');
      _scheduleSessionLongPressMenu();
    },{passive:true});
    el.addEventListener('touchmove',(e)=>{
      const touch=e.changedTouches&&e.changedTouches[0];
      if(!touch) return;
      if(_updateSessionGesture(touch.clientX,touch.clientY)) e.preventDefault();
    },{passive:false});
    el.addEventListener('touchcancel',_clearPointerDragState,{passive:true});
    el.addEventListener('touchend',(e)=>{
      const touch=e.changedTouches&&e.changedTouches[0];
      if(!touch) return;
      if(_finishSessionGesture(touch.clientX,touch.clientY,e.target,'touch')) e.stopPropagation();
    },{passive:true});
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

async function removeWorktree(session){
  // Fetch status first
  let status=null;
  try{
    const statusResp=await api('/api/session/worktree/status?session_id='+encodeURIComponent(session.session_id));
    status=statusResp.status;
  }catch(e){
    showToast(t('session_worktree_remove_status_failed')+e.message,0,'error');
    return;
  }
  if(!status){
    showToast(t('session_worktree_remove_status_failed'),0,'error');
    return;
  }
  // Build confirm message
  let details='';
  if(!status.exists){
    details=t('session_worktree_remove_not_exists',status.path);
  }else{
    details=t('session_worktree_remove_confirm',status.path);
    if(status.locked_by_stream){
      showToast(t('session_worktree_remove_locked_by_stream'),0,'error');
      return;
    }
    if(status.locked_by_terminal){
      showToast(t('session_worktree_remove_locked_by_terminal'),0,'error');
      return;
    }
    if(status.dirty){
      details+='\n\n'+t('session_worktree_remove_dirty_warning');
    }
    if(status.untracked_count>0){
      details+='\n'+t('session_worktree_remove_untracked_warning',status.untracked_count);
    }
    if(status.ahead_behind&&status.ahead_behind.ahead>0){
      details+='\n'+t('session_worktree_remove_ahead_warning',status.ahead_behind.ahead);
    }
    if(status.dirty||status.untracked_count>0||(status.ahead_behind&&status.ahead_behind.ahead>0)){
      showToast(t('session_worktree_remove_failed')+t('session_worktree_remove_unsafe_blocked'),0,'error');
      await showConfirmDialog({
        message:details,
        confirmLabel:t('dialog_confirm_btn'),
        danger:true,
        focusCancel:true
      });
      return;
    }
  }
  const ok=await showConfirmDialog({
    message:details,
    confirmLabel:t('session_worktree_remove_confirm_label'),
    danger:true
  });
  if(!ok)return;
  try{
    const result=await api('/api/session/worktree/remove',{
      method:'POST',
      body:JSON.stringify({session_id:session.session_id, force:false})
    });
    const warn=result.warnings&&result.warnings.length?(' '+result.warnings.join(' ')):'';
    showToast(t('session_worktree_removed')+warn);
    // Clear the worktree_path from cached session so menu doesn't show stale remove action
    if(session.worktree_path){
      session.worktree_path=null;
    }
    // Re-render the list if this is the active session
    if(S.session&&S.session.session_id===session.session_id&&S.session.worktree_path){
      S.session.worktree_path=null;
    }
    await renderSessionList();
  }catch(e){
    showToast(t('session_worktree_remove_failed')+e.message,0,'error');
  }
}

async function deleteSession(sid, beforeDelete=null){
  const session=_sessionSnapshotById(sid);
  const ok=await showConfirmDialog({
    message:session&&session.worktree_path?t('session_delete_worktree_confirm',session.worktree_path):t('session_delete_confirm'),
    confirmLabel:t('delete_title'),
    danger:true
  });
  if(!ok)return false;
  const reflowPositions=_captureSessionReflowPositions();
  const beforeDeleteHold=beforeDelete?Promise.resolve().then(beforeDelete):null;
  const previousSessions=_allSessions;
  let optimisticRendered=false;
  const deleteRequest=api('/api/session/delete',{method:'POST',body:JSON.stringify({session_id:sid})}).then(response=>{
    _clearHandoffStorageForSession(sid);
    return {response};
  }, error=>({error}));
  if(beforeDeleteHold){
    await beforeDeleteHold;
    _optimisticallyRemovedSessionIds.add(sid);
    _pendingSessionReflowPositions=reflowPositions;
    _optimisticallyRemoveSessionFromList(sid);
    optimisticRendered=true;
  }
  const deleteResult=await deleteRequest;
  if(deleteResult&&deleteResult.error){
    _pendingSessionReflowPositions=null;
    if(optimisticRendered){
      _optimisticallyRemovedSessionIds.delete(sid);
      _allSessions=previousSessions;
      renderSessionListFromCache();
    }
    const err=deleteResult.error;
    setStatus(`Delete failed: ${err&&err.message?err.message:String(err)}`);
    return false;
  }
  const response=deleteResult&&deleteResult.response;
  if(typeof _clearPersistedSessionQueue==='function') _clearPersistedSessionQueue(sid);
  if(!optimisticRendered){
    _pendingSessionReflowPositions=reflowPositions;
    _optimisticallyRemoveSessionFromList(sid);
  }
  if(S.session&&S.session.session_id===sid){
    S.session=null;S.messages=[];S.entries=[];
    if(typeof _hydrateTodosFromSession==='function') _hydrateTodosFromSession(null);
    localStorage.removeItem('hermes-webui-session');
    // load the most recent remaining session, or show blank if none left
    const remaining=await api('/api/sessions');
    if(remaining.sessions&&remaining.sessions.length){
      await loadSession(remaining.sessions[0].session_id);
    }else{
      const _tt=$('topbarTitle');if(_tt)_tt.textContent=assistantDisplayName();
      const _tm=$('topbarMeta');if(_tm)_tm.textContent='Start a new conversation';
      $('msgInner').innerHTML='';
      $('emptyState').style.display='';
      $('fileTree').innerHTML='';
      if(typeof S!=='undefined') S.session=null;
      if(typeof syncAppTitlebar==='function') syncAppTitlebar();
    }
  }
  showToast(_sessionResponseRetainsWorktree(response,session)?t('session_deleted_worktree'):t('session_deleted'));
  if(optimisticRendered) void renderSessionList().finally(()=>_optimisticallyRemovedSessionIds.delete(sid));
  else await renderSessionList();
  return true;
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
    try {
      await api('/api/session/move',{method:'POST',body:JSON.stringify({session_id:session.session_id,project_id:null})});
      // Sidebar rows are shallow copies of _allSessions entries (see
      // _attachChildSessionsToSidebarRows), so mutating `session` only updates
      // the discarded copy. Write into the authoritative cache so the next
      // renderSessionListFromCache() reflects the move. (#2551)
      const idx=_allSessions.findIndex(s=>s&&s.session_id===session.session_id);
      if(idx>=0) _allSessions[idx].project_id=null;
      renderSessionListFromCache();
      showToast('Removed from project');
    } catch(e) {
      showToast('Unassign failed: '+(e.message||e));
    }
  };
  picker.appendChild(none);
  // Project options — only show projects matching the session's profile.
  // #3331 follow-up (Codex gate): mirror the server's root-alias tolerance —
  // `_profiles_match` treats the literal 'default' and a renamed-root display
  // name as equivalent, so a server-approved `profile:'default'` project must
  // not be hidden for a session stamped with the renamed-root profile (and
  // vice versa). Only hide when BOTH sides are explicit, distinct, AND neither
  // is the 'default' alias; let the server's allowlist be authoritative for the
  // default/renamed-root case.
  const sessionProfile = session ? (session.profile || undefined) : undefined;
  const _profileHidesProject = (projProfile) => {
    if(!sessionProfile || !projProfile) return false;
    if(projProfile === sessionProfile) return false;
    if(projProfile === 'default' || sessionProfile === 'default') return false;
    return true;
  };
  for(const p of _allProjects){
    if (_profileHidesProject(p.profile)) continue;
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
      try{
        await api('/api/session/move',{method:'POST',body:JSON.stringify({session_id:session.session_id,project_id:p.project_id})});
        // See #2551 — write to _allSessions, not the shallow sidebar copy.
        const idx=_allSessions.findIndex(s=>s&&s.session_id===session.session_id);
        if(idx>=0) _allSessions[idx].project_id=p.project_id;
        renderSessionListFromCache();
        showToast('Moved to '+p.name);
      }catch(e){showToast('Move failed: '+(e.message||e));}
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
    const profile = session.profile || undefined;
    const res=await api('/api/projects/create',{method:'POST',body:JSON.stringify({name:name.trim(),color,profile})});
    if(res.project){
      _allProjects.push(res.project);
      // Guard the move so a 503 (session busy/streaming, #3746) shows a toast
      // instead of an unhandled rejection. Keep the authoritative refetch (#2551).
      try{
        await api('/api/session/move',{method:'POST',body:JSON.stringify({session_id:session.session_id,project_id:res.project.project_id})});
        session.project_id=res.project.project_id;
        await renderSessionList();
        showToast('Created "'+res.project.name+'" and moved session');
      }catch(e){
        await renderSessionList();
        showToast('Created "'+res.project.name+'" but move failed: '+(e&&e.message||'try again'));
      }
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
  let _finishDone=false;
  const finish=async(save)=>{
    if(_finishDone) return;
    _finishDone=true;
    if(save&&inp.value.trim()){
      const color=PROJECT_COLORS[_allProjects.length%PROJECT_COLORS.length];
      try{
        await api('/api/projects/create',{method:'POST',body:JSON.stringify({name:inp.value.trim(),color})});
      }catch(e){
        _finishDone=false;
        showToast('Project create failed: '+(e.message||e));
        return;
      }
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
  inp.onblur=()=>finish(true);
  inp.addEventListener('input',()=>_resizeProjectInput(inp));
  addBtn.replaceWith(inp);
  _resizeProjectInput(inp);
  setTimeout(()=>inp.focus(),10);
}

function _startProjectRename(proj, chip){
  const inp=document.createElement('input');
  inp.className='project-create-input';
  inp.value=proj.name;
  let _finishDone=false;
  const finish=async(save)=>{
    if(_finishDone) return;
    _finishDone=true;
    if(save&&inp.value.trim()&&inp.value.trim()!==proj.name){
      try {
        await api('/api/projects/rename',{method:'POST',body:JSON.stringify({project_id:proj.project_id,name:inp.value.trim()})});
        await renderSessionList();
        showToast('Project renamed');
      } catch(e) {
        _finishDone=false;
        showToast('Rename failed: '+(e.message||e));
      }
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
  inp.onblur=()=>finish(true);
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
      try {
        await api('/api/projects/rename',{method:'POST',body:JSON.stringify({project_id:proj.project_id,name:proj.name,color:hex})});
        await renderSessionList();
        showToast('Color updated');
      } catch(e) {
        showToast('Color update failed: '+(e.message||e));
      }
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
  try {
    await api('/api/projects/delete',{method:'POST',body:JSON.stringify({project_id:proj.project_id})});
    if(_activeProject===proj.project_id) _activeProject=null;
    await renderSessionList();
    showToast('Project deleted');
  } catch(e) {
    showToast('Delete failed: '+(e.message||e));
  }
}

// Global Escape handler for batch select mode
document.addEventListener('keydown',(e)=>{
  if(e.key==='Escape'&&_sessionSelectMode) exitSessionSelectMode();
});
