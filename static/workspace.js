async function api(path,opts={}){
  const res=await fetch(path,{headers:{'Content-Type':'application/json'},credentials:'include',...opts});
  if(!res.ok)throw new Error(await res.text());
  const ct=res.headers.get('content-type')||'';
  return ct.includes('application/json')?res.json():res.text();
}

let _currentDir = '.';
let _lastDirWorkspace = null; // tracks which workspace the current file tree belongs to

async function loadDir(path){
  if(!S.session)return;
  // Skip if workspace and path are unchanged
  const ws=S.session.workspace||'';
  if(path===_currentDir && ws===_lastDirWorkspace) return;
  // Pre-load label cache so getFolderLabel() works synchronously in renderFileTree
  await _ensureLabelCache();
  try{
    const data=await api(`/api/list?session_id=${encodeURIComponent(S.session.session_id)}&path=${encodeURIComponent(path)}`);
    _currentDir = path;
    _lastDirWorkspace = ws;
    S.entries=data.entries||[];renderFileTree();
    // Show/hide back button depending on whether we're at root
    const isRoot=(path==='.'||path===''||path==='/');
    const btn=document.getElementById('btnDirUp');
    if(btn) btn.style.display = isRoot ? 'none' : '';
    // Update breadcrumb in panel header
    _updateExplorerBreadcrumb(path, isRoot);
    // Persist dir in URL so refresh restores position
    _syncDirToUrl(path);
  }catch(e){console.warn('loadDir',e);}
}

function _updateExplorerBreadcrumb(path, isRoot){
  const el=document.getElementById('explorerBreadcrumb');
  if(!el)return;
  if(isRoot){
    el.textContent='Explorer';
    return;
  }
  // Build breadcrumb: Explorer > part1 > part2 ...
  const parts=path.replace(/^\.?\//,'').replace(/\/+$/,'').split('/').filter(Boolean);
  el.innerHTML='';
  const root=document.createElement('span');
  root.textContent='Explorer';
  root.style.cssText='cursor:pointer;opacity:.6';
  root.onclick=()=>loadDir('.');
  el.appendChild(root);
  let cumPath='';
  parts.forEach((part,i)=>{
    cumPath=cumPath?cumPath+'/'+part:part;
    const sep=document.createElement('span');
    sep.textContent=' / ';
    sep.style.cssText='opacity:.4;margin:0 2px;';
    el.appendChild(sep);
    const seg=document.createElement('span');
    seg.textContent=part;
    const isCurrent=(i===parts.length-1);
    seg.style.cssText=isCurrent?'font-weight:600':'cursor:pointer;opacity:.6';
    if(!isCurrent){
      const capPath=cumPath;
      seg.onclick=()=>loadDir(capPath);
    }
    el.appendChild(seg);
  });
}

function _syncDirToUrl(path){
  const sp=new URLSearchParams(location.search);
  if(path==='.'||path===''||path==='/'){
    sp.delete('dir');
  } else {
    sp.set('dir',path);
  }
  const qs=sp.toString();
  const newUrl=qs?'?'+qs:location.pathname;
  if(location.search!==('?'+qs)) history.replaceState(history.state,'',newUrl);
}

function dirUp(){
  if(_currentDir==='.'||_currentDir===''||_currentDir==='/') return;
  const parts=_currentDir.replace(/\/+$/,'').split('/');
  parts.pop();
  const parent=parts.length===0?'.':parts.join('/');
  loadDir(parent);
}

// File extension sets for preview routing (must match server-side sets)
const IMAGE_EXTS  = new Set(['.png','.jpg','.jpeg','.gif','.svg','.webp','.ico','.bmp']);
const MD_EXTS     = new Set(['.md','.markdown','.mdown']);
// Binary formats that should download rather than preview
const DOWNLOAD_EXTS = new Set([
  '.docx','.doc','.xlsx','.xls','.pptx','.ppt','.odt','.ods','.odp',
  '.pdf','.zip','.tar','.gz','.bz2','.7z','.rar',
  '.mp3','.mp4','.wav','.m4a','.ogg','.flac','.mov','.avi','.mkv','.webm',
  '.exe','.dmg','.pkg','.deb','.rpm',
  '.woff','.woff2','.ttf','.otf','.eot',
  '.bin','.dat','.db','.sqlite','.pyc','.class','.so','.dylib','.dll',
]);

function fileExt(p){ const i=p.lastIndexOf('.'); return i>=0?p.slice(i).toLowerCase():''; }

let _previewCurrentPath = '';  // relative path of currently previewed file
let _previewCurrentMode = '';  // 'code' | 'md' | 'image'
let _previewDirty = false;     // true when edits are unsaved

function showPreview(mode){
  // mode: 'code' | 'image' | 'md'
  $('previewCode').style.display     = mode==='code'  ? '' : 'none';
  $('previewImgWrap').style.display  = mode==='image' ? '' : 'none';
  $('previewMd').style.display       = mode==='md'    ? '' : 'none';
  $('previewEditArea').style.display = 'none';  // start in read-only
  const badge=$('previewBadge');
  badge.className='preview-badge '+mode;
  badge.textContent = mode==='image'?'image':mode==='md'?'md':fileExt($('previewPathText')?.textContent||_previewCurrentPath)||'text';
  _previewCurrentMode = mode;
  _previewDirty = false;
  updateEditBtn();
}

function updateEditBtn(){
  const btn=$('btnEditFile');
  if(!btn)return;
  const editable = _previewCurrentMode==='code'||_previewCurrentMode==='md';
  btn.style.display = editable?'':'none';
  btn.innerHTML = '<i class="fas fa-code"></i>';
  btn.title = 'Open in VS Code';
  btn.style.color = '';
}

async function toggleEditMode(){
  // Open in VS Code instead of inline editing
  if(!S.session||!_previewCurrentPath) return;
  try{
    await api('/api/file/open-in-vscode',{method:'POST',body:JSON.stringify({
      session_id:S.session.session_id, path:_previewCurrentPath
    })});
    showToast('Opening in VS Code...');
  }catch(e){
    setStatus('VS Code open failed: '+e.message);
  }
}

let _previewRawContent = '';  // raw text for md files (to populate editor)

function cancelEditMode(){
  // Discard changes and return to read-only view
  $('previewEditArea').style.display='none';
  $('previewEditArea').onkeydown=null;
  if(_previewCurrentMode==='code') $('previewCode').style.display='';
  else $('previewMd').style.display='';
  _previewDirty=false;
  updateEditBtn();
}

async function openFile(path){
  if(!S.session)return;
  const ext=fileExt(path);

  // Binary/download-only formats: trigger browser download, don't preview
  if(DOWNLOAD_EXTS.has(ext)){
    downloadFile(path);
    return;
  }

  // Show filename in header, path as breadcrumb below
  const parts=path.split('/');
  const filename=parts[parts.length-1];
  const dirPart=parts.length>1?parts.slice(0,-1).join('/'):'';
  if($('previewFilename')) $('previewFilename').textContent=filename;
  const crumb=$('previewPathBreadcrumb');
  if(crumb){ crumb.textContent=dirPart?'WORKSPACE / '+dirPart.replace(/\//g,' / '):'WORKSPACE'; crumb.style.display=''; }
  // Keep previewPathText in sync (used by toggleEditMode etc)
  if($('previewPathText')) $('previewPathText').textContent=path;
  $('previewArea').classList.add('visible');
  $('fileTree').style.display='none';
  // Hide back (up) button while viewing file -- it navigates the file tree not the file
  const upBtn=document.getElementById('btnDirUp');
  if(upBtn) upBtn.style.display='none';
  const closeBtn=document.getElementById('btnClearPreview');
  if(closeBtn) closeBtn.style.display='';

  _previewCurrentPath = path;
  if(IMAGE_EXTS.has(ext)){
    // Image: load via raw endpoint, show as <img>
    showPreview('image');
    const url=`/api/file/raw?session_id=${encodeURIComponent(S.session.session_id)}&path=${encodeURIComponent(path)}`;
    $('previewImg').alt=path;
    $('previewImg').src=url;
    $('previewImg').onerror=()=>setStatus('Could not load image');
  } else if(MD_EXTS.has(ext)){
    // Markdown: fetch text, render with renderMd, display as formatted HTML
    try{
      const data=await api(`/api/file?session_id=${encodeURIComponent(S.session.session_id)}&path=${encodeURIComponent(path)}`);
      showPreview('md');
      _previewRawContent = data.content;
      $('previewMd').innerHTML=renderMd(data.content);
    }catch(e){setStatus('Could not open file');}
  } else {
    // Plain code / text -- but fall back to download if server signals binary
    try{
      const data=await api(`/api/file?session_id=${encodeURIComponent(S.session.session_id)}&path=${encodeURIComponent(path)}`);
      if(data.binary){
        // Server flagged this as binary content
        downloadFile(path);
        return;
      }
      showPreview('code');
      $('previewCode').textContent=data.content;
    }catch(e){
      // If it's a 400/too-large error, offer download instead
      downloadFile(path);
    }
  }
}

function downloadFile(path){
  if(!S.session)return;
  // Trigger browser download via the raw file endpoint with content-disposition attachment
  const url=`/api/file/raw?session_id=${encodeURIComponent(S.session.session_id)}&path=${encodeURIComponent(path)}&download=1`;
  const filename=path.split('/').pop();
  const a=document.createElement('a');
  a.href=url;a.download=filename;
  document.body.appendChild(a);a.click();
  setTimeout(()=>document.body.removeChild(a),100);
  showToast(`Downloading ${filename}\u2026`,2000);
}

