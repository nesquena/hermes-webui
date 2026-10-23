// ── Conversation Outline Panel (#2124) ───────────────────────────────────────
// Floating panel listing user messages as jump targets.
// _outlineSid guards against stale renders when the user switches sessions.

'use strict';

(function() {

let _outlineSid = null;       // session id the panel was last built for
let _panelOpen  = false;      // whether the panel is currently visible
let _outlineResizeObserver = null;
let _outlineWorkspaceObserver = null;

// Returns the current session id, or null if no session is loaded.
function _currentSid() {
  return (S && S.session && S.session.session_id) || null;
}

function _outlineAllowed() {
  const compact = window.matchMedia && window.matchMedia('(max-width:900px)').matches;
  // The outline is a chat-view affordance only — never show the toggle or panel
  // while another MAIN panel (settings, tasks, insights, …) is active. _currentPanel
  // is owned by panels.js; treat an undefined/absent value as the chat default.
  // 'todos' is a sidebar-only panel that leaves the chat transcript in <main>, so
  // the outline stays valid there too (and switching to it emits no <main> class
  // mutation for the observer, so allowing it keeps the toggle stable).
  const panel = (typeof _currentPanel === 'undefined') ? 'chat' : (_currentPanel || 'chat');
  const onChatView = panel === 'chat' || panel === 'todos';
  return window._showConversationOutline === true && !compact && onChatView;
}

function _syncOutlinePosition() {
  const root = document.documentElement;
  const panel = document.querySelector('.rightpanel');
  const open = root.dataset.workspacePanel === 'open';
  const width = open && panel ? Math.max(0, Math.round(panel.offsetWidth || 0)) : 0;
  root.style.setProperty('--outline-workspace-offset', width + 'px');
}

function applyConversationOutlinePreference() {
  const toggle = document.getElementById('outlineToggleBtn');
  const wrapper = document.getElementById('outlinePanelWrapper');
  const enabled = _outlineAllowed();
  document.documentElement.dataset.conversationOutline = enabled ? 'enabled' : 'disabled';
  _syncOutlinePosition();
  if (toggle) toggle.hidden = !enabled;
  if (!enabled) {
    _panelOpen = false;
    if (wrapper) wrapper.hidden = true;
  }
}

function _expandOutlineRenderWindow() {
  if (typeof _currentMessageRenderWindowSize !== 'function' ||
      typeof _messageRenderableMessageCount !== 'function' ||
      typeof _messageRenderWindowSize === 'undefined') return;
  _messageRenderWindowSize = Math.max(
    _currentMessageRenderWindowSize(),
    _messageRenderableMessageCount()
  );
}

function _ensureOutlineMessagesLoaded(sid) {
  if (!sid || S.busy || S.activeStreamId) return Promise.resolve(false);
  if (typeof _messagesTruncated === 'undefined' || !_messagesTruncated) {
    return Promise.resolve(false);
  }
  if (typeof _ensureAllMessagesLoaded !== 'function') return Promise.resolve(false);
  return _ensureAllMessagesLoaded().then(function() {
    if (!S.session || S.session.session_id !== sid) return false;
    _expandOutlineRenderWindow();
    return true;
  }).catch(function() {
    return false;
  });
}

// Extracts the first 60 visible characters from a message content value.
function _excerptText(content) {
  let text = '';
  if (Array.isArray(content)) {
    text = content
      .filter(p => p && p.type === 'text')
      .map(p => p.text || p.content || '')
      .join(' ');
  } else {
    text = String(content || '');
  }
  text = text.trim().replace(/\s+/g, ' ');
  return text.length > 60 ? text.slice(0, 60) + '…' : text;
}

// Scrolls to a user message row identified by its rawIdx and flashes it.
async function _jumpToMessage(rawIdx) {
  const sid = _currentSid();
  if (!sid) return;

  const rowId = 'msg-user-' + rawIdx;
  const targetRow = function() {
    const row = document.getElementById(rowId);
    // Loading full history changes local indices without rebuilding the DOM.
    // A tail row with the same local id must not impersonate this target.
    return row && Number(row.dataset.sessionMsgIdx) === _messageSessionIndexForRawIdx(rawIdx) ? row : null;
  };
  const scrollToRow = function() {
    if (_currentSid() !== sid) return;
    const row = targetRow();
    if (!row) return;
    // Use the same smooth-scroll ownership as response jumps. Cancel delayed
    // bottom writes before scrolling, including the already-rendered-row path.
    const container = document.getElementById('messages');
    _beginMessageJumpScroll(container);
    // Far jumps land instantly (as search and jump-to-question do) so the landing cue is seen from its start.
    const behavior = typeof _messageJumpScrollBehavior === 'function'
      ? _messageJumpScrollBehavior(container, row) : 'smooth';
    row.scrollIntoView({ block: 'center', behavior: behavior });
    _flashRow(row);
  };
  if (targetRow()) {
    _cancelBottomSettle();
    scrollToRow();
    return;
  }

  if (S.busy || S.activeStreamId) return;
  try {
    // A missing DOM row is not missing history. The shared loader requests
    // /api/session with msg_limit=all only while truncated, and preserves
    // message generations, ephemeral turn fields and tool-call metadata.
    if (typeof _ensureAllMessagesLoaded !== 'function') return;
    await _ensureAllMessagesLoaded();
    if (_currentSid() !== sid || S.busy || S.activeStreamId) return;
    if (typeof _messagesTruncated !== 'undefined' && _messagesTruncated) return;
    _cancelBottomSettle();
    _beginMessageJumpScroll(document.getElementById('messages'));
    _expandOutlineRenderWindow();
    if (typeof renderMessages === 'function') renderMessages({ preserveScroll: true });
    // Re-arm ownership after a potentially long synchronous render, before
    // starting the native animation. No arbitrary delay or bottom-follow race.
    requestAnimationFrame(scrollToRow);
  } catch (_) {}
}

// Content-search jump (#4159, hardened in #5106). `fullIdx` is the server's
// `match_message_idx`: an index into the COMPLETE transcript. Rendered rows are
// stamped with LOCAL indices (msg-user-<localIdx>, data-msg-idx) offset by
// _oldestIdx while a long session is paged, so the full index is translated and
// never used as a DOM id directly. `targetSid` is the session that was clicked.
// Resolves true once the jump is handed to the transcript's jump helper.
// Same text the server's content search scans (_session_search_message_text):
// a string content, or the text parts of a multi-part content list.
function _searchHitMessageText(message) {
  const content = message && typeof message === 'object' ? message.content : '';
  if (Array.isArray(content)) {
    return content
      .filter(function(part) { return part && typeof part === 'object' && part.type === 'text'; })
      .map(function(part) { return String(part.text || ''); })
      .join(' ');
  }
  return String(content || '');
}

async function _jumpToFullSessionMessage(fullIdx, targetSid, query) {
  const sid = targetSid || _currentSid();
  if (!sid || !Number.isSafeInteger(fullIdx) || fullIdx < 0) return false;
  // The user may have switched (or started switching) sessions before this
  // fired: never apply one session's match index to another's transcript.
  const stillTarget = function() {
    if (_currentSid() !== sid) return false;
    return typeof _loadingSessionId === 'undefined' || _loadingSessionId === null || _loadingSessionId === sid;
  };
  if (!stillTarget()) return false;
  let rerender = false;
  const truncated = (typeof _messagesTruncated !== 'undefined' && !!_messagesTruncated) ||
    _messageSessionIndexBase() > 0;
  if (truncated && !(S.busy || S.activeStreamId) && typeof _ensureAllMessagesLoaded === 'function') {
    // Uncapped full-history load (msg_limit=all), not a larger tail window, so a
    // match near the start of a very long session is still reachable. It resets
    // _oldestIdx to 0, making local == full for the translation below. Nobody
    // asked for this load, so the user may send (or finish) a turn while it is
    // out: rejectIfStale makes a late reply leave the live transcript alone,
    // and the session then just stays open without the jump.
    let loadedAll;
    try { loadedAll = await _ensureAllMessagesLoaded({ rejectIfStale: true }); } catch (_) { return false; }
    if (loadedAll === false) return false;
    if (!stillTarget()) return false;
    rerender = true;
  }
  let localIdx = _messageRawIdxForSessionIndex(fullIdx);
  let expectedAbs = fullIdx;
  const loaded = S.messages || [];
  let msg = Number.isSafeInteger(localIdx) && localIdx >= 0 ? loaded[localIdx] : null;
  // The server indexes the session's OWN stored messages, but the transcript can
  // show a merged view (a compression continuation stitches its parent's archived
  // rows in front, messaging sessions merge CLI rows), which shifts indices. With
  // the query in hand, never highlight a message that does not contain it: fall
  // back to the first rendered user/assistant message that does, else no jump.
  const q = String(query || '').trim().toLowerCase();
  if (q && !(msg && _searchHitMessageText(msg).toLowerCase().includes(q))) {
    localIdx = -1;
    msg = null;
    for (let i = 0; i < loaded.length; i++) {
      const m = loaded[i];
      if (!m || (m.role !== 'user' && m.role !== 'assistant')) continue;
      if (_searchHitMessageText(m).toLowerCase().includes(q)) { localIdx = i; msg = m; break; }
    }
    if (!msg) return false;
    expectedAbs = _messageSessionIndexBase() + localIdx;
  }
  if (!msg || !Number.isSafeInteger(localIdx) || localIdx < 0) return false;  // not in the loaded window
  const container = document.getElementById('messages');
  // Loading full history elsewhere (e.g. the outline panel) re-bases indices
  // without a re-render: a stale row with the same local id must not stand in
  // for the target, so re-render first when its absolute index disagrees.
  const targetVisible = function() {
    const rows = container ? container.querySelectorAll('[data-msg-idx="' + localIdx + '"]') : [];
    for (let i = 0; i < rows.length; i++) if (rows[i].getClientRects().length > 0) return true;
    return false;
  };
  let offWindow = false;
  if (!rerender && container) {
    const rows = container.querySelectorAll('[data-msg-idx="' + localIdx + '"]');
    for (let i = 0; i < rows.length; i++) {
      const abs = Number(rows[i].dataset && rows[i].dataset.sessionMsgIdx);
      if (Number.isFinite(abs) && abs !== expectedAbs) { rerender = true; break; }
    }
    if (!rerender) offWindow = !targetVisible();
  }
  // Fresh or stale history: render the whole transcript so the rows match the loaded messages.
  if (rerender) {
    _cancelBottomSettle();
    _beginMessageJumpScroll(container);
    _expandOutlineRenderWindow();
    if (typeof renderMessages === 'function') renderMessages({ preserveScroll: true, _virtualFallback: true });
    await new Promise(function(resolve) { requestAnimationFrame(resolve); });
    if (!stillTarget()) return false;
  }
  // Delegate to the transcript's virtualization-aware jump: it materializes a
  // target outside the virtual window before scrolling, and resolves assistant
  // segments as well as user rows. A user hit passes no assistant index so the
  // row itself is centred and highlighted.
  if (typeof jumpToTurnQuestion !== 'function') return false;
  const assistantIdx = msg.role === 'user' ? -1 : localIdx;
  await jumpToTurnQuestion(localIdx, assistantIdx);
  if (!offWindow) return true;
  // A target outside the virtual window. jumpToTurnQuestion() mounts it through the window when it can, which is
  // cheap; it cannot when no earlier history is hidden, and then leaves the view at the tail with no error (Codex
  // round F, 2026-10-23: a compression continuation's hit on a phone). Let its mounting frames run, check, and only
  // then render the whole transcript (a full render took ~1.1 s for 1,500 messages on a phone).
  for (let i = 0; i < 3; i++) await new Promise(function(resolve) { requestAnimationFrame(resolve); });
  if (!stillTarget()) return false;
  if (targetVisible()) return true;
  _cancelBottomSettle();
  _beginMessageJumpScroll(container);
  _expandOutlineRenderWindow();
  if (typeof renderMessages === 'function') renderMessages({ preserveScroll: true, _virtualFallback: true });
  await new Promise(function(resolve) { requestAnimationFrame(resolve); });
  if (!stillTarget()) return false;
  await jumpToTurnQuestion(localIdx, assistantIdx);
  return true;
}

// Brief highlight flash on a message row after jumping.
function _flashRow(row) {
  if (!row) return;
  // One landing cue for every jump: the outline panel, search hits and jump-to-question all use the
  // transcript's highlight (row tint plus ring, started on the next frame, static under reduced motion).
  if (typeof _highlightQuestionRow === 'function') { _highlightQuestionRow(row); return; }
  row.classList.remove('outline-jump-flash');
  void row.offsetWidth;   // reflow to restart animation
  row.classList.add('outline-jump-flash');
  window.setTimeout(function() { row.classList.remove('outline-jump-flash'); }, 1200);
}

// Builds the list of user messages from S.messages.
// Returns [{rawIdx, label, excerpt}, …] for every user message with content.
function _buildEntries() {
  const msgs = (S && S.messages) || [];
  const entries = [];
  let userN = 0;

  for (let i = 0; i < msgs.length; i++) {
    const m = msgs[i];
    if (!m || m.role !== 'user') continue;
    const text = _excerptText(m.content);
    if (!text) continue;
    userN++;
    entries.push({ rawIdx: i, label: userN, excerpt: text });
  }
  return entries;
}

// Renders the panel body.  Called every time the panel opens or session changes.
function _renderPanel() {
  const panel = document.getElementById('outlinePanel');
  if (!panel) return;

  const sid = _currentSid();

  // Session-scoped staleness guard.
  if (!sid) {
    panel.innerHTML = '<p class="outline-empty">' + t('outline_empty') + '</p>';
    _outlineSid = null;
    return;
  }

  if (!S.messages) {
    panel.innerHTML = '<p class="outline-empty">' + t('outline_loading') + '</p>';
    _outlineSid = sid;
    return;
  }

  _outlineSid = sid;
  const entries = _buildEntries();

  if (!entries.length) {
    panel.innerHTML = '<p class="outline-empty">' + t('outline_empty') + '</p>';
    return;
  }

  const items = entries.map(function(e) {
    return '<button class="outline-entry" type="button" ' +
      'onclick="window._outlineJump(' + e.rawIdx + ')">' +
      '<span class="outline-entry-num">' + e.label + '</span>' +
      '<span class="outline-entry-text">' + _escHtml(e.excerpt) + '</span>' +
      '</button>';
  });

  panel.innerHTML = items.join('');
}

// Simple HTML-escape for entry text.
function _escHtml(str) {
  return String(str)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

// Opens or closes the outline panel.
function toggleOutlinePanel() {
  if (!_outlineAllowed()) {
    applyConversationOutlinePreference();
    return;
  }
  _panelOpen = !_panelOpen;
  const wrapper = document.getElementById('outlinePanelWrapper');
  if (!wrapper) return;

  if (_panelOpen) {
    _syncOutlinePosition();
    wrapper.hidden = false;
    const sid = _currentSid();
    const panel = document.getElementById('outlinePanel');
    if (panel) panel.innerHTML = '<p class="outline-empty">' + t('outline_loading') + '</p>';
    _ensureOutlineMessagesLoaded(sid).then(function() {
      if (!_panelOpen || _currentSid() !== sid) return;
      _renderPanel();
      // Keep rendered data fresh after every renderMessages() call.
      _outlineSid = _currentSid();
    });
  } else {
    wrapper.hidden = true;
  }
}

// Jump target exposed on window so inline onclick handlers can reach it.
window._outlineJump = _jumpToMessage;
// Full-session-index jump for the sidebar content-search click (sessions.js);
// exported because this IIFE is otherwise unreachable from other scripts.
window._jumpToFullSessionMessage = _jumpToFullSessionMessage;
window.applyConversationOutlinePreference = applyConversationOutlinePreference;

// Re-render after renderMessages() if the panel is open and the session
// changed or new messages arrived since the last render.
(function _hookRenderMessages() {
  if (typeof window._outlineRenderHooked !== 'undefined') return;

  const _orig = window.renderMessages;
  if (typeof _orig !== 'function') {
    // renderMessages may not be defined yet — retry after DOMContentLoaded.
    if (!window._outlineRenderHookPending) {
      window._outlineRenderHookPending = true;
      document.addEventListener('DOMContentLoaded', _hookRenderMessages, { once: true });
    }
    return;
  }
  window._outlineRenderHooked = true;
  window._outlineRenderHookPending = false;
  window.renderMessages = function() {
    const result = _orig.apply(this, arguments);
    if (_panelOpen) {
      const sid = _currentSid();
      if (sid && (sid !== _outlineSid || (S.messages || []).length > 0)) {
        _renderPanel();
      }
    }
    return result;
  };
})();

// Expose public API.
window.toggleOutlinePanel = toggleOutlinePanel;

document.addEventListener('DOMContentLoaded', function() {
  applyConversationOutlinePreference();
  const root = document.documentElement;
  const rightPanel = document.querySelector('.rightpanel');
  if (rightPanel && typeof ResizeObserver !== 'undefined' && !_outlineResizeObserver) {
    _outlineResizeObserver = new ResizeObserver(_syncOutlinePosition);
    _outlineResizeObserver.observe(rightPanel);
  }
  if (!_outlineWorkspaceObserver) {
    _outlineWorkspaceObserver = new MutationObserver(applyConversationOutlinePreference);
    _outlineWorkspaceObserver.observe(root, {
      attributes: true,
      attributeFilter: ['data-workspace-panel']
    });
    // Also re-evaluate when the active main panel changes. switchPanel() is a
    // global function declaration (called via inline onclick), so it can't be
    // reliably wrapped from this script; instead we watch the `showing-<panel>`
    // class it toggles on <main>. The outline is a chat-only affordance, so this
    // hides the toggle + closes the panel when leaving chat (settings, tasks,
    // insights, …) and restores the toggle on return to chat. _outlineAllowed()
    // reads _currentPanel for the actual gate; this observer just triggers it.
    const mainEl = document.querySelector('main.main');
    if (mainEl) {
      _outlineWorkspaceObserver.observe(mainEl, {
        attributes: true,
        attributeFilter: ['class']
      });
    }
  }
});
window.addEventListener('resize', applyConversationOutlinePreference);

})();
