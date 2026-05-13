async function cancelStream(){
  const streamId = S.activeStreamId;
  if(!streamId) return;
  try{
    await fetch(new URL(`api/chat/cancel?stream_id=${encodeURIComponent(streamId)}`,document.baseURI||location.href).href,{credentials:'include'});
  }catch(e){/* cancel request failed - cleanup below still runs */}
  // Clear status unconditionally after the cancel request completes.
  // The SSE cancel event may also fire, but if the connection is already
  // closed it won't arrive — so we handle cleanup here as the guaranteed path.
  S.activeStreamId=null;
  setBusy(false);
  if(typeof setComposerStatus==='function') setComposerStatus('');
  else setStatus('');
}

async function cancelSessionStream(session){
  const streamId = session&&session.active_stream_id;
  const sid = session&&session.session_id;
  if(!streamId||!sid) return;
  try{
    await fetch(new URL(`api/chat/cancel?stream_id=${encodeURIComponent(streamId)}`,document.baseURI||location.href).href,{credentials:'include'});
  }catch(e){/* cancel request failed - cleanup below still runs */}
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
  if(!sid) return false;
  try{
    const data = await api(`/api/session?session_id=${encodeURIComponent(sid)}&messages=0&resolve_model=0`);
    const session = data&&data.session;
    return !!(session&&(session.active_stream_id||session.pending_user_message));
  }catch(e){
    return false;
  }
}

// ── Mobile navigation ──────────────────────────────────────────────────────
let _workspacePanelMode='closed'; // 'closed' | 'browse' | 'preview'

function _isCompactWorkspaceViewport(){
  return window.matchMedia('(max-width: 900px)').matches;
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
  localStorage.setItem('hermes-webui-workspace-panel', open ? 'open' : 'closed');
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

function syncWorkspacePanelUI(){
  const {layout,panel,toggleBtn,collapseBtn}= _workspacePanelEls();
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
    _setButtonTooltip(toggleBtn, isOpen?'Hide workspace panel':'Show workspace panel');
    toggleBtn.disabled=!canBrowse;
  }
  if(collapseBtn){
    _setButtonTooltip(collapseBtn, isCompact?'Close workspace panel':'Hide workspace panel');
  }
  const hasSession=!!S.session;
  ['btnUpDir','btnNewFile','btnNewFolder','btnRefreshPanel'].forEach(id=>{
    const el=$(id);
    if(el)el.disabled=!hasSession;
  });
  const clearBtn=$('btnClearPreview');
  if(clearBtn){
    clearBtn.disabled=!isOpen;
    _setButtonTooltip(clearBtn, hasPreview?'Close preview':'Hide workspace panel');
    // On desktop, only show the X button when a file preview is open.
    // In browse mode the chevron (btnCollapseWorkspacePanel) already serves
    // as the close control, so showing both produces a duplicate X.
    if(!isCompact) clearBtn.style.display=hasPreview?'':'none';
  }
}

function toggleMobileSidebar(){
  const sidebar=document.querySelector('.sidebar');
  const overlay=$('mobileOverlay');
  if(!sidebar)return;
  const isOpen=sidebar.classList.contains('mobile-open');
  if(isOpen){closeMobileSidebar();}
  else{sidebar.classList.add('mobile-open');if(overlay)overlay.classList.add('visible');}
}
function closeMobileSidebar(){
  const sidebar=document.querySelector('.sidebar');
  const overlay=$('mobileOverlay');
  if(sidebar)sidebar.classList.remove('mobile-open');
  if(overlay)overlay.classList.remove('visible');
}

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
function toggleMobileFiles(){
  toggleWorkspacePanel();
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
    const overlay=$('mobileOverlay');
    if(sidebar){
      sidebar.classList.add('mobile-open');
      if(overlay)overlay.classList.add('visible');
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
$('btnAttach').onclick=e=>{if(e&&e.preventDefault)e.preventDefault();$('fileInput').value='';$('fileInput').click();};

// ── Voice input (Web Speech API + MediaRecorder fallback) ───────────────────
(function(){
  const SpeechRecognition=window.SpeechRecognition||window.webkitSpeechRecognition;
  const _canRecordAudio=!!(navigator.mediaDevices&&navigator.mediaDevices.getUserMedia&&window.MediaRecorder);
  if(!SpeechRecognition&&!_canRecordAudio) return; // Browser unsupported — mic button stays hidden

  // Persist SR failure across reloads (e.g. Tailscale/network error)
  const _micForceMediaRecorderKey='mic_force_mediarecorder';
  let _forceMediaRecorder=!SpeechRecognition||localStorage.getItem(_micForceMediaRecorderKey)==='1';

  const btn=$('btnMic');
  const status=$('micStatus');
  const ta=$('msg');
  const statusText=status?status.querySelector('.status-text'):null;
  btn.style.display=''; // Show button — browser supports speech recognition or recording fallback

  let recognition=(!_forceMediaRecorder&&SpeechRecognition)?new SpeechRecognition():null;
  let mediaRecorder=null;
  let mediaStream=null;
  let audioChunks=[];
  let _finalText='';
  let _prefix='';
  let _isRecording=false;

  function _setRecording(on){
    window._micActive=on;
    btn.classList.toggle('recording',on);
    // Active-state title flips so the tooltip is honest about what
    // pressing the button will do (#1488).
    _setButtonTooltip(btn, on ? t('voice_dictate_active') : t('voice_dictate'));
    status.style.display=on?'':'none';
    if(statusText) statusText.textContent=on?'Listening':'Listening';
    if(!on){ _finalText=''; _prefix=''; }
  }

  function _commitTranscript(text){
    const clean=(text||'').trim();
    const committed=clean
      ? (_prefix&&!_prefix.endsWith(' ')&&!_prefix.endsWith('\n')
          ? _prefix+' '+clean.trimStart()
          : _prefix+clean)
      : ta.value;
    ta.value=committed;
    autoResize();
    if(window._micPendingSend){
      window._micPendingSend=false;
      send();
    }
  }

  async function _transcribeBlob(blob){
    const ext=(blob.type&&blob.type.includes('ogg'))?'ogg':'webm';
    const form=new FormData();
    form.append('file',new File([blob],`voice-input.${ext}`,{type:blob.type||`audio/${ext}`}));
    setComposerStatus('Transcribing…');
    try{
      const res=await fetch('api/transcribe',{method:'POST',body:form});
      const data=await res.json().catch(()=>({}));
      if(!res.ok) throw new Error(data.error||'Transcription failed');
      _commitTranscript(data.transcript||'');
    }catch(err){
      window._micPendingSend=false;
      showToast(err.message||t('mic_network'));
    }finally{
      setComposerStatus('');
    }
  }

  function _stopTracks(){
    if(mediaStream){
      mediaStream.getTracks().forEach(track=>track.stop());
      mediaStream=null;
    }
  }

  function _stopMic(){
    if(!window._micActive) return;
    if(recognition){
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

  if(recognition && !_forceMediaRecorder){
    recognition.continuous=false;
    recognition.interimResults=true;
    recognition.lang=(typeof _locale!=='undefined'&&_locale._speech)||'en-US';

    recognition.onstart=()=>{ _finalText=''; };

    recognition.onresult=(event)=>{
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

    recognition.onend=()=>{
      const committed=_finalText
        ? (_prefix&&!_prefix.endsWith(' ')&&!_prefix.endsWith('\n')
            ? _prefix+' '+_finalText.trimStart()
            : _prefix+_finalText)
        : ta.value;
      _setRecording(false);
      ta.value=committed;
      autoResize();
      if(window._micPendingSend){
        window._micPendingSend=false;
        send();
      }
    };

    recognition.onerror=(event)=>{
      _setRecording(false);
      window._micPendingSend=false;
      _isRecording=false;
      if(event.error==='network'||event.error==='not-allowed'){
        // Persist SR failure: next reload will skip SpeechRecognition
        localStorage.setItem(_micForceMediaRecorderKey,'1');
        _forceMediaRecorder=true;
        recognition=null;
      }
      const msgs={
        'not-allowed':t('mic_denied'),
        'no-speech':t('mic_no_speech'),
        'network':t('mic_network'),
      };
      showToast(msgs[event.error]||t('mic_error')+event.error);
    };
  }

  btn.onclick=async()=>{
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
    if(recognition && !_forceMediaRecorder){
      recognition.start();
      _setRecording(true);
      return;
    }
    if(!_canRecordAudio){
      _isRecording=false;
      showToast(t('mic_network'));
      return;
    }
    try{
      mediaStream=await navigator.mediaDevices.getUserMedia({audio:true});
      const preferredTypes=['audio/webm;codecs=opus','audio/webm','audio/ogg;codecs=opus','audio/ogg'];
      const mimeType=preferredTypes.find(type=>window.MediaRecorder.isTypeSupported?.(type))||'';
      mediaRecorder=new MediaRecorder(mediaStream,mimeType?{mimeType}:undefined);
      audioChunks=[];
      mediaRecorder.ondataavailable=e=>{if(e.data&&e.data.size)audioChunks.push(e.data);};
      mediaRecorder.onerror=()=>{
        _isRecording=false;
        _setRecording(false);
        window._micPendingSend=false;
        _stopTracks();
        showToast(t('mic_network'));
      };
      mediaRecorder.onstop=async()=>{
        _isRecording=false;
        const blob=new Blob(audioChunks,{type:mediaRecorder.mimeType||mimeType||'audio/webm'});
        _setRecording(false);
        _stopTracks();
        if(blob.size){ await _transcribeBlob(blob); }
        else if(window._micPendingSend){
          window._micPendingSend=false;
        }
      };
      mediaRecorder.start();
      _setRecording(true);
    }catch(err){
      _isRecording=false;
      window._micPendingSend=false;
      _stopTracks();
      showToast(t('mic_denied'));
    }
  };
})();
window._micActive=window._micActive||false;
window._micPendingSend=window._micPendingSend||false;

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
  const SILENCE_MS=1800; // auto-send after 1.8s silence

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
    _setState('listening');

    _recognition=new SpeechRecognition();
    _recognition.continuous=false;
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
        },SILENCE_MS);
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
        showToast(t('mic_denied'));
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
      // After speaking, go back to listening
      if(_voiceModeActive) setTimeout(()=>_startListening(),500);
    };
    utter.onerror=()=>{
      if(_voiceModeActive) setTimeout(()=>_startListening(),1000);
    };

    speechSynthesis.speak(utter);
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
    modeBtn.classList.remove('active');
    _setButtonTooltip(modeBtn, t('voice_mode_toggle'));
    bar.style.display='none';
    clearTimeout(_silenceTimer);
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
  if(S.session
     && (S.session.message_count||0)===0
     && !S.busy
     && !S.session.active_stream_id
     && !S.session.pending_user_message){
    $('msg').focus();closeMobileSidebar();return;
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
$('modelSelect').onchange=async()=>{
  if(!S.session)return;
  const selectedModel=$('modelSelect').value;
  const modelState=(typeof _modelStateForSelect==='function')
    ? _modelStateForSelect($('modelSelect'),selectedModel)
    : {model:selectedModel,model_provider:null};
  if(typeof closeModelDropdown==='function') closeModelDropdown();
  if(typeof _writePersistedModelState==='function') _writePersistedModelState(modelState.model,modelState.model_provider);
  else try{localStorage.setItem('hermes-webui-model',modelState.model)}catch{}
  await api('/api/session/update',{method:'POST',body:JSON.stringify({
    session_id:S.session.session_id,
    workspace:S.session.workspace,
    model:modelState.model,
    model_provider:modelState.model_provider||null,
  })});
  S.session.model=modelState.model;
  S.session.model_provider=modelState.model_provider||null;
  if(typeof syncModelChip==='function') syncModelChip();
  syncTopbar();
  // Clarify scope: composer model changes are session-local, not the global default.
  if(typeof showToast==='function'){
    showToast(t('model_scope_toast')||'Applies to this conversation from your next message.', 3000);
  }
  // Warn if selected model belongs to a different provider than what Hermes is configured for
  if(typeof _checkProviderMismatch==='function'){
    const warn=_checkProviderMismatch(selectedModel);
    if(warn&&typeof showToast==='function') showToast(warn,4000);
  }
};
$('msg').addEventListener('input',()=>{
  autoResize();
  updateSendBtn();
  // Persist composer draft to server (debounced in _saveComposerDraft).
  const sid = S && S.session && S.session.session_id;
  if (sid && typeof _saveComposerDraft === 'function') {
    _saveComposerDraft(sid, $('msg').value, S.pendingFiles ? [...S.pendingFiles] : []);
  }
  const text=$('msg').value;
  if(text.startsWith('/')&&text.indexOf('\n')===-1){
    if(typeof getSlashAutocompleteMatches==='function'){
      getSlashAutocompleteMatches(text).then(matches=>{
        if(($('msg').value||'')!==text) return;
        if(matches.length)showCmdDropdown(matches); else hideCmdDropdown();
      });
    }else{
      const prefix=text.slice(1);
      const matches=getMatchingCommands(prefix);
      if(matches.length)showCmdDropdown(matches); else hideCmdDropdown();
    }
    if(typeof ensureSkillCommandsLoadedForAutocomplete==='function') ensureSkillCommandsLoadedForAutocomplete();
  } else {
    hideCmdDropdown();
  }
});
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
$('msg').addEventListener('keydown',e=>{
  // Autocomplete navigation when dropdown is open
  const dd=$('cmdDropdown');
  const dropdownOpen=dd&&dd.classList.contains('open');
  if(dropdownOpen){
    if(e.key==='ArrowUp'){e.preventDefault();navigateCmdDropdown(-1);return;}
    if(e.key==='ArrowDown'){e.preventDefault();navigateCmdDropdown(1);return;}
    if(e.key==='Tab'){e.preventDefault();selectCmdDropdownItem();return;}
    if(e.key==='Escape'){e.preventDefault();hideCmdDropdown();return;}
    if(e.key==='Enter'&&!e.shiftKey){
      if(_isImeEnter(e)){return;}
      e.preventDefault();
      selectCmdDropdownItem();
      return;
    }
  }
  // Send key: respect user preference.
  // On touch-primary devices (software keyboard), default to Enter = newline
  // since there's no physical Shift key. Users send via the Send button.
  // The 'ctrl+enter' setting also uses this behavior (Enter = newline).
  // Users can override in Settings by explicitly choosing 'enter' mode.
  if(e.key==='Enter'){
    if(_isImeEnter(e)){return;}
    const _mobileDefault=matchMedia('(pointer:coarse)').matches&&window._sendKey==='enter';
    if(window._sendKey==='ctrl+enter'||_mobileDefault){
      if(e.ctrlKey||e.metaKey){e.preventDefault();send();}
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
    e.preventDefault();
    // If the current session has no messages AND nothing is in flight, just focus
    // the composer rather than creating another empty session that will clutter
    // the sidebar list (#1171). See the matching guard in $('btnNewChat').onclick
    // and bug #1432 for why the in-flight check is needed.
    if(S.session
       && (S.session.message_count||0)===0
       && !S.busy
       && !S.session.active_stream_id
       && !S.session.pending_user_message){
      $('msg').focus();return;
    }
    // Cmd/Ctrl+K should always create a new conversation, even while the current
    // one is still streaming. The old !S.busy guard meant users had to wait for
    // a long generation to finish before they could start something new — exactly
    // the moment they want to switch context. newSession() leaves the in-flight
    // stream running on its own session; the user just gets a fresh blank one.
    await newSession();await renderSessionList();closeMobileSidebar();$('msg').focus();
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
    if(ss&&ss.value){ss.value='';filterSessions();}
    // Cancel any active message edit
    const editArea=document.querySelector('.msg-edit-area');
    if(editArea){
      const bar=editArea.closest('.msg-row')&&editArea.closest('.msg-row').querySelector('.msg-edit-bar');
      if(bar){const cancel=bar.querySelector('.msg-edit-cancel');if(cancel)cancel.click();}
    }
  }
});
$('msg').addEventListener('paste',e=>{
  const items=Array.from(e.clipboardData?.items||[]);
  // When the clipboard carries BOTH text and an image (common from Notes,
  // Word, browsers, Slack — the OS attaches a rendered preview alongside
  // the plain text), prefer the text and let the browser paste normally.
  // Only intercept when the clipboard is image-only (true screenshot paste).
  // Tighten the image filter to kind==='file' so string items advertising an
  // image MIME (e.g. text/html with an embedded data URI) are not misclassified.
  const hasText=items.some(i=>i.kind==='string'&&(i.type==='text/plain'||i.type==='text/html'));
  const imageItems=items.filter(i=>i.kind==='file'&&i.type.startsWith('image/'));
  if(!imageItems.length||hasText)return;
  e.preventDefault();
  const pasteTs=Date.now();
  const files=imageItems.map((i,idx)=>{
    const blob=i.getAsFile();
    const ext=i.type.split('/')[1]||'png';
    const suffix=imageItems.length>1?`-${idx+1}`:'';
    return new File([blob],`screenshot-${pasteTs}${suffix}.${ext}`,{type:i.type});
  });
  addFiles(files);
  setStatus(t('image_pasted')+files.map(f=>f.name).join(', '));
});
document.querySelectorAll('.suggestion').forEach(btn=>{
  btn.onclick=()=>{$('msg').value=btn.dataset.msg;send();};
});

window.addEventListener('resize',()=>{
  _syncWorkspacePanelInlineWidth();
  syncWorkspacePanelState();
});

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

// ── Appearance helpers (theme = light/dark/system, skin = accent color) ──────
const _THEMES=[
  {name:'Light', value:'light', colors:['#FEFCF7','#FAF7F0','#B8860B']},
  {name:'Dark', value:'dark', colors:['#0D0D1A','#141425','#FFD700']},
  {name:'System', value:'system', colors:['#FEFCF7','#0D0D1A','#B8860B']},
];
const _SKINS=[
  {name:'Default',  colors:['#FFD700','#FFBF00','#CD7F32']},
  {name:'Ares',     colors:['#FF4444','#CC3333','#992222']},
  {name:'Mono',     colors:['#CCCCCC','#999999','#666666']},
  {name:'Slate',    colors:['#334155','#475569','#64748b']},
  {name:'Poseidon', colors:['#0EA5E9','#0284C7','#0369A1']},
  {name:'Sisyphus', colors:['#A78BFA','#8B5CF6','#7C3AED']},
  {name:'Charizard',colors:['#FB923C','#F97316','#EA580C']},
  {name:'Sienna',   colors:['#D97757','#C06A49','#9A523A']},
];
const _VALID_THEMES=new Set((_THEMES||[]).map(t=>t.value));
const _VALID_SKINS=new Set((_SKINS||[]).map(s=>s.name.toLowerCase()));
const _LEGACY_THEME_MAP={
  slate:{theme:'dark',skin:'slate'},
  solarized:{theme:'dark',skin:'poseidon'},
  monokai:{theme:'dark',skin:'sisyphus'},
  nord:{theme:'dark',skin:'slate'},
  oled:{theme:'dark',skin:'default'},
};
let _systemThemeMq=null;
let _onSystemThemeChange=null;

function _normalizeAppearance(theme,skin){
  const rawTheme=typeof theme==='string'?theme.trim().toLowerCase():'';
  const rawSkin=typeof skin==='string'?skin.trim().toLowerCase():'';
  const legacy=_LEGACY_THEME_MAP[rawTheme];
  const nextTheme=legacy?legacy.theme:(_VALID_THEMES.has(rawTheme)?rawTheme:'dark');
  const nextSkin=_VALID_SKINS.has(rawSkin)?rawSkin:(legacy?legacy.skin:'default');
  return {theme:nextTheme,skin:nextSkin};
}

// Sync <meta name="theme-color"> with the active theme's computed --bg.
// This surfaces the WebUI's exact theme background to:
//   1. Mobile Safari status bar (the prefers-color-scheme media variants in index.html
//      cover the pre-load case; this updater handles user-toggled changes mid-session).
//   2. iOS PWA / Add to Home Screen status bar.
//   3. Native WKWebView wrappers (e.g. hermes-swift-mac) that read this attribute as
//      the source of truth for AppKit chrome (tab bar, title bar, traffic-light area)
//      instead of pixel-sampling — overlay-resistant and IPC-free.
// Reading getComputedStyle(html).getPropertyValue('--bg') picks up the active skin
// (Default, Sienna, Sisyphus, Charizard, etc.) so each skin's distinct paint reaches
// the meta tag.
function _syncThemeColorMeta(){
  try{
    const bg=getComputedStyle(document.documentElement).getPropertyValue('--bg').trim();
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

function _setResolvedTheme(isDark){
  document.documentElement.classList.toggle('dark',!!isDark);
  const link=document.getElementById('prism-theme');
  if(!link){ _syncThemeColorMeta(); return; }
  const want=isDark
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
  _syncThemeColorMeta();
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
    const key=skin.name.toLowerCase();
    const btn=document.createElement('button');
    btn.type='button';
    btn.className='skin-pick-btn';
    btn.dataset.skinVal=key;
    btn.style.cssText='border:1px solid var(--border2);border-radius:8px;padding:8px 4px;text-align:center;cursor:pointer;background:none;transition:all .15s';
    btn.onclick=()=>_pickSkin(skin.name);
    const dots=skin.colors.map(c=>`<span style="display:inline-block;width:10px;height:10px;border-radius:50%;background:${c}"></span>`).join('');
    btn.innerHTML=`<div style="display:flex;gap:3px;justify-content:center;margin-bottom:4px">${dots}</div><span style="font-size:11px;color:var(--text)">${skin.name}</span>`;
    grid.appendChild(btn);
  }
  _syncSkinPicker((activeSkin||'default').toLowerCase());
}

function applyBotName(){
  // Prefer profile name over global bot_name for personalised placeholder.
  // If activeProfile is set and not 'default', use it (capitalised).
  // Falls back to window._botName (global bot_name setting) or 'Hermes'.
  let name;
  if(S.activeProfile && S.activeProfile!=='default'){
    name=S.activeProfile.charAt(0).toUpperCase()+S.activeProfile.slice(1);
  }else{
    name=window._botName||'Hermes';
  }
  document.title=name;
  const sidebarH1=document.querySelector('.sidebar-header h1');
  if(sidebarH1) sidebarH1.textContent=name;
  const logo=document.querySelector('.sidebar-header .logo');
  if(logo) logo.textContent=name.charAt(0).toUpperCase();
  const topbarTitle=$('topbarTitle');
  if(topbarTitle && (!S.session)) topbarTitle.textContent=name;
  const msg=$('msg');
  if(msg) msg.placeholder='Message '+name+'\u2026';
}

(async()=>{
  // Load send key preference
  let _bootSettings={};
  try{
    const s=await api('/api/settings');
    _bootSettings=s;
    window._sendKey=s.send_key||'enter';
    window._showTokenUsage=!!s.show_token_usage;
    window._showTps=!!s.show_tps;
    window._showCliSessions=!!s.show_cli_sessions;
    window._soundEnabled=!!s.sound_enabled;
    window._notificationsEnabled=!!s.notifications_enabled;
    window._showThinking=s.show_thinking!==false;
    window._simplifiedToolCalling=s.simplified_tool_calling!==false;
    window._sidebarDensity=(s.sidebar_density==='detailed'?'detailed':'compact');
    window._busyInputMode=(s.busy_input_mode||'queue');
    window._sessionEndlessScrollEnabled=!!s.session_endless_scroll;
    window._botName=s.bot_name||'Hermes';
    if(s.default_model) window._defaultModel=s.default_model;
    // Persist default workspace so the blank new-chat page can show it
    // and workspace actions (New file/folder) work before the first session (#804).
    if(s.default_workspace) S._profileDefaultWorkspace=s.default_workspace;
    window._sessionJumpButtonsEnabled=!!s.session_jump_buttons;
    const appearance=_normalizeAppearance(s.theme,s.skin);
    localStorage.setItem('hermes-theme',appearance.theme);
    _applyTheme(appearance.theme);
    localStorage.setItem('hermes-skin',appearance.skin);
    _applySkin(appearance.skin);
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
    applyBotName();
    // TTS: apply enabled state on boot so buttons show/hide correctly (#499)
    if(typeof _applyTtsEnabled==='function') _applyTtsEnabled(localStorage.getItem('hermes-tts-enabled')==='true');
  }catch(e){
    window._sendKey='enter';
    window._showTokenUsage=false;
    window._showTps=false;
    window._showCliSessions=false;
    window._soundEnabled=false;
    window._notificationsEnabled=false;
    window._showThinking=true;
    window._simplifiedToolCalling=true;
    window._sessionJumpButtonsEnabled=false;
    window._sidebarDensity='compact';
    window._busyInputMode='queue';
    window._sessionEndlessScrollEnabled=false;
    window._botName='Hermes';
    _bootSettings={check_for_updates:false};
    if(typeof setLocale==='function'){
      const _lang=typeof resolvePreferredLocale==='function'
        ? resolvePreferredLocale(null, localStorage.getItem('hermes-lang'))
        : (localStorage.getItem('hermes-lang') || 'en');
      setLocale(_lang);
      if(typeof applyLocaleToDOM==='function')applyLocaleToDOM();
    }
    applyBotName();
    if(typeof _applyTtsEnabled==='function') _applyTtsEnabled(localStorage.getItem('hermes-tts-enabled')==='true');
  }
  // Non-blocking update check (fire-and-forget, once per tab session)
  // ?test_updates=1 in URL forces banner display for testing (bypasses sessionStorage guards)
  const _testUpdates=new URLSearchParams(location.search).get('test_updates')==='1';
  if(_testUpdates||(_bootSettings.check_for_updates!==false&&!sessionStorage.getItem('hermes-update-checked')&&!sessionStorage.getItem('hermes-update-dismissed'))){
    const _checkUrl='api/updates/check'+(_testUpdates?'?simulate=1':'');
    api(_checkUrl).then(d=>{if(!_testUpdates)sessionStorage.setItem('hermes-update-checked','1');if((d.webui&&d.webui.behind>0)||(d.agent&&d.agent.behind>0))_showUpdateBanner(d);}).catch(()=>{});
  }
  // Fetch active profile
  try{const p=await api('/api/profile/active');S.activeProfile=p.name||'default';}catch(e){S.activeProfile='default';}
  // Update profile chip label immediately
  const profileLabel=$('profileChipLabel');
  if(profileLabel) profileLabel.textContent=S.activeProfile||'default';
  // Fetch available models without blocking session restore. The static HTML
  // options are enough for first paint; the dynamic provider list can settle
  // after the saved session is visible.
  const _modelDropdownReady=populateModelDropdown().then(()=>{
    const savedState=(typeof _readPersistedModelState==='function')
      ? _readPersistedModelState()
      : (localStorage.getItem('hermes-webui-model')?{model:localStorage.getItem('hermes-webui-model'),model_provider:null}:null);
    const savedModel=savedState&&savedState.model;
    if(savedModel && $('modelSelect')){
      const applied=(typeof _applyModelToDropdown==='function')
        ? _applyModelToDropdown(savedModel,$('modelSelect'),savedState.model_provider||null)
        : null;
      if(!applied) $('modelSelect').value=savedModel;
      // If the value didn't take (model not in list), clear the bad pref
      if(!applied&&$('modelSelect').value!==savedModel){
        if(typeof _clearPersistedModelState==='function') _clearPersistedModelState();
        else localStorage.removeItem('hermes-webui-model');
      }
      else if(typeof syncModelChip==='function') syncModelChip();
    }
    if(S.session) syncTopbar();
  }).catch(()=>{});
  window._modelDropdownReady=_modelDropdownReady;
  // Pre-load workspace list so sidebar name is correct from first render.
  // Render the session list before restoring the saved conversation so a stale
  // saved-session/client-side boot error cannot leave the sidebar empty forever.
  await loadWorkspaceList();
  await loadOnboardingWizard();
  await renderSessionList();
  _initResizePanels();
  // Workspace panel restore happens AFTER loadSession so we know if
  // the session has a workspace — prevents the snap-open-then-closed flash (#576).
  // Fix #822: clear any browser-restored value before first render. This
  // covers fresh page loads and reloads. The bfcache restore case is handled
  // separately below by a `pageshow` listener — the async IIFE here does NOT
  // re-run when the browser restores the page from bfcache.
  const _srch = document.getElementById('sessionSearch'); if (_srch) _srch.value = '';
  // Initialize reasoning chip on boot (fixes #1103 — chip hidden until session load)
  if(typeof fetchReasoningChip==='function') fetchReasoningChip();
  const urlSession=(typeof _sessionIdFromLocation==='function')?_sessionIdFromLocation():null;
  const savedLocal=localStorage.getItem('hermes-webui-session');
  const saved=urlSession||savedLocal;
  if(saved){
    try{
      if(!urlSession&&savedLocal&&await _savedSessionShouldStaySidebarOnly(savedLocal)){
        S.session=null; S.messages=[]; S.activeStreamId=null; S.busy=false;
        S._bootReady=true;
        syncTopbar();syncWorkspacePanelState();
        $('emptyState').style.display='';
        await renderSessionList();if(typeof startGatewaySSE==='function')startGatewaySSE();
        return;
      }
      await loadSession(saved);
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
      if(S.session && (S.session.message_count||0) === 0 && !_restoredInFlight){
        S.session=null; S.messages=[];
        S._bootReady=true;
        // Restore panel pref before syncing so the workspace panel stays visible
        // even though there is no active session (#workspace-persist).
        const _ephPanelPref=localStorage.getItem('hermes-webui-workspace-panel-pref')==='open'
          || localStorage.getItem('hermes-webui-workspace-panel')==='open';
        if(_ephPanelPref) _workspacePanelMode='browse';
        syncTopbar();syncWorkspacePanelState();
        $('emptyState').style.display='';
        await renderSessionList();if(typeof startGatewaySSE==='function')startGatewaySSE();
        return;
      }
      // Restore the panel from localStorage when the session has a workspace.
      // Preference key takes priority over runtime state so that closing
      // the panel via toolbar X doesn't suppress the "keep open" setting.
      const panelPref=localStorage.getItem('hermes-webui-workspace-panel-pref')==='open'
        || localStorage.getItem('hermes-webui-workspace-panel')==='open';
      if(S.session&&S.session.workspace&&panelPref){
        _workspacePanelMode='browse';
      }
      S._bootReady=true;
      syncTopbar();syncWorkspacePanelState();await renderSessionList();if(typeof startGatewaySSE==='function')startGatewaySSE();await checkInflightOnBoot(saved);return;}
    catch(e){localStorage.removeItem('hermes-webui-session');}
  }
  // no saved session - show empty state, wait for user to hit +
  S._bootReady=true;
  syncTopbar();
  // Restore panel pref so the workspace panel stays visible on a fresh load if the
  // user had it open during their last session (#workspace-persist).
  const _freshPanelPref=localStorage.getItem('hermes-webui-workspace-panel-pref')==='open'
    || localStorage.getItem('hermes-webui-workspace-panel')==='open';
  if(_freshPanelPref) _workspacePanelMode='browse';
  syncWorkspacePanelState();
  $('emptyState').style.display='';
  await renderSessionList();
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
  const _srch = document.getElementById('sessionSearch');
  if (_srch) _srch.value = '';
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

// Hermes local session-continuity controls: 세션 압축 / 이동 준비 / 세션 이동
(function(){
  function _scEl(id){return document.getElementById(id);}
  function _scToast(msg,ms,type){if(typeof showToast==='function')showToast(msg,ms||2200,type);}
  function _scRoleLabel(role){return role==='user'?'User':role==='assistant'?'Assistant':role||'message';}
  function _scContent(msg){
    if(!msg)return '';
    const raw=msg.content||msg.text||'';
    if(Array.isArray(raw))return raw.map(p=>typeof p==='string'?p:(p&&(p.text||p.content)||'')).join(' ').trim();
    return String(raw||'').trim();
  }
  function _scVisibleMessages(){
    const items=Array.isArray(S&&S.messages)?S.messages:[];
    return items.filter(m=>m&&m.role&&m.role!=='tool'&&_scContent(m));
  }
  function _scHash(text){
    let h=2166136261;
    for(let i=0;i<text.length;i++){h^=text.charCodeAt(i);h=Math.imul(h,16777619);}
    return ('00000000'+(h>>>0).toString(16)).slice(-8);
  }
  async function _scCopy(text){
    if(typeof _copyText==='function')return _copyText(text);
    if(navigator.clipboard&&window.isSecureContext)return navigator.clipboard.writeText(text);
    const ta=document.createElement('textarea');ta.value=text;ta.setAttribute('readonly','');ta.style.position='fixed';ta.style.left='-9999px';document.body.appendChild(ta);ta.select();document.execCommand('copy');ta.remove();
  }
  async function _scSendSlash(command){
    if(S&&S.busy){_scToast('현재 응답이 진행 중입니다. 완료 후 다시 시도하세요.',2600,'warn');return;}
    const msg=_scEl('msg');
    if(!msg){_scToast('메시지 입력창을 찾지 못했습니다.',2600,'error');return;}
    msg.value=command;
    if(typeof autoResize==='function')autoResize();
    if(typeof send==='function')await send();
  }
  function _scBoundedLine(raw, maxLen){
    const text=String(raw||'').replace(/\s+/g,' ').trim();
    if(text.length<=maxLen)return text;
    return text.slice(0,Math.max(0,maxLen-1))+'…';
  }
  function _scVisibleOutline(messages, startIdx){
    const maxPerMessage=320;
    const maxTotal=42000;
    const offset=Number.isFinite(startIdx)?Math.max(0,startIdx):0;
    const lines=[];
    let used=0;
    let truncated=false;
    for(let i=0;i<messages.length;i++){
      const m=messages[i];
      const line=`${String(offset+i+1).padStart(3,'0')}. ${_scRoleLabel(m.role)}: ${_scBoundedLine(_scContent(m),maxPerMessage)}`;
      if(used+line.length+1>maxTotal){truncated=true;break;}
      lines.push(line);
      used+=line.length+1;
    }
    if(!lines.length)return '표시 가능한 대화 내용이 없습니다.';
    if(truncated)lines.push(`[중단: 인계문 길이 보호를 위해 ${messages.length-lines.length}개 메시지 outline 생략]`);
    return lines.join('\n');
  }
  function _scPriorHandoff(messages){
    let best=null;
    for(let i=0;i<messages.length;i++){
      const text=_scContent(messages[i]);
      if(!text||!text.includes('[이동 준비: 새 세션 이어가기 안내]'))continue;
      const hashMatch=text.match(/(?:^|\n)packet_hash:\s*([^\n]+)/);
      const sourceMatch=text.match(/(?:^|\n)source_session_id:\s*([^\n]+)/);
      best={index:i,text,hash:hashMatch?hashMatch[1].trim():'unknown',source:sourceMatch?sourceMatch[1].trim():'unknown'};
    }
    return best;
  }
  function _scExtractSection(text, title){
    const marker=`\n\n${title}:\n`;
    const idx=String(text||'').indexOf(marker);
    if(idx<0)return '';
    const start=idx+marker.length;
    const rest=String(text||'').slice(start);
    const next=rest.search(/\n\n[^\n:]{1,80}:\n/);
    return (next>=0?rest.slice(0,next):rest).trim();
  }
  function _scPriorDigest(prior){
    if(!prior||!prior.text)return {text:'',truncated:false};
    const text=String(prior.text||'');
    const lines=text.split('\n');
    const meta=[];
    for(const line of lines){
      if(line.startsWith('[이동 준비:')||/^(source_session_id|packet_scope|packet_hash|prior_packet_hash|handoff_chain|delta_visible_message_count|created_at|generation_method):/.test(line)){
        meta.push(line);
      }
      if(meta.length>=12)break;
    }
    const recent=_scBoundedLine(_scExtractSection(text,'최근 요약'),3500);
    const instruction=_scBoundedLine(_scExtractSection(text,'이어가기 지시'),900);
    const parts=[];
    if(meta.length)parts.push('이전 인계문 메타:\n'+meta.join('\n'));
    if(recent)parts.push('이전 운영 요약:\n'+recent);
    if(instruction)parts.push('이전 이어가기 지시:\n'+instruction);
    const digest=parts.join('\n\n')||`이전 인계문 ${prior.hash||'unknown'} 감지됨. 원문 전문은 효율을 위해 재첨부하지 않음.`;
    const maxLen=9000;
    if(digest.length<=maxLen)return {text:digest,truncated:false};
    return {text:digest.slice(0,maxLen-100)+'\n[중단: 이전 운영 요약 길이 보호로 일부 생략]',truncated:true};
  }
  function _scLatestByRole(messages, role){
    for(let i=messages.length-1;i>=0;i--){
      if(messages[i]&&messages[i].role===role){
        const text=_scContent(messages[i]);
        if(text)return text;
      }
    }
    return '';
  }
  function _scEvidenceLines(messages, pattern, limit){
    const lines=[];
    for(const m of messages.slice().reverse()){
      const text=_scContent(m);
      if(!text||!pattern.test(text))continue;
      lines.push(`- ${_scRoleLabel(m.role)}: ${_scBoundedLine(text,220)}`);
      if(lines.length>=limit)break;
    }
    return lines.reverse().join('\n');
  }
  function _scOperationalChecklist(messages, summary, prior, deltaMessages, sid){
    const latestUser=_scLatestByRole(messages,'user');
    const latestAssistant=_scLatestByRole(messages,'assistant');
    const verification=_scEvidenceLines(messages, /(PASS|검증|확인|console|served|active\/running|오류 없음|JS errors 없음|재시작 없음)/i, 3);
    const caution=[
      '- 전체 원문 이동이 아니라 운영 요약 + bounded delta handoff임.',
      '- backend 최근 요약은 recent-50 제한 가능성이 있음.',
      '- 필요하면 원본 세션/수정 파일/검증 로그를 다시 확인할 것.',
    ].join('\n');
    return [
      `- 현재 목표: ${_scBoundedLine(latestUser||'확인 필요',320)}`,
      `- 완료/변경: ${_scBoundedLine(summary||latestAssistant||'확인 필요',520)}`,
      `- 검증 결과: ${verification||'확인 필요 — 새 세션에서 필요한 파일/상태를 재검증할 것.'}`,
      `- 남은 작업: 최신 사용자 의도 기준으로 다음 안전 단계부터 진행. 불명확하면 한 번만 확인.`,
      `- 주의/금지:\n${caution}`,
      `- 참조: source_session_id=${sid}${prior?`; prior_packet_hash=${prior.hash}; prior_source_session_id=${prior.source}; delta_visible_message_count=${deltaMessages.length}`:''}`,
    ].join('\n');
  }
  async function _scBuildPacket(kind){
    const session=S&&S.session;
    if(!session||!session.session_id)throw new Error('활성 세션이 없습니다.');
    const sid=session.session_id;
    const messages=_scVisibleMessages();
    const created=new Date().toISOString();
    const prior=_scPriorHandoff(messages);
    const deltaStart=prior?prior.index+1:0;
    const deltaMessages=messages.slice(deltaStart);
    const outline=_scVisibleOutline(prior?deltaMessages:messages, deltaStart);
    const priorDigest=prior?_scPriorDigest(prior):null;
    let summary='';
    let method=prior?'local_prior_handoff+delta_visible_outline':'local_all_visible_outline';
    try{
      if(typeof api==='function'){
        const res=await api('/api/session/handoff-summary',{method:'POST',body:JSON.stringify({session_id:sid})});
        if(res&&res.ok&&res.summary){summary=String(res.summary).trim();method=(res.fallback?'api_recent_summary_fallback':'api_recent_summary')+'+'+(prior?'prior_handoff+delta_visible_outline':'local_all_visible_outline');}
      }
    }catch(e){
      summary='';
    }
    if(!summary){
      const tail=messages.slice(-24).map(m=>`- ${_scRoleLabel(m.role)}: ${_scBoundedLine(_scContent(m),420)}`).join('\n');
      summary=tail||'표시 가능한 대화 내용이 없습니다.';
      method='local_recent_tail+'+(prior?'prior_handoff+delta_visible_outline':'local_all_visible_outline');
    }
    const terminalHint=kind==='terminal'?'새 Hermes CLI/터미널 세션에 이 인계문을 붙여넣고 이어서 진행해. 원본 WebUI 세션을 직접 수정한다고 가정하지 마.':null;
    const scope=prior?'chained_handoff_plus_delta_visible_outline':'all_visible_messages_bounded_outline';
    const meta=[
      '[이동 준비: 새 세션 이어가기 안내]',
      `source_session_id: ${sid}`,
      kind==='move'&&S&&S.session&&S.session.session_id?`target_session_id: pending_new_session`:null,
      `packet_scope: ${scope}`,
      prior?'handoff_chain: true':null,
      prior?`prior_packet_hash: ${prior.hash}`:null,
      prior?`prior_source_session_id: ${prior.source}`:null,
      prior?`delta_visible_message_count: ${deltaMessages.length}`:null,
      prior&&priorDigest&&priorDigest.truncated?'prior_digest_truncated: true':null,
      'packet_limit_note: backend_summary_may_cover_recent_50; continuity is preserved by prior operational digest plus local bounded delta outline when present',
      'per_message_outline_chars: 320',
      `message_count: ${messages.length}`,
      `created_at: ${created}`,
      `generation_method: ${method}`,
    ].filter(Boolean);
    const instructions=terminalHint||'위 인계문 기준으로 이어서 진행해. 오래된 요청을 재실행하지 말고 최신 사용자 의도부터 확인해.';
    const checklist=_scOperationalChecklist(messages, summary, prior, deltaMessages, sid);
    const inherited=priorDigest?`\n\n계승된 운영 요약:\n${priorDigest.text}`:'';
    const outlineTitle=prior?'이번 세션 추가 visible 메시지 outline':'전체 visible 메시지 outline';
    const body=`${meta.join('\n')}\n\n운영 상태 체크리스트:\n${checklist}${inherited}\n\n최근 요약:\n${summary}\n\n${outlineTitle}:\n${outline}\n\n이어가기 지시:\n${instructions}`;
    const hash=_scHash(body);
    return body.replace(`generation_method: ${method}`,`packet_hash: local:${hash}\ngeneration_method: ${method}`);
  }
  async function _scHandoff(){
    try{
      const packet=await _scBuildPacket('handoff');
      await _scCopy(packet);
      const msg=_scEl('msg');
      if(msg){msg.value=packet;if(typeof autoResize==='function')autoResize();msg.focus();}
      _scToast('이동 준비 인계문을 복사하고 입력창에 넣었습니다.',2600);
    }catch(e){_scToast('이동 준비 실패: '+(e&&e.message||e),3200,'error');}
  }
  async function _scTerminal(){
    try{
      const packet=await _scBuildPacket('terminal');
      await _scCopy(packet);
      const msg=_scEl('msg');
      if(msg){msg.value=packet;if(typeof autoResize==='function')autoResize();msg.focus();}
      _scToast('터미널 이동 인계문을 복사했습니다. 터미널에서 새 Hermes 세션을 열고 붙여넣으세요.',3600);
    }catch(e){_scToast('터미널 이동 준비 실패: '+(e&&e.message||e),3200,'error');}
  }
  async function _scMove(){
    try{
      if(S&&S.busy){_scToast('현재 응답이 진행 중입니다. 완료 후 다시 시도하세요.',2600,'warn');return;}
      const packet=await _scBuildPacket('move');
      if(typeof newSession!=='function'||typeof send!=='function')throw new Error('새 세션/전송 기능을 찾지 못했습니다.');
      await _scCopy(packet);
      await newSession();
      if(typeof renderSessionList==='function')await renderSessionList();
      const msg=_scEl('msg');
      if(!msg)throw new Error('메시지 입력창을 찾지 못했습니다.');
      msg.value=packet;
      if(typeof autoResize==='function')autoResize();
      await send();
      _scToast('새 세션으로 인계문을 전달했습니다.',2600);
    }catch(e){_scToast('세션 이동 실패: '+(e&&e.message||e),3200,'error');}
  }
  function _scSetHandoffMenu(open){
    const h=_scEl('btnSessionHandoff'),menu=_scEl('sessionHandoffMenu');
    if(!h||!menu)return;
    const next=!!open;
    menu.hidden=!next;
    h.setAttribute('aria-expanded',next?'true':'false');
  }
  function _scToggleHandoffMenu(){
    const menu=_scEl('sessionHandoffMenu');
    _scSetHandoffMenu(menu?menu.hidden:true);
  }
  window._sessionContinuityHandoff=_scHandoff;
  window._sessionContinuityTerminal=_scTerminal;
  window._sessionContinuityMove=_scMove;
  function _scBind(){
    const c=_scEl('btnSessionCompress'),h=_scEl('btnSessionHandoff'),hg=_scEl('btnSessionHandoffGeneral'),ht=_scEl('btnSessionHandoffTerminal'),m=_scEl('btnSessionMove');
    if(c&&!c.dataset.bound){c.dataset.bound='1';c.addEventListener('click',()=>_scSendSlash('/compress'));}
    if(h&&!h.dataset.bound){h.dataset.bound='1';h.addEventListener('click',(ev)=>{ev.stopPropagation();_scToggleHandoffMenu();});}
    if(hg&&!hg.dataset.bound){hg.dataset.bound='1';hg.addEventListener('click',async(ev)=>{ev.stopPropagation();_scSetHandoffMenu(false);await _scHandoff();});}
    if(ht&&!ht.dataset.bound){ht.dataset.bound='1';ht.addEventListener('click',async(ev)=>{ev.stopPropagation();_scSetHandoffMenu(false);await _scTerminal();});}
    if(m&&!m.dataset.bound){m.dataset.bound='1';m.addEventListener('click',_scMove);}
    if(!window._sessionContinuityMenuCloseBound){
      window._sessionContinuityMenuCloseBound=true;
      document.addEventListener('click',(ev)=>{
        const wrap=document.querySelector('.session-continuity-menu-wrap');
        if(wrap&&!wrap.contains(ev.target))_scSetHandoffMenu(false);
      });
      document.addEventListener('keydown',(ev)=>{if(ev.key==='Escape')_scSetHandoffMenu(false);});
    }
  }
  if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',_scBind);else _scBind();
})();

