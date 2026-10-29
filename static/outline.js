// ── Conversation Outline Panel (#2124) ───────────────────────────────────────
// Floating panel listing user messages as jump targets.
// _outlineSid guards against stale renders when the user switches sessions.

'use strict';

(function() {

let _outlineSid = null;       // session id the panel was last built for
let _panelOpen  = false;      // whether the panel is currently visible
let _outlineResizeObserver = null;
let _outlineWorkspaceObserver = null;
let _sheetChrome = false;     // compact-only modal chrome (dialog + focus) applied

// Returns the current session id, or null if no session is loaded.
function _currentSid() {
  return (S && S.session && S.session.session_id) || null;
}

// Narrow viewports get the SAME outline, rendered as a bottom sheet instead of
// the desktop floating panel — the 320px-wide side panel has nowhere to sit on
// a phone, so style.css re-shapes it into a bottom-anchored sheet at the same
// 900px breakpoint. Every form-dependent call site asks this helper: the
// visibility gate, the backdrop, the modal chrome, and the "collapse the sheet
// after jumping" behavior.
let _outlineCompactMedia = null;
let _outlineCompactMediaWired = false;

function _outlineCompactQuery() {
  if (_outlineCompactMedia) return _outlineCompactMedia;
  if (!window.matchMedia) return null;
  _outlineCompactMedia = window.matchMedia('(max-width:900px)');
  return _outlineCompactMedia;
}

function _outlineIsCompact() {
  const mq = _outlineCompactQuery();
  return !!(mq && mq.matches);
}

function _outlineAllowed() {
  // The outline is a chat-view affordance only — never show the toggle or panel
  // while another MAIN panel (settings, tasks, insights, …) is active. _currentPanel
  // is owned by panels.js; treat an undefined/absent value as the chat default.
  // 'todos' is a sidebar-only panel that leaves the chat transcript in <main>, so
  // the outline stays valid there too (and switching to it emits no <main> class
  // mutation for the observer, so allowing it keeps the toggle stable).
  const panel = (typeof _currentPanel === 'undefined') ? 'chat' : (_currentPanel || 'chat');
  const onChatView = panel === 'chat' || panel === 'todos';
  return window._showConversationOutline === true && onChatView;
}

// The dimming layer behind the bottom-sheet form. Only exists on narrow
// viewports (and only while the panel is open); tapping it closes the sheet.
// Desktop keeps its non-modal floating panel and never gets a backdrop.
function _syncOutlineBackdrop() {
  const wanted = _panelOpen && _outlineIsCompact();
  let backdrop = document.getElementById('outlineBackdrop');

  if (!wanted) {
    if (backdrop) backdrop.remove();
    return;
  }
  if (backdrop) return;

  backdrop = document.createElement('div');
  backdrop.id = 'outlineBackdrop';
  backdrop.className = 'outline-backdrop';
  backdrop.setAttribute('aria-hidden', 'true');
  // Keep focus inside the sheet through the tap so the close path can hand it
  // back to the toggle (a mousedown on the backdrop would otherwise move focus
  // to <body> before the click handler runs).
  backdrop.addEventListener('mousedown', function(ev) { ev.preventDefault(); });
  backdrop.addEventListener('click', closeOutlinePanel);
  document.body.appendChild(backdrop);
}

// ── Compact chrome: dialog semantics + focus lifecycle ───────────────────────
// The backdrop blocks pointer access to the page, so the compact form is modal
// in behavior even though the desktop panel is plain navigation. It therefore
// gets the full modal contract: `role="dialog"` + `aria-modal`, focus moved
// into the sheet on open, Tab contained, Escape to close, and focus handed back
// to the outline toggle. Desktop (and any width that is not compact) keeps
// `role="navigation"` and none of this — it stays non-modal.
const _SHEET_FOCUSABLE =
  'a[href], button, textarea, input, select, summary, [tabindex]:not([tabindex="-1"])';

function _sheetFocusable() {
  const wrapper = document.getElementById('outlinePanelWrapper');
  if (!wrapper) return [];
  const found = wrapper.querySelectorAll(_SHEET_FOCUSABLE);
  return Array.prototype.filter.call(found, function(el) {
    if (el.disabled || el.hidden || el.tabIndex < 0) return false;
    const style = window.getComputedStyle ? window.getComputedStyle(el) : null;
    return !style || (style.display !== 'none' && style.visibility !== 'hidden');
  });
}

// True when an approval or clarification card is actually on screen. Both are
// time-sensitive safety surfaces rendered in the composer, i.e. underneath the
// sheet's pointer-blocking backdrop. Shared by the preemption hooks and the
// breakpoint gate, so a sheet is never built over a prompt that is ALREADY up.
function _visiblePromptCard() {
  const ids = ['approvalCard', 'clarifyCard'];
  for (let i = 0; i < ids.length; i++) {
    const id = ids[i];
    if (_promptCardVisible(id)) return document.getElementById(id);
  }
  return null;
}

function _onSheetKeydown(ev) {
  if (ev.key === 'Escape') {
    // Runs in the CAPTURE phase, so the app-wide Escape handler (boot.js) never
    // sees this one: closing the sheet must not also clear the sidebar search.
    ev.preventDefault();
    ev.stopPropagation();
    closeOutlinePanel();
    return;
  }
  if ((ev.metaKey || ev.ctrlKey) && !ev.altKey && (ev.key === 'k' || ev.key === 'K' || ev.key === '/')) {
    // New Chat / focus-composer are app-level chords that open or focus things
    // BEHIND the sheet. Get out of the way first; the chord itself still runs,
    // so the sheet never traps focus back over the thing the user just opened.
    closeOutlinePanel();
    return;
  }
  if (ev.key !== 'Tab') return;
  const els = _sheetFocusable();
  if (!els.length) {
    ev.preventDefault();
    return;
  }
  const current = document.activeElement;
  let idx = els.indexOf(current);
  if (idx === -1) {
    ev.preventDefault();
    // Plain focus() on purpose: Tab navigation must scroll the entry into view.
    // A preventScroll focus parks focus on an off-screen row in a long outline.
    els[0].focus();
    return;
  }
  idx = (idx + (ev.shiftKey ? -1 : 1) + els.length) % els.length;
  ev.preventDefault();
  els[idx].focus();
}

// Idempotent: the gate refreshes on every resize/rotation, and only the
// transition into the compact form may touch focus or the key listener.
// Focus containment for focus moves the key handler cannot see: the approval
// poller re-focuses its "once" button on every redraw, which would otherwise
// park keyboard focus underneath the backdrop while the sheet is open.
function _onSheetFocusIn(ev) {
  const wrapper = document.getElementById('outlinePanelWrapper');
  if (!wrapper || wrapper.hidden || wrapper.contains(ev.target)) return;
  const closeBtn = wrapper.querySelector('.outline-close-btn');
  const target = closeBtn || _sheetFocusable()[0];
  if (target && target.focus) target.focus({ preventScroll: true });
}

function _applySheetChrome() {
  if (_sheetChrome) return;
  const wrapper = document.getElementById('outlinePanelWrapper');
  if (!wrapper) return;
  _sheetChrome = true;
  wrapper.setAttribute('role', 'dialog');
  wrapper.setAttribute('aria-modal', 'true');
  // Capture phase: Escape and the app-level chords must be settled BEFORE the
  // global handlers in boot.js act on them.
  document.addEventListener('keydown', _onSheetKeydown, true);
  document.addEventListener('focusin', _onSheetFocusIn);
  const closeBtn = wrapper.querySelector('.outline-close-btn');
  const target = closeBtn || _sheetFocusable()[0];
  if (target && target.focus) target.focus({ preventScroll: true });
}

function _removeSheetChrome(restoreFocus) {
  if (!_sheetChrome) return;
  _sheetChrome = false;
  document.removeEventListener('keydown', _onSheetKeydown, true);
  document.removeEventListener('focusin', _onSheetFocusIn);
  const wrapper = document.getElementById('outlinePanelWrapper');
  if (!wrapper) return;
  wrapper.removeAttribute('aria-modal');
  wrapper.setAttribute('role', 'navigation');
  // Only reclaim focus that is still inside the sheet: a close triggered by
  // something else (panel switch, incoming prompt) must not steal it back.
  if (restoreFocus !== false && wrapper.contains(document.activeElement)) {
    const toggle = document.getElementById('outlineToggleBtn');
    if (toggle && toggle.focus) toggle.focus({ preventScroll: true });
  }
}

// The compact chrome follows both the open state and the current width, so it
// is re-evaluated on every gate refresh (open, close, resize, rotation).
function _syncSheetChrome(restoreFocus) {
  if (_panelOpen && _outlineIsCompact()) _applySheetChrome();
  else _removeSheetChrome(restoreFocus);
}

// Closes the panel and any compact-only chrome. Shared by the close button,
// the backdrop tap, Escape, panel switches, and post-jump auto-collapse.
// `options.yieldToPrompt` is used by the approval/clarification preemption path:
// it tears the modal chrome down WITHOUT pulling focus back to the toggle,
// because the incoming prompt takes focus next.
function closeOutlinePanel(options) {
  const yieldToPrompt = !!(options && options.yieldToPrompt);
  _panelOpen = false;
  const wrapper = document.getElementById('outlinePanelWrapper');
  if (wrapper) wrapper.hidden = true;
  _syncSheetChrome(!yieldToPrompt);
  _syncOutlineBackdrop();
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
  const enabled = _outlineAllowed();
  document.documentElement.dataset.conversationOutline = enabled ? 'enabled' : 'disabled';
  _syncOutlinePosition();
  if (toggle) toggle.hidden = !enabled;
  if (!enabled) {
    closeOutlinePanel();
  } else if (_panelOpen && _outlineIsCompact() && _visiblePromptCard()) {
    // The viewport narrowed under an open desktop panel (rotation, devtools,
    // sidebar) while an approval or clarification is on screen: master closes
    // the outline there, and a sheet would land ON TOP of the safety surface and
    // steal focus. A deliberate re-open from the toggle is NOT routed here, so
    // the user can still review an earlier question over a pending prompt.
    closeOutlinePanel({ yieldToPrompt: true });
  } else {
    // Width can change under an open panel (rotation, resize, sidebar toggle),
    // so re-evaluate the compact-only chrome (backdrop + modal semantics) on
    // every gate refresh.
    _syncOutlineBackdrop();
    _syncSheetChrome();
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
    _beginMessageJumpScroll(document.getElementById('messages'));
    row.scrollIntoView({ block: 'center', behavior: 'smooth' });
    _flashRow(row);
    // Compact sheet: the jump is the terminus of the interaction, so close the
    // sheet from the single scroll path that both branches above funnel into.
    // Guarded because upstream's white-box jump tests eval this function alone,
    // with only the collaborators it needs stubbed (no sheet helpers).
    if (typeof _collapseAfterJump === 'function') _collapseAfterJump();
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

// Brief highlight flash on a message row after jumping.
function _flashRow(row) {
  if (!row) return;
  row.classList.remove('outline-jump-flash');
  void row.offsetWidth;   // reflow to restart animation
  row.classList.add('outline-jump-flash');
  window.setTimeout(function() { row.classList.remove('outline-jump-flash'); }, 1200);
}

// On narrow viewports the outline is a modal bottom sheet that covers the
// transcript — so a successful jump must also get out of the way. Desktop's
// floating panel stays open, matching its non-modal behavior.
function _collapseAfterJump() {
  if (_outlineIsCompact()) closeOutlinePanel();
}

// ── Incoming prompts preempt the sheet ───────────────────────────────────────
// An approval or clarification is a time-sensitive safety surface rendered in
// the composer — i.e. underneath the sheet's full-screen, pointer-blocking
// backdrop. Rather than fight over z-index, the compact sheet yields the moment
// either prompt opens. Both entry points are wrapped here (outline.js is loaded
// after messages.js) so messages.js keeps owning prompt rendering while this
// file keeps owning the sheet; nothing else in the prompt path changes.
function _yieldSheetToPrompt(cardId) {
  if (!_panelOpen || !_outlineIsCompact()) return;
  const card = document.getElementById(cardId);
  if (!card || card.hidden || !card.classList.contains('visible')) return;
  closeOutlinePanel({ yieldToPrompt: true });
}

// A card is "already up" while the pollers keep redrawing it (every 1.5 s).
function _promptCardVisible(cardId) {
  const card = document.getElementById(cardId);
  return !!(card && !card.hidden && card.classList.contains('visible'));
}

function _hookPromptPreemption() {
  const hooks = [['showApprovalCard', 'approvalCard'], ['showClarifyCard', 'clarifyCard']];
  for (let i = 0; i < hooks.length; i++) {
    const name = hooks[i][0];
    const cardId = hooks[i][1];
    const original = window[name];
    if (typeof original !== 'function' || original.__outlinePreemptionHook) continue;
    const wrapped = function() {
      // Yield only when the prompt APPEARS. The approval/clarify pollers redraw
      // the same visible card every 1.5 s, and a sheet the user deliberately
      // reopened over a pending prompt must not be torn down on every tick.
      const wasVisible = _promptCardVisible(cardId);
      const result = original.apply(this, arguments);
      if (!wasVisible) _yieldSheetToPrompt(cardId);
      return result;
    };
    wrapped.__outlinePreemptionHook = true;
    window[name] = wrapped;
  }
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
  const wrapper = document.getElementById('outlinePanelWrapper');
  if (!wrapper) return;

  if (_panelOpen) {
    closeOutlinePanel();
    return;
  }

  _panelOpen = true;
  _syncOutlinePosition();
  wrapper.hidden = false;
  _syncOutlineBackdrop();
  _syncSheetChrome();
  const sid = _currentSid();
  const panel = document.getElementById('outlinePanel');
  if (panel) panel.innerHTML = '<p class="outline-empty">' + t('outline_loading') + '</p>';
  _ensureOutlineMessagesLoaded(sid).then(function() {
    if (!_panelOpen || _currentSid() !== sid) return;
    _renderPanel();
    // Keep rendered data fresh after every renderMessages() call.
    _outlineSid = _currentSid();
  });
}

// Jump target exposed on window so inline onclick handlers can reach it.
window._outlineJump = _jumpToMessage;
window.applyConversationOutlinePreference = applyConversationOutlinePreference;
window.closeOutlinePanel = closeOutlinePanel;

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

// Wrap the approval/clarification entry points now that both scripts have run,
// and again on DOMContentLoaded in case this file was ever loaded earlier.
_hookPromptPreemption();

document.addEventListener('DOMContentLoaded', function() {
  _hookPromptPreemption();
  applyConversationOutlinePreference();
  const root = document.documentElement;
  // The breakpoint drives the gate directly, not only through window resize:
  // environments that change the effective viewport without a resize event
  // (devtools responsive mode, some orientation changes) would otherwise leave
  // an open panel in the wrong form.
  if (!_outlineCompactMediaWired) {
    const compactMq = _outlineCompactQuery();
    if (compactMq && typeof compactMq.addEventListener === 'function') {
      compactMq.addEventListener('change', applyConversationOutlinePreference);
      _outlineCompactMediaWired = true;
    }
  }
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
