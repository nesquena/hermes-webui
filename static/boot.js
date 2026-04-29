async function cancelStream(){
  const streamId = S.activeStreamId;
  if(!streamId) return;
  try{
    await fetch(new URL(`api/chat/cancel?stream_id=${encodeURIComponent(streamId)}`,location.href).href,{credentials:'include'});
  }catch(e){/* cancel request failed — cleanup below still runs */}
  // Clear status unconditionally after the cancel request completes.
  // The SSE cancel event may also fire, but if the connection is already
  // closed it won't arrive — so we handle cleanup here as the guaranteed path.
  S.activeStreamId=null;
  setBusy(false);
  if(typeof setComposerStatus==='function') setComposerStatus('');
  else setStatus('');
}

// ── Mobile navigation ──────────────────────────────────────────────────────
let _workspacePanelMode='closed'; // 'closed' | 'browse' | 'preview'

function _isCompactWorkspaceViewport(){
  return window.matchMedia('(max-width: 900px)').matches;
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
    toggleBtn.title=isOpen?'Hide workspace panel':'Show workspace panel';
    toggleBtn.disabled=!canBrowse;
  }
  if(collapseBtn){
    collapseBtn.title=isCompact?'Close workspace panel':'Hide workspace panel';
  }
  const hasSession=!!S.session;
  ['btnUpDir','btnNewFile','btnNewFolder','btnRefreshPanel'].forEach(id=>{
    const el=$(id);
    if(el)el.disabled=!hasSession;
  });
  const clearBtn=$('btnClearPreview');
  if(clearBtn){
    clearBtn.disabled=!isOpen;
    clearBtn.title=hasPreview?'Close preview':'Hide workspace panel';
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
  send();
};
$('btnAttach').onclick=()=>$('fileInput').click();

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
$('fileInput').onchange=e=>{addFiles(Array.from(e.target.files));e.target.value='';};
$('btnNewChat').onclick=async()=>{
  // If the current session has no messages, just focus the composer rather than
  // creating another empty session that will clutter the sidebar list (#1171).
  if(S.session&&(S.session.message_count||0)===0){$('msg').focus();closeMobileSidebar();return;}
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
function clearPreview(){
  const closePanelAfter=_workspacePanelMode==='preview';
  const pa=$('previewArea');if(pa)pa.classList.remove('visible');
  const pi=$('previewImg');if(pi){pi.onerror=null;pi.src='';}
  const pm=$('previewMd');if(pm)pm.innerHTML='';
  const pc=$('previewCode');if(pc)pc.textContent='';
  const pp=$('previewPathText');if(pp)pp.textContent='';
  const ft=$('fileTree');if(ft)ft.style.display='';
  _previewCurrentPath='';_previewCurrentMode='';_previewDirty=false;
  // Restore directory breadcrumb after closing file preview
  if(typeof renderBreadcrumb==='function') renderBreadcrumb();
  if(closePanelAfter)closeWorkspacePanel();
  else syncWorkspacePanelUI();
}
$('btnClearPreview').onclick=handleWorkspaceClose;
// workspacePath click handler removed -- use topbar workspace chip dropdown instead
$('modelSelect').onchange=async()=>{
  if(!S.session)return;
  const selectedModel=$('modelSelect').value;
  if(typeof closeModelDropdown==='function') closeModelDropdown();
  localStorage.setItem('hermes-webui-model', selectedModel);
  await api('/api/session/update',{method:'POST',body:JSON.stringify({session_id:S.session.session_id,workspace:S.session.workspace,model:selectedModel})});
  S.session.model=selectedModel;
  if(typeof syncModelChip==='function') syncModelChip();
  syncTopbar();
  // Warn if selected model belongs to a different provider than what Hermes is configured for
  if(typeof _checkProviderMismatch==='function'){
    const warn=_checkProviderMismatch(selectedModel);
    if(warn&&typeof showToast==='function') showToast(warn,4000);
  }
  // Clarify scope: composer model changes are session-local, not the global default.
  if(typeof showToast==='function'){
    showToast(t('model_scope_toast')||'Applies to this conversation from your next message.', 3000);
  }
};
$('msg').addEventListener('input',()=>{
  autoResize();
  updateSendBtn();
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
      if(e.isComposing){return;}
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
    if(e.isComposing){return;}
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
    // If the current session has no messages, just focus the composer rather than
    // creating another empty session that will clutter the sidebar list (#1171).
    if(S.session&&(S.session.message_count||0)===0){$('msg').focus();return;}
    if(!S.busy){await newSession();await renderSessionList();closeMobileSidebar();$('msg').focus();}
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
  const imageItems=items.filter(i=>i.type.startsWith('image/'));
  if(!imageItems.length)return;
  e.preventDefault();
  const files=imageItems.map(i=>{
    const blob=i.getAsFile();
    const ext=i.type.split('/')[1]||'png';
    return new File([blob],`screenshot-${Date.now()}.${ext}`,{type:i.type});
  });
  addFiles(files);
  setStatus(t('image_pasted')+files.map(f=>f.name).join(', '));
});
document.querySelectorAll('.suggestion').forEach(btn=>{
  btn.onclick=()=>{$('msg').value=btn.dataset.msg;send();};
});

window.addEventListener('resize',()=>{
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
    const saved = localStorage.getItem(storageKey);
    if(saved) targetEl.style.width = saved + 'px';

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
const _SKINS=[
  {name:'Default',  colors:['#FFD700','#FFBF00','#CD7F32']},
  {name:'Ares',     colors:['#FF4444','#CC3333','#992222']},
  {name:'Mono',     colors:['#CCCCCC','#999999','#666666']},
  {name:'Slate',    colors:['#334155','#475569','#64748b']},
  {name:'Poseidon', colors:['#0EA5E9','#0284C7','#0369A1']},
  {name:'Sisyphus', colors:['#A78BFA','#8B5CF6','#7C3AED']},
  {name:'Charizard',colors:['#FB923C','#F97316','#EA580C']},
];
const _VALID_THEMES=new Set(['system','dark','light']);
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

function _setResolvedTheme(isDark){
  document.documentElement.classList.toggle('dark',!!isDark);
  const link=document.getElementById('prism-theme');
  if(!link) return;
  const want=isDark
    ?'https://cdn.jsdelivr.net/npm/prismjs@1.29.0/themes/prism-tomorrow.min.css'
    :'https://cdn.jsdelivr.net/npm/prismjs@1.29.0/themes/prism.min.css';
  // No SRI integrity on theme CSS — jsdelivr edge nodes serve different
  // digests for the same pinned version, causing intermittent blocking (#1100).
  if(link.href!==want){ link.integrity=''; link.href=want; }
}

function _applyTheme(name){
  const normalized=_normalizeAppearance(name,'default');
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
  if(typeof _markSettingsDirty==='function') _markSettingsDirty();
  const hidden=$('settingsTheme');
  if(hidden) hidden.value=appearance.theme;
  const skinHidden=$('settingsSkin');
  if(skinHidden) skinHidden.value=appearance.skin;
}

function _pickSkin(name){
  const appearance=_normalizeAppearance(localStorage.getItem('hermes-theme'),name);
  localStorage.setItem('hermes-theme',appearance.theme);
  localStorage.setItem('hermes-skin',appearance.skin);
  _applyTheme(appearance.theme);
  _applySkin(appearance.skin);
  _syncThemePicker(appearance.theme);
  _syncSkinPicker(appearance.skin);
  if(typeof _markSettingsDirty==='function') _markSettingsDirty();
  const hidden=$('settingsSkin');
  if(hidden) hidden.value=appearance.skin;
  const themeHidden=$('settingsTheme');
  if(themeHidden) themeHidden.value=appearance.theme;
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
  if(typeof _markSettingsDirty==='function') _markSettingsDirty();
  const hidden=$('settingsFontSize');
  if(hidden) hidden.value=size;
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
    window._showCliSessions=!!s.show_cli_sessions;
    window._soundEnabled=!!s.sound_enabled;
    window._notificationsEnabled=!!s.notifications_enabled;
    window._showThinking=s.show_thinking!==false;
    window._sidebarDensity=(s.sidebar_density==='detailed'?'detailed':'compact');
    window._busyInputMode=(s.busy_input_mode||'queue');
    window._botName=s.bot_name||'Hermes';
    if(s.default_model) window._defaultModel=s.default_model;
    // Persist default workspace so the blank new-chat page can show it
    // and workspace actions (New file/folder) work before the first session (#804).
    if(s.default_workspace) S._profileDefaultWorkspace=s.default_workspace;
    const appearance=_normalizeAppearance(s.theme,s.skin);
    localStorage.setItem('hermes-theme',appearance.theme);
    _applyTheme(appearance.theme);
    localStorage.setItem('hermes-skin',appearance.skin);
    _applySkin(appearance.skin);
    if(typeof setLocale==='function'){
      const _lang=typeof resolvePreferredLocale==='function'
        ? resolvePreferredLocale(s.language, localStorage.getItem('hermes-lang'))
        : (s.language || localStorage.getItem('hermes-lang') || 'en');
      setLocale(_lang);
      if(typeof applyLocaleToDOM==='function')applyLocaleToDOM();
    }
    applyBotName();
  }catch(e){
    window._sendKey='enter';
    window._showTokenUsage=false;
    window._showCliSessions=false;
    window._soundEnabled=false;
    window._notificationsEnabled=false;
    window._showThinking=true;
    window._sidebarDensity='compact';
    window._busyInputMode='queue';
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
  }
  // Non-blocking update check (fire-and-forget, once per tab session)
  // ?test_updates=1 in URL forces banner display for testing (bypasses sessionStorage guards)
  const _testUpdates=new URLSearchParams(location.search).get('test_updates')==='1';
  if(_testUpdates||(_bootSettings.check_for_updates!==false&&!sessionStorage.getItem('hermes-update-checked')&&!sessionStorage.getItem('hermes-update-dismissed'))){
    const _checkUrl='/api/updates/check'+(_testUpdates?'?simulate=1':'');
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
    const savedModel=localStorage.getItem('hermes-webui-model');
    if(savedModel && $('modelSelect')){
      $('modelSelect').value=savedModel;
      // If the value didn't take (model not in list), clear the bad pref
      if($('modelSelect').value!==savedModel) localStorage.removeItem('hermes-webui-model');
      else if(typeof syncModelChip==='function') syncModelChip();
    }
    if(S.session) syncTopbar();
  }).catch(()=>{});
  window._modelDropdownReady=_modelDropdownReady;
  // Pre-load workspace list so sidebar name is correct from first render
  await loadWorkspaceList();
  await loadOnboardingWizard();
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
  const saved=localStorage.getItem('hermes-webui-session');
  if(saved){
    try{
      await loadSession(saved);
      // If the restored session has no messages it is an ephemeral scratch pad —
      // treat the page as a fresh start rather than resuming a blank conversation.
      // loadSession() already ran, so loadDir() has populated the workspace file tree.
      // Do NOT remove the session ID from localStorage — keeping it means every
      // subsequent refresh will also run loadSession() → loadDir() → files stay visible.
      // Removing it here caused the file tree to go blank on the second refresh
      // because the "no saved session" path never calls loadDir (#workspace-files).
      if(S.session && (S.session.message_count||0) === 0){
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
})();

// Fix #822 (bfcache path): when the browser restores the page from the
// back-forward cache, the async boot IIFE above does NOT re-run, but the
// DOM — including any stale value in #sessionSearch — IS restored.  A
// prior search string would silently hide all sessions via the filter in
// renderSessionListFromCache().  Clear the field and re-run the full layout
// sync whenever the page is restored from cache (`event.persisted === true`).
// Fix #1045: also re-run topbar/workspace/panel state so the rail and layout
// chrome aren't left in the stale bfcache snapshot.
window.addEventListener('pageshow', (event) => {
  if (!event.persisted) return;  // fresh loads are handled by the IIFE above
  const _srch = document.getElementById('sessionSearch');
  if (_srch) _srch.value = '';
  // Close any dropdowns/popovers that were open when the user navigated away.
  // bfcache freezes DOM state, so a dropdown left open remains open on restore.
  if (typeof closeModelDropdown === 'function') try { closeModelDropdown(); } catch (_) {}
  if (typeof closeReasoningDropdown === 'function') try { closeReasoningDropdown(); } catch (_) {}
  if (typeof closeWsDropdown === 'function') try { closeWsDropdown(); } catch (_) {}
  if (typeof closeProfileDropdown === 'function') try { closeProfileDropdown(); } catch (_) {}
  // Re-synchronise layout chrome that the boot IIFE sets up but bfcache
  // doesn't re-run. Each call is guarded so missing helpers degrade silently.
  if (typeof syncTopbar === 'function') try { syncTopbar(); } catch (_) {}
  if (typeof syncWorkspacePanelState === 'function') try { syncWorkspacePanelState(); } catch (_) {}
  if (typeof renderSessionListFromCache === 'function') {
    try { renderSessionListFromCache(); } catch (_) {}
  }
  // Restart the gateway SSE watcher — the persisted connection is dead after bfcache
  if (typeof startGatewaySSE === 'function') try { startGatewaySSE(); } catch (_) {}
});
