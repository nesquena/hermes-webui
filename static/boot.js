// Early boot initialization that must run before any other code.
// These run during script evaluation to handle server-stopped state
// and cross-tab shutdown broadcasts as early as possible.
(function(){
  // Clear stale stop-server flag on successful page load (server is reachable)
  try{localStorage.removeItem('hermes-webui-server-stopped');}catch(_){}
  // Listen for shutdown broadcast from other tabs
  try {
    var _stopChan = new BroadcastChannel('hermes-webui-shutdown');
    _stopChan.onmessage = function() { _showServerStopped(); };
  } catch(_) {}
})();

// cancelStream: stop the active chat stream.
// See docs/rfcs/webui-run-state-consistency-contract.md (Invariants #2, #4)
// for the owner-aware + terminal-settle rationale.
async function cancelStream(reason){
  const sid = S.session && S.session.session_id;
  const streamId = S.activeStreamId;
  if(!streamId) return;
  // Interrupt provenance: log WHY the active run is being cancelled so operators
  // can tell an explicit Stop / interrupt from any other trigger when they see a
  // SIGINT/exit-code-130 in the backend logs. Only explicit user paths reach
  // this function (Stop button, /stop, /interrupt, busy-interrupt); passive
  // lifecycle events — session switch, tab hide, page unload — tear down the
  // LOCAL SSE transport via closeLiveStream() and never call /api/chat/cancel,
  // so they never interrupt the backend agent/tool run. (#5345)
  const _reason = reason || 'explicit-cancel';
  if(typeof console !== 'undefined' && console.info){
    console.info('[stream] cancel requested', {reason:_reason, streamId, sessionId:sid});
  }
  let respBody=null;
  try{
    const r=await fetch(new URL(`api/chat/cancel?stream_id=${encodeURIComponent(streamId)}`,document.baseURI||location.href).href,{credentials:'include'});
    try{respBody=await r.json();}catch(_){}
  }catch(e){
    if(typeof console !== 'undefined' && console.warn){
      console.warn('cancelStream: /api/chat/cancel request failed', e);
    }
  }
  // Active-session cancel should not tear down the current SSE transport before
  // the backend emits its terminal event; do that only for stale owner paths
  // where the user moved on to a different stream before this request
  // completed.
  if(sid && S.activeStreamId !== streamId && typeof closeLiveStream==='function'){
    closeLiveStream(sid, streamId);
  }
  // Owner guard: if the backend accepted the active-session cancel, leave
  // the current SSE transport and owner state intact so the terminal
  // `cancel` event can clear INFLIGHT, render "Task cancelled", and refresh
  // the sidebar. Only clear locally when the backend says there is no active
  // stream left to settle.
  if(respBody && respBody.cancelled===false && S.activeStreamId===streamId){
    S.activeStreamId=null;
    setBusy(false);
    if(typeof setComposerStatus==='function') setComposerStatus('');
    else setStatus('');
    // /api/chat/cancel only exposes `cancelled:bool`, so we cannot
    // distinguish reasons — keep the toast generic and short.
    if(typeof showToast==='function') showToast('Stream is no longer active',2000);
  }
}

async function cancelSessionStream(session){
  const streamId = session&&session.active_stream_id;
  const sid = session&&session.session_id;
  if(!streamId||!sid) return;
  // Explicit sidebar "Stop response" — log provenance for the same reason as
  // cancelStream(). (#5345)
  if(typeof console !== 'undefined' && console.info){
    console.info('[stream] cancel requested', {reason:'sidebar-stop', streamId, sessionId:sid});
  }
  try{
    await fetch(new URL(`api/chat/cancel?stream_id=${encodeURIComponent(streamId)}`,document.baseURI||location.href).href,{credentials:'include'});
  }catch(e){/* close local stream; keep UI state honest below */}
  if(typeof closeLiveStream==='function') closeLiveStream(sid, streamId);
  session.active_stream_id=null;
  delete INFLIGHT[sid];
  clearInflightState(sid);
  if(S.session&&S.session.session_id===sid){
    S.activeStreamId=null;
    if(S.session) S.session.active_stream_id=null;
    clearInflight();
    setBusy(false);
    if(typeof setComposerStatus==='function') setComposerStatus('');
    else setStatus('');
  }
  if(typeof _approvalSessionId!=='undefined' && _approvalSessionId===sid){
    stopApprovalPolling();
    hideApprovalCard(true);
  }
  if(typeof _clarifySessionId!=='undefined' && _clarifySessionId===sid){
    stopClarifyPolling();
    hideClarifyCard(true, 'cancelled');
  }
  if(typeof renderSessionList==='function') renderSessionList();
}

async function _savedSessionShouldStaySidebarOnly(sid){
  const state = await _savedSessionSidebarOnlyState(sid);
  return !!(state&&state.sidebarOnly);
}

async function _savedSessionSidebarOnlyState(sid){
  if(!sid) return false;
  try{
    const data = await api(`/api/session?session_id=${encodeURIComponent(sid)}&messages=0&resolve_model=0`);
    const session = data&&data.session;
    const archived = !!(session&&session.archived);
    const running = !!(session&&(session.active_stream_id||session.pending_user_message));
    return {sidebarOnly:archived||running, archived};
  }catch(e){
    return null;
  }
}

// ── Mobile navigation ──────────────────────────────────────────────────────
// URL prefill boot helpers.
function _prefillHasDraftText(prefillIntent){
  return !!(prefillIntent&&prefillIntent.hasText);
}
function _rootPrefillNeedsFreshComposer(urlSession, savedLocal, prefillIntent){
  return !urlSession&&!!savedLocal&&_prefillHasDraftText(prefillIntent);
}
function _profileQueryBlocksSavedLocalRestore(profileIntent, urlSession){
  return !!(profileIntent&&profileIntent.hasParam&&profileIntent.valid&&!urlSession);
}
async function _applyComposerPrefillOnBoot(prefillIntent){
  if(!prefillIntent||!prefillIntent.hasText) return;
  const msg=(typeof $==='function')?$('msg'):document.getElementById('msg');
  if(!msg) return;
  const text=String(prefillIntent.text||'');
  msg.value=text;
  if(typeof autoResize==='function') autoResize();
  else if(typeof updateSendBtn==='function') updateSendBtn();
  if(typeof msg.focus==='function') msg.focus();
}
async function _finalizeComposerPrefillOnBoot(prefillIntent){
  if(prefillIntent&&prefillIntent.hasParams&&typeof _consumeComposerPrefillParamsFromLocation==='function'){
    _consumeComposerPrefillParamsFromLocation();
  }
  await _applyComposerPrefillOnBoot(prefillIntent);
}

// Mobile navigation.
let _workspacePanelMode='closed'; // 'closed' | 'browse' | 'preview'

function _isCompactWorkspaceViewport(){
  return window.matchMedia('(max-width: 900px)').matches;
}

function _isPhoneWidthViewport(){
  return window.matchMedia('(max-width: 640px)').matches;
}

function _isTouchKeyboardViewport(){
  try{return matchMedia('(hover:none) and (pointer:coarse)').matches&&!_hasFinePointerCoexisting();}catch(_){return false;}
}

function _syncKeyboardBottomInset(){
  const root=document.documentElement;
  if(!root) return;
  if(!window.visualViewport||!_isTouchKeyboardViewport()){
    root.style.removeProperty('--keyboard-bottom-inset');
    return;
  }
  const vv=window.visualViewport;
  // A pinch-zoomed viewport (vv.scale != 1) makes innerHeight - vv.height
  // reflect the zoom, not the keyboard — on Chromium touch devices with
  // accessibility "force enable zoom" that yields a large spurious inset that
  // jitters on pan. Treat only the unzoomed state as keyboard occlusion.
  if(Math.abs((vv.scale||1)-1)>0.05){
    root.style.removeProperty('--keyboard-bottom-inset');
    return;
  }
  const inset=Math.max(0,Math.ceil(window.innerHeight-(vv.height+vv.offsetTop)));
  if(inset>0){
    root.style.setProperty('--keyboard-bottom-inset',`${inset}px`);
  }else{
    root.style.removeProperty('--keyboard-bottom-inset');
  }
}

// Mobile PWA viewport reflow guard. When the on-screen keyboard / browser
// chrome shows or hides, visualViewport (or a plain resize on browsers without
// it) changes height without a layout invalidation, leaving the phone layout
// painted against stale geometry. Toggling a one-frame `viewport-reflow` class
// (which applies a cheap GPU-promotion transform under the @media(max-width:640px)
// rule) forces a repaint, then we resync the workspace panel + sidebar aria.
function _forceMobileViewportReflow(){
  _syncKeyboardBottomInset();
  if(!_isPhoneWidthViewport()) return;
  const layout=document.querySelector('.layout');
  if(!layout) return;
  document.documentElement.classList.add('viewport-reflow');
  void layout.offsetWidth;
  requestAnimationFrame(()=>{
    document.documentElement.classList.remove('viewport-reflow');
    try{ syncWorkspacePanelState(); }catch(_){ }
    try{ if(typeof _syncSidebarAria==='function') _syncSidebarAria(); }catch(_){ }
  });
}

function _syncWorkspacePanelInlineWidth(){
  const {panel}= _workspacePanelEls();
  if(!panel) return;

  const isCompact = _isCompactWorkspaceViewport();
  if(isCompact){
    if(panel.style.width) panel.style.removeProperty('width');
    return;
  }

  const saved = localStorage.getItem('hermes-panel-w');
  if(!saved) return;
  const parsed = parseInt(saved, 10);
  if(Number.isNaN(parsed) || parsed <= 0) return;
  panel.style.width = `${parsed}px`;
}

function _workspacePanelEls(){
  return {
    layout: document.querySelector('.layout'),
    panel: document.querySelector('.rightpanel'),
    toggleBtn: $('btnWorkspacePanelToggle'),
    edgeToggleBtn: $('btnWorkspacePanelEdgeToggle'),
    collapseBtn: $('btnCollapseWorkspacePanel'),
  };
}

function _hasWorkspacePreviewVisible(){
  const preview=$('previewArea');
  return !!(preview&&preview.classList.contains('visible'));
}

function _setWorkspacePanelMode(mode){
  const {layout,panel}= _workspacePanelEls();
  if(!layout||!panel)return;
  _workspacePanelMode=(mode==='browse'||mode==='preview')?mode:'closed';
  const open=_workspacePanelMode!=='closed';
  document.documentElement.dataset.workspacePanel=open?'open':'closed';
  // Persist open/closed across refreshes (browse/preview → open; closed → closed)
  // Do NOT overwrite the user's "keep open" preference — only track runtime state
  // so that toggleWorkspacePanel(false) from the toolbar doesn't clear the setting.
  try{localStorage.setItem('hermes-webui-workspace-panel', open ? 'open' : 'closed');}catch(_){}
  layout.classList.toggle('workspace-panel-collapsed',!open);
  if(_isCompactWorkspaceViewport()){
    panel.classList.toggle('mobile-open',open);
  }else{
    panel.classList.remove('mobile-open');
  }
  syncWorkspacePanelUI();
}

function syncWorkspacePanelState(){
  const hasPreview=_hasWorkspacePreviewVisible();
  if(hasPreview){
    if(_workspacePanelMode==='closed') _setWorkspacePanelMode('preview');
    else syncWorkspacePanelUI();
    return;
  }
  if(!S.session){
    // No active session — if the panel was explicitly opened (browse mode), keep it
    // open so the workspace pane doesn't vanish on a fresh-page or empty-session boot.
    // The file tree will show the "no workspace" placeholder naturally via renderFileTree().
    // Only force-close if the mode is 'preview' (file preview without a session is invalid).
    if(_workspacePanelMode==='preview') _setWorkspacePanelMode('closed');
    else syncWorkspacePanelUI();
    return;
  }
  _setWorkspacePanelMode(_workspacePanelMode==='preview'?'closed':_workspacePanelMode);
}

function openWorkspacePanel(mode='browse'){
  if(mode==='browse'&&!S.session&&!_hasWorkspacePreviewVisible()&&!S._profileDefaultWorkspace)return;
  if(mode==='preview'&&_workspacePanelMode==='browse'){
    syncWorkspacePanelUI();
    return;
  }
  _setWorkspacePanelMode(mode);
}

function closeWorkspacePanel(){
  _setWorkspacePanelMode('closed');
}

function ensureWorkspacePreviewVisible(){
  if(_workspacePanelMode==='closed') _setWorkspacePanelMode('preview');
  else syncWorkspacePanelUI();
}

function handleWorkspaceClose(){
  if(_hasWorkspacePreviewVisible()){
    clearPreview();
    return;
  }
  closeWorkspacePanel();
}

async function _maybeBindFreshDefaultWorkspaceSession(prefillIntent=null){
  if(_prefillHasDraftText(prefillIntent)) return false;
  if(S.session) return false;
  if(_workspacePanelMode!=='browse') return false;
  if(!S._profileDefaultWorkspace) return false;
  try{
    // worktree:false is explicit and load-bearing — this auto-bind runs on
    // page load, and a config-level worktree default must never leak a fresh
    // worktree + branch from simply opening the UI (#6022).
    await newSession(false, {awaitWorkspaceLoad: true, worktree: false});
    return true;
  }catch(e){
    console.warn('[hermes] failed to bind fresh default workspace session', e);
    return false;
  }
}

/**
 * Set a tooltip on a button, preferring the custom CSS tooltip (`data-tooltip`)
 * when the element opts in via the `has-tooltip` class. Falls back to the
 * native `title` attribute for elements that haven't opted in.
 *
 * Critical: when the element DOES have data-tooltip, this MUST also clear any
 * existing native `title` attribute, otherwise the slow ~1.5s native browser
 * tooltip co-fires alongside the fast custom CSS tooltip — exactly the bug
 * #1775 reports. Always pair `data-tooltip` with `removeAttribute('title')`.
 */
function _setButtonTooltip(btn, text){
  if(!btn) return;
  if(btn.hasAttribute('data-tooltip')){
    btn.setAttribute('data-tooltip', text);
    if(btn.hasAttribute('title')) btn.removeAttribute('title');
  } else {
    btn.title = text;
  }
}

function _uiText(key, fallback){
  if(typeof t==='function'){
    const val=t(key);
    if(val&&val!==key) return val;
  }
  return fallback;
}

function syncWorkspacePanelUI(){
  const {layout,panel,toggleBtn,edgeToggleBtn,collapseBtn}= _workspacePanelEls();
  if(!layout||!panel)return;
  const desktopOpen=_workspacePanelMode!=='closed';
  const mobileOpen=panel.classList.contains('mobile-open');
  const isCompact=_isCompactWorkspaceViewport();
  const isOpen=isCompact?mobileOpen:desktopOpen;
  const canBrowse=!!S.session||_hasWorkspacePreviewVisible()||!!(S._profileDefaultWorkspace);
  const hasPreview=_hasWorkspacePreviewVisible();
  if(toggleBtn){
    toggleBtn.classList.toggle('active',isOpen);
    toggleBtn.setAttribute('aria-pressed',isOpen?'true':'false');
    const label=_uiText(isOpen?'workspace_panel_hide':'workspace_panel_show', isOpen?'Hide workspace panel':'Show workspace panel');
    _setButtonTooltip(toggleBtn, label);
    toggleBtn.setAttribute('aria-label', label);
    toggleBtn.disabled=!canBrowse;
  }
  if(edgeToggleBtn){
    edgeToggleBtn.classList.toggle('active',isOpen);
    edgeToggleBtn.setAttribute('aria-expanded',isOpen?'true':'false');
    const label=_uiText(isOpen?'workspace_panel_hide':'workspace_panel_show', isOpen?'Hide workspace panel':'Show workspace panel');
    _setButtonTooltip(edgeToggleBtn, label);
    edgeToggleBtn.setAttribute('aria-label', label);
    edgeToggleBtn.disabled=!canBrowse;
  }
  if(collapseBtn){
    _setButtonTooltip(collapseBtn, isCompact?_uiText('workspace_panel_close','Close workspace panel'):_uiText('workspace_panel_hide','Hide workspace panel'));
  }
  const hasSession=!!S.session;
  ['btnUpDir','btnNewFile','btnNewFolder','btnRefreshPanel'].forEach(id=>{
    const el=$(id);
    if(el)el.disabled=!hasSession;
  });
  const clearBtn=$('btnClearPreview');
  if(clearBtn){
    clearBtn.disabled=!isOpen;
    const label=hasPreview?_uiText('workspace_close_preview','Close preview'):_uiText('terminal_close','Close');
    _setButtonTooltip(clearBtn, label);
    clearBtn.setAttribute('aria-label', label);
    if(!isCompact) clearBtn.style.display='';
  }
}

function toggleMobileSidebar(){
  const sidebar=document.querySelector('.sidebar');
  if(!sidebar)return;
  const isOpen=sidebar.classList.contains('mobile-open');
  if(isOpen){closeMobileSidebar();}
  else{
    try{if(typeof _syncMobileSidebarPanelFromMainView==='function')_syncMobileSidebarPanelFromMainView();}catch(_){}
    sidebar.classList.remove('mobile-session-page');sidebar.classList.add('mobile-panel-drawer','mobile-open');
  }
}
function closeMobileSidebar(){
  const sidebar=document.querySelector('.sidebar');
  const overlay=$('mobileOverlay');
  if(sidebar)sidebar.classList.remove('mobile-open','mobile-session-page','mobile-panel-drawer');
  if(overlay)overlay.classList.remove('visible');
}

const _PWA_SIDEBAR_SWIPE_EDGE=80;
const _PWA_SIDEBAR_SWIPE_CLAIM=10;
const _PWA_SIDEBAR_SWIPE_TRIGGER=64;
const _PWA_SIDEBAR_SWIPE_MAX_VERTICAL=56;
let _pwaSidebarSwipe=null;

function _isPwaStandalone(){
  try{
    return document.documentElement.classList.contains('pwa-standalone')
      || window.matchMedia('(display-mode: standalone)').matches
      || window.navigator.standalone===true;
  }catch(_){return false;}
}

function _isInteractiveSwipeTarget(target){
  try{return !!(target&&target.closest&&target.closest('input,textarea,select,button,a,[contenteditable="true"],.topbar-chips,.composer-left,.sidebar,.rightpanel'));}
  catch(_){return false;}
}

function _pwaSidebarSwipePoint(e){
  const touch=e&&e.touches&&e.touches[0]||e&&e.changedTouches&&e.changedTouches[0];
  const src=touch||e;
  if(!src)return null;
  return {clientX:Number(src.clientX)||0,clientY:Number(src.clientY)||0};
}

function _isTouchPointerEvent(e){
  return !!(e&&e.pointerType==='touch');
}

function _openMobileSidebarFromGesture(){
  if(_isDesktopWidth())return;
  const sidebar=document.querySelector('.sidebar');
  if(!sidebar)return;
  try{if(typeof _syncMobileSidebarPanelFromMainView==='function')_syncMobileSidebarPanelFromMainView();}catch(_){}
  const layout=document.querySelector('.layout');
  if(layout)layout.classList.remove('sidebar-collapsed');
  sidebar.classList.remove('sidebar-collapsed');
  try{document.documentElement.removeAttribute('data-sidebar-collapsed');}catch(_){}
  sidebar.classList.remove('mobile-session-page');
  sidebar.classList.add('mobile-panel-drawer');
  sidebar.classList.add('mobile-open');
}

function _onPwaSidebarSwipeStart(e){
  if(_isDesktopWidth())return;
  if(_isTouchPointerEvent(e))return;
  if(e.pointerType==='mouse'||(e.pointerType&&e.pointerType!=='touch'&&e.pointerType!=='pen'))return;
  if(document.querySelector('.sidebar')?.classList.contains('mobile-open'))return;
  const point=_pwaSidebarSwipePoint(e);
  if(!point)return;
  if(point.clientX>_PWA_SIDEBAR_SWIPE_EDGE)return;
  if(_isInteractiveSwipeTarget(e.target))return;
  _pwaSidebarSwipe={startX:point.clientX,startY:point.clientY,active:true,opened:false};
}

function _onPwaSidebarSwipeMove(e){
  if(_isTouchPointerEvent(e))return;
  const swipe=_pwaSidebarSwipe;
  if(!swipe||!swipe.active||swipe.opened)return;
  const point=_pwaSidebarSwipePoint(e);
  if(!point)return;
  const dx=point.clientX-swipe.startX;
  const dy=point.clientY-swipe.startY;
  if(dx<0||Math.abs(dy)>_PWA_SIDEBAR_SWIPE_MAX_VERTICAL*1.5){_pwaSidebarSwipe=null;return;}
  if(dx>=_PWA_SIDEBAR_SWIPE_CLAIM&&dx>Math.abs(dy)*1.2){
    if(e.cancelable)e.preventDefault();
  }
  if(dx>=_PWA_SIDEBAR_SWIPE_TRIGGER&&Math.abs(dy)<=_PWA_SIDEBAR_SWIPE_MAX_VERTICAL&&dx>Math.abs(dy)*1.5){
    if(e.cancelable)e.preventDefault();
    swipe.opened=true;
    _openMobileSidebarFromGesture();
  }
}

function _onPwaSidebarSwipeEnd(e){if(_isTouchPointerEvent(e))return;_pwaSidebarSwipe=null;}
function _onPwaSidebarSwipeCancel(e){if(_isTouchPointerEvent(e))return;_pwaSidebarSwipe=null;}

function _installPwaSidebarSwipeGesture(){
  // #4660 review (Codex CORE): the #pwaSidebarEdgeGuard element is now
  // pointer-events:none (CSS), so it can no longer intercept hit-testing for
  // taps / vertical scrolls that merely start in the left edge strip — those
  // pass through to the underlying .messages scroller. The edge-swipe-to-open
  // gesture is handled entirely by the window-level CAPTURE touch/pointer
  // listeners below (which see the event regardless of the guard), so no
  // dedicated guard-element listener is needed.
  window.addEventListener('touchstart', _onPwaSidebarSwipeStart, {capture:true,passive:true});
  window.addEventListener('touchmove', _onPwaSidebarSwipeMove, {capture:true,passive:false});
  window.addEventListener('touchend', _onPwaSidebarSwipeEnd, {capture:true,passive:true});
  window.addEventListener('touchcancel', _onPwaSidebarSwipeCancel, {capture:true,passive:true});
  window.addEventListener('pointerdown', _onPwaSidebarSwipeStart, {passive:true});
  window.addEventListener('pointermove', _onPwaSidebarSwipeMove, {passive:false});
  window.addEventListener('pointerup', _onPwaSidebarSwipeEnd, {passive:true});
  window.addEventListener('pointercancel', _onPwaSidebarSwipeCancel, {passive:true});
}
_installPwaSidebarSwipeGesture();

// ── Desktop sidebar collapse toggle ────────────────────────────────────────
// Two discoverability paths into the same state:
//   (1) Click the already-active rail icon → collapse / expand the sidebar.
//   (2) Cmd/Ctrl+B keyboard shortcut (VS Code convention).
// Mobile is unaffected: the sidebar is an overlay there, and every collapse
// code path is gated on `_isDesktopWidth()` (min-width:641px).
// State is persisted via localStorage and survives reloads + bfcache.
const _SIDEBAR_COLLAPSED_KEY='hermes-webui-sidebar-collapsed';

function _isDesktopWidth(){
  try{return window.matchMedia('(min-width:641px)').matches;}catch(_){return true;}
}

function _isSidebarCollapsed(){
  return document.querySelector('.layout')?.classList.contains('sidebar-collapsed')||false;
}

function _syncSidebarAria(){
  // Mirror the open/collapsed state on the active rail button via aria-expanded
  // so screen readers announce the toggle. Open=true, collapsed=false.
  const active=document.querySelector('.rail .rail-btn.nav-tab.active[data-panel]');
  if(active)active.setAttribute('aria-expanded',!_isSidebarCollapsed());
}

function toggleSidebar(forceState){
  if(!_isDesktopWidth())return; // mobile uses an overlay; never collapse there
  const layout=document.querySelector('.layout');
  if(!layout)return;
  const next=typeof forceState==='boolean'?forceState:!_isSidebarCollapsed();
  layout.classList.toggle('sidebar-collapsed',next);
  // Clear the flash-prevention root-level marker once JS owns the state.
  try{document.documentElement.removeAttribute('data-sidebar-collapsed');}catch(_){}
  try{localStorage.setItem(_SIDEBAR_COLLAPSED_KEY,next?'1':'0');}catch(_){}
  _syncSidebarAria();
}

function expandSidebar(){
  if(_isSidebarCollapsed())toggleSidebar(false);
}

// Boot-time restore. The inline flash-prevention script in index.html already
// set data-sidebar-collapsed='1' on <html> before the stylesheet so the page
// renders collapsed without paint flash. This IIFE promotes that pre-paint
// state into the .layout class system where both JS and CSS can read it.
(function _restoreSidebarState(){
  try{document.documentElement.removeAttribute('data-sidebar-collapsed');}catch(_){}
  if(!_isDesktopWidth())return;
  try{
    if(localStorage.getItem(_SIDEBAR_COLLAPSED_KEY)==='1'){
      const layout=document.querySelector('.layout');
      if(layout)layout.classList.add('sidebar-collapsed');
    }
  }catch(_){}
  _syncSidebarAria();
})();
// ── Boot-time tab visibility ────────────────────────────────────────────────
// Apply hidden tabs from localStorage. The primary flash-prevention is an
// inline <script> in index.html (after sidebar-nav) that runs synchronously
// before first paint. This IIFE is a secondary fallback: it ensures consistency
// after panels.js is loaded and handles the active-tab switch. No-op if
// panels.js hasn't loaded yet (typeof guard).
(function _restoreTabVisibility(){
  try{
    if(typeof _applyTabOrder==='function'&&typeof _getTabOrder==='function'){
      _applyTabOrder(_getTabOrder());
    }
    if(typeof _applyTabVisibility==='function'&&typeof _getHiddenTabs==='function'){
      _applyTabVisibility(_getHiddenTabs());
    }
    var active=document.querySelector('.rail .rail-btn.nav-tab.active[data-panel]')
               ||document.querySelector('.sidebar-nav .nav-tab.active[data-panel]');
    if(active&&active.classList.contains('nav-tab-hidden')){
      var chatBtn=document.querySelector('.rail .rail-btn.nav-tab[data-panel="chat"]');
      if(chatBtn)chatBtn.classList.add('active');
      if(active)active.classList.remove('active');
    }
  }catch(_){}
})();
function toggleMobileFiles(){
  toggleWorkspacePanel();
}
function closeMobileWorkspacePanelFromChat(e){
  if(!_isCompactWorkspaceViewport()||_workspacePanelMode==='closed') return;
  const panel=document.querySelector('.rightpanel');
  if(panel&&panel.contains(e.target)) return;
  closeWorkspacePanel();
}
function toggleWorkspacePanel(force){
  const {panel}= _workspacePanelEls();
  if(!panel)return;
  const currentlyOpen=_workspacePanelMode!=='closed';
  const nextOpen=typeof force==='boolean'?force:!currentlyOpen;
  if(!nextOpen){
    closeWorkspacePanel();
    return;
  }
  const nextMode=_hasWorkspacePreviewVisible()?'preview':'browse';
  openWorkspacePanel(nextMode);
}
function mobileSwitchPanel(name){
  switchPanel(name);
  if(name==='chat'){
    closeMobileSidebar();
  } else {
    const sidebar=document.querySelector('.sidebar');
    if(sidebar){
      sidebar.classList.remove('mobile-session-page');
      sidebar.classList.add('mobile-panel-drawer','mobile-open');
    }
  }
}

$('btnSend').onclick=()=>{
  if(typeof handleComposerPrimaryAction==='function') return handleComposerPrimaryAction();
  if(window._micActive){
    window._micPendingSend=true;
    _stopMic();
    return;
  }
  // Turn-based voice mode: let the voice mode system handle the send flow
  if(typeof window._voiceModeActive==='function'&&window._voiceModeActive()){
    // Immediately send whatever is in the textarea
    if(typeof window._voiceModeImmediateSend==='function') window._voiceModeImmediateSend();
    return;
  }
  send();
};
$('mainChat')?.addEventListener('pointerdown', closeMobileWorkspacePanelFromChat);
$('btnAttach').onclick=e=>{if(e&&e.preventDefault)e.preventDefault();$('fileInput').value='';$('fileInput').click();};

// ── Voice input (Web Speech API + MediaRecorder fallback) ───────────────────
function _micIsLocalhostOrLoopback(hostname){
  const host=String(hostname||'').toLowerCase().replace(/^\[|\]$/g,'');
  return host==='localhost'
    || host.endsWith('.localhost')
    || host==='::1'
    || host==='0:0:0:0:0:0:0:1'
    || /^127\./.test(host);
}

function _micOriginNeedsSecureContext(){
  if(window.isSecureContext===true) return false;
  const loc=window.location||{};
  const protocol=loc.protocol||'';
  return protocol==='http:'&&!_micIsLocalhostOrLoopback(loc.hostname);
}

function _micToastKeyForRecognitionError(error){
  if((error==='not-allowed'||error==='service-not-allowed'||error==='audio-capture')
      && _micOriginNeedsSecureContext()){
    return 'mic_insecure_origin';
  }
  const msgs={
    'not-allowed':'mic_denied',
    'service-not-allowed':'mic_denied',
    'no-speech':'mic_no_speech',
    'network':'mic_network',
  };
  return msgs[error]||null;
}

(function(){
  const SpeechRecognition=window.SpeechRecognition||window.webkitSpeechRecognition;
  const _canRecordAudio=!!(navigator.mediaDevices&&navigator.mediaDevices.getUserMedia&&window.MediaRecorder);
  if(!SpeechRecognition&&!_canRecordAudio) return; // Browser unsupported — mic button stays hidden

  // Persist SR failure across reloads (e.g. Tailscale/network error)
  const _micForceMediaRecorderKey='mic_force_mediarecorder';
  const _micForceMediaRecorderStored=localStorage.getItem(_micForceMediaRecorderKey);
  // Prefer Hermes server-side STT (MediaRecorder -> /api/transcribe) only
  // after the server confirms an STT provider is available. No stored key must
  // keep browser SpeechRecognition as the first-click default until then; that
  // avoids dropping the first dictation on installs without server STT.
  let _serverSttAvailable=false;
  let _forceMediaRecorder=!SpeechRecognition||(_micForceMediaRecorderStored===null?(_serverSttAvailable&&_canRecordAudio):_micForceMediaRecorderStored==='1');

  // Raw audio mode preference: send audio file instead of transcribing
  let _rawAudioMode = localStorage.getItem('hermes-raw-audio-mode') === 'true';
  // Append-on-commit preference: when ON (default), dictated text is appended
  // to any text already in the composer. When OFF, dictated text replaces the
  // composer content (the pre-existing behavior).
  let _dictationAppend = localStorage.getItem('hermes-dictation-append') !== 'false';
  // Capture backend pinned at recording start ('speech' | 'media' | null) so
  // _stopMic / onstop act on the backend that actually started, even if the
  // raw-audio toggle changes mid-recording (#3169 Codex review).
  let _activeCaptureMode = null;

  const btn=$('btnMic');
  const status=$('micStatus');
  const ta=$('msg');
  const statusText=status?status.querySelector('.status-text'):null;
  btn.style.display=''; // Show button — browser supports speech recognition or recording fallback

  let recognition=null;
  let mediaRecorder=null;
  let mediaStream=null;
  let audioChunks=[];
  let _finalText='';
  let _prefix='';
  let _isRecording=false;
  // #5294 salvage — mobile composer-mic dictation continuity.
  // _speechStopRequested distinguishes an intentional stop (send/toggle) from a
  // natural pause so onend only auto-restarts on real pauses. _micWakeLock keeps
  // the screen awake while dictating; _micWakeLockOp serializes acquire/release
  // so rapid start/stop/visibility churn can't leak a lock. _micRestartCount is
  // bounded by _micMaxRestarts to stop a tight loop if the audio session is stolen.
  let _speechStopRequested=false;
  let _micWakeLock=null;
  let _micWakeLockOp=null;
  let _micRestartCount=0;
  const _micMaxRestarts=20;
  let _micHoldTimer=null;
  let _micHoldActive=false;
  let _micPointerDown=false;
  let _micStartSeq=0;
  const _micHoldThresholdMs=300;

  function _setButtonTooltipAndKey(btn, key){
    const text = t(key);
    btn.setAttribute('data-i18n-title', key);
    if(btn.hasAttribute('data-tooltip')){
      btn.setAttribute('data-tooltip', text);
      if(btn.hasAttribute('title')) btn.removeAttribute('title');
    } else {
      btn.title = text;
    }
  }

  function _setRecording(on){
    window._micActive=on;
    btn.classList.toggle('recording',on);
    // Active-state title flips so the tooltip is honest about what
    // pressing the button will do (#1488).
    _setButtonTooltipAndKey(btn, on ? (_rawAudioMode ? 'voice_recording_active' : 'voice_dictate_active') : (_rawAudioMode ? 'voice_send_raw' : 'voice_dictate'));
    status.style.display=on?'':'none';
    if(statusText) statusText.textContent=on?'Listening':'Listening';
    if(!on){ _finalText=''; _prefix=''; }
  }

  function _updateMicTooltip(){
    if(!window._micActive){
      _setButtonTooltipAndKey(btn, _rawAudioMode ? 'voice_send_raw' : 'voice_dictate');
    }
  }

  function _applyRawAudioModePreference(enabled){
    _rawAudioMode=!!enabled;
    try{localStorage.setItem('hermes-raw-audio-mode',_rawAudioMode?'true':'false');}catch(_){}
    const rawAudioCheckbox=document.getElementById('settingsRawAudio');
    if(rawAudioCheckbox) rawAudioCheckbox.checked=_rawAudioMode;
    _updateMicTooltip();
  }
  window._applyRawAudioModePreference=_applyRawAudioModePreference;

  function _applyDictationAppendPreference(enabled){
    _dictationAppend=!!enabled;
    try{localStorage.setItem('hermes-dictation-append',_dictationAppend?'true':'false');}catch(_){}
    const cb=document.getElementById('settingsDictationAppend');
    if(cb) cb.checked=_dictationAppend;
  }
  window._applyDictationAppendPreference=_applyDictationAppendPreference;

  async function _sendRawAudio(blob){
    const ext=(blob.type&&blob.type.includes('ogg'))?'ogg':'webm';
    const file=new File([blob],`voice-input-${Date.now()}.${ext}`,{type:blob.type||`audio/${ext}`});
    S.pendingFiles.push(file);
    renderTray();
    // An explicit Send-button click while recording sets _micPendingSend — that
    // is an unambiguous send intent, so honor it even when the composer already
    // has text (mirrors the transcribe path). Otherwise (manual mic-stop): send
    // immediately only if the composer is empty, else just attach + toast so the
    // user can keep composing.
    if(window._micPendingSend){
      window._micPendingSend=false;
      send();
    }else if(!ta.value.trim()){
      send();
    }else{
      showToast(t('voice_raw_attached'));
    }
  }

  function _commitTranscript(text, prefixOverride){
    // `prefixOverride` is the composer content captured at recording start,
    // passed only by the async server-STT path (recorder.onstop → _transcribeBlob).
    // The sync browser-SR path doesn't call this function — it commits inline
    // in sr.onend using _prefix directly.
    //
    // Three concerns this function has to balance (Greptile reviews):
    //   1. Race condition: user types during async transcription → preserve those
    //      keystrokes. Read live ta.value, not the stale snapshot.
    //   2. Clear-during-transcription: user clears textarea during async wait →
    //      respect that intent. Live ta.value (even empty) wins over snapshot.
    //   3. Browser-SR fallback: if a future caller passes no prefixOverride
    //      and live ta.value is empty, fall back to _prefix as a safety net.
    //
    // Resolution: when prefixOverride IS provided (server-STT path), trust live
    // ta.value unconditionally — even when empty. Otherwise fall back to _prefix.
    const clean=(text||'').trim();
    let committed;
    if(!clean){
      committed = ta.value;
    }else if(_dictationAppend){
      const base = prefixOverride !== undefined ? ta.value : (ta.value || _prefix);
      if(!base){
        committed = clean;
      }else{
        committed = (!base.endsWith(' ') && !base.endsWith('\n'))
          ? base+' '+clean.trimStart()
          : base+clean;
      }
    }else{
      // Replace mode (explicit): dictated text overwrites the composer.
      committed = clean;
    }
    ta.value=committed;
    autoResize();
    if(window._micPendingSend){
      window._micPendingSend=false;
      send();
    }
  }

  function _isServerSttUnavailable(err){
    const status=err&&err.status;
    if(status===404||status===503||status>=500) return true;
    if(!status) return true;
    const msg=String((err&&err.message)||'').toLowerCase();
    return msg.includes('unavailable')||msg.includes('not configured');
  }

  function _allowBrowserSttFallback(){
    return !!(SpeechRecognition&&localStorage.getItem(_micForceMediaRecorderKey)!=='1');
  }

  async function _transcribeBlob(blob, prefixSnapshot){
    const ext=(blob.type&&blob.type.includes('ogg'))?'ogg':'webm';
    const form=new FormData();
    form.append('file',new File([blob],`voice-input.${ext}`,{type:blob.type||`audio/${ext}`}));
    // Snapshot is passed in from the recorder.onstop handler — taken there
    // BEFORE _setRecording(false) clears _prefix (async server STT path).
    setComposerStatus('Transcribing…');
    try{
      const res=await fetch('api/transcribe',{method:'POST',body:form});
      const data=await res.json().catch(()=>({}));
      if(!res.ok){
        const err=new Error(data.error||'Transcription failed');
        err.status=res.status;
        throw err;
      }
      _commitTranscript(data.transcript||'', prefixSnapshot);
    }catch(err){
      if(_isServerSttUnavailable(err)&&_allowBrowserSttFallback()){
        window._micPendingSend=false;
        localStorage.setItem(_micForceMediaRecorderKey,'0');
        _forceMediaRecorder=false;
        recognition=_ensureSpeechRecognition();
        showToast(err.message||t('mic_network'));
        return;
      }
      window._micPendingSend=false;
      showToast(err.message||t('mic_network'));
    }finally{
      setComposerStatus('');
    }
  }

  function _stopTracks(stream=mediaStream){
    if(stream){
      stream.getTracks().forEach(track=>track.stop());
      if(mediaStream===stream) mediaStream=null;
    }
  }

  // Gate continuous dictation to MOBILE so desktop stays one-shot (single
  // utterance). An explicit hermes-mic-continuous flag wins in both directions,
  // mirroring the hermes-voice-continuous pattern: 'true' opts a desktop in,
  // 'false' opts a mobile out. Absent a flag, coarse-pointer (touch) devices get
  // continuity and everything else stays single-utterance.
  function _micDictationContinuous(){
    try{
      const flag=localStorage.getItem('hermes-mic-continuous');
      if(flag==='true') return true;
      if(flag==='false') return false;
    }catch(_){}
    try{ return window.matchMedia('(pointer:coarse)').matches; }catch(_){ return false; }
  }

  // Only auto-restart the composer-mic session on a natural pause: continuity
  // must be enabled (mobile/opt-in), the session must still be active speech,
  // the stop must not have been requested, and we must be under the restart cap.
  function _micShouldRestartDictation(){
    return _micDictationContinuous()
      && !_speechStopRequested
      && !!window._micActive
      && _activeCaptureMode==='speech'
      && _micRestartCount<_micMaxRestarts;
  }

  // Screen Wake Lock while dictating. All acquire/release ops are chained onto a
  // single in-flight promise (_micWakeLockOp) so rapid start/stop/visibility
  // churn runs strictly in order and can't interleave to leak a lock or null
  // _micWakeLock mid-request.
  function _acquireMicWakeLock(){
    if(!navigator.wakeLock) return Promise.resolve();
    _micWakeLockOp=Promise.resolve(_micWakeLockOp).then(async()=>{
      if(_micWakeLock) return;
      if(!window._micActive||_activeCaptureMode!=='speech') return;
      try{
        const lock=await navigator.wakeLock.request('screen');
        // If the session ended while awaiting, don't hold a stale lock.
        if(!window._micActive||_activeCaptureMode!=='speech'){
          try{ await lock.release(); }catch(_){}
          return;
        }
        _micWakeLock=lock;
        _micWakeLock.addEventListener?.('release',()=>{ _micWakeLock=null; },{once:true});
      }catch(_){
        _micWakeLock=null;
      }
    });
    return _micWakeLockOp;
  }

  function _releaseMicWakeLock(){
    _micWakeLockOp=Promise.resolve(_micWakeLockOp).then(async()=>{
      const lock=_micWakeLock;
      _micWakeLock=null;
      if(!lock) return;
      try{ await lock.release(); }catch(_){}
    });
    return _micWakeLockOp;
  }

  // The OS drops a screen wake lock when the tab is hidden; reacquire on return
  // if we're still dictating (release on hide is a no-op if none is held).
  document.addEventListener('visibilitychange',()=>{
    if(document.visibilityState==='hidden'){
      void _releaseMicWakeLock();
      return;
    }
    if(window._micActive&&_activeCaptureMode==='speech'){
      void _acquireMicWakeLock();
    }
  });

  function _stopMic(){
    _micStartSeq+=1;
    _isRecording=false;
    if(!window._micActive) return;
    // Stop the backend that was ACTIVE WHEN RECORDING STARTED — not whatever
    // _rawAudioMode says now. The user can toggle Settings → Sound mid-recording,
    // which would otherwise make us stop the wrong backend and orphan the other
    // (#3169 Codex review). _activeCaptureMode is pinned at start.
    if(recognition && _activeCaptureMode==='speech'){
      _speechStopRequested=true;
      recognition.stop();
      return;
    }
    if(mediaRecorder&&mediaRecorder.state!=='inactive'){
      mediaRecorder.stop();
      return;
    }
    _setRecording(false);
    _stopTracks();
  }
  window._stopMic=_stopMic; // expose for send-guard above

  function _ensureSpeechRecognition(){
    if(!SpeechRecognition) return null;
    const sr=recognition||new SpeechRecognition();
    // Desktop dictation stays one-shot (single utterance); mobile / opt-in
    // devices run continuous so a natural pause doesn't end the session (#5294).
    sr.continuous=_micDictationContinuous();
    sr.interimResults=true;
    sr.lang=(typeof _locale!=='undefined'&&_locale._speech)||'en-US';

    sr.onstart=()=>{ _finalText=''; };

    sr.onresult=(event)=>{
      // #5294: a real result means the continuity restarts are PRODUCTIVE, not a
      // stolen-audio-session tight loop — reset the restart budget so a long
      // dictation with many natural pauses isn't silently capped at
      // _micMaxRestarts. The cap still guards the failure case: consecutive
      // restarts that yield no speech (onend without an intervening onresult)
      // keep incrementing and trip the bound.
      _micRestartCount=0;
      let interim='';
      let final=_finalText;
      for(let i=event.resultIndex;i<event.results.length;i++){
        const t=event.results[i][0].transcript;
        if(event.results[i].isFinal){ final+=t; _finalText=final; }
        else{ interim+=t; }
      }
      ta.value=_prefix+(final||interim);
      autoResize();
    };

    sr.onend=()=>{
      const committed=_finalText
        ? (_prefix&&!_prefix.endsWith(' ')&&!_prefix.endsWith('\n')
            ? _prefix+' '+_finalText.trimStart()
            : _prefix+_finalText)
        : ta.value;
      ta.value=committed;
      autoResize();
      // Mobile / opt-in continuity: a natural pause ends this recognition run but
      // the user is still dictating, so restart to keep the session alive. Desktop
      // (one-shot) and intentional stops (_speechStopRequested) skip this and
      // finalize. Bounded by _micMaxRestarts so a stolen audio session can't loop.
      if(_micShouldRestartDictation()){
        _prefix=committed&&!committed.endsWith(' ')&&!committed.endsWith('\n')
          ? committed+' '
          : committed;
        _finalText='';
        _micRestartCount++;
        try{
          sr.start();
          return;
        }catch(err){
          // Restart failed (e.g. the audio session was taken by another app).
          // Surface it instead of silently dropping to idle (Greptile P2).
          showToast(t('mic_error')+String((err&&err.message)||'restart'));
        }
      }
      _speechStopRequested=false;
      _isRecording=false;
      _micRestartCount=0;
      void _releaseMicWakeLock();
      _setRecording(false);
      if(window._micPendingSend){
        window._micPendingSend=false;
        send();
      }
      _applyDeferredServerSttFlip();
    };

    sr.onerror=(event)=>{
      // While dictating with continuity on, a no-speech/aborted error is a normal
      // pause or transient audio-session hiccup — swallow it and let onend restart
      // (bounded by _micMaxRestarts). Desktop one-shot still surfaces the toast.
      if((event.error==='no-speech'||event.error==='aborted')
          && _micDictationContinuous()
          && window._micActive
          && _activeCaptureMode==='speech'
          && !_speechStopRequested
          && _micRestartCount<_micMaxRestarts){
        return;
      }
      _speechStopRequested=false;
      _setRecording(false);
      window._micPendingSend=false;
      _isRecording=false;
      _micRestartCount=0;
      void _releaseMicWakeLock();
      if(event.error==='network'||event.error==='not-allowed'
          ||event.error==='service-not-allowed'||event.error==='audio-capture'){
        // Persist SR failure: next reload will skip SpeechRecognition
        localStorage.setItem(_micForceMediaRecorderKey,'1');
        _forceMediaRecorder=true;
        recognition=null;
      }
      const messageKey=_micToastKeyForRecognitionError(event.error);
      showToast(messageKey?t(messageKey):t('mic_error')+event.error);
    };

    return sr;
  }

  if(!_forceMediaRecorder){
    recognition=_ensureSpeechRecognition();
  }

  async function _probeServerSttCapability(){
    if(!_canRecordAudio||_micForceMediaRecorderStored!==null) return;
    try{
      const res=await fetch('api/transcribe/capability',{cache:'no-store'});
      const data=await res.json().catch(()=>({}));
      if(res.ok&&data&&data.available){
        _serverSttAvailable=true;
        if(!window._micActive){
          _forceMediaRecorder=true;
          recognition=null;
        }
      }
    }catch(_err){
      // Keep browser SpeechRecognition as the safe first-click default when the
      // passive capability probe fails.
    }
  }

  // If the capability probe resolved WHILE a session was active, the flip to
  // server STT was deferred to protect that in-flight session. Apply it once the
  // session ends so subsequent clicks use the configured server STT as intended.
  // Reads LIVE localStorage (not the init-time const) so a fallback that just
  // persisted '0' is respected and not re-flipped.
  function _applyDeferredServerSttFlip(){
    if(_serverSttAvailable&&!_forceMediaRecorder&&!window._micActive
        &&localStorage.getItem(_micForceMediaRecorderKey)===null){
      _forceMediaRecorder=true;
      recognition=null;
    }
  }

  _probeServerSttCapability();

  function _clearMicHoldTimer(){
    if(_micHoldTimer){
      clearTimeout(_micHoldTimer);
      _micHoldTimer=null;
    }
  }

  function _resetMicHoldState(){
    _clearMicHoldTimer();
    _micHoldActive=false;
    _micPointerDown=false;
  }

  function _micButtonAvailable(){
    if(!btn||btn.disabled) return false;
    if(btn.style.display==='none') return false;
    if(btn.classList.contains('composer-control-hidden')) return false;
    if(btn.getAttribute('aria-hidden')==='true') return false;
    if(window.getComputedStyle&&window.getComputedStyle(btn).display==='none') return false;
    return true;
  }

  async function _startMicCapture(holdRequired=false){
    if(!_micButtonAvailable()) return;
    const startSeq=++_micStartSeq;
    // Race-condition guard: ignore rapid double-clicks
    if(_isRecording){
      _stopMic();
      _isRecording=false;
      return;
    }
    if(window._micActive){
      _stopMic();
      return;
    }
    _isRecording=true;
    _finalText='';
    _prefix=ta.value;
    if(_micOriginNeedsSecureContext()){
      _isRecording=false;
      window._micPendingSend=false;
      showToast(t('mic_insecure_origin'));
      return;
    }
    if(recognition && !_forceMediaRecorder && !_rawAudioMode){
      _activeCaptureMode='speech';
      _speechStopRequested=false;
      _micRestartCount=0;
      // Refresh continuity gate at start so a settings/orientation change takes
      // effect for this session (desktop stays one-shot, mobile stays continuous).
      recognition.continuous=_micDictationContinuous();
      recognition.lang=(typeof _locale!=='undefined'&&_locale._speech)||'en-US';
      recognition.start();
      void _acquireMicWakeLock();
      _setRecording(true);
      return;
    }
    if(!_canRecordAudio){
      _isRecording=false;
      showToast(t('mic_network'));
      return;
    }
    try{
      const captureStream=await navigator.mediaDevices.getUserMedia({audio:true});
      if(startSeq!==_micStartSeq||!_micButtonAvailable()||(holdRequired&&!_micHoldActive)){
        _isRecording=false;
        _stopTracks(captureStream);
        return;
      }
      mediaStream=captureStream;
      const preferredTypes=['audio/webm;codecs=opus','audio/webm','audio/ogg;codecs=opus','audio/ogg'];
      const mimeType=preferredTypes.find(type=>window.MediaRecorder.isTypeSupported?.(type))||'';
      const captureMode=_rawAudioMode?'media-raw':'media-transcribe';
      const recorder=new MediaRecorder(captureStream,mimeType?{mimeType}:undefined);
      audioChunks=[];
      const captureChunks=audioChunks;
      recorder.ondataavailable=e=>{if(e.data&&e.data.size)captureChunks.push(e.data);};
      recorder.onerror=()=>{
        const isCurrentCapture=mediaRecorder===recorder||mediaStream===captureStream;
        _isRecording=false;
        if(mediaRecorder===recorder) mediaRecorder=null;
        if(isCurrentCapture) _setRecording(false);
        window._micPendingSend=false;
        _stopTracks(captureStream);
        showToast(t('mic_network'));
      };
      recorder.onstop=async()=>{
        const isCurrentCapture=mediaRecorder===recorder||mediaStream===captureStream;
        if(mediaRecorder===recorder) mediaRecorder=null;
        _isRecording=false;
        // Capture the composer prefix BEFORE _setRecording(false) clears _prefix.
        // The await on _transcribeBlob runs after this sync block, so by the
        // time _transcribeBlob is called, _prefix is already ''. Passing the
        // snapshot through keeps append-mode working on the async server-STT
        // path. See _commitTranscript() for how the snapshot is consumed.
        const prefixSnapshot = _prefix;
        const blob=new Blob(captureChunks,{type:recorder.mimeType||mimeType||'audio/webm'});
        if(isCurrentCapture) _setRecording(false);
        _stopTracks(captureStream);
        if(blob.size){
          if(captureMode==='media-raw'){
            await _sendRawAudio(blob);
          }else{
            await _transcribeBlob(blob, prefixSnapshot);
          }
        }
        else if(window._micPendingSend){
          window._micPendingSend=false;
        }
        _applyDeferredServerSttFlip();
      };
      _activeCaptureMode=captureMode;
      mediaRecorder=recorder;
      recorder.start();
      _setRecording(true);
    }catch(err){
      if(startSeq!==_micStartSeq) return;
      _isRecording=false;
      window._micPendingSend=false;
      _stopTracks();
      showToast(t(_micToastKeyForRecognitionError('not-allowed')||'mic_denied'));
    }
  }

  async function _toggleMicCapture(){
    if(!_micButtonAvailable()) return;
    if(window._micActive){
      _stopMic();
      return;
    }
    await _startMicCapture();
  }
  window._toggleMicCapture=_toggleMicCapture;

  btn.addEventListener('pointerdown',e=>{
    if(e.button!==0) return;
    if(!_micButtonAvailable()) return;
    _resetMicHoldState();
    _micPointerDown=true;
    _micHoldTimer=setTimeout(async()=>{
      _micHoldTimer=null;
      if(!_micPointerDown||window._micActive) return;
      _micHoldActive=true;
      await _startMicCapture(true);
    },_micHoldThresholdMs);
  });

  btn.addEventListener('pointerup',async e=>{
    if(e.button!==0||!_micPointerDown) return;
    _clearMicHoldTimer();
    if(_micHoldActive){
      _micHoldActive=false;
      _micPointerDown=false;
      _stopMic();
      return;
    }
    _micPointerDown=false;
    await _toggleMicCapture();
  });

  btn.addEventListener('pointerleave',()=>{
    _clearMicHoldTimer();
    if(_micHoldActive){
      _micHoldActive=false;
      _micPointerDown=false;
      _stopMic();
      return;
    }
    _micPointerDown=false;
  });

  btn.addEventListener('pointercancel',()=>{
    _clearMicHoldTimer();
    if(_micHoldActive){
      _micHoldActive=false;
      _micPointerDown=false;
      _stopMic();
      return;
    }
    _micPointerDown=false;
  });

  btn.addEventListener('click',async e=>{
    if(e.detail!==0) return;
    await _toggleMicCapture();
  });

  // Wire up the settings checkbox
  const rawAudioCheckbox = document.getElementById('settingsRawAudio');
  if(rawAudioCheckbox){
    rawAudioCheckbox.checked = _rawAudioMode;
    rawAudioCheckbox.addEventListener('change', function(){
      _applyRawAudioModePreference(this.checked);
    });
  }
  const appendCheckbox = document.getElementById('settingsDictationAppend');
  if(appendCheckbox){
    appendCheckbox.checked = _dictationAppend;
    appendCheckbox.addEventListener('change', function(){
      _applyDictationAppendPreference(this.checked);
    });
  }
  _updateMicTooltip();
})();
window._micActive=window._micActive||false;
window._micPendingSend=window._micPendingSend||false;

// ── Default message mode eager default (#5167 / #5145) ──────────────────────
// The Default message mode preference (queue/interrupt/steer) is read on the
// send path via `window._defaultMessageMode||'steer'`. The authoritative value
// only arrives once the async boot IIFE below resolves the `/api/settings`
// fetch. Without an eager value, every send during that boot window silently
// falls back, ignoring a saved 'queue'/'interrupt' preference (worse on
// slow/contended environments like WSL2, see #5132). Mirror the resolved value
// into localStorage — the same synchronous-source pattern used by hermes-lang /
// hermes-theme — so the very first send after a reload honors the saved choice.
const _DEFAULT_MESSAGE_MODES=['queue','interrupt','steer'];
// Legacy localStorage key (pre-#5145 rename); read it as a fallback so an
// existing user's persisted busy-input-mode preference survives the rename.
const _LEGACY_DEFAULT_MESSAGE_MODE_KEY='hermes-busy-input-mode';
const _DEFAULT_MESSAGE_MODE_KEY='hermes-default-message-mode';
function _normalizeDefaultMessageMode(mode){
  return _DEFAULT_MESSAGE_MODES.includes(mode)?mode:'steer';
}
function _persistDefaultMessageMode(mode){
  const m=_normalizeDefaultMessageMode(mode);
  try{localStorage.setItem(_DEFAULT_MESSAGE_MODE_KEY,m);}catch(_){}
  return m;
}
function _readPersistedDefaultMessageMode(){
  let stored=null;
  try{
    // Prefer the new key; fall back to the legacy key so a pre-rename
    // preference is honored until the next explicit save rewrites the new key.
    stored=localStorage.getItem(_DEFAULT_MESSAGE_MODE_KEY);
    if(stored===null||stored===undefined) stored=localStorage.getItem(_LEGACY_DEFAULT_MESSAGE_MODE_KEY);
  }catch(_){}
  return _normalizeDefaultMessageMode(stored);
}
window._persistDefaultMessageMode=_persistDefaultMessageMode;
window._readPersistedDefaultMessageMode=_readPersistedDefaultMessageMode;
// Eager default set BEFORE the async settings fetch resolves so first sends in
// the boot window honor the persisted preference instead of the raw default.
window._defaultMessageMode=_readPersistedDefaultMessageMode();

// ── Extension TTS-engine registry (registerHermesTtsEngine) ──────────────────
// Defined at MODULE scope (not inside the voice-mode IIFE below) so the public
// API exists even on browsers without SpeechRecognition / speechSynthesis — an
// extension can register a TTS engine regardless of STT/browser-TTS support.
// Lets a trusted local extension contribute a TTS engine that appears in the
// Settings -> TTS Engine dropdown and is used by BOTH playback paths (voice-mode
// auto-read and the per-message Listen button). The extension provides an async
// synthesize(text, opts) that returns audio bytes (ArrayBuffer or Blob); core
// handles selection, the dropdown option, and playback. Mirrors registerHermesSkin.
//
//   window.registerHermesTtsEngine({
//     id: 'voicevox',            // [a-z0-9_-], not a built-in (browser/edge/elevenlabs/openai)
//     label: 'VOICEVOX (local)',
//     synthesize(text, opts) { return Promise<ArrayBuffer|Blob>; }
//   }) -> true on success, false if rejected
var _HERMES_TTS_ENGINES = Object.create(null);
var _HERMES_TTS_RESERVED = { browser:1, edge:1, elevenlabs:1, openai:1 };
function _hermesTtsValidId(id){ return typeof id==='string' && /^[a-z0-9][a-z0-9_-]{0,31}$/.test(id); }
function _hermesAddTtsOption(id, label){
  var sel=document.getElementById('settingsTtsEngine');
  if(!sel) return;
  if(sel.querySelector('option[value="'+id+'"]')) return;
  var opt=document.createElement('option');
  opt.value=id;
  opt.textContent=label;   // textContent — never innerHTML (no injection)
  sel.appendChild(opt);
}
window.registerHermesTtsEngine=function(desc){
  try{
    if(!desc||typeof desc!=='object') return false;
    var id=String(desc.id||'').toLowerCase();
    if(!_hermesTtsValidId(id)) return false;
    if(_HERMES_TTS_RESERVED[id]) return false;          // can't shadow a built-in
    if(typeof desc.synthesize!=='function') return false;
    var label=(typeof desc.label==='string' && desc.label.trim()) ? desc.label.trim().slice(0,48) : id;
    _HERMES_TTS_ENGINES[id]={ id:id, label:label, synthesize:desc.synthesize };
    _hermesAddTtsOption(id, label);
    return true;
  }catch(_){ return false; }
};
window._hermesTtsIsRegistered=function(id){ return !!_HERMES_TTS_ENGINES[id]; };
// List registered engines (for the settings panel to re-add options on render).
window._hermesTtsEngineOptions=function(){
  return Object.keys(_HERMES_TTS_ENGINES).map(function(k){
    return { id:_HERMES_TTS_ENGINES[k].id, label:_HERMES_TTS_ENGINES[k].label };
  });
};
// Returns a Promise<ArrayBuffer> or null if the engine isn't registered.
window._hermesTtsSynth=function(id, text, opts){
  var eng=_HERMES_TTS_ENGINES[id];
  if(!eng) return null;
  return Promise.resolve()
    .then(function(){ return eng.synthesize(text, opts||{}); })
    .then(function(out){
      if(!out) throw new Error('empty TTS result');
      if(out instanceof ArrayBuffer) return out;
      if(typeof Blob!=='undefined' && out instanceof Blob) return out.arrayBuffer();
      if(out.buffer instanceof ArrayBuffer) return out.buffer;   // typed array
      throw new Error('TTS engine returned an unsupported type');
    });
};

// ── Turn-based voice mode (#1333) ────────────────────────────────────────
// Chained flow: listen → send → (agent processes) → TTS response → listen again
(function(){
  const SpeechRecognition=window.SpeechRecognition||window.webkitSpeechRecognition;
  const hasSTT=!(!SpeechRecognition);
  const hasTTS=!!('speechSynthesis' in window);

  // Need both STT and TTS for turn-based voice mode
  if(!hasSTT||!hasTTS) return;

  const modeBtn=$('btnVoiceMode');
  const bar=$('voiceModeBar');
  const indicator=$('voiceModeIndicator');
  const label=$('voiceModeLabel');
  const micBtn=$('btnMic');
  const ta=$('msg');

  if(!modeBtn||!bar||!indicator||!label) return;

  // Voice-mode button is gated behind a Preferences toggle (#1488).
  // Default off — keeps the composer footer uncluttered for users who
  // only need plain dictation. The hands-free conversation feature is
  // a power-user surface; explicit opt-in avoids the visual confusion
  // of two near-identical mic icons.
  function _voiceModePrefEnabled(){
    try{ return localStorage.getItem('hermes-voice-mode-button')==='true'; }
    catch(_){ return false; }
  }
  let _voiceModeActive=false;

  function _applyVoiceModePref(){
    const enabled = _voiceModePrefEnabled();
    modeBtn.style.display = enabled ? '' : 'none';
    if(!enabled && _voiceModeActive) _deactivate();
  }
  _applyVoiceModePref();
  // Expose so the settings pane can re-apply immediately on toggle.
  window._applyVoiceModePref = _applyVoiceModePref;

  let _voiceModeState='idle'; // idle | listening | thinking | speaking
  let _recognition=null;
  let _silenceTimer=null;
  // Capture the session id at thinking-time so the TTS callback won't read
  // a different session's last assistant reply if the user navigated away
  // between send and stream completion. (Opus pre-release advisor.)
  let _voiceModeThinkingSid=null;
  let _browserTtsKeepAlive=null;
  let _browserTtsWatchdog=null;
  let _browserTtsSuppressNextErrorRearm=false;
  // Configurable via localStorage keys (set from dev console or a future settings panel).
  //   hermes-voice-silence-ms, pause duration before auto-send (ms, default 1800)
  //   hermes-voice-continuous, keep mic open across natural pauses ("true"/"false", default false)
  function _voiceSilenceMs(){
    const _silenceMsRaw=parseInt(localStorage.getItem('hermes-voice-silence-ms'),10);
    return (Number.isFinite(_silenceMsRaw)&&_silenceMsRaw>0)?Math.max(200,_silenceMsRaw):1800;
  }

  function _clearBrowserTtsRecovery(){
    if(_browserTtsKeepAlive){
      clearInterval(_browserTtsKeepAlive);
      _browserTtsKeepAlive=null;
    }
    if(_browserTtsWatchdog){
      clearTimeout(_browserTtsWatchdog);
      _browserTtsWatchdog=null;
    }
  }

  function _armBrowserTtsRecovery(clean, rate){
    _clearBrowserTtsRecovery();
    _browserTtsSuppressNextErrorRearm=false;
    const safeRate=(Number.isFinite(rate)&&rate>0)?rate:1;
    // Chromium can drop utter.onend on later turns, so force a recovery path.
    const watchdogMs=Math.max(4000,Math.round((String(clean||'').length/(12*safeRate))*1000)+10000);
    _browserTtsWatchdog=setTimeout(()=>{
      if(!_voiceModeActive||_voiceModeState!=='speaking') return;
      _browserTtsSuppressNextErrorRearm=true;
      try{ speechSynthesis.cancel(); }catch(_){}
      _clearBrowserTtsRecovery();
      _startListening();
    },watchdogMs);
    _browserTtsKeepAlive=setInterval(()=>{
      if(!_voiceModeActive||_voiceModeState!=='speaking'){
        _clearBrowserTtsRecovery();
        return;
      }
      if(!speechSynthesis.speaking) return;
      try{
        speechSynthesis.pause();
        speechSynthesis.resume();
      }catch(_){}
    },10000);
  }

  function _setState(state){
    _voiceModeState=state;
    indicator.className='voice-mode-indicator '+state;
    label.textContent=state==='listening'?t('voice_listening')
      :state==='speaking'?t('voice_speaking')
      :state==='thinking'?t('voice_thinking')
      :'';
    bar.style.display=_voiceModeActive?(state==='idle'?'none':''):'none';
  }

  function _startListening(){
    if(!_voiceModeActive) return;
    if(_micOriginNeedsSecureContext()){
      _deactivate();
      showToast(t('mic_insecure_origin'));
      return;
    }
    _clearBrowserTtsRecovery();
    _setState('listening');

    _recognition=new SpeechRecognition();
    _recognition.continuous=localStorage.getItem('hermes-voice-continuous')==='true';
    _recognition.interimResults=true;
    _recognition.lang=(typeof _locale!=='undefined'&&_locale._speech)||'en-US';

    let _finalText='';

    _recognition.onstart=()=>{ _finalText=''; };

    _recognition.onresult=(event)=>{
      // Reset silence timer on any result
      clearTimeout(_silenceTimer);
      let interim='';
      let final=_finalText;
      for(let i=event.resultIndex;i<event.results.length;i++){
        const txt=event.results[i][0].transcript;
        if(event.results[i].isFinal){ final+=txt; _finalText=final; }
        else{ interim+=txt; }
      }
      ta.value=final||interim;
      autoResize();

      // Auto-send on silence after final result
      if(_finalText){
        _silenceTimer=setTimeout(()=>{
          _voiceModeSend();
        },_voiceSilenceMs());
      }
    };

    _recognition.onend=()=>{
      clearTimeout(_silenceTimer);
      // If we have text and haven't sent yet, send it
      if(_finalText&&_voiceModeActive&&_voiceModeState==='listening'){
        _voiceModeSend();
      } else if(_voiceModeActive&&_voiceModeState==='listening'){
        // No speech detected — restart listening
        setTimeout(()=>{ if(_voiceModeActive) _startListening(); },500);
      }
    };

    _recognition.onerror=(event)=>{
      clearTimeout(_silenceTimer);
      if(event.error==='no-speech'||event.error==='aborted'){
        // Restart if still active
        if(_voiceModeActive){
          setTimeout(()=>{ if(_voiceModeActive) _startListening(); },800);
        }
        return;
      }
      if(event.error==='not-allowed'||event.error==='service-not-allowed'||event.error==='audio-capture'){
        _deactivate();
        const messageKey=_micToastKeyForRecognitionError(event.error);
        showToast(messageKey?t(messageKey):t('mic_error')+event.error);
        return;
      }
      // Other errors — try to restart
      if(_voiceModeActive){
        setTimeout(()=>{ if(_voiceModeActive) _startListening(); },1500);
      }
    };

    try{ _recognition.start(); }catch(e){
      // Already started or other error — retry shortly
      setTimeout(()=>{ if(_voiceModeActive) _startListening(); },1000);
    }
  }

  function _voiceModeSend(){
    if(!_voiceModeActive) return;
    const text=(ta.value||'').trim();
    if(!text){
      ta.value='';
      setTimeout(()=>{ if(_voiceModeActive) _startListening(); },300);
      return;
    }
    _setState('thinking');
    // Pin the active session id so the TTS callback won't speak a different
    // session's reply if the user navigates away mid-stream.
    _voiceModeThinkingSid=(typeof S!=='undefined'&&S.session)?S.session.session_id:null;
    try{ if(_recognition) _recognition.abort(); }catch(_){}
    _recognition=null;
    // send() is global from boot.js
    if(typeof send==='function') send();
  }

  function _speakResponse(){
    if(!_voiceModeActive) return;
    // Bail out if the user navigated to a different session between send and
    // stream completion. The patched autoReadLastAssistant fires globally;
    // without this guard it would TTS-read the wrong session's last assistant
    // message. Drop back to listening on the new session instead.
    const currentSid=(typeof S!=='undefined'&&S.session)?S.session.session_id:null;
    if(_voiceModeThinkingSid && currentSid && currentSid!==_voiceModeThinkingSid){
      _voiceModeThinkingSid=null;
      _startListening();
      return;
    }
    _voiceModeThinkingSid=null;
    _setState('speaking');

    // Find last assistant message
    const rows=document.querySelectorAll('.msg-row[data-role="assistant"], .assistant-segment[data-raw-text]');
    if(!rows.length){ _startListening(); return; }
    const last=rows[rows.length-1];
    const rawText=last.dataset.rawText||'';
    if(!rawText.trim()){ _startListening(); return; }

    // Strip for TTS (reuse existing helper if available)
    let clean=rawText;
    if(typeof _stripForTTS==='function') clean=_stripForTTS(rawText);
    else{
      // Basic strip: remove code blocks, images, links
      clean=clean.replace(/```[\s\S]*?```/g,' code block ')
        .replace(/`([^`]*)`/g,'$1')
        .replace(/!\[([^\]]*)\]\([^)]*\)/g,'$1')
        .replace(/\[([^\]]*)\]\([^)]*\)/g,'$1')
        .replace(/#{1,6}\s/g,'')
        .replace(/[*_~]+/g,'')
        .replace(/\n{2,}/g,'. ')
        .replace(/\n/g,' ')
        .trim();
    }
    if(!clean){ _startListening(); return; }
    const engine=localStorage.getItem("hermes-tts-engine")||"browser";
    // Extension-registered TTS engine (window.registerHermesTtsEngine): synth
    // via the extension, then play through the same Audio lifecycle as edge.
    if(typeof window._hermesTtsIsRegistered==='function' && window._hermesTtsIsRegistered(engine)){
      _ttsSpeaking=true;
      const _opts={
        voice: localStorage.getItem("hermes-tts-voice")||'',
        rate: parseFloat(localStorage.getItem("hermes-tts-rate")),
        pitch: parseFloat(localStorage.getItem("hermes-tts-pitch")),
      };
      Promise.resolve(window._hermesTtsSynth(engine, clean, _opts))
        .then(function(buf){
          const blob=new Blob([buf]);
          const url=URL.createObjectURL(blob);
          const audio=new Audio(url);
          _playingEdgeAudio=audio;
          audio.onended=function(){
            _ttsSpeaking=false;
            if(_playingEdgeAudio===audio) _playingEdgeAudio=null;
            URL.revokeObjectURL(url);
            if(_voiceModeActive) setTimeout(function(){_startListening();},500);
          };
          audio.onerror=function(){
            _ttsSpeaking=false;
            if(_playingEdgeAudio===audio) _playingEdgeAudio=null;
            URL.revokeObjectURL(url);
            if(_voiceModeActive) setTimeout(function(){_startListening();},1000);
          };
          audio.play().catch(function(){
            _ttsSpeaking=false;
            if(_playingEdgeAudio===audio) _playingEdgeAudio=null;
            URL.revokeObjectURL(url);
            if(_voiceModeActive) setTimeout(function(){_startListening();},1000);
          });
        })
        .catch(function(){
          _ttsSpeaking=false;
          if(_voiceModeActive) setTimeout(function(){_startListening();},1000);
        });
      return;
    }
    if(engine==="elevenlabs"){
      _ttsSpeaking=true;
      fetch(new URL('api/tts', document.baseURI || location.href).href, {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({text: clean, engine: 'elevenlabs'})
      })
      .then(r => {
        if(!r.ok) throw new Error('TTS request failed: ' + r.status);
        return r.blob();
      })
      .then(blob => {
        const url = URL.createObjectURL(blob);
        const audio = new Audio(url);
        _playingEdgeAudio=audio;
        audio.onended = () => {
          _ttsSpeaking=false;
          if(_playingEdgeAudio===audio) _playingEdgeAudio=null;
          URL.revokeObjectURL(url);
          if(_voiceModeActive) setTimeout(()=>_startListening(),500);
        };
        audio.onerror = () => {
          _ttsSpeaking=false;
          if(_playingEdgeAudio===audio) _playingEdgeAudio=null;
          URL.revokeObjectURL(url);
          if(_voiceModeActive) setTimeout(()=>_startListening(),1000);
        };
        audio.play().catch(e => {
          _ttsSpeaking=false;
          if(_playingEdgeAudio===audio) _playingEdgeAudio=null;
          URL.revokeObjectURL(url);
          if(_voiceModeActive) setTimeout(()=>_startListening(),1000);
        });
      })
      .catch(() => {
        _ttsSpeaking=false;
        if(_voiceModeActive) setTimeout(()=>_startListening(),1000);
      });
      return;
    }
    if(engine==="openai"){
      _ttsSpeaking=true;
      fetch(new URL('api/tts', document.baseURI || location.href).href, {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({text: clean, engine: 'openai'})
      })
      .then(r => {
        if(!r.ok) throw new Error('TTS request failed: ' + r.status);
        return r.blob();
      })
      .then(blob => {
        const url = URL.createObjectURL(blob);
        const audio = new Audio(url);
        _playingEdgeAudio=audio;
        audio.onended = () => {
          _ttsSpeaking=false;
          if(_playingEdgeAudio===audio) _playingEdgeAudio=null;
          URL.revokeObjectURL(url);
          if(_voiceModeActive) setTimeout(()=>_startListening(),500);
        };
        audio.onerror = () => {
          _ttsSpeaking=false;
          if(_playingEdgeAudio===audio) _playingEdgeAudio=null;
          URL.revokeObjectURL(url);
          if(_voiceModeActive) setTimeout(()=>_startListening(),1000);
        };
        audio.play().catch(() => {
          _ttsSpeaking=false;
          if(_playingEdgeAudio===audio) _playingEdgeAudio=null;
          URL.revokeObjectURL(url);
          if(_voiceModeActive) setTimeout(()=>_startListening(),1000);
        });
      })
      .catch(() => {
        _ttsSpeaking=false;
        if(_voiceModeActive) setTimeout(()=>_startListening(),1000);
      });
      return;
    }
    if(engine==="edge"){
      const voice=localStorage.getItem("hermes-tts-voice")||"zh-CN-XiaoxiaoNeural";
      const savedRate=parseFloat(localStorage.getItem("hermes-tts-rate"));
      const savedPitch=parseFloat(localStorage.getItem("hermes-tts-pitch"));
      let rate='', pitch='';
      if(!isNaN(savedRate)){const pct=Math.round((savedRate-1)*100);const sign=pct>=0?'+':'';rate=sign+pct+'%';}
      if(!isNaN(savedPitch)){const hz=Math.round((savedPitch-1)*50);const sign=hz>=0?'+':'';pitch=sign+hz+'Hz';}
      _ttsSpeaking=true;
      fetch(new URL('api/tts', document.baseURI || location.href).href, {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({text: clean, voice, rate, pitch})
      })
      .then(r => {
        if(!r.ok) throw new Error('TTS request failed: ' + r.status);
        return r.blob();
      })
      .then(blob => {
        const url = URL.createObjectURL(blob);
        const audio = new Audio(url);
        // Register with the shared handle (declared in ui.js, same global scope;
        // both scripts are fully evaluated before any voice interaction) so
        // stopTTS() — called from _deactivate() — can actually pause hands-free
        // Edge playback. Without this the audio is local here and unstoppable.
        _playingEdgeAudio=audio;
        audio.onended = () => {
          _ttsSpeaking=false;
          if(_playingEdgeAudio===audio) _playingEdgeAudio=null;
          URL.revokeObjectURL(url);
          if(_voiceModeActive) setTimeout(()=>_startListening(),500);
        };
        audio.onerror = () => {
          _ttsSpeaking=false;
          if(_playingEdgeAudio===audio) _playingEdgeAudio=null;
          URL.revokeObjectURL(url);
          if(_voiceModeActive) setTimeout(()=>_startListening(),1000);
        };
        audio.play().catch(e => {
          _ttsSpeaking=false;
          if(_playingEdgeAudio===audio) _playingEdgeAudio=null;
          if(_voiceModeActive) setTimeout(()=>_startListening(),1000);
        });
      })
      .catch(() => {
        _ttsSpeaking=false;
        if(_voiceModeActive) setTimeout(()=>_startListening(),1000);
      });
      return;
    }
    const utter=new SpeechSynthesisUtterance(clean);

    // Apply saved voice preferences
    const savedVoice=localStorage.getItem('hermes-tts-voice');
    const voices=speechSynthesis.getVoices();
    if(savedVoice&&voices.length){
      const match=voices.find(v=>v.name===savedVoice);
      if(match) utter.voice=match;
    }
    const savedRate=parseFloat(localStorage.getItem('hermes-tts-rate'));
    if(!isNaN(savedRate)) utter.rate=Math.min(2,Math.max(0.5,savedRate));
    const savedPitch=parseFloat(localStorage.getItem('hermes-tts-pitch'));
    if(!isNaN(savedPitch)) utter.pitch=Math.min(2,Math.max(0,savedPitch));

    utter.onend=()=>{
      _browserTtsSuppressNextErrorRearm=false;
      _clearBrowserTtsRecovery();
      // After speaking, go back to listening
      if(_voiceModeActive&&_voiceModeState==='speaking') setTimeout(()=>_startListening(),500);
    };
    utter.onerror=()=>{
      _clearBrowserTtsRecovery();
      if(_browserTtsSuppressNextErrorRearm){
        _browserTtsSuppressNextErrorRearm=false;
        return;
      }
      if(_voiceModeActive) setTimeout(()=>_startListening(),1000);
    };

    _armBrowserTtsRecovery(clean, utter.rate);
    try{
      speechSynthesis.speak(utter);
    }catch(_){
      _clearBrowserTtsRecovery();
      if(_voiceModeActive) setTimeout(()=>_startListening(),1000);
    }
  }

  // Hook into response completion — observe when the agent finishes
  // We patch setComposerStatus to detect when a response completes
  const _origSetComposerStatus=(typeof setComposerStatus==='function')?setComposerStatus.bind(window):null;

  window._voiceModeOnResponseComplete=function(){
    if(_voiceModeActive&&_voiceModeState==='thinking'){
      // Small delay to let DOM render the final message
      setTimeout(()=>{
        if(_voiceModeActive&&_voiceModeState==='thinking'){
          _speakResponse();
        }
      },400);
    }
  };

  // Observe S.busy changes to detect response completion
  // The existing code calls setBusy(false) when response completes
  const _origSetBusy=(typeof setBusy==='function')?setBusy.bind(window):null;
  if(_origSetBusy){
    // We use a MutationObserver-style approach via polling S.busy
    // Actually, we'll use a simpler approach: hook into the message stream completion
  }

  // Most reliable hook: use the existing autoReadLastAssistant call site.
  // We override autoReadLastAssistant so that if voice mode is active, we use our
  // own speak-and-resume flow instead of the default auto-read.
  const _origAutoRead=(typeof autoReadLastAssistant==='function')?autoReadLastAssistant:null;
  window.autoReadLastAssistant=function(){
    if(_voiceModeActive&&_voiceModeState==='thinking'){
      _speakResponse();
      return;
    }
    if(_origAutoRead) _origAutoRead.apply(this,arguments);
  };

  function _activate(){
    if(_micOriginNeedsSecureContext()){
      showToast(t('mic_insecure_origin'));
      return;
    }
    _voiceModeActive=true;
    modeBtn.classList.add('active');
    _setButtonTooltip(modeBtn, t('voice_mode_toggle_active'));
    showToast(t('voice_mode_active'),1500);
    // If the agent is busy, wait — state will be 'thinking' and we'll detect completion
    if(typeof S!=='undefined'&&S.busy){
      _setState('thinking');
      return;
    }
    // Cancel any existing TTS
    if(typeof stopTTS==='function') stopTTS();
    _startListening();
  }

  function _deactivate(){
    _voiceModeActive=false;
    _voiceModeState='idle';
    _voiceModeThinkingSid=null;
    _browserTtsSuppressNextErrorRearm=false;
    modeBtn.classList.remove('active');
    _setButtonTooltip(modeBtn, t('voice_mode_toggle'));
    bar.style.display='none';
    clearTimeout(_silenceTimer);
    _clearBrowserTtsRecovery();
    try{ if(_recognition) _recognition.abort(); }catch(_){}
    _recognition=null;
    if(typeof stopTTS==='function') stopTTS();
    // Restore original autoReadLastAssistant
    if(_origAutoRead) window.autoReadLastAssistant=_origAutoRead;
    // Clear textarea if it was only voice input
    ta.value='';
    autoResize();
  }

  modeBtn.onclick=()=>{
    if(_voiceModeActive){
      _deactivate();
      showToast(t('voice_mode_off'),1500);
    }else{
      _activate();
    }
  };

  // Expose for external use
  window._voiceModeActive=()=>_voiceModeActive;
  window._voiceModeDeactivate=_deactivate;
  window._voiceModeImmediateSend=_voiceModeSend;
})();
function _currentSessionIsReusableEmptyChat(){
  if(!S.session) return false;
  const hasVisibleMessages=Array.isArray(S.messages)
    && S.messages.some(m=>m&&m.role&&m.role!=='tool');
  return (S.session.message_count||0)===0
    && !hasVisibleMessages
    && !S.busy
    && !S.session.active_stream_id
    && !S.session.pending_user_message;
}

$('fileInput').onchange=e=>{addFiles(Array.from(e.target.files));e.target.value='';};
$('btnNewChat').onclick=async()=>{
  // If the current session has no messages AND nothing is in flight, just focus
  // the composer rather than creating another empty session that will clutter the
  // sidebar list (#1171).
  //
  // The "nothing in flight" half is critical (#1432): if the user clicks + while
  // their first message is still streaming (or queued), `message_count` is still 0
  // server-side because the user turn hasn't been merged yet. The old guard treated
  // that as "empty" and made + a no-op for the entire stream duration, so users
  // couldn't actually start a parallel chat. Use the same in-flight signal as
  // `_restoreSettledSession()` in messages.js: an active stream id or a queued
  // pending user message means the session is real, not empty.
  if(_currentSessionIsReusableEmptyChat()){
    $('msg').focus();closeMobileSidebar();return;
  }
  if(typeof _restoreRememberedNewChatDraftSession==='function'
     && await _restoreRememberedNewChatDraftSession()){
    await renderSessionList();closeMobileSidebar();$('msg').focus();return;
  }
  await newSession();await renderSessionList();closeMobileSidebar();$('msg').focus();
};
$('btnDownload').onclick=()=>{
  if(!S.session)return;
  const blob=new Blob([transcript()],{type:'text/markdown'});
  const a=document.createElement('a');a.href=URL.createObjectURL(blob);
  a.download=`hermes-${S.session.session_id}.md`;a.click();URL.revokeObjectURL(a.href);
};
$('btnExportJSON').onclick=()=>{
  if(!S.session)return;
  const url=`/api/session/export?session_id=${encodeURIComponent(S.session.session_id)}`;
  const a=document.createElement('a');a.href=url;
  a.download=`hermes-${S.session.session_id}.json`;a.click();
};
$('btnShareSession').onclick=async()=>{
  if(!S.session) return;
  try{
    const existing=(S.session&&S.session.share_token)?new URL(`/share/${encodeURIComponent(S.session.share_token)}`,location.origin).href:null;
    if(existing){
      const reuse=await showConfirmDialog({
        title:t('share_session'),
        message:t('share_session_existing_confirm'),
        confirmLabel:t('share_session_copy_existing'),
        cancelLabel:t('share_session_refresh_snapshot'),
      });
      if(reuse){
        await _copyText(existing);
        showToast(t('share_session_link_copied'));
        window.open(existing,'_blank','noopener');
        return;
      }
    }
    const res=await api('/api/share/create',{method:'POST',body:JSON.stringify({session_id:S.session.session_id})});
    if(res&&res.session) S.session=res.session;
    const href=new URL(String(res&&res.share&&res.share.url||''),location.origin).href;
    await _copyText(href);
    showToast(t('share_session_created'));
    if(typeof _syncHermesPanelSessionActions==='function') _syncHermesPanelSessionActions();
    window.open(href,'_blank','noopener');
  }catch(err){
    showToast(t('share_session_failed')+(err&&err.message?err.message:String(err||'')),4000,'error');
  }
};
$('btnStopSharingSession').onclick=async()=>{
  if(!S.session||!S.session.share_token) return;
  const ok=await showConfirmDialog({
    title:t('stop_sharing_session'),
    message:t('stop_sharing_session_confirm'),
    confirmLabel:t('stop_sharing_session'),
    danger:true,
  });
  if(!ok) return;
  try{
    const res=await api('/api/share/revoke',{method:'POST',body:JSON.stringify({session_id:S.session.session_id})});
    if(res&&res.session) S.session=res.session;
    showToast(t('share_session_revoked'));
    if(typeof _syncHermesPanelSessionActions==='function') _syncHermesPanelSessionActions();
  }catch(err){
    showToast(t('share_session_revoke_failed')+(err&&err.message?err.message:String(err||'')),4000,'error');
  }
};
function exportSessionHTML(session){
  const target=session||S.session;
  if(!target||!target.session_id)return;
  const sid=target.session_id;
  const theme=document.documentElement.classList.contains('dark')?'dark':'light';
  // Capture the live WebUI palette so the export matches the user's active
  // theme + skin exactly, not just the built-in dark/light fallback. Map each
  // export-template variable to its WebUI source; getComputedStyle resolves to
  // the currently rendered colour regardless of which skin is selected.
  const cs=getComputedStyle(document.documentElement);
  const read=(...names)=>{for(const n of names){const v=cs.getPropertyValue(n).trim();if(v)return v;}return '';};
  const palette={
    'bg':read('--bg'),
    'panel':read('--surface','--bg'),
    'panel2':read('--code-bg','--surface'),
    'border':read('--border'),
    'text':read('--text'),
    'muted':read('--muted','--text'),
    'accent':read('--accent'),
    'code-bg':read('--code-bg'),
    'code-border':read('--border2','--border'),
    'code-text':read('--text'),
  };
  // Drop empties so the inlined fallback keeps working for anything we couldn't read.
  const clean={};for(const k in palette){if(palette[k])clean[k]=palette[k];}
  const paletteB64=btoa(unescape(encodeURIComponent(JSON.stringify(clean))));
  const url=`/api/session/export?session_id=${encodeURIComponent(sid)}&format=html&theme=${theme}&palette=${encodeURIComponent(paletteB64)}`;
  const a=document.createElement('a');a.href=url;
  a.download=`hermes-${sid}.html`;a.click();
}
$('btnExportHTML').onclick=()=>exportSessionHTML();
$('btnImportJSON').onclick=()=>$('importFileInput').click();
$('importFileInput').onchange=async(e)=>{
  const file=e.target.files[0];
  if(!file)return;
  e.target.value='';
  try{
    const text=await file.text();
    const data=JSON.parse(text);
    const res=await api('/api/session/import',{method:'POST',body:JSON.stringify(data)});
    if(res.ok&&res.session){
      await loadSession(res.session.session_id);
      await renderSessionList();
      if(_currentPanel==='settings') switchPanel('chat');
      showToast(t('session_imported'));
    }
  }catch(err){
    showToast(t('import_failed')+(err.message||t('import_invalid_json')));
  }
};
// btnRefreshFiles is now panel-icon-btn in header (see HTML)
function clearPreview(opts={}){
  const keepPanelOpen=!!(opts&&opts.keepPanelOpen);
  // Restore directory breadcrumb after closing file preview
  if(typeof renderBreadcrumb==='function') renderBreadcrumb();
  const closePanelAfter=_workspacePanelMode==='preview'&&!keepPanelOpen;
  const pa=$('previewArea');if(pa)pa.classList.remove('visible');
  const pi=$('previewImg');if(pi){pi.onerror=null;pi.src='';}
  const pdf=$('previewPdfFrame');if(pdf)pdf.src='';
  const html=$('previewHtmlIframe');if(html)html.src='';
  const pm=$('previewMd');if(pm)pm.innerHTML='';
  const pc=$('previewCode');if(pc)pc.textContent='';
  const pp=$('previewPathText');if(pp)pp.textContent='';
  const ft=$('fileTree');if(ft)ft.style.display='';
  _previewCurrentPath='';_previewCurrentMode='';_previewDirty=false;
  if(closePanelAfter)closeWorkspacePanel();
  else if(keepPanelOpen&&_workspacePanelMode==='preview')openWorkspacePanel('browse');
  else syncWorkspacePanelUI();
}
$('btnClearPreview').onclick=handleWorkspaceClose;
// workspacePath click handler removed -- use topbar workspace chip dropdown instead
function _applySessionContextMetadataUpdate(data){
  if(!S.session||!data||!data.session)return;
  S.session.context_length=data.session.context_length||0;
  S.session.threshold_tokens=data.session.threshold_tokens||0;
  S.session.last_prompt_tokens=data.session.last_prompt_tokens||0;
  S.session.post_compression_context_tokens_estimate=data.session.post_compression_context_tokens_estimate||null;
  if(typeof _syncCtxIndicator==='function'){
    const u=S.lastUsage||{};
    const _pick=(latest,stored,dflt=0)=>latest!=null?latest:(stored!=null?stored:dflt);
    _syncCtxIndicator({
      input_tokens:_pick(u.input_tokens,S.session.input_tokens),
      output_tokens:_pick(u.output_tokens,S.session.output_tokens),
      estimated_cost:_pick(u.estimated_cost,S.session.estimated_cost),
      context_length:S.session.context_length||0,
      last_prompt_tokens:_pick(u.last_prompt_tokens,S.session.last_prompt_tokens),
      post_compression_context_tokens_estimate:S.session.post_compression_context_tokens_estimate,
      threshold_tokens:S.session.threshold_tokens||0,
    });
  }
}

$('modelSelect').onchange=async()=>{
  const selectedModel=$('modelSelect').value;
  const modelState=(typeof _modelStateForSelect==='function')
    ? _modelStateForSelect($('modelSelect'),selectedModel)
    : {model:selectedModel,model_provider:null};
  if(typeof clearProfileTransitionReasoningContext==='function') clearProfileTransitionReasoningContext();
  if(typeof closeModelDropdown==='function') closeModelDropdown();
  if(typeof _writePersistedModelState==='function') _writePersistedModelState(modelState.model,modelState.model_provider);
  else try{localStorage.setItem('hermes-webui-model',modelState.model)}catch{}
  if(!S.session){
    if(typeof _rememberEmptyComposerModelOverride==='function') _rememberEmptyComposerModelOverride(modelState.model,modelState.model_provider);
    if(typeof syncModelChip==='function') syncModelChip();
    if(typeof syncReasoningChip==='function') syncReasoningChip();
    return;
  }
  if(typeof _rememberPendingSessionModel==='function') _rememberPendingSessionModel(S.session.session_id,modelState.model,modelState.model_provider);
  S.session.model=modelState.model;
  S.session.model_provider=modelState.model_provider||null;
  if(typeof syncModelChip==='function') syncModelChip();
  if(typeof syncReasoningChip==='function') syncReasoningChip();
  syncTopbar();
  // Clarify scope: composer model changes are session-local, not the global default.
  if(typeof showToast==='function'){
    showToast(t('model_scope_toast')||'Applies to this conversation from your next message.', 3000);
  }
  const data=await api('/api/session/update',{method:'POST',body:JSON.stringify({
    session_id:S.session.session_id,
    workspace:S.session.workspace,
    model:modelState.model,
    model_provider:modelState.model_provider||null,
  })});
  // NOTE: do NOT clear the pending explicit-pick marker here. It must survive until
  // the NEXT send() consumes it, otherwise the normal "pick → session-update → send"
  // flow loses the explicit-pick signal before /api/chat/start runs and the server
  // re-reverts a cross-family pick (the #3737 bug, Codex catch). send() clears it
  // after reading a matching pending pick. (#3739/#3737)
  _applySessionContextMetadataUpdate(data);
  // Warn if selected model belongs to a different provider than what Hermes is configured for
  if(typeof _checkProviderMismatch==='function'){
    const warn=_checkProviderMismatch(selectedModel);
    if(warn&&typeof showToast==='function') showToast(warn,4000);
  }
};
$('msg').addEventListener('input',()=>{
  updateSendBtn();
  scheduleComposerAutoResize();
  // Persist composer draft to server (debounced in _saveComposerDraft).
  const sid = S && S.session && S.session.session_id;
  if (sid && typeof _saveComposerDraft === 'function') {
    _saveComposerDraft(sid, $('msg').value, S.pendingFiles ? [...S.pendingFiles] : []);
  }
  const text=$('msg').value;
  const _slashIdx=typeof _activeSlashCommandOffset==='function'?_activeSlashCommandOffset(text):-1;
  if(_slashIdx>=0&&text.indexOf('\n')===-1){
    if(typeof getSlashAutocompleteMatches==='function'){
      getSlashAutocompleteMatches(text).then(matches=>{
        if(($('msg').value||'')!==text) return;
        if(matches.length)showCmdDropdown(matches); else hideCmdDropdown();
      });
    }else{
      const prefix=text.slice(_slashIdx+1);
      const matches=getMatchingCommands(prefix);
      if(matches.length)showCmdDropdown(matches); else hideCmdDropdown();
    }
    if(typeof ensureSkillCommandsLoadedForAutocomplete==='function') ensureSkillCommandsLoadedForAutocomplete();
  } else if(typeof getComposerPathAutocompleteMatches==='function'){
    const cursor=$('msg').selectionStart;
    getComposerPathAutocompleteMatches(text,cursor).then(matches=>{
      const ta=$('msg');
      if(!ta||ta.value!==text||ta.selectionStart!==cursor) return;
      if(matches.length)showCmdDropdown(matches); else hideCmdDropdown();
    }).catch(()=>hideCmdDropdown());
  } else {
    hideCmdDropdown();
  }
});
// #5514/#5515: re-pin the transcript on ANY composer height change, not only the
// ones that route through the input->autoResize path. A multi-line paste
// (WisprFlow), a draft restore, an attachment tray / selection-chip appearing, a
// programmatic value set, or a font/reflow can all grow the composer and shrink
// the flex:1 transcript viewport, stranding a pinned reader above the bottom
// (reads as a "random" upward jump — #5515). Observe the whole #composerWrap
// (not just #msg) so tray/chip growth is covered too, at one seam. The re-pin is
// guarded (only fires when genuinely pinned), so it never fights a reader who
// scrolled away. First callback fires on observe (initial size) — the guard
// makes that a cheap no-op.
(()=>{
  const _cw=$('composerWrap')||$('msg');
  if(!_cw || typeof ResizeObserver!=='function' || typeof _repinMessagesAfterComposerResize!=='function') return;
  let _lastComposerH=_cw.offsetHeight;
  const _ro=new ResizeObserver(()=>{
    const h=_cw.offsetHeight;
    if(h<=_lastComposerH){_lastComposerH=h;return;}   // shrink/no-op: enlarges the viewport, can't strand
    _lastComposerH=h;
    _repinMessagesAfterComposerResize();               // grow: re-pin the pinned reader
  });
  try{ _ro.observe(_cw); }catch(_){ }
})();
// Track IME composition for East Asian input. Safari fires the committing
// keydown AFTER compositionend with isComposing=false, so we also keep a
// manual flag and reset it on the next tick to swallow that trailing Enter.
// Also reset on blur so the flag can never get stuck in a true state if
// compositionend never fires (focus loss with some IME implementations).
//
// The `_imeComposing` flag is bound to the chat composer (`#msg`); other
// inputs (session/project rename, app dialog, message edit, workspace rename)
// rely on the state-free `e.isComposing || e.keyCode === 229` part of
// `_isImeEnter`, which is sufficient for the Safari race because keyCode 229
// is the canonical "still composing" signal regardless of which field is
// focused. Promote `_isImeEnter` to `window` so other modules can reuse it
// without duplicating the full IIFE per input (issue #1443).
let _imeComposing=false;
(()=>{const _c=$('msg');if(!_c)return;
  _c.addEventListener('compositionstart',()=>{_imeComposing=true;});
  _c.addEventListener('compositionend',()=>{setTimeout(()=>{_imeComposing=false;},0);});
  _c.addEventListener('blur',()=>{_imeComposing=false;});
})();
function _isImeEnter(e){return e.isComposing||e.keyCode===229||_imeComposing;}
window._isImeEnter=_isImeEnter;
// #3076: a touch-primary device (`pointer:coarse`) can still have a
// physical keyboard attached (Android tablet + Bluetooth keyboard,
// detachable Surface in tablet mode, iPad + Magic Keyboard). When that
// happens we should NOT force the mobile newline-on-Enter override
// because Shift+Enter / Ctrl+Enter come from real keys and the user
// expects desktop semantics. `matchMedia('(any-pointer:fine)')` is true
// whenever ANY available pointing device is fine-grained — which is the
// strongest signal browsers expose for "there is a real keyboard /
// trackpad in the picture too". Skip the mobile default in that case.
function _hasFinePointerCoexisting(){
  try{ return matchMedia('(any-pointer:fine)').matches; }catch(_){ return false; }
}
function _isNumpadEnter(e){
  return e.key==='Enter'&&(e.code==='NumpadEnter'||e.location===KeyboardEvent.DOM_KEY_LOCATION_NUMPAD);
}
$('msg').addEventListener('keydown',e=>{
  // Autocomplete navigation when dropdown is open
  const dd=$('cmdDropdown');
  const dropdownOpen=dd&&dd.classList.contains('open');
  if(dropdownOpen){
    if(e.key==='ArrowUp'){e.preventDefault();navigateCmdDropdown(-1);return;}
    if(e.key==='ArrowDown'){e.preventDefault();navigateCmdDropdown(1);return;}
    if(e.key==='Tab'){e.preventDefault();selectCmdDropdownItem();return;}
    if(e.key==='Escape'){e.preventDefault();e.stopPropagation();hideCmdDropdown();return;}
    if(e.key==='Enter'&&!e.shiftKey){
      if(_isImeEnter(e)){return;}
      if(window._sendKey==='shift+enter'){
        return;
      }
      e.preventDefault();
      selectCmdDropdownItem();
      return;
    }
  }
  // Send key: respect user preference.
  // On touch-primary devices (coarse pointer, no fine pointer co-existing),
  // default to Enter = newline regardless of whether the visual viewport has
  // shrunk. The viewport-shrink heuristic (_isVirtualKeyboardLikelyOpen) was
  // unreliable on iOS Safari and some Android browsers where the keyboard
  // doesn't consistently reduce vv.height by >120px. The pointer media query
  // pair is a sufficient and more reliable signal for "software keyboard only".
  // Hardware keyboards on tablets are covered by _hasFinePointerCoexisting.
  // The 'ctrl+enter' and 'shift+enter' settings also use this behavior
  // (plain Enter = newline).
  // Users can override in Settings by explicitly choosing 'enter' mode.
  if(e.key==='Enter'){
    if(_isImeEnter(e)){return;}
    const isNumpadEnter=_isNumpadEnter(e);
    const _mobileDefault=matchMedia('(pointer:coarse)').matches
      &&!_hasFinePointerCoexisting()
      &&window._sendKey==='enter';
    if(window._sendKey==='shift+enter'){
      if(e.shiftKey){e.preventDefault();send();}
    } else if(window._sendKey==='ctrl+enter'||_mobileDefault){
      if(isNumpadEnter||e.ctrlKey||e.metaKey){e.preventDefault();send();}
    } else {
      if(!e.shiftKey){e.preventDefault();send();}
    }
  }
});
// B14: Cmd/Ctrl+K creates a new chat from anywhere
document.addEventListener('keydown',async e=>{
  // Cmd/Ctrl+B toggles desktop sidebar collapse (VS Code convention).
  // Skip when typing in an input/textarea/contenteditable so text-edit
  // shortcuts (e.g. bold in some embedded editors) are never stolen.
  if((e.metaKey||e.ctrlKey)&&!e.shiftKey&&!e.altKey&&(e.key==='b'||e.key==='B')){
    const t=e.target;
    const isText=t&&(t.tagName==='INPUT'||t.tagName==='TEXTAREA'||t.isContentEditable);
    if(!isText&&typeof toggleSidebar==='function'&&_isDesktopWidth()){
      e.preventDefault();
      toggleSidebar();
      return;
    }
  }
  // Cmd/Ctrl+/ focuses the message composer without creating a chat.
  // Match on the '/' CHARACTER (e.key), not the physical key position: on QWERTZ
  // layouts the physical Slash key produces Ctrl+- (browser zoom-out) and '/' is
  // typed as Shift+7, so matching the physical code both steals zoom and misses
  // the real '/' chord. e.key==='/' is layout-correct on every keyboard.
  if((e.metaKey||e.ctrlKey)&&!e.altKey&&e.key==='/'){
    const t=e.target;
    const isText=t&&(t.tagName==='INPUT'||t.tagName==='TEXTAREA'||t.isContentEditable);
    if(isText) return;
    const composer=$('msg');
    if(composer){e.preventDefault();composer.focus();}
    return;
  }
  // Enter on approval card = Allow once (when a button inside the card is focused or
  // card is visible and focus is not on an input/textarea/select)
  if(e.key==='Enter'&&!e.metaKey&&!e.ctrlKey&&!e.shiftKey){
    const card=$('approvalCard');
    const tag=(document.activeElement||{}).tagName||'';
    if(card&&card.classList.contains('visible')&&tag!=='TEXTAREA'&&tag!=='INPUT'&&tag!=='SELECT'){
      e.preventDefault();
      if(typeof respondApproval==='function') respondApproval('once');
      return;
    }
  }
  if((e.metaKey||e.ctrlKey)&&e.key==='k'){
    const t=e.target;
    const isText=t&&(t.tagName==='INPUT'||t.tagName==='TEXTAREA'||t.isContentEditable);
    if(isText) return;
    e.preventDefault();
    // If the current session has no messages AND nothing is in flight, just focus
    // the composer rather than creating another empty session that will clutter
    // the sidebar list (#1171). See the matching guard in $('btnNewChat').onclick
    // and bug #1432 for why the in-flight check is needed.
    if(_currentSessionIsReusableEmptyChat()){
      $('msg').focus();return;
    }
    // Cmd/Ctrl+K should always create a new conversation, even while the current
    // one is still streaming. The old !S.busy guard meant users had to wait for
    // a long generation to finish before they could start something new — exactly
    // the moment they want to switch context. newSession() leaves the in-flight
    // stream running on its own session; the user just gets a fresh blank one.
    await newSession();await renderSessionList();closeMobileSidebar();$('msg').focus();
  }
  // Cmd/Ctrl+, opens/closes Settings (VS Code convention).
  // Fire globally — like VS Code, don't skip text inputs.
  if((e.metaKey||e.ctrlKey)&&!e.shiftKey&&!e.altKey&&e.key===','){
    e.preventDefault();
    if(typeof toggleSettings==='function') toggleSettings();
    return;
  }
  if(e.key==='Escape'){
    // Close onboarding overlay if open (skip/dismiss the wizard)
    const onboardingOverlay=$('onboardingOverlay');
    if(onboardingOverlay&&onboardingOverlay.style.display!=='none'){
      if(typeof skipOnboarding==='function') skipOnboarding();
      return;
    }
    // Close settings panel if active
    if(_currentPanel==='settings'){_closeSettingsPanel();return;}
    // Close workspace dropdown
    closeWsDropdown();
    // Clear session search
    const ss=$('sessionSearch');
    if(ss&&ss.value){
      if(typeof clearSessionSearch==='function') clearSessionSearch(false);
      else { ss.value=''; filterSessions(); }
    }
    // Cancel any active message edit
    const editArea=document.querySelector('.msg-edit-area');
    if(editArea){
      const bar=editArea.closest('.msg-row')&&editArea.closest('.msg-row').querySelector('.msg-edit-bar');
      if(bar){const cancel=bar.querySelector('.msg-edit-cancel');if(cancel)cancel.click();}
    }
    // Blur composer to enable j/k message navigation.
    // Skip while an IME candidate window is composing — Escape there should
    // dismiss the candidate, not blur the composer (CJK input).
    if(document.activeElement===$('msg') && !e.isComposing && !_imeComposing){
      $('msg').blur();
    }
  }
});
const LARGE_TEXT_PASTE_CHAR_THRESHOLD=4000;
const LARGE_TEXT_PASTE_LINE_THRESHOLD=100;
function _largeTextPasteLineCount(text){
  const value=String(text||'');
  const lines=value.split('\n');
  return value.endsWith('\n')?lines.length-1:lines.length;
}
function _shouldAttachLargePastedText(text){
  if(window._largeTextPasteAsAttachment===false)return false;
  const value=String(text||'');
  if(!value.trim())return false;
  return value.length>=LARGE_TEXT_PASTE_CHAR_THRESHOLD || _largeTextPasteLineCount(value)>=LARGE_TEXT_PASTE_LINE_THRESHOLD;
}
function _largeTextPasteFileName(now){
  const d=new Date(now||Date.now());
  const p=n=>String(n).padStart(2,'0');
  const stamp=`${d.getFullYear()}-${p(d.getMonth()+1)}-${p(d.getDate())}_${p(d.getHours())}-${p(d.getMinutes())}-${p(d.getSeconds())}-${String(d.getMilliseconds()).padStart(3,'0')}`;
  const existing=new Set((S.pendingFiles||[]).map(f=>f&&f.name).filter(Boolean));
  let name=`pasted-text-${stamp}.md`;
  for(let i=2;existing.has(name);i++)name=`pasted-text-${stamp}-${i}.md`;
  return name;
}
function _largeTextPasteFile(text,now){
  const name=_largeTextPasteFileName(now||Date.now());
  return new File([String(text||'')],name,{type:'text/markdown;charset=utf-8'});
}
function _largeTextPasteFitsUploadLimit(file){
  return !(file&&typeof MAX_UPLOAD_BYTES==='number'&&file.size>MAX_UPLOAD_BYTES);
}
function _attachLargePastedText(file){
  addFiles([file]);
  if(typeof setStatus==='function')setStatus(t('text_pasted')+file.name);
  return file;
}
$('msg').addEventListener('paste',e=>{
  const items=Array.from(e.clipboardData?.items||[]);
  // Extract image items (kind==='file' filter avoids misclassifying text/html
  // with embedded data URIs as images).
  const imageItems=items.filter(i=>i.kind==='file'&&i.type.startsWith('image/'));
  if(imageItems.length){
    // If text is also present (common when copying images from browsers, Notes,
    // Slack, etc.), let the browser paste the text normally AND attach the image.
    // Only preventDefault when the clipboard is image-only (true screenshot paste).
    const hasText=items.some(i=>i.kind==='string'&&(i.type==='text/plain'||i.type==='text/html'));
    if(!hasText)e.preventDefault();
    const pasteTs=Date.now();
    const files=imageItems.map((i,idx)=>{
      const blob=i.getAsFile();
      const ext=i.type.split('/')[1]||'png';
      const suffix=imageItems.length>1?`-${idx+1}`:'';
      return new File([blob],`screenshot-${pasteTs}${suffix}.${ext}`,{type:i.type});
    });
    addFiles(files);
    setStatus(t('image_pasted')+files.map(f=>f.name).join(', '));
    return;
  }
  const plainText=e.clipboardData?.getData('text/plain')||'';
  if(!_shouldAttachLargePastedText(plainText))return;
  const pastedTextFile=_largeTextPasteFile(plainText);
  if(!_largeTextPasteFitsUploadLimit(pastedTextFile))return;
  e.preventDefault();
  _attachLargePastedText(pastedTextFile);
});
document.querySelectorAll('.suggestion').forEach(btn=>{
  btn.onclick=()=>{$('msg').value=btn.dataset.msg;send();};
});

function applyEmptyStateSuggestionPref(){
  if(!$('emptyState')) return;
  $('emptyState').classList.toggle('no-suggestions',window._hideEmptyStateSuggestions===true);
}

window.addEventListener('resize',()=>{
  _syncWorkspacePanelInlineWidth();
  syncWorkspacePanelState();
  if(!window.visualViewport) _forceMobileViewportReflow();
});

// On PWAs / mobile browsers that expose visualViewport, keyboard show/hide and
// URL-bar collapse fire visualViewport resize/scroll rather than window resize.
// Debounce a reflow so the phone layout repaints against the new geometry.
if(window.visualViewport){
  _syncKeyboardBottomInset();
  let _mobileViewportReflowTimer=0;
  const _scheduleMobileViewportReflow=()=>{
    if(_mobileViewportReflowTimer) clearTimeout(_mobileViewportReflowTimer);
    _mobileViewportReflowTimer=setTimeout(()=>{
      _mobileViewportReflowTimer=0;
      _forceMobileViewportReflow();
    },60);
  };
  window.visualViewport.addEventListener('resize', _scheduleMobileViewportReflow);
  window.visualViewport.addEventListener('scroll', _scheduleMobileViewportReflow);
}

// Boot: restore last session or start fresh
// ── Resizable panels ──────────────────────────────────────────────────────
(function(){
  const SIDEBAR_MIN=180, SIDEBAR_MAX=420;
  const PANEL_MIN=180,   PANEL_MAX=1200;

  function initResize(handleId, targetEl, edge, minW, maxW, storageKey){
    const handle = $(handleId);
    if(!handle || !targetEl) return;

    // Restore saved width
    if(storageKey === 'hermes-panel-w'){
      _syncWorkspacePanelInlineWidth();
    }else{
      const saved = localStorage.getItem(storageKey);
      if(saved) targetEl.style.width = saved + 'px';
    }

    let startX=0, startW=0;

    handle.addEventListener('mousedown', e=>{
      e.preventDefault();
      startX = e.clientX;
      startW = targetEl.getBoundingClientRect().width;
      handle.classList.add('dragging');
      document.body.classList.add('resizing');

      const onMove = ev=>{
        const delta = edge==='right' ? ev.clientX - startX : startX - ev.clientX;
        const newW = Math.min(maxW, Math.max(minW, startW + delta));
        targetEl.style.width = newW + 'px';
      };
      const onUp = ()=>{
        handle.classList.remove('dragging');
        document.body.classList.remove('resizing');
        localStorage.setItem(storageKey, parseInt(targetEl.style.width));
        document.removeEventListener('mousemove', onMove);
        document.removeEventListener('mouseup', onUp);
      };
      document.addEventListener('mousemove', onMove);
      document.addEventListener('mouseup', onUp);
    });
  }

  // Run after DOM ready (called from boot)
  window._initResizePanels = function(){
    const sidebar    = document.querySelector('.sidebar');
    const rightpanel = document.querySelector('.rightpanel');
    initResize('sidebarResize',    sidebar,    'right', SIDEBAR_MIN, SIDEBAR_MAX, 'hermes-sidebar-w');
    initResize('rightpanelResize', rightpanel, 'left',  PANEL_MIN,   PANEL_MAX,   'hermes-panel-w');
  };
})();

// ── Appearance helpers (theme = light/dark/system, skin = palette/accent) ────
const _THEMES=[
  {name:'Light', value:'light', colors:['#FEFCF7','#FAF7F0','#B8860B']},
  {name:'Dark', value:'dark', colors:['#0D0D1A','#141425','#FFD700']},
  {name:'System', value:'system', colors:['#FEFCF7','#0D0D1A','#B8860B']},
];
const _SKINS=[
  {name:'Default',  colors:['#FFD700','#FFBF00','#CD7F32']},
  {name:'Ares',     colors:['#FF4444','#CC3333','#992222']},
  {name:'Mono',     colors:['#CCCCCC','#999999','#666666']},
  {name:'Graphite', colors:['#FFFFFF','#D6D6D6','#242424']},
  {name:'GitHub', colors:['#0969DA','#1F883D','#242424']},
  {name:'Codex', colors:['#72B39A','#242624','#ECEBE4']},
  {name:'Terracotta', colors:['#D97757','#F0EEE6','#141413']},
  {name:'Slate',    colors:['#334155','#475569','#64748b']},
  {name:'Poseidon', colors:['#0EA5E9','#0284C7','#0369A1']},
  {name:'Sisyphus', colors:['#A78BFA','#8B5CF6','#7C3AED']},
  {name:'Charizard',colors:['#FB923C','#F97316','#EA580C']},
  {name:'Sienna',   colors:['#D97757','#C06A49','#9A523A']},
  {name:'Catppuccin',colors:['#CBA6F7','#B4BEFE','#8839EF']},
  {name:'Hepburn',   colors:['#c6246a','#ec5597','#f2abca']},
  {name:'Nous',     colors:['#4682B4','#3A6E9A','#2C5F88']},
  {name:'Neon',     colors:['#B347FF','#C76BFF','#00DDFF']},
  {name:'Neon Soft', value:'neon-soft', colors:['#B347FF','#C76BFF','#00DDFF']},
  {name:'Neon Paint', value:'neon-paint', colors:['#FF2D95','#00E5FF','#FFB800']},
  {name:'Geist Contrast', value:'geist-contrast', colors:['#000000','#ffffff','#FFF175']},
  {name:'Zeus',     colors:['#FFD700','#FFBF00','#1A1A00']},
  {name:'Verdigris', value:'verdigris', colors:['#C89A5A','#0F1714','#22342C']},
];
const _VALID_THEMES=new Set((_THEMES||[]).map(t=>t.value));
const _VALID_SKINS=new Set((_SKINS||[]).map(s=>(s.value||s.name).toLowerCase()));
const _LEGACY_THEME_MAP={
  slate:{theme:'dark',skin:'slate'},
  solarized:{theme:'dark',skin:'poseidon'},
  monokai:{theme:'dark',skin:'sisyphus'},
  nord:{theme:'dark',skin:'slate'},
  oled:{theme:'dark',skin:'default'},
};
let _systemThemeMq=null;
let _onSystemThemeChange=null;
let _resolvedThemeBaseDark=false;

function _normalizeAppearance(theme,skin){
  const rawTheme=typeof theme==='string'?theme.trim().toLowerCase():'';
  const rawSkin=typeof skin==='string'?skin.trim().toLowerCase():'';
  const legacy=_LEGACY_THEME_MAP[rawTheme];
  const nextTheme=legacy?legacy.theme:(_VALID_THEMES.has(rawTheme)?rawTheme:'dark');
  const nextSkin=_VALID_SKINS.has(rawSkin)?rawSkin:(legacy?legacy.skin:'default');
  return {theme:nextTheme,skin:nextSkin};
}

// Sync <meta name="theme-color"> with the active theme's app chrome color.
// This surfaces the WebUI's exact theme background to:
//   1. Mobile Safari status bar (the prefers-color-scheme media variants in index.html
//      cover the pre-load case; this updater handles user-toggled changes mid-session).
//   2. iOS PWA / Add to Home Screen status bar.
//   3. Native WKWebView wrappers (e.g. hermes-swift-mac) that read this attribute as
//      the source of truth for AppKit chrome (tab bar, title bar, traffic-light area)
//      instead of pixel-sampling — overlay-resistant and IPC-free.
// Reading getComputedStyle(html).getPropertyValue('--sidebar') picks up the active skin
// (Default, Sienna, Sisyphus, Charizard, etc.) so each skin's distinct paint reaches
// the meta tag.
function _syncThemeColorMeta(){
  try{
    const bg=getComputedStyle(document.documentElement).getPropertyValue('--sidebar').trim();
    if(!bg) return;
    const known=document.getElementById('hermes-theme-color');
    if(known){
      known.setAttribute('content',bg);
      known.removeAttribute('media');
    }
    document.querySelectorAll('meta[name="theme-color"]').forEach(meta=>{
      meta.setAttribute('content',bg);
      meta.removeAttribute('media');
    });
  }catch(e){}
}

function _skinKey(skin){
  return (skin&&String(skin.value||skin.name||'').toLowerCase())||'';
}

function _findSkinEntry(key){
  const normalized=String(key||'default').toLowerCase();
  return (_SKINS||[]).find(s=>_skinKey(s)===normalized)||null;
}

function _activeSkinScheme(){
  const key=(document.documentElement.dataset.skin||'default').toLowerCase();
  const skin=_findSkinEntry(key);
  const scheme=skin&&skin._extScheme;
  return scheme==='light'||scheme==='dark'?scheme:'';
}

function _effectiveThemeDark(baseIsDark){
  const skinScheme=_activeSkinScheme();
  if(skinScheme==='dark') return true;
  if(skinScheme==='light') return false;
  return !!baseIsDark;
}

function _setResolvedTheme(isDark){
  _resolvedThemeBaseDark=!!isDark;
  const effectiveDark=_effectiveThemeDark(_resolvedThemeBaseDark);
  document.documentElement.classList.toggle('dark',effectiveDark);
  const link=document.getElementById('prism-theme');
  if(!link){ _syncThemeColorMeta(); return; }
  const want=effectiveDark
    ?'https://cdn.jsdelivr.net/npm/prismjs@1.29.0/themes/prism-tomorrow.min.css'
    :'https://cdn.jsdelivr.net/npm/prismjs@1.29.0/themes/prism.min.css';
  // No SRI integrity on theme CSS — jsdelivr edge nodes serve different
  // digests for the same pinned version, causing intermittent blocking (#1100).
  if(link.href!==want){ link.integrity=''; link.href=want; }
  _syncThemeColorMeta();
}

function _applyTheme(name){
  const normalized=_normalizeAppearance(name,'default');
  delete document.documentElement.dataset.theme;
  if(_systemThemeMq&&_onSystemThemeChange){
    _systemThemeMq.removeEventListener('change',_onSystemThemeChange);
    _systemThemeMq=null;
    _onSystemThemeChange=null;
  }
  if(normalized.theme==='system'){
    _systemThemeMq=window.matchMedia('(prefers-color-scheme:dark)');
    _onSystemThemeChange=()=>_setResolvedTheme(_systemThemeMq.matches);
    _setResolvedTheme(_systemThemeMq.matches);
    _systemThemeMq.addEventListener('change',_onSystemThemeChange);
    return;
  }
  _setResolvedTheme(normalized.theme==='dark');
}

function _applySkin(name){
  const key=(name||'default').toLowerCase();
  if(key==='default') delete document.documentElement.dataset.skin;
  else document.documentElement.dataset.skin=key;
  _setResolvedTheme(_resolvedThemeBaseDark);
}

function _pickTheme(name){
  const currentSkin=localStorage.getItem('hermes-skin');
  const appearance=_normalizeAppearance(name,currentSkin);
  localStorage.setItem('hermes-theme',appearance.theme);
  localStorage.setItem('hermes-skin',appearance.skin);
  _applyTheme(appearance.theme);
  _applySkin(appearance.skin);
  _syncThemePicker(appearance.theme);
  _syncSkinPicker(appearance.skin);
  const hidden=$('settingsTheme');
  if(hidden) hidden.value=appearance.theme;
  const skinHidden=$('settingsSkin');
  if(skinHidden) skinHidden.value=appearance.skin;
  if(typeof _scheduleAppearanceAutosave==='function') _scheduleAppearanceAutosave();
}

function _pickSkin(name){
  const appearance=_normalizeAppearance(localStorage.getItem('hermes-theme'),name);
  localStorage.setItem('hermes-theme',appearance.theme);
  localStorage.setItem('hermes-skin',appearance.skin);
  _applyTheme(appearance.theme);
  _applySkin(appearance.skin);
  _syncThemePicker(appearance.theme);
  _syncSkinPicker(appearance.skin);
  const hidden=$('settingsSkin');
  if(hidden) hidden.value=appearance.skin;
  const themeHidden=$('settingsTheme');
  if(themeHidden) themeHidden.value=appearance.theme;
  if(typeof _scheduleAppearanceAutosave==='function') _scheduleAppearanceAutosave();
}

function _syncThemePicker(active){
  document.querySelectorAll('#themePickerGrid .theme-pick-btn').forEach(btn=>{
    btn.classList.toggle('active',btn.dataset.themeVal===active);
    btn.style.borderColor='';
    btn.style.boxShadow='';
  });
}

function _syncSkinPicker(active){
  document.querySelectorAll('#skinPickerGrid .skin-pick-btn').forEach(btn=>{
    btn.classList.toggle('active',btn.dataset.skinVal===active);
    btn.style.borderColor='';
    btn.style.boxShadow='';
  });
}

function _applyFontSize(size){
  if(size&&size!=='default'){
    document.documentElement.dataset.fontSize=size;
  } else {
    delete document.documentElement.dataset.fontSize;
  }
}

function _pickFontSize(size){
  localStorage.setItem('hermes-font-size',size);
  _applyFontSize(size);
  _syncFontSizePicker(size);
  const hidden=$('settingsFontSize');
  if(hidden) hidden.value=size;
  if(typeof _scheduleAppearanceAutosave==='function') _scheduleAppearanceAutosave();
}

function _syncFontSizePicker(active){
  document.querySelectorAll('#fontSizePickerGrid .font-size-pick-btn').forEach(btn=>{
    btn.classList.toggle('active',btn.dataset.fontSizeVal===(active||'default'));
    btn.style.borderColor='';
    btn.style.boxShadow='';
  });
}

function _buildSkinPicker(activeSkin){
  const grid=$('skinPickerGrid');
  if(!grid) return;
  grid.innerHTML='';
  for(const skin of _SKINS){
    const key=(skin.value||skin.name).toLowerCase();
    const btn=document.createElement('button');
    btn.type='button';
    btn.className='skin-pick-btn';
    btn.dataset.skinVal=key;
    btn.style.cssText='border:1px solid var(--border2);border-radius:8px;padding:8px 4px;text-align:center;cursor:pointer;background:none;transition:all .15s';
    btn.onclick=()=>_pickSkin(key);
    // Build with DOM nodes + textContent so an extension-registered skin's
    // label/name (registerHermesSkin descriptor) can never inject markup into
    // the picker. Swatch colors are already value-sanitized upstream, but set
    // them via element.style.background (not interpolated HTML) as defense in depth.
    const dotRow=document.createElement('div');
    dotRow.style.cssText='display:flex;gap:3px;justify-content:center;margin-bottom:4px';
    for(const c of (skin.colors||[])){
      const dot=document.createElement('span');
      dot.style.cssText='display:inline-block;width:10px;height:10px;border-radius:50%';
      dot.style.background=c;
      dotRow.appendChild(dot);
    }
    const labelEl=document.createElement('span');
    labelEl.style.cssText='font-size:11px;color:var(--text)';
    labelEl.textContent=skin.label||skin.name||'';
    btn.appendChild(dotRow);
    btn.appendChild(labelEl);
    grid.appendChild(btn);
  }
  _syncSkinPicker((activeSkin||'default').toLowerCase());
}

// ── Extension-registered skins (theme-registration capability) ───────────────
// Lets a trusted local extension contribute a custom skin that appears in the
// NATIVE skin picker (rather than bolting on a parallel theme switcher). An
// extension calls window.registerHermesSkin(descriptor); core validates +
// sanitizes it, injects a managed <style> rule for its CSS-variable tokens,
// appends it to _SKINS so the picker renders it, and re-applies the persisted
// selection if it was waiting on this (late-registered) skin.
//
// Security: token values are written into CSS, so every value is sanitized
// against a strict allowlist HERE, once, so all theme extensions inherit the
// guard safe-by-construction. Reserved core skin keys cannot be overwritten.
const _EXT_SKIN_STYLE_ID='hermesExtensionSkinStyles';
const _EXT_SKIN_KEYS=new Set();                 // keys we registered (for idempotent re-register)
const _RESERVED_SKIN_KEYS=new Set((_SKINS||[]).map(s=>(s.value||s.name).toLowerCase()));
// CSS custom-property names a skin is allowed to set. Mirrors the documented
// design-token contract; anything outside this set is dropped.
const _ALLOWED_SKIN_TOKENS=new Set([
  '--bg','--surface','--surface2','--surface-subtle','--text','--text2','--muted',
  '--accent','--accent2','--accent3','--accent-contrast','--accent-hover',
  '--accent-text','--accent-bg','--accent-bg-strong','--accent-rgb',
  '--border','--border2','--hover-bg','--code-bg','--code-text',
  '--sidebar','--sidebar-text','--user-bubble','--assistant-bubble',
  '--success','--warning','--danger','--info','--link'
]);
// Accept only safe color / simple numeric-with-unit values, OR a bare RGB triple
// (e.g. "0, 0, 0" for --accent-rgb, consumed inside rgba(...)). Rejects anything
// with url(), expression(), semicolons, braces, or other CSS-injection vectors.
const _SAFE_SKIN_VALUE_RE=/^(#(?:[0-9a-fA-F]{3,8})|rg(?:b|ba)\(\s*[0-9.,%\s/]+\)|hsl(?:a)?\(\s*[0-9.,%\s/deg]+\)|[0-9]{1,3}\s*,\s*[0-9]{1,3}\s*,\s*[0-9]{1,3}|[a-zA-Z]{3,20}|[0-9.]+(?:px|em|rem|%)?)$/;

function _sanitizeSkinScheme(scheme){
  const value=String(scheme||'').trim().toLowerCase();
  return value==='light'||value==='dark'?value:'';
}

function _sanitizeSkinTokens(tokens){
  const out={};
  if(!tokens||typeof tokens!=='object') return out;
  for(const rawKey of Object.keys(tokens)){
    const key=String(rawKey).trim();
    if(!_ALLOWED_SKIN_TOKENS.has(key)) continue;          // unknown token → drop
    const val=String(tokens[rawKey]).trim();
    if(val.length>64) continue;                            // absurd length → drop
    if(!_SAFE_SKIN_VALUE_RE.test(val)) continue;          // unsafe value → drop
    out[key]=val;
  }
  return out;
}

function _renderExtensionSkinStyles(){
  let styleEl=document.getElementById(_EXT_SKIN_STYLE_ID);
  if(!styleEl){
    styleEl=document.createElement('style');
    styleEl.id=_EXT_SKIN_STYLE_ID;
    document.head.appendChild(styleEl);
  }
  const blocks=[];
  for(const skin of _SKINS){
    if(!skin||!skin._extToken) continue;                  // only ext-registered skins
    const key=(skin.value||skin.name).toLowerCase();
    const decls=Object.keys(skin._extToken).map(k=>`${k}:${skin._extToken[k]}`).join(';');
    if(decls) blocks.push(`:root[data-skin="${key}"]{${decls}}`);
  }
  styleEl.textContent=blocks.join('\n');
}

// Public API for extensions. Returns true on success, false if rejected.
function registerHermesSkin(descriptor){
  try{
    if(!descriptor||typeof descriptor!=='object') return false;
    const name=String(descriptor.name||'').trim();
    if(!name) return false;
    const rawVal=String(descriptor.value||name).trim().toLowerCase();
    // key must be a simple slug (safe as a data-skin attr + CSS attr selector)
    const key=rawVal.replace(/[^a-z0-9_-]/g,'');
    if(!key) return false;
    if(_RESERVED_SKIN_KEYS.has(key)) return false;        // never shadow a core skin
    const tokens=_sanitizeSkinTokens(descriptor.tokens);
    if(Object.keys(tokens).length===0) return false;      // nothing valid to apply
    const scheme=_sanitizeSkinScheme(descriptor.scheme);
    // 3 swatch colors for the picker (sanitized); fall back to accent/bg/text.
    let colors=Array.isArray(descriptor.colors)?descriptor.colors.slice(0,3):[];
    colors=colors.map(c=>String(c).trim()).filter(c=>_SAFE_SKIN_VALUE_RE.test(c));
    while(colors.length<3) colors.push(tokens['--accent']||tokens['--bg']||tokens['--text']||'#888');
    const label=String(descriptor.label||name).slice(0,40);
    const entry={name:name.slice(0,40),value:key,label,colors,_extToken:tokens,_extScheme:scheme,_extension:true};

    const existingIdx=_SKINS.findIndex(s=>(s.value||s.name).toLowerCase()===key);
    if(existingIdx>=0&&_EXT_SKIN_KEYS.has(key)){
      _SKINS[existingIdx]=entry;                           // idempotent update
    }else if(existingIdx>=0){
      return false;                                        // collides w/ a non-ext skin
    }else{
      _SKINS.push(entry);
    }
    _EXT_SKIN_KEYS.add(key);
    _VALID_SKINS.add(key);
    _renderExtensionSkinStyles();
    // Refresh the picker if it's already built.
    if(document.getElementById('skinPickerGrid')){
      _buildSkinPicker((localStorage.getItem('hermes-skin')||'default').toLowerCase());
    }
    // If the user had previously selected this (now-available) skin, apply it.
    if((localStorage.getItem('hermes-skin')||'').toLowerCase()===key){
      _applySkin(key);
    }
    return true;
  }catch(_){ return false; }
}
if(typeof window!=='undefined') window.registerHermesSkin=registerHermesSkin;

function applyBotName(){
  // The saved assistant name applies to the default profile only.
  // Non-default profiles use their own profile names.
  const name=assistantDisplayName();
  if(!S.session) document.title=name;
  const sidebarH1=document.querySelector('.sidebar-header h1');
  if(sidebarH1) sidebarH1.textContent=name;
  const logo=document.querySelector('.sidebar-header .logo');
  if(logo) logo.textContent=name.charAt(0).toUpperCase();
  const topbarTitle=$('topbarTitle');
  if(topbarTitle && (!S.session)) topbarTitle.textContent=name;
  const msg=$('msg');
  if(msg) msg.placeholder='Message '+name+'\u2026';
  if(typeof _applyBusyComposerPlaceholder==='function') _applyBusyComposerPlaceholder();
}

const _COMPOSER_CONTROL_TOGGLE_DEFS=[
  {key:'hide_composer_attach',label:'Attach',labelKey:'composer_control_attach',selectors:['#btnAttach'],orderSelector:'#btnAttach',orderGroup:'left'},
  {key:'hide_composer_saved_prompts',label:'Saved prompts',labelKey:'composer_control_saved_prompts',selectors:['#btnSavedPrompts'],orderSelector:'#btnSavedPrompts',orderGroup:'left'},
  {key:'hide_composer_mic',label:'Mic',labelKey:'composer_control_mic',selectors:['#btnMic'],orderSelector:'#btnMic',orderGroup:'left'},
  {key:'hide_composer_profile',label:'Profile',labelKey:'composer_control_profile',selectors:['#profileChipWrap'],orderSelector:'#profileChipWrap',orderGroup:'left'},
  {key:'hide_composer_workspace',label:'Workspace',labelKey:'composer_control_workspace',selectors:['.composer-ws-wrap','#composerMobileWorkspaceAction'],orderSelector:'.composer-ws-wrap',orderGroup:'left'},
  {key:'hide_composer_model',label:'Model',labelKey:'composer_control_model',selectors:['.composer-model-wrap','#composerMobileModelAction'],orderSelector:'.composer-model-wrap',orderGroup:'left'},
  {key:'hide_composer_reasoning',label:'Reasoning',labelKey:'composer_control_reasoning',selectors:['#composerReasoningWrap','#composerMobileReasoningAction'],orderSelector:'#composerReasoningWrap',orderGroup:'left'},
  {key:'hide_composer_context',label:'Context',labelKey:'composer_control_context',selectors:['#ctxIndicatorWrap','#composerMobileContextAction'],orderSelector:'#ctxIndicatorWrap',orderGroup:'right'},
];
window._COMPOSER_CONTROL_TOGGLE_DEFS=_COMPOSER_CONTROL_TOGGLE_DEFS;

const _COMPOSER_SITUATIONAL_CONTROL_TOGGLE_DEFS=[
  {key:'hide_composer_voice_mode',label:'Voice mode',labelKey:'composer_control_voice_mode',selectors:['#btnVoiceMode'],orderSelector:'#btnVoiceMode',orderGroup:'left'},
  {key:'hide_composer_yolo',label:'YOLO',labelKey:'composer_control_yolo',selectors:['#yoloPill'],orderSelector:'#yoloPill',orderGroup:'left'},
  {key:'hide_composer_bg_badge',label:'Background badge',labelKey:'composer_control_bg_badge',selectors:['#bgBadge'],orderSelector:'#bgBadge',orderGroup:'right'},
  {key:'hide_composer_mobile_config',label:'Mobile config',labelKey:'composer_control_mobile_config',selectors:['#composerMobileConfigBtn'],orderSelector:'#composerMobileConfigBtn',orderGroup:'left'},
  {key:'hide_composer_quota_chip',label:'Quota chip',labelKey:'composer_control_quota_chip',selectors:['#providerQuotaChip','#composerMobileQuotaAction'],orderSelector:'#providerQuotaChip',orderGroup:'left'},
  {key:'hide_composer_toolsets',label:'Toolsets',labelKey:'composer_control_toolsets',selectors:['#composerToolsetsWrap'],orderSelector:'#composerToolsetsWrap',orderGroup:'left'},
  {key:'hide_composer_status',label:'Status',labelKey:'composer_control_status',selectors:['#composerStatus'],orderSelector:'#composerStatus',orderGroup:'right'},
];
window._COMPOSER_SITUATIONAL_CONTROL_TOGGLE_DEFS=_COMPOSER_SITUATIONAL_CONTROL_TOGGLE_DEFS;

function _allComposerControlToggleDefs(){
  return _COMPOSER_CONTROL_TOGGLE_DEFS.concat(_COMPOSER_SITUATIONAL_CONTROL_TOGGLE_DEFS);
}

function _sanitizeComposerControlOrder(order){
  if(!Array.isArray(order)) return [];
  const allowed=new Set(_allComposerControlToggleDefs().map(def=>def.key));
  const out=[];
  order.forEach(key=>{
    if(typeof key!=='string') return;
    key=key.trim();
    if(!key||!allowed.has(key)||out.includes(key)) return;
    out.push(key);
  });
  return out;
}
window._sanitizeComposerControlOrder=_sanitizeComposerControlOrder;

function _orderedComposerControlDefs(order){
  const defs=_allComposerControlToggleDefs();
  const byKey=new Map(defs.map(def=>[def.key,def]));
  const out=[];
  _sanitizeComposerControlOrder(Array.isArray(order)?order:window._composerControlOrder).forEach(key=>{
    if(byKey.has(key)) out.push(byKey.get(key));
  });
  defs.forEach(def=>{if(!out.includes(def)) out.push(def);});
  return out;
}
window._orderedComposerControlDefs=_orderedComposerControlDefs;

function _applyComposerControlOrder(order){
  window._composerControlOrder=_sanitizeComposerControlOrder(order);
  const grouped=new Map();
  _orderedComposerControlDefs(window._composerControlOrder).forEach(def=>{
    const node=document.querySelector(def.orderSelector||def.selectors&&def.selectors[0]);
    if(!node||!node.parentNode) return;
    const parent=node.parentNode;
    if(!grouped.has(parent)) grouped.set(parent,[]);
    grouped.get(parent).push(node);
  });
  grouped.forEach((nodes,parent)=>{
    if(!nodes.length) return;
    const marker=document.createComment('composer-control-order');
    parent.insertBefore(marker,nodes[0]);
    let ref=marker;
    nodes.forEach(node=>{
      parent.insertBefore(node,ref.nextSibling);
      ref=node;
    });
    marker.remove();
  });
  if(typeof _fitComposerFooter==='function') _fitComposerFooter();
}
window._applyComposerControlOrder=_applyComposerControlOrder;

function _composerControlVisibilityFromSettings(settings){
  const next={};
  for(const def of _allComposerControlToggleDefs()){
    next[def.key]=!!(settings&&settings[def.key]);
  }
  return next;
}
window._composerControlVisibilityFromSettings=_composerControlVisibilityFromSettings;

function _setComposerControlHidden(el, hidden){
  if(!el) return;
  el.classList.toggle('composer-control-hidden', !!hidden);
  if(hidden) el.setAttribute('aria-hidden','true');
  else el.removeAttribute('aria-hidden');
}

function _applyComposerFooterVisibilitySettings(){
  const hidden=window._composerControlVisibility||{};
  for(const def of _allComposerControlToggleDefs()){
    const isHidden=!!hidden[def.key];
    for(const selector of def.selectors){
      document.querySelectorAll(selector).forEach(el=>_setComposerControlHidden(el,isHidden));
    }
  }

  const hideMic=!!hidden.hide_composer_mic;
  if(hideMic&&window._micActive&&typeof window._stopMic==='function'){
    try{window._stopMic();}catch(_){ }
  }

  const hideSavedPrompts=!!hidden.hide_composer_saved_prompts;
  const savedBtn=$('btnSavedPrompts');
  const savedPopup=$('savedPromptsPopup');
  if(hideSavedPrompts&&savedPopup){
    savedPopup.style.display='none';
    if(savedBtn) savedBtn.setAttribute('aria-expanded','false');
  }

  if(hidden.hide_composer_workspace&&typeof closeWsDropdown==='function') closeWsDropdown();
  if(hidden.hide_composer_profile&&typeof closeProfileDropdown==='function') closeProfileDropdown();
  if(hidden.hide_composer_model&&typeof closeModelDropdown==='function') closeModelDropdown();
  if(hidden.hide_composer_reasoning&&typeof closeReasoningDropdown==='function') closeReasoningDropdown();
  if(hidden.hide_composer_toolsets&&typeof closeToolsetsDropdown==='function') closeToolsetsDropdown();
  if(hidden.hide_composer_mobile_config&&typeof closeMobileComposerConfig==='function') closeMobileComposerConfig();

  // Hide the divider when all left-group buttons before it are hidden
  // Stops a lone vertical separator from appearing when attach/saved-prompts/mic/voice are all hidden.
  const _divider=document.querySelector('.composer-divider');
  if(_divider){
    const _leftBtnSelectors=['#btnAttach','#btnSavedPrompts','#btnMic','#btnVoiceMode'];
    const _allLeftHidden=_leftBtnSelectors.every(sel=>{
      const el=document.querySelector(sel);
      return !el||el.classList.contains('composer-control-hidden')||el.style.display==='none';
    });
    // Use classList.toggle directly instead of _setComposerControlHidden
    // so we don't strip the intentional aria-hidden="true" on the decorative divider
    // when buttons are visible (Greptile feedback).
    _divider.classList.toggle('composer-control-hidden',_allLeftHidden);
  }
}
window._applyComposerFooterVisibilitySettings=_applyComposerFooterVisibilitySettings;

function _applyTitlebarProfileVisibility(){
  const btn=$('titlebarProfileBtn');
  if(!btn) return;
  btn.style.display=window._showTitlebarProfile?'':'none';
}
window._applyTitlebarProfileVisibility=_applyTitlebarProfileVisibility;

function _mirrorSpeechSettingsFromServer(s){
  if(!s||typeof s!=='object') return;
  const persistedSpeechKeys = new Set(
    Array.isArray(s.persisted_speech_keys) ? s.persisted_speech_keys : []
  );
  const hasServerValue=(settingKey)=>persistedSpeechKeys.has(settingKey);
  const defaults={
    tts_enabled:false,
    tts_auto_read:false,
    tts_engine:'browser',
    tts_voice:'',
    tts_rate:1,
    tts_pitch:1,
    voice_mode_button:false,
    voice_continuous:false,
    voice_silence_ms:1800,
    raw_audio_mode:false,
  };
  const cachedValue=(storageKey)=>{
    try{return localStorage.getItem(storageKey);}catch(_){return null;}
  };
  const boolValue=(value)=>value===true||value==='true';
  const resolveBool=(settingKey,storageKey)=>{
    const server=hasServerValue(settingKey)?s[settingKey]:defaults[settingKey];
    const cached=cachedValue(storageKey);
    if(!hasServerValue(settingKey)&&cached!==null){
      return boolValue(cached);
    }
    return boolValue(server);
  };
  const resolveScalar=(settingKey,storageKey)=>{
    const server=hasServerValue(settingKey)?s[settingKey]:defaults[settingKey];
    const cached=cachedValue(storageKey);
    if(!hasServerValue(settingKey)&&cached!==null){
      return cached;
    }
    return server;
  };
  const boolKeys=[
    ['tts_enabled','hermes-tts-enabled'],
    ['tts_auto_read','hermes-tts-auto-read'],
    ['voice_mode_button','hermes-voice-mode-button'],
    ['voice_continuous','hermes-voice-continuous'],
  ];
  boolKeys.forEach(([settingKey,storageKey])=>{
    if(hasServerValue(settingKey)){
      try{localStorage.setItem(storageKey,resolveBool(settingKey,storageKey)?'true':'false');}catch(_){}
    }
  });
  [
    ['tts_engine','hermes-tts-engine'],
    ['tts_voice','hermes-tts-voice'],
    ['tts_rate','hermes-tts-rate'],
    ['tts_pitch','hermes-tts-pitch'],
    ['voice_silence_ms','hermes-voice-silence-ms'],
  ].forEach(([settingKey,storageKey])=>{
    if(hasServerValue(settingKey)){
      try{localStorage.setItem(storageKey,String(resolveScalar(settingKey,storageKey)));}catch(_){}
    }
  });
  if(hasServerValue('raw_audio_mode')){
    const rawAudioMode=resolveBool('raw_audio_mode','hermes-raw-audio-mode');
    if(typeof window._applyRawAudioModePreference==='function'){
      window._applyRawAudioModePreference(rawAudioMode);
    }else{
      try{localStorage.setItem('hermes-raw-audio-mode',rawAudioMode?'true':'false');}catch(_){}
    }
  }
}
window._mirrorSpeechSettingsFromServer=_mirrorSpeechSettingsFromServer;

(async()=>{
  // Load send key preference
  let _bootSettings={};
  const prefillIntent=(typeof _composerPrefillIntentFromLocation==='function')?_composerPrefillIntentFromLocation():null;
  try{
    const s=await api('/api/settings');
    _bootSettings=s;
    if(typeof checkWebUIVersionSkew==='function'){try{checkWebUIVersionSkew(s);}catch(_){}}
    window._sendKey=s.send_key||'enter';
    // Persist default workspace so the blank new-chat page can show it
    // and workspace actions (New file/folder) work before the first session (#804).
    if(s.default_workspace) S._profileDefaultWorkspace=s.default_workspace;
    window._showTokenUsage=!!s.show_token_usage;
    window._showQuotaChip=s.show_quota_chip===true;
    window._showConversationOutline=s.show_conversation_outline===true;
    document.documentElement.dataset.conversationOutline=window._showConversationOutline?'enabled':'disabled';
    if(typeof applyConversationOutlinePreference==='function') applyConversationOutlinePreference();
    window._hideEmptyStateSuggestions=s.hide_empty_state_suggestions===true;
    applyEmptyStateSuggestionPref();
    // #4343: transcript virtualization is EXPERIMENTAL/opt-IN (default OFF).
    // It caused scroll-up flicker on long sessions, so it's off for everyone
    // unless explicitly opted in; long transcripts render in full by default.
    window._virtualizeTranscript=s.virtualize_transcript===true;
    window._showTps=!!s.show_tps;
    window._fadeTextEffect=!!s.fade_text_effect;
    window._showCliSessions=s.show_cli_sessions!==false;
    window._showPreviousMessagingSessions=!!s.show_previous_messaging_sessions;
    window._soundEnabled=!!s.sound_enabled;
    window._notificationsEnabled=!!s.notifications_enabled;
    window._whatsNewSummaryEnabled=!!s.whats_new_summary_enabled;
    window._showThinking=s.show_thinking!==false;
    window._simplifiedToolCalling=true;
    window._chatActivityDisplayMode=s.chat_activity_display_mode==='transparent_stream'||s.chat_activity_display_mode==='hide_all_activity'
      ? s.chat_activity_display_mode
      : 'compact_worklog';
    window._transparentStream=window._chatActivityDisplayMode==='transparent_stream';
    window._terminalAutoExpandOnOutput=!!s.terminal_auto_expand_on_output;
    window._worklogDetailsExpandedByDefault=!!(
      Object.prototype.hasOwnProperty.call(s,'worklog_details_expanded_default')
        ? s.worklog_details_expanded_default
        : s.activity_feed_expanded_default
    );
    window._workspaceTodosTab=!!s.workspace_todos_tab;
    if(typeof _applyWorkspaceTodosTabVisibility==='function') _applyWorkspaceTodosTabVisibility();
    window._sidebarDensity=(s.sidebar_density==='detailed'?'detailed':'compact');
    window._pinnedSessionsLimit=parseInt(s.pinned_sessions_limit||3,10)||3;
    window._inflightStateLimits={
      maxSessions:parseInt(s.inflight_state_max_sessions||8,10)||8,
      messages:parseInt(s.inflight_state_max_messages||24,10)||24,
      toolCalls:parseInt(s.inflight_state_max_tool_calls||48,10)||48,
      stringChars:parseInt(s.inflight_state_max_string_chars||60000,10)||60000,
      jsonChars:parseInt(s.inflight_state_max_json_chars||1500000,10)||1500000,
    };
    // #5162 rename + steer default, layered on the #5170 localStorage mirror:
    // resolve the mode (new key, legacy busy_input_mode fallback, else 'steer'
    // via _normalizeDefaultMessageMode) and persist it so the very first send
    // after a reload honors the saved choice.
    window._defaultMessageMode=_persistDefaultMessageMode(s.default_message_mode||s.busy_input_mode);
    window._showBusyPlaceholderHint=!!s.show_busy_placeholder_hint;
    window._newChatOnWorkspaceSwitch=!!s.new_chat_on_workspace_switch;  // #5473 opt-in
    window._sessionEndlessScrollEnabled=!!s.session_endless_scroll;
    window._autoScrollFollow=s.auto_scroll_follow!==false;
    window._largeTextPasteAsAttachment=s.large_text_paste_as_attachment!==false;
    window._projectQuickCreate=!!s.project_quick_create_buttons;
    window._composerControlVisibility=_composerControlVisibilityFromSettings(s);
    window._composerControlOrder=_sanitizeComposerControlOrder(s.composer_control_order);
    _applyComposerControlOrder(window._composerControlOrder);
    window._showTitlebarProfile=!!s.show_titlebar_profile;
    _applyTitlebarProfileVisibility();
    window._botName=s.bot_name||'Hermes';
    if(s.default_model_provider) window._activeProvider=s.default_model_provider;
    if(s.default_model){
      window._defaultModel=s.default_model;
      const sel=$('modelSelect');
      if(sel&&typeof _applyModelToDropdown==='function'){
        // Fresh page boot must prefer the profile/server default over stale
        // browser-persisted model state. A restored session can still apply its
        // own persisted model later through loadSession(). Preserve the browser
        // keys for legacy/no-default fallback paths instead of deleting them.
        const existingDefaultOpt=Array.from(sel.options).find(o=>o.value===s.default_model);
        if(existingDefaultOpt&&window._activeProvider&&!existingDefaultOpt.dataset.provider){
          existingDefaultOpt.dataset.provider=window._activeProvider;
        }
        if(!existingDefaultOpt){
          const opt=document.createElement('option');
          opt.value=s.default_model;
          opt.textContent=typeof getModelLabel==='function'?getModelLabel(s.default_model):s.default_model;
          opt.dataset.custom='1';
          opt.dataset.provider=window._activeProvider||'';
          sel.querySelectorAll('option[data-custom]').forEach(o=>o.remove());
          sel.appendChild(opt);
        }
        _applyModelToDropdown(s.default_model,sel,window._activeProvider||null);
      }
    }
    window._sessionJumpButtonsEnabled=!!s.session_jump_buttons;
    window._renderUserMarkdown=!!s.render_user_markdown;
    // JSON/YAML structured code-block default view (#484): auto | on | off,
    // plus the 'auto'-mode line threshold (sanitized int 1..1000, fallback 10).
    window._structuredCodeDefaultView=['on','off','auto'].includes(s.structured_code_default_view)?s.structured_code_default_view:'auto';
    const _sctLines=parseInt(s.structured_code_auto_tree_lines,10);
    window._structuredCodeAutoTreeLines=(Number.isFinite(_sctLines)&&_sctLines>=1&&_sctLines<=1000)?_sctLines:10;
    // Reconcile appearance: prefer localStorage (what the user last saw) over
    // the server.  If they diverge (e.g. a previous autosave POST failed),
    // push the localStorage values back to the server so settings.json stays
    // in sync without ever clobbering the user's chosen theme/skin.
    //
    // Caveat: the pre-paint inline script in index.html normalises empty
    // localStorage into 'dark'/'default' BEFORE this code runs, so a truly
    // empty (new-browser) state is indistinguishable from a user who chose
    // the defaults.  To avoid blocking server→client sync on first visit we
    // only let localStorage override the server when it carries an explicit
    // user-selectable theme value or a NON-DEFAULT skin.  That keeps the
    // server in charge for empty first-visit state while preserving explicit
    // light/dark/system choices after a failed autosave.
    const srvAppearance=_normalizeAppearance(s.theme,s.skin);
    const lsTheme=(localStorage.getItem('hermes-theme')||'').trim().toLowerCase();
    const lsSkin=(localStorage.getItem('hermes-skin')||'').trim().toLowerCase();
    const lsAppearance=_normalizeAppearance(lsTheme||null,lsSkin||null);
    // An unknown non-default persisted skin is most likely an extension-provided
    // skin (registerHermesSkin) whose extension script hasn't registered it yet
    // at this point in boot. Preserve it verbatim instead of normalizing it away
    // to 'default' — the extension's registerHermesSkin() will inject the CSS and
    // re-apply it once it loads. Without this, the boot sync would clobber the
    // saved choice before the extension runs.
    const lsSkinIsPendingExt=!!lsSkin&&lsSkin!=='default'&&!_VALID_SKINS.has(lsSkin)&&!_LEGACY_THEME_MAP[lsSkin];
    const lsHasExplicitSkin=lsSkin&&lsSkin!=='default';
    const lsHasExplicitTheme=lsTheme&&['system','light','dark'].includes(lsTheme);
    const theme=lsHasExplicitTheme?lsAppearance.theme:srvAppearance.theme;
    const skin=lsHasExplicitSkin?(lsSkinIsPendingExt?lsSkin:lsAppearance.skin):srvAppearance.skin;
    localStorage.setItem('hermes-theme',theme);
    _applyTheme(theme);
    localStorage.setItem('hermes-skin',skin);
    _applySkin(skin);
    // Reconcile: if localStorage and server disagree, push localStorage
    // values to the server so the next refresh won't revert. Skip the push for a
    // still-pending extension skin (don't persist it server-side until it's a
    // confirmed-registered skin — avoids writing a skin the server can't validate).
    if((lsHasExplicitTheme||lsHasExplicitSkin)&&!lsSkinIsPendingExt&&(theme!==srvAppearance.theme||skin!==srvAppearance.skin)){
      try{
        api('/api/settings',{method:'POST',body:JSON.stringify({theme,skin})});
      }catch(_){}
    }
    const fontSize=(s.font_size||localStorage.getItem('hermes-font-size')||'default');
    localStorage.setItem('hermes-font-size',fontSize);
    _applyFontSize(fontSize);
    if(typeof setLocale==='function'){
      const _lang=typeof resolvePreferredLocale==='function'
        ? resolvePreferredLocale(s.language, localStorage.getItem('hermes-lang'))
        : (s.language || localStorage.getItem('hermes-lang') || 'en');
      setLocale(_lang);
      if(typeof applyLocaleToDOM==='function')applyLocaleToDOM();
    }
    _mirrorSpeechSettingsFromServer(s);
    // Apply voice-mode visibility BEFORE computing the divider so the
    // .composer-divider (#5451) sees #btnVoiceMode final display even
    // when a server/localStorage sync path flipped the pref between
    // module init and settings-load completion (round-2 SILENT race).
    // Note: must use window._applyVoiceModePref — the bare name is
    // closure-local to the voice-mode IIFE and not visible here.
    if(typeof window._applyVoiceModePref==='function') window._applyVoiceModePref();
    _applyComposerFooterVisibilitySettings();
    // TTS: apply enabled state on boot so buttons show/hide correctly (#499)
    if(typeof _applyTtsEnabled==='function') _applyTtsEnabled(localStorage.getItem('hermes-tts-enabled')==='true');
  }catch(e){
    window._sendKey='enter';
    window._showTokenUsage=false;
    window._showQuotaChip=false;
    window._showConversationOutline=false;
    document.documentElement.dataset.conversationOutline='disabled';
    if(typeof applyConversationOutlinePreference==='function') applyConversationOutlinePreference();
    window._hideEmptyStateSuggestions=false;
    applyEmptyStateSuggestionPref();
    window._virtualizeTranscript=false;  // settings-load failed: default-OFF (experimental/opt-in) (#4343)
    window._showTps=false;
    window._fadeTextEffect=false;
    window._showCliSessions=true;  // settings-load failed: mirror the True config default (#3988)
    window._soundEnabled=false;
    window._notificationsEnabled=false;
    window._whatsNewSummaryEnabled=false;
    window._showThinking=true;
    window._simplifiedToolCalling=true;
    window._chatActivityDisplayMode='compact_worklog';
    window._transparentStream=false;
    window._terminalAutoExpandOnOutput=false;
    window._workspaceTodosTab=false;
    if(typeof _applyWorkspaceTodosTabVisibility==='function') _applyWorkspaceTodosTabVisibility();
    window._sessionJumpButtonsEnabled=false;
    window._structuredCodeDefaultView='auto';
    window._structuredCodeAutoTreeLines=10;
    window._sidebarDensity='compact';
    window._pinnedSessionsLimit=3;
    // Settings load failed: keep the persisted default-message-mode preference
    // (the eager default already read it from the localStorage mirror) instead
    // of clobbering it, so a saved 'steer'/'interrupt'/'queue' still applies
    // when the server is unreachable (#5167). The placeholder-hint has no
    // persisted mirror, so it defaults off on failure.
    window._defaultMessageMode=_readPersistedDefaultMessageMode();
    window._showBusyPlaceholderHint=false;
    window._sessionEndlessScrollEnabled=false;
    window._autoScrollFollow=true;
    window._composerControlVisibility=_composerControlVisibilityFromSettings(null);
    window._composerControlOrder=[];
    _applyComposerControlOrder(window._composerControlOrder);
    window._botName='Hermes';
    _bootSettings={check_for_updates:false};
    if(typeof setLocale==='function'){
      const _lang=typeof resolvePreferredLocale==='function'
        ? resolvePreferredLocale(null, localStorage.getItem('hermes-lang'))
        : (localStorage.getItem('hermes-lang') || 'en');
      setLocale(_lang);
      if(typeof applyLocaleToDOM==='function')applyLocaleToDOM();
    }
    // Apply voice-mode visibility BEFORE computing the divider so the
    // .composer-divider (#5451) sees #btnVoiceMode final display even when
    // a server/localStorage sync path flipped the pref between module init
    // and settings-load completion (round-2 SILENT race fix; safe no-op on
    // the failure-fallback path because _applyVoiceModePref is idempotent).
    // Note: must use window._applyVoiceModePref — the bare name is
    // closure-local to the voice-mode IIFE and not visible here.
    if(typeof window._applyVoiceModePref==='function') window._applyVoiceModePref();
    _applyComposerFooterVisibilitySettings();
    if(typeof _applyTtsEnabled==='function') _applyTtsEnabled(localStorage.getItem('hermes-tts-enabled')==='true');
  }
  // Non-blocking update check (fire-and-forget, once per tab session)
  // ?test_updates=1 in URL forces banner display for testing (bypasses sessionStorage guards)
  const _testUpdates=new URLSearchParams(location.search).get('test_updates')==='1';
  if(_testUpdates||(_bootSettings.check_for_updates!==false&&!sessionStorage.getItem('hermes-update-checked')&&!sessionStorage.getItem('hermes-update-dismissed'))){
    const _checkUrl='api/updates/check'+(_testUpdates?'?simulate=1':'');
    api(_checkUrl,{method:_testUpdates?'GET':'POST',body:_testUpdates?undefined:JSON.stringify({force:false})}).then(d=>{if(!_testUpdates)sessionStorage.setItem('hermes-update-checked','1');if((d.webui&&d.webui.behind>0)||(d.agent&&d.agent.behind>0))_showUpdateBanner(d);}).catch(()=>{});
  }
  const _bootActiveProfileUnauthRedirectBudget=(()=>{
    const markerKey='hermes-webui-active-profile-bootstrap-401';
    let consumed=false;
    const readAttempted=(storage=sessionStorage)=>{
      try{
        const attempted=storage&&storage.getItem?storage.getItem(markerKey)==='1':false;
        if(attempted) consumed=true;
        return attempted;
      }catch(_){
        return false;
      }
    };
    const markAttempted=(storage=sessionStorage)=>{
      consumed=true;
      try{
        if(storage&&storage.setItem) storage.setItem(markerKey,'1');
      }catch(_){}
    };
    const clearAttempted=(storage=sessionStorage)=>{
      try{
        if(storage&&storage.removeItem) storage.removeItem(markerKey);
      }catch(_){}
    };
    const spendOnFallback=(storage=sessionStorage)=>{
      consumed=true;
      clearAttempted(storage);
    };
    const spendOnRedirect=(storage=sessionStorage)=>{
      if(consumed) return false;
      markAttempted(storage);
      return true;
    };
    const redirectToLogin=(nextUrl)=>{
      // #5578: never nest the login URL into its own next= — if already on a
      // login-shaped page, reload 'login' bare (the page keeps its inner next).
      const _p=(window.location.pathname||'').replace(/\/+$/,'');
      if(/(?:^|\/)login$/.test(_p)){window.location.href='login';return;}
      window.location.href='login?next='+encodeURIComponent(nextUrl);
    };
    return {
      readAttempted,
      clearAttempted,
      spendOnFallback,
      spendOnRedirect,
      redirectToLogin,
      isConsumed:()=>consumed,
    };
  })();
  async function _resolveActiveProfileBootstrapState({
    loadActiveProfile = () => api('/api/profile/active', {redirect401: false}),
    getNextUrl = () => window.location.pathname + window.location.search,
    redirectToLogin = (nextUrl) => {
      _bootActiveProfileUnauthRedirectBudget.redirectToLogin(nextUrl);
    },
    markerStorage = sessionStorage,
  } = {}) {
    const alreadyAttempted = _bootActiveProfileUnauthRedirectBudget.readAttempted(markerStorage);
    try {
      const p = await loadActiveProfile();
      if (p && typeof p === 'object' && typeof p.name === 'string') {
        _bootActiveProfileUnauthRedirectBudget.clearAttempted(markerStorage);
        if (p.default_workspace) S._profileDefaultWorkspace = p.default_workspace;
        return {status: 'resolved', profile: p.name || 'default', isDefault: !!p.is_default};
      }
      if (p === undefined && !alreadyAttempted) {
        if (_bootActiveProfileUnauthRedirectBudget.spendOnRedirect(markerStorage)) {
          redirectToLogin(getNextUrl());
        }
        return {status: 'recovery-redirect'};
      }
      if (p === undefined) _bootActiveProfileUnauthRedirectBudget.spendOnFallback(markerStorage);
      else _bootActiveProfileUnauthRedirectBudget.clearAttempted(markerStorage);
      return {status: 'fallback', profile: 'default', isDefault: true};
    } catch (e) {
      _bootActiveProfileUnauthRedirectBudget.clearAttempted(markerStorage);
      if (!alreadyAttempted && e && e.status === 401) {
        if (_bootActiveProfileUnauthRedirectBudget.spendOnRedirect(markerStorage)) {
          redirectToLogin(getNextUrl());
        }
        return {status: 'recovery-redirect'};
      }
      if (e && e.status === 401) _bootActiveProfileUnauthRedirectBudget.spendOnFallback(markerStorage);
      return {status: 'fallback', profile: 'default', isDefault: true};
    }
  }

  // Fetch active profile
  const activeProfileState = await _resolveActiveProfileBootstrapState();
  if (activeProfileState.status === 'recovery-redirect') return;
  S.activeProfile = activeProfileState.profile;
  S.activeProfileIsDefault = activeProfileState.isDefault;
  applyBotName();
  // Update profile chip label immediately
  const profileLabel=$('profileChipLabel');
  if(profileLabel) profileLabel.textContent=S.activeProfile||'default';
  const titleLabel=$('titlebarProfileLabel');
  if(titleLabel) titleLabel.textContent=S.activeProfile||'default';
  const profileIntent=(typeof _profileQueryIntentFromLocation==='function')?_profileQueryIntentFromLocation():null;
  const _savedLocalBeforeProfileSwitch=localStorage.getItem('hermes-webui-session');
  const _profileSwitchProfileBefore=S.activeProfile||'default';
  const _profileSwitchIsDefaultBefore=!!S.activeProfileIsDefault;
  let _profileSwitchCompleted=false;
  let _profileSwitchChangedProfile=false;
  if(profileIntent&&profileIntent.hasParam){
    try{
      if(profileIntent.valid){
        if(typeof switchToProfile==='function'){
          _profileSwitchCompleted=await switchToProfile(profileIntent.name)===true;
          if(_profileSwitchCompleted){
            _profileSwitchChangedProfile=(S.activeProfile||'default')!==_profileSwitchProfileBefore||!!S.activeProfileIsDefault!==_profileSwitchIsDefaultBefore;
            if(typeof _consumeProfileQueryParamFromLocation==='function') _consumeProfileQueryParamFromLocation();
          }
        }
      }else{
        console.warn('[boot] ignored invalid profile query', profileIntent.name);
        if(typeof _consumeProfileQueryParamFromLocation==='function') _consumeProfileQueryParamFromLocation();
      }
    }catch(e){
      console.warn('[boot] profile query switch failed', e);
    }
  }
  if(typeof fetchReasoningChip==='function'&&(!_profileSwitchCompleted||!_profileSwitchChangedProfile)) fetchReasoningChip();
  // Fetch available models without blocking session restore. The static HTML
  // options are enough for first paint; the dynamic provider list can settle
  // after the saved session is visible.
  const _redirectBootModelDropdownIfUnauth=(res)=>{
    if(!res||res.status!==401) return false;
    window._modelDropdownReady=null;
    if(_bootActiveProfileUnauthRedirectBudget.isConsumed()) return true;
    if(_bootActiveProfileUnauthRedirectBudget.spendOnRedirect(sessionStorage)){
      _bootActiveProfileUnauthRedirectBudget.redirectToLogin(window.location.pathname+window.location.search);
    }
    return true;
  };
  const _hydrateModelDropdown=({redirectIfUnauth=null}={})=>populateModelDropdown({
    preferProfileDefaultOnFreshBoot:true,
    ...(redirectIfUnauth?{redirectIfUnauth}:{}),
  }).then(()=>{
    const sessionModelState=S.session&&S.session.model
      ? {model:S.session.model,model_provider:S.session.model_provider||null}
      : null;
    const savedState=(typeof _readPersistedModelState==='function')
      ? _readPersistedModelState()
      : (localStorage.getItem('hermes-webui-model')?{model:localStorage.getItem('hermes-webui-model'),model_provider:null}:null);
    // Active sessions are authoritative. On fresh boot without a restored
    // session, keep the profile/server default ahead of stale browser model
    // state when a default exists.
    const stateToApply=sessionModelState||(!window._defaultModel?savedState:null);
    const savedModel=stateToApply&&stateToApply.model;
    if(savedModel && $('modelSelect')){
      const applied=(typeof _applyModelToDropdown==='function')
        ? (sessionModelState
          ? _applyModelToDropdown(sessionModelState.model,$('modelSelect'),sessionModelState.model_provider||null)
          : _applyModelToDropdown(savedState.model,$('modelSelect'),savedState.model_provider||null))
        : null;
      if(!applied) $('modelSelect').value=stateToApply.model;
      // If the value didn't take (model not in list), clear the bad pref only
      // for persisted browser preferences. Active sessions remain authoritative.
      if(!applied&&sessionModelState&&typeof _ensureModelOptionInDropdown==='function'){
        _ensureModelOptionInDropdown(sessionModelState.model,$('modelSelect'),sessionModelState.model_provider||null);
      }
      else if(!applied&&!sessionModelState&&$('modelSelect').value!==stateToApply.model){
        if(typeof _clearPersistedModelState==='function') _clearPersistedModelState();
        else {
          localStorage.removeItem('hermes-webui-model');
          localStorage.removeItem('hermes-webui-model-state');
        }
      }
      else if(typeof syncModelChip==='function') syncModelChip();
    }
    if(S.session) syncTopbar();
    else if(typeof syncReasoningChip==='function') syncReasoningChip();
  }).catch(e=>{
    window._modelDropdownReady=null;
    throw e;
  });
  const _startModelDropdown=()=>{
    const ready=window._modelDropdownReady;
    if(ready&&typeof ready.then==='function') return ready;
    const next=_hydrateModelDropdown();
    window._modelDropdownReady=next;
    return next;
  };
  const _startBootModelDropdown=()=>{
    const ready=window._modelDropdownReady;
    if(ready&&typeof ready.then==='function') return ready;
    const next=_hydrateModelDropdown({redirectIfUnauth:_redirectBootModelDropdownIfUnauth});
    window._modelDropdownReady=next;
    return next;
  };
  window._modelDropdownReady=null;
  window._startBootModelDropdown=_startBootModelDropdown;
  window._ensureModelDropdownReady=_startModelDropdown;
  setTimeout(()=>{
    try{Promise.resolve(_startBootModelDropdown()).catch(()=>{});}catch(_){}
  },0);
  // Start independent boot fetches without holding the conversation list behind
  // them. The sidebar can render from /api/sessions while workspace/onboarding
  // metadata settles in parallel.
  const _workspaceListReady=loadWorkspaceList();
  const _onboardingReady=_bootSettings.onboarding_completed?Promise.resolve(false):loadOnboardingWizard();
  // Render the session list before restoring the saved conversation so a stale
  // saved-session/client-side boot error cannot leave the sidebar empty forever.
  await renderSessionList();
  await _workspaceListReady;
  await _onboardingReady;
  _initResizePanels();
  // Workspace panel restore happens AFTER loadSession so we know if
  // the session has a workspace — prevents the snap-open-then-closed flash (#576).
  // Fix #822: clear any browser-restored value before first render. This
  // covers fresh page loads and reloads. The bfcache restore case is handled
  // separately below by a `pageshow` listener — the async IIFE here does NOT
  // re-run when the browser restores the page from bfcache.
  const _srch = document.getElementById('sessionSearch'); if (_srch) _srch.value = '';
  if (typeof syncSessionSearchClear === 'function') syncSessionSearchClear();
  if(typeof refreshProviderQuotaIndicator==='function') refreshProviderQuotaIndicator();
  const urlSession=(typeof _sessionIdFromLocation==='function')?_sessionIdFromLocation():null;
  const pwaLaunchAction=(window.HermesPWA&&typeof window.HermesPWA.launchAction==='function')
    ? window.HermesPWA.launchAction()
    : null;
  if(pwaLaunchAction==='new-chat'){
    try{
      await newSession(true);
      // New-chat PWA launches need the empty conversation visible immediately.
      // Boot model hydration can take several seconds when /api/models falls
      // into a cold provider-catalog rebuild; it is already safe to finish in
      // the background because newSession() posted the configured default and
      // rendered the session's authoritative model/provider.
      if(S.session){
        try{Promise.resolve(_startBootModelDropdown()).catch(()=>{});}catch(_){}
      }
      S._bootReady=true;
      syncTopbar();syncWorkspacePanelState();await renderSessionList();await _finalizeComposerPrefillOnBoot(prefillIntent);if(typeof startGatewaySSE==='function')startGatewaySSE();return;
    }catch(e){console.warn('[pwa] new-chat launch action failed', e);}
  }
  const _profileQueryBlocksSavedLocal=_profileQueryBlocksSavedLocalRestore(profileIntent, urlSession);
  if(_profileQueryBlocksSavedLocal&&_profileSwitchCompleted&&_profileSwitchChangedProfile){
    try{
      if(localStorage.getItem('hermes-webui-session')===_savedLocalBeforeProfileSwitch) localStorage.removeItem('hermes-webui-session');
    }catch(_){}
  }
  const savedLocal=localStorage.getItem('hermes-webui-session');
  const saved=urlSession||savedLocal;
  if(saved){
    try{
      const savedSidebarOnlyState=(!urlSession&&savedLocal)
        ? await _savedSessionSidebarOnlyState(savedLocal)
        : null;
      if(savedSidebarOnlyState&&savedSidebarOnlyState.sidebarOnly){
        if(savedSidebarOnlyState.archived){
          try{localStorage.removeItem('hermes-webui-session');}catch(_){}
        }
        S.session=null; S.messages=[]; S.activeStreamId=null; S.busy=false;
        S._bootReady=true;
        syncTopbar();syncWorkspacePanelState();
        $('emptyState').style.display='';
        await renderSessionList();await _finalizeComposerPrefillOnBoot(prefillIntent);if(typeof startGatewaySSE==='function')startGatewaySSE();
        return;
      }
      if(_rootPrefillNeedsFreshComposer(urlSession, savedLocal, prefillIntent)){
        S.session=null; S.messages=[]; S.activeStreamId=null; S.busy=false;
        S._bootReady=true;
        const _ephPanelPref=localStorage.getItem('hermes-webui-workspace-panel-pref')==='open'
          || localStorage.getItem('hermes-webui-workspace-panel')==='open';
        if(_ephPanelPref&&!_isCompactWorkspaceViewport()) _workspacePanelMode='browse';
        await _maybeBindFreshDefaultWorkspaceSession(prefillIntent);
        syncTopbar();syncWorkspacePanelState();
        $('emptyState').style.display='';
        await renderSessionList();await _finalizeComposerPrefillOnBoot(prefillIntent);if(typeof startGatewaySSE==='function')startGatewaySSE();
        return;
      }
      await loadSession(saved, {preserveActiveInput:true});
      // Hard refresh starts from the static HTML model list. Hydrate the live
      // catalog after the saved session is known, then re-apply that session's
      // model before S._bootReady lets syncModelChip reveal the composer label.
      // Otherwise the chip can display the static default (e.g. GPT-5.4 Mini)
      // even though S.session already points at the Codex/current model.
      if(S.session) await _startBootModelDropdown();
      // If the restored session has no messages it is an ephemeral scratch pad —
      // treat the page as a fresh start rather than resuming a blank conversation.
      // loadSession() already ran, so loadDir() has populated the workspace file tree.
      // Do NOT remove the session ID from localStorage — keeping it means every
      // subsequent refresh will also run loadSession() → loadDir() → files stay visible.
      // Removing it here caused the file tree to go blank on the second refresh
      // because the "no saved session" path never calls loadDir (#workspace-files).
      const _restoredInFlight = S.session && (
        S.session.active_stream_id ||
        S.session.pending_user_message
      );
      const _restoredDraft = (S.session && S.session.composer_draft) || {};
      const _restoredDraftText = String(_restoredDraft.text||'').trim();
      const _restoredDraftFiles = Array.isArray(_restoredDraft.files)
        ? _restoredDraft.files.filter(Boolean)
        : [];
      const _restoredHasDraft = !!(_restoredDraftText || _restoredDraftFiles.length);
      if(S.session && (S.session.message_count||0) === 0 && !_restoredInFlight && !_restoredHasDraft){
        S.session=null; S.messages=[];
        S._bootReady=true;
        // Restore panel pref before syncing so the workspace panel stays visible
        // even though there is no active session (#workspace-persist).
        const _ephPanelPref=localStorage.getItem('hermes-webui-workspace-panel-pref')==='open'
          || localStorage.getItem('hermes-webui-workspace-panel')==='open';
        if(_ephPanelPref&&!_isCompactWorkspaceViewport()) _workspacePanelMode='browse';
        await _maybeBindFreshDefaultWorkspaceSession(prefillIntent);
        syncTopbar();syncWorkspacePanelState();
        $('emptyState').style.display='';
        await renderSessionList();await _finalizeComposerPrefillOnBoot(prefillIntent);if(typeof startGatewaySSE==='function')startGatewaySSE();
        return;
      }
      // Restore the panel from localStorage when the session has a workspace.
      // Preference key takes priority over runtime state so that closing
      // the panel via toolbar X doesn't suppress the "keep open" setting.
      const panelPref=localStorage.getItem('hermes-webui-workspace-panel-pref')==='open'
        || localStorage.getItem('hermes-webui-workspace-panel')==='open';
      if(S.session&&S.session.workspace&&panelPref&&!_isCompactWorkspaceViewport()){
        _workspacePanelMode='browse';
      }
      S._bootReady=true;
      syncTopbar();syncWorkspacePanelState();await renderSessionList();if(typeof startGatewaySSE==='function')startGatewaySSE();await checkInflightOnBoot(saved);await _finalizeComposerPrefillOnBoot(prefillIntent);return;}
    catch(e){localStorage.removeItem('hermes-webui-session');}
  }
  // no saved session - show empty state, wait for user to hit +
  S._bootReady=true;
  syncTopbar();
  // Restore panel pref so the workspace panel stays visible on a fresh load if the
  // user had it open during their last session (#workspace-persist).
  const _freshPanelPref=localStorage.getItem('hermes-webui-workspace-panel-pref')==='open'
    || localStorage.getItem('hermes-webui-workspace-panel')==='open';
  if(_freshPanelPref&&!_isCompactWorkspaceViewport()) _workspacePanelMode='browse';
  await _maybeBindFreshDefaultWorkspaceSession(prefillIntent);
  syncWorkspacePanelState();
  $('emptyState').style.display='';
  await renderSessionList();await _finalizeComposerPrefillOnBoot(prefillIntent);
  // Start real-time gateway session sync if setting is enabled
  if(typeof startGatewaySSE==='function') startGatewaySSE();
})().catch(e=>{
  console.error('[hermes] boot failed', e);
  try{S._bootReady=true;}catch(_){}
  try{syncTopbar();}catch(_){}
  try{syncWorkspacePanelState();}catch(_){}
  try{$('emptyState').style.display='';}catch(_){}
  try{if(typeof renderSessionList==='function') void renderSessionList();}catch(_){}
});

// Fix #822 (bfcache path): when the browser restores the page from the
// back-forward cache, the async boot IIFE above does NOT re-run, but the
// DOM — including any stale value in #sessionSearch — IS restored.  A
// prior search string would silently hide all sessions via the filter in
// renderSessionListFromCache().  Clear the field and re-run the full layout
// sync whenever the page is restored from cache (`event.persisted === true`).
// Fix #1045: also re-run topbar/workspace/panel state so the rail and layout
// chrome aren't left in the stale bfcache snapshot.
window.addEventListener('pageshow', async (event) => {
  if (!event.persisted) return;  // fresh loads are handled by the IIFE above
  _syncKeyboardBottomInset();
  const _srch = document.getElementById('sessionSearch');
  if (_srch) _srch.value = '';
  if (typeof syncSessionSearchClear === 'function') syncSessionSearchClear();
  // Close any dropdowns/popovers that were open when the user navigated away.
  // bfcache freezes DOM state, so a dropdown left open remains open on restore.
  if (typeof closeModelDropdown === 'function') try { closeModelDropdown(); } catch (_) {}
  if (typeof closeReasoningDropdown === 'function') try { closeReasoningDropdown(); } catch (_) {}
  if (typeof closeWsDropdown === 'function') try { closeWsDropdown(); } catch (_) {}
  if (typeof closeProfileDropdown === 'function') try { closeProfileDropdown(); } catch (_) {}
  // BFCache restores the frozen DOM without rerunning boot. Refresh the active
  // session through the normal load path so in-flight sessions with
  // active_stream_id / pending_user_message can reattach like a reload restore.
  if (S.session && S.session.session_id && typeof loadSession === 'function') {
    try {
      await loadSession(S.session.session_id);
      if (S.session && S.session.session_id && typeof checkInflightOnBoot === 'function') {
        try { await checkInflightOnBoot(S.session.session_id); } catch (_) {}
      }
    } catch (_) {}
  }
  // Re-synchronise layout chrome that the boot IIFE sets up but bfcache
  // doesn't re-run. Each call is guarded so missing helpers degrade silently.
  if (typeof syncTopbar === 'function') try { syncTopbar(); } catch (_) {}
  if (typeof syncWorkspacePanelState === 'function') try { syncWorkspacePanelState(); } catch (_) {}
  if (typeof renderSessionListFromCache === 'function') {
    try { renderSessionListFromCache(); } catch (_) {}
  }
  // Restart the gateway SSE watcher — the persisted connection is dead after bfcache
  if (typeof startGatewaySSE === 'function') try { startGatewaySSE(); } catch (_) {}
  // Re-sync sidebar collapse state from localStorage. bfcache restored the
  // frozen DOM but another tab may have toggled the sidebar in the meantime.
  if (typeof _isSidebarCollapsed === 'function' && typeof toggleSidebar === 'function') {
    try {
      const _want = localStorage.getItem('hermes-webui-sidebar-collapsed') === '1';
      const _have = _isSidebarCollapsed();
      if (_want !== _have) toggleSidebar(_want);
      if (typeof _syncSidebarAria === 'function') _syncSidebarAria();
    } catch (_) {}
  }
});

async function shutdownServer() {
  const ok = await showConfirmDialog({
    title: (typeof t === 'function' ? t('settings_shutdown_confirm_title') : 'Stop Hermes WebUI'),
    message: (typeof t === 'function' ? t('settings_shutdown_confirm_message') : 'Stop the Hermes WebUI server?'),
    confirmLabel: (typeof t === 'function' ? t('settings_shutdown_confirm_btn') : 'Stop'),
    danger: true,
  });
  if (!ok) return;
  localStorage.setItem('hermes-webui-server-stopped', '1');
  try { var bc = new BroadcastChannel('hermes-webui-shutdown'); bc.postMessage('stop'); bc.close(); } catch(_) {}
  _showServerStopped();
  try { await api('/api/shutdown', { method: 'POST' }); } catch (_) {}
}

function _showServerStopped() {
  var stoppedMsg = (typeof t === 'function' ? t('settings_shutdown_stopped_message') : 'Server stopped. You can close this tab.');
  document.body.innerHTML = '<div style="display:flex;align-items:center;justify-content:center;height:100vh;color:var(--muted);font-family:system-ui,ui-sans-serif;font-size:14px"><p>' + stoppedMsg + '</p></div>';
}
