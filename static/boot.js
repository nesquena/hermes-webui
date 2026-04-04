// ── Restore panel collapse states before boot overlay hides ──────────────────
(function(){
  if(localStorage.getItem('hermes-sidebar-collapsed')==='1'){
    const sb=document.querySelector('.sidebar');
    if(sb) sb.classList.add('collapsed');
  }
  if(localStorage.getItem('hermes-rightpanel-collapsed')==='1'){
    const rp=document.querySelector('.rightpanel');
    if(rp) rp.classList.add('collapsed');
  }
})();

async function cancelStream(){
  const streamId = S.activeStreamId;
  if(!streamId) return;
  try{
    await fetch(`/api/chat/cancel?stream_id=${encodeURIComponent(streamId)}`,{credentials:'include'});
    const btn=$('btnCancel');if(btn)btn.style.display='none';
    setStatus('Cancelling…');
  }catch(e){setStatus('Cancel failed: '+e.message);}
}

$('btnSend').onclick=send;
$('btnAttach').onclick=()=>$('fileInput').click();
$('fileInput').onchange=e=>{addFiles(Array.from(e.target.files));e.target.value='';};
$('btnNewChat').onclick=async()=>{await newSession();await renderSessionList();$('msg').focus();};
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
      showToast('Session imported');
    }
  }catch(err){
    showToast('Import failed: '+(err.message||'Invalid JSON'));
  }
};
// btnRefreshFiles is now panel-icon-btn in header (see HTML)
$('btnClearPreview').onclick=()=>{
  $('previewArea').classList.remove('visible');
  $('previewImg').src='';
  $('previewMd').innerHTML='';
  $('previewCode').textContent='';
  if($('previewPathText')) $('previewPathText').textContent='';
  if($('previewFilename')) $('previewFilename').textContent='';
  const crumb=$('previewPathBreadcrumb'); if(crumb) crumb.style.display='none';
  $('fileTree').style.display='';
  $('btnClearPreview').style.display='none';
  // Restore the up button (visible state based on current directory)
  const upBtn=document.getElementById('btnDirUp');
  if(upBtn) upBtn.style.display=(typeof _currentDir!=='undefined'&&_currentDir&&_currentDir!=='.'&&_currentDir!==''&&_currentDir!=='/')?'':'none';
};
// workspacePath click handler removed -- use topbar workspace chip dropdown instead
$('modelSelect').onchange=async()=>{
  if(!S.session)return;
  const selectedModel=$('modelSelect').value;
  localStorage.setItem('hermes-webui-model', selectedModel);
  await api('/api/session/update',{method:'POST',body:JSON.stringify({session_id:S.session.session_id,workspace:S.session.workspace,model:selectedModel})});
  S.session.model=selectedModel;syncTopbar();
  // Sync custom selector label
  syncModelCSelect();
};

// ── Custom model selector ────────────────────────────────────────────────────
function buildModelCSelect(){
  const sel=$('modelSelect');
  const menu=$('modelCSelectMenu');
  if(!sel||!menu) return;
  menu.innerHTML='';
  const groups=sel.querySelectorAll('optgroup');
  if(groups.length){
    groups.forEach((g,gi)=>{
      if(gi>0){const d=document.createElement('div');d.className='model-cselect-divider';menu.appendChild(d);}
      const gl=document.createElement('div');gl.className='model-cselect-group-label';gl.textContent=g.label;menu.appendChild(gl);
      const grp=document.createElement('div');grp.className='model-cselect-group';
      g.querySelectorAll('option').forEach(o=>{
        const el=document.createElement('div');el.className='model-cselect-opt';el.textContent=o.textContent;el.dataset.value=o.value;
        if(o.value===sel.value) el.classList.add('selected');
        el.onclick=()=>selectModelCSelect(o.value);
        grp.appendChild(el);
      });
      menu.appendChild(grp);
    });
  } else {
    // Flat option list (no groups)
    const grp=document.createElement('div');grp.className='model-cselect-group';
    sel.querySelectorAll('option').forEach(o=>{
      const el=document.createElement('div');el.className='model-cselect-opt';el.textContent=o.textContent;el.dataset.value=o.value;
      if(o.value===sel.value) el.classList.add('selected');
      el.onclick=()=>selectModelCSelect(o.value);
      grp.appendChild(el);
    });
    menu.appendChild(grp);
  }
  syncModelCSelect();
}
function syncModelCSelect(){
  const sel=$('modelSelect');
  const lbl=$('modelCSelectLabel');
  if(!sel||!lbl) return;
  const cur=sel.options[sel.selectedIndex];
  lbl.textContent=cur?cur.textContent:sel.value;
  // Mark selected option
  document.querySelectorAll('.model-cselect-opt').forEach(el=>{
    el.classList.toggle('selected',el.dataset.value===sel.value);
  });
}
function selectModelCSelect(val){
  const sel=$('modelSelect');
  sel.value=val;
  sel.dispatchEvent(new Event('change'));
  closeModelCSelect();
}
function toggleModelCSelect(){
  const c=$('modelCSelect');
  if(c.classList.contains('open')) closeModelCSelect();
  else c.classList.add('open');
}
function closeModelCSelect(){
  const c=$('modelCSelect');
  if(c) c.classList.remove('open');
}

// Sidebar bottom (model/workspace/transcript) collapsible
(function initSidebarBottom(){
  const collapsed=localStorage.getItem('sidebarBottomCollapsed')==='1';
  if(collapsed) _applySidebarBottomState(true);
})();
function _applySidebarBottomState(collapsed){
  const body=$('sidebarBottomBody');
  const caret=$('sidebarBottomCaret');
  if(!body)return;
  body.style.display=collapsed?'none':'';
  if(caret) caret.style.transform=collapsed?'rotate(180deg)':'';
}
function toggleSidebarBottom(){
  const body=$('sidebarBottomBody');
  if(!body)return;
  const collapsed=body.style.display==='none';
  _applySidebarBottomState(!collapsed);
  localStorage.setItem('sidebarBottomCollapsed',collapsed?'0':'1');
}
// Close on outside click
document.addEventListener('click',e=>{
  if(!e.target.closest('#modelCSelect')) closeModelCSelect();
});
$('msg').addEventListener('input',autoResize);
$('msg').addEventListener('keydown',e=>{if(e.key==='Enter'&&!e.shiftKey){e.preventDefault();send();}});
// B14: Cmd/Ctrl+K creates a new chat from anywhere
document.addEventListener('keydown',async e=>{
  if((e.metaKey||e.ctrlKey)&&e.key==='k'){
    e.preventDefault();
    if(!S.busy){await newSession();await renderSessionList();$('msg').focus();}
  }
  if(e.key==='Escape'){
    // Close git modal if open
    const gitModal=$('gitModal');
    if(gitModal&&gitModal.style.display!=='none'){gitModal.style.display='none';return;}
    // Close settings overlay if open
    const settingsOverlay=$('settingsOverlay');
    if(settingsOverlay&&settingsOverlay.style.display!=='none'){toggleSettings();return;}
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
  setStatus(`Image pasted: ${files.map(f=>f.name).join(', ')}`);
});
document.querySelectorAll('.suggestion').forEach(btn=>{
  btn.onclick=()=>{$('msg').value=btn.dataset.msg;send();};
});

// Quick action starter buttons on empty state
const QA_PROMPTS = {
  feature: `I want to implement a new feature. Let's start by gathering context.\n\nWhat type of work is this? Feature.`,
  debug:   `I have a bug to investigate and fix. Let's start by gathering context.\n\nWhat type of work is this? Debug.`,
  plan:    `I want to map out a masterplan before starting implementation. Let's begin.\n\nWhat type of work is this? Plan / Refactor.`,
};
function qaStart(type) {
  const prompt = QA_PROMPTS[type];
  if (!prompt) return;
  const ta = $('msg');
  ta.value = prompt;
  autoResize();
  ta.focus();
  ta.setSelectionRange(prompt.length, prompt.length);
}

// Boot: restore last session or start fresh
// ── Resizable panels ──────────────────────────────────────────────────────
(function(){
  const SIDEBAR_MIN=180, SIDEBAR_MAX=420;
  const PANEL_MIN=180,   PANEL_MAX=500;

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

// Handle browser back/forward -- restore session from popstate
window.addEventListener('popstate',async(e)=>{
  const sid=(e.state&&e.state.s)||new URLSearchParams(location.search).get('s');
  if(sid){
    try{await loadSession(sid);renderSessionListFromCache();}
    catch(err){console.warn('popstate loadSession failed',err);}
  }
});

(async()=>{
  // Fetch available models from server and populate dropdown dynamically
  await populateModelDropdown();
  buildModelCSelect();
  // Restore last-used model preference
  const savedModel=localStorage.getItem('hermes-webui-model');
  if(savedModel && $('modelSelect')){
    $('modelSelect').value=savedModel;
    // If the value didn't take (model not in list), clear the bad pref
    if($('modelSelect').value!==savedModel) localStorage.removeItem('hermes-webui-model');
  }
  syncModelCSelect();
  // Pre-load workspace list so sidebar name is correct from first render
  await loadWorkspaceList();
  _initResizePanels();
  // Restore session from URL ?s= param (URL is now the only source of truth)
  const _bootParams=new URLSearchParams(location.search);
  const urlSid=_bootParams.get('s');
  const urlDir=_bootParams.get('dir');
  if(urlSid){
    try{
      await loadSession(urlSid);
      await renderSessionList();
      // Restore dir position after session is loaded so workspace is set
      if(urlDir) await loadDir(urlDir).catch(()=>loadDir('.'));
      await checkInflightOnBoot(urlSid);
    }
    catch(e){
      history.replaceState({},'','/');
      await renderSessionList();
    }
  } else {
    // no saved session - show empty state, wait for user to hit +
    $('emptyState').style.display='';
    await renderSessionList();
  }
  // Dismiss boot overlay - all data is ready, no more flicker
  const ov=document.getElementById('bootOverlay');
  if(ov){ov.classList.add('done');setTimeout(()=>ov.remove(),300);}
})();

