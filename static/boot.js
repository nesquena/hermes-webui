async function cancelStream(){
  const streamId = S.activeStreamId;
  if(!streamId) return;
  try{
    await fetch(new URL(`/api/chat/cancel?stream_id=${encodeURIComponent(streamId)}`,location.origin).href,{credentials:'include'});
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
  $('previewPathText').textContent='';
  $('fileTree').style.display='';
};
// workspacePath click handler removed -- use topbar workspace chip dropdown instead
$('modelSelect').onchange=async()=>{
  if(!S.session)return;
  const selectedModel=$('modelSelect').value;
  localStorage.setItem('hermes-webui-model', selectedModel);
  await api('/api/session/update',{method:'POST',body:JSON.stringify({session_id:S.session.session_id,workspace:S.session.workspace,model:selectedModel})});
  S.session.model=selectedModel;syncTopbar();
};
$('msg').addEventListener('input',autoResize);
$('msg').addEventListener('keydown',e=>{if(e.key==='Enter'&&!e.shiftKey){e.preventDefault();send();}});
// B14: Cmd/Ctrl+K creates a new chat from anywhere
document.addEventListener('keydown',async e=>{
  if((e.metaKey||e.ctrlKey)&&e.key==='k'){
    e.preventDefault();
    if(!S.busy){await newSession();await renderSessionList();$('msg').focus();}
  }
  if(e.key==='Escape'){
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

(async()=>{
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
  _initResizePanels();
  const saved=localStorage.getItem('hermes-webui-session');
  if(saved){
    try{await loadSession(saved);await renderSessionList();await checkInflightOnBoot(saved);return;}
    catch(e){localStorage.removeItem('hermes-webui-session');}
  }
  // no saved session - show empty state, wait for user to hit +
  $('emptyState').style.display='';
  await renderSessionList();
})();

