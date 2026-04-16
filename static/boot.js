async function cancelStream(){
  const streamId = S.activeStreamId;
  if(!streamId) return;
  try{
    await fetch(new URL(`api/chat/cancel?stream_id=${encodeURIComponent(streamId)}`,location.href).href,{credentials:'include'});
  }catch(e){/* cancel request failed — cleanup below still runs */}
  // Clear status unconditionally after the cancel request completes.
  // The SSE cancel event may also fire, but if the connection is already
  // closed it won't arrive — so we handle cleanup here as the guaranteed path.
  const btn=$('btnCancel');if(btn)btn.style.display='none';
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
    _setWorkspacePanelMode('closed');
    return;
  }
  _setWorkspacePanelMode(_workspacePanelMode==='preview'?'closed':_workspacePanelMode);
}

function openWorkspacePanel(mode='browse'){
  if(mode==='browse'&&!S.session&&!_hasWorkspacePreviewVisible())return;
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
  const canBrowse=!!S.session||_hasWorkspacePreviewVisible();
  const hasPreview=_hasWorkspacePreviewVisible();
  if(toggleBtn){
    toggleBtn.classList.toggle('active',isOpen);
    toggleBtn.setAttribute('aria-pressed',isOpen?'true':'false');
    toggleBtn.title=isOpen?'Скрыть панель файлов':'Показать панель файлов';
    toggleBtn.disabled=!canBrowse;
  }
  if(collapseBtn){
    collapseBtn.title=isCompact?'Закрыть панель файлов':'Скрыть панель файлов';
  }
  const hasSession=!!S.session;
  ['btnUpDir','btnNewFile','btnNewFolder','btnRefreshPanel'].forEach(id=>{
    const el=$(id);
    if(el)el.disabled=!hasSession;
  });
  const clearBtn=$('btnClearPreview');
  if(clearBtn){
    clearBtn.disabled=!isOpen;
    clearBtn.title=hasPreview?'Закрыть предпросмотр':'Скрыть панель файлов';
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

  const btn=$('btnMic');
  const status=$('micStatus');
  const ta=$('msg');
  const statusText=status?status.querySelector('.status-text'):null;
  btn.style.display=''; // Show button — browser supports speech recognition or recording fallback

  let recognition=SpeechRecognition?new SpeechRecognition():null;
  let mediaRecorder=null;
  let mediaStream=null;
  let audioChunks=[];
  let _finalText='';
  let _prefix='';

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

  if(recognition){
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
      const msgs={
        'not-allowed':t('mic_denied'),
        'no-speech':t('mic_no_speech'),
        'network':t('mic_network'),
      };
      showToast(msgs[event.error]||t('mic_error')+event.error);
    };
  }

  btn.onclick=async()=>{
    if(window._micActive){
      _stopMic();
      return;
    }
    _finalText='';
    _prefix=ta.value;
    if(recognition){
      recognition.start();
      _setRecording(true);
      return;
    }
    if(!_canRecordAudio){
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
        _setRecording(false);
        window._micPendingSend=false;
        _stopTracks();
        showToast(t('mic_network'));
      };
      mediaRecorder.onstop=async()=>{
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
      window._micPendingSend=false;
      _stopTracks();
      showToast(t('mic_denied'));
    }
  };
})();
window._micActive=window._micActive||false;
window._micPendingSend=window._micPendingSend||false;
$('fileInput').onchange=e=>{addFiles(Array.from(e.target.files));e.target.value='';};
$('btnNewChat').onclick=async()=>{await newSession();await renderSessionList();closeMobileSidebar();$('msg').focus();};
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
      const overlay=$('settingsOverlay');
      if(overlay) overlay.style.display='none';
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
  // Notify user that model changes only take effect in the next conversation (#419)
  if(S.messages && S.messages.length > 0 && typeof showToast==='function'){
    showToast('Model change takes effect in your next conversation', 3000);
  }
};
$('msg').addEventListener('input',()=>{
  autoResize();
  updateSendBtn();
  const text=$('msg').value;
  if(text.startsWith('/')&&text.indexOf('\n')===-1){
    const prefix=text.slice(1);
    const matches=getMatchingCommands(prefix);
    if(matches.length)showCmdDropdown(matches); else hideCmdDropdown();
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
    if(!S.busy){await newSession();await renderSessionList();closeMobileSidebar();$('msg').focus();}
  }
  if(e.key==='Escape'){
    // Close onboarding overlay if open (skip/dismiss the wizard)
    const onboardingOverlay=$('onboardingOverlay');
    if(onboardingOverlay&&onboardingOverlay.style.display!=='none'){
      if(typeof skipOnboarding==='function') skipOnboarding();
      return;
    }
    // Close settings overlay if open
    const settingsOverlay=$('settingsOverlay');
    if(settingsOverlay&&settingsOverlay.style.display!=='none'){_closeSettingsPanel();return;}
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

// ── System theme helper ──────────────────────────────────────────────────────
function _applyTheme(name){
  const resolved=(name==='system')
    ?(window.matchMedia('(prefers-color-scheme:dark)').matches?'dark':'light')
    :name;
  document.documentElement.dataset.theme=resolved||'dark';
  // Swap Prism syntax-highlighting theme to match UI theme
  (function(){
    const link=document.getElementById('prism-theme');
    if(!link) return;
    const isDark=(resolved!=='light');
    const want=isDark
      ?'https://cdn.jsdelivr.net/npm/prismjs@1.29.0/themes/prism-tomorrow.min.css'
      :'https://cdn.jsdelivr.net/npm/prismjs@1.29.0/themes/prism.min.css';
    if(link.href!==want){ link.href=want; }
  })();
  // Re-register OS change listener whenever system theme is active
  if(name==='system'){
    const mq=window.matchMedia('(prefers-color-scheme:dark)');
    const _onOsChange=()=>{ document.documentElement.dataset.theme=mq.matches?'dark':'light'; };
    mq.removeEventListener('change',_onOsChange);
    mq.addEventListener('change',_onOsChange);
  }
}

function applyBotName(){
  const name=window._botName||'Hermes';
  document.title=name;
  const sidebarH1=document.querySelector('.sidebar-header h1');
  if(sidebarH1) sidebarH1.textContent=name;
  const logo=document.querySelector('.sidebar-header .logo');
  if(logo) logo.textContent=name.charAt(0).toUpperCase();
  const topbarTitle=$('topbarTitle');
  if(topbarTitle && (!S.session)) topbarTitle.textContent=name;
  const msg=$('msg');
  if(msg) msg.placeholder='Сообщение '+name+'\u2026';
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
    window._botName=s.bot_name||'Hermes';
    const _theme=s.theme||'dark';
    localStorage.setItem('hermes-theme',_theme);
    _applyTheme(_theme);
    document.body.classList.toggle('bubble-layout',!!s.bubble_layout);
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
    window._botName='Hermes';
    _bootSettings={check_for_updates:false};
    document.body.classList.remove('bubble-layout');
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
  if(profileLabel) profileLabel.textContent=((S.activeProfile||'default')==='default'?'По умолчанию':(S.activeProfile||'default'));
  // Fetch available models from server and populate dropdown dynamically
  await populateModelDropdown();
  // Restore last-used model preference
  const savedModel=localStorage.getItem('hermes-webui-model');
  if(savedModel && $('modelSelect')){
    $('modelSelect').value=savedModel;
    // If the value didn't take (model not in list), clear the bad pref
    if($('modelSelect').value!==savedModel) localStorage.removeItem('hermes-webui-model');
  }
  // Pre-load workspace list so sidebar name is correct from first render
  await loadWorkspaceList();
  await loadOnboardingWizard();
  _initResizePanels();
  // Workspace panel restore happens AFTER loadSession so we know if
  // the session has a workspace — prevents the snap-open-then-closed flash (#576).
  const saved=localStorage.getItem('hermes-webui-session');
  if(saved){
    try{
      await loadSession(saved);
      // Only restore the panel from localStorage when the session actually has a workspace.
      // Without this guard, sessions without a workspace snap open then immediately closed.
      if(S.session&&S.session.workspace&&localStorage.getItem('hermes-webui-workspace-panel')==='open'){
        _workspacePanelMode='browse';
      }
      syncWorkspacePanelState();await renderSessionList();if(typeof startGatewaySSE==='function')startGatewaySSE();await checkInflightOnBoot(saved);return;}
    catch(e){localStorage.removeItem('hermes-webui-session');}
  }
  // no saved session - show empty state, wait for user to hit +
  syncTopbar();
  syncWorkspacePanelState();
  $('emptyState').style.display='';
  await renderSessionList();
  // Start real-time gateway session sync if setting is enabled
  if(typeof startGatewaySSE==='function') startGatewaySSE();
})();
