async function api(path,opts={}){
  // Strip leading slash so URL resolves relative to location.href (supports subpath mounts)
  const rel = path.startsWith('/') ? path.slice(1) : path;
  const url=new URL(rel,document.baseURI||location.href);
  // Retry up to 2 times on network errors (e.g. stale keep-alive after long idle).
  // Server errors (4xx/5xx) are NOT retried — only connection failures.
  let lastErr;
  for(let attempt=0;attempt<3;attempt++){
    try{
      const res=await fetch(url.href,{credentials:'include',headers:{'Content-Type':'application/json'},...opts});
      if(!res.ok){
        // 401 means the auth session expired. Redirect to login so the user can
        // re-authenticate. This is especially important for iOS PWA (standalone mode)
        // and for subpath mounts like /hermes/, where /login escapes to the site root.
        if(res.status===401){window.location.href='login?next='+encodeURIComponent(window.location.pathname+window.location.search);return;}
        const text=await res.text();
        // Parse JSON error body and surface the human-readable message,
        // rather than showing raw JSON like {"error":"Profile 'x' does not exist."}
        let message=text;
        try{const j=JSON.parse(text);message=j.error||j.message||text;}catch(e){}
        // Attach the raw HTTP context so callers can branch on status (404 stale-session
        // cleanup, 401 redirect, 503 retry, etc.) without re-parsing the message string.
        const err=new Error(message);
        err.status=res.status;
        err.statusText=res.statusText;
        err.body=text;
        throw err;
      }
      const ct=res.headers.get('content-type')||'';
      return ct.includes('application/json')?res.json():res.text();
    }catch(e){
      lastErr=e;
      // Only retry on network errors (TypeError from fetch), not on HTTP errors
      // that were already thrown above. Re-throw 401 redirects immediately.
      if(e.message&&/401/.test(e.message)) throw e;
      if(attempt<2 && e instanceof TypeError) continue;
      throw e;
    }
  }
  throw lastErr;
}

// Persist/restore expanded directory state per workspace in localStorage
function _wsExpandKey(){
  const ws=S.session&&S.session.workspace;
  return ws?'hermes-webui-expanded:'+ws:null;
}
function _saveExpandedDirs(){
  const key=_wsExpandKey();if(!key)return;
  try{localStorage.setItem(key,JSON.stringify([...(S._expandedDirs||new Set())]));}catch(e){}
}
function _restoreExpandedDirs(){
  const key=_wsExpandKey();
  if(!key){S._expandedDirs=new Set();return;}
  try{
    const raw=localStorage.getItem(key);
    S._expandedDirs=raw?new Set(JSON.parse(raw)):new Set();
  }catch(e){S._expandedDirs=new Set();}
}

function _normalizeWorkspaceDirPath(path){
  return (!path||path==='')?'.':String(path);
}

function _ensureWorkspaceDirMetadata(){
  if(!S._dirCache)S._dirCache={};
  if(!S._dirSignatures)S._dirSignatures={};
  return S._dirSignatures;
}

async function _fetchWorkspaceDir(path){
  const dirPath=_normalizeWorkspaceDirPath(path);
  return api(`/api/list?session_id=${encodeURIComponent(S.session.session_id)}&path=${encodeURIComponent(dirPath)}`);
}

function _storeWorkspaceDirListing(path,data){
  const dirPath=_normalizeWorkspaceDirPath(path);
  const signatures=_ensureWorkspaceDirMetadata();
  const entries=(data&&data.entries)||[];
  if(typeof data?.signature==='string')signatures[dirPath]=data.signature;
  if(dirPath===_normalizeWorkspaceDirPath(S.currentDir))S.entries=entries;
  if(dirPath!=='.')S._dirCache[dirPath]=entries;
  return entries;
}

async function loadDir(path){
  if(!S.session)return;
  try{
    const dirPath=_normalizeWorkspaceDirPath(path);
    if(dirPath==='.'){
      S._dirCache={};
      S._dirSignatures={};
      _restoreExpandedDirs();  // restore per-workspace expanded state on root load
    }
    S.currentDir=dirPath;
    const data=await _fetchWorkspaceDir(dirPath);
    _storeWorkspaceDirListing(dirPath,data);renderBreadcrumb();renderFileTree();
    // Pre-fetch contents of restored expanded dirs so they render without a second click
    // (parallelized — avoids serial waterfall when multiple dirs are expanded)
    if(dirPath==='.'){
      const expanded=S._expandedDirs||new Set();
      const pending=[...expanded].filter(dirPath=>!S._dirCache[dirPath]);
      if(pending.length){
        const results=await Promise.all(pending.map(dirPath=>
          _fetchWorkspaceDir(dirPath)
            .then(dc=>({dirPath,data:dc}))
            .catch(()=>({dirPath,data:{entries:[]}}))
        ));
        for(const {dirPath,data} of results) _storeWorkspaceDirListing(dirPath,data);
      }
      if(expanded.size>0)renderFileTree();
    }
    if(typeof clearPreview==='function'){
      if(typeof _previewDirty!=='undefined'&&_previewDirty){
        showConfirmDialog({title:t('unsaved_confirm'),message:'',confirmLabel:'Discard',danger:true,focusCancel:true}).then(ok=>{if(ok)clearPreview({keepPanelOpen:true});});
      }else{
        clearPreview({keepPanelOpen:true});
      }
    }
    // Fetch git status for workspace root (non-blocking)
    if(dirPath==='.') refreshGitStatus();
  }catch(e){console.warn('loadDir',e);}
}

async function _refreshGitBadge(){
  return refreshGitStatus();
}

const GIT_AUTO_REFRESH_MS = 5000;
const WORKSPACE_TREE_AUTO_REFRESH_MS = 7000;
const WORKSPACE_TREE_AUTO_REFRESH_MAX_DIRS = 50;
const GIT_AUTO_FETCH_MS = 90000;
const GIT_AUTO_FETCH_MIN_ERROR_BACKOFF_MS = 300000;

function _ensureGitState(){
  const scopeKey=`${(S.session&&S.session.session_id)||''}\n${(S.session&&S.session.workspace)||''}`;
  if(!S.git)S.git={status:null,selectedTab:'files',selectedDiff:null,loading:false,syncing:null,generatingCommitMessage:false,mutating:false,selectedPaths:new Set(),selectionTouched:false,selectionKey:scopeKey};
  if(typeof S.git.mutating==='undefined')S.git.mutating=false;
  if(typeof S.git.branchLoading==='undefined')S.git.branchLoading=false;
  if(typeof S.git.lastAutoFetchAt==='undefined')S.git.lastAutoFetchAt=0;
  if(typeof S.git.lastAutoFetchErrorAt==='undefined')S.git.lastAutoFetchErrorAt=0;
  if(typeof S.git.lastAutoFetchError==='undefined')S.git.lastAutoFetchError='';
  if(typeof S.git.autoFetching==='undefined')S.git.autoFetching=false;
  if(typeof S.git.autoFetchFailureCount==='undefined')S.git.autoFetchFailureCount=0;
  if(typeof S.git.branchMenuOpen==='undefined')S.git.branchMenuOpen=false;
  if(typeof S.git.branchFilter==='undefined')S.git.branchFilter='';
  if(typeof S.git.diffMode==='undefined'){
    try{S.git.diffMode=localStorage.getItem('hermes-webui-git-diff-mode')||'';}catch(e){S.git.diffMode='';}
  }
  if(typeof S.git.selectionTouched==='undefined')S.git.selectionTouched=false;
  if(!(S.git.selectedPaths instanceof Set)){
    S.git.selectedPaths=new Set(Array.isArray(S.git.selectedPaths)?S.git.selectedPaths:Object.keys(S.git.selectedPaths||{}).filter(k=>S.git.selectedPaths[k]));
  }
  if(S.git.selectionKey!==scopeKey){
    S.git.selectedPaths=new Set();
    S.git.selectionTouched=false;
    S.git.selectionKey=scopeKey;
  }
  return S.git;
}

function _gitStatusSignature(status){
  if(!status||!status.is_git)return 'not-git';
  const files=(status.files||[]).map(f=>[
    f.path,f.old_path||'',f.status||'',!!f.staged,!!f.unstaged,!!f.untracked,
    !!f.conflict,f.additions||0,f.deletions||0,!!f.binary
  ]);
  return JSON.stringify({
    branch:status.branch||'',
    upstream:status.upstream||'',
    ahead:status.ahead||0,
    behind:status.behind||0,
    totals:status.totals||{},
    truncated:!!status.truncated,
    files
  });
}

function _workspacePanelOpenForAutoRefresh(){
  if(document.visibilityState&&document.visibilityState!=='visible')return false;
  if(document.documentElement.dataset.workspacePanel==='open')return true;
  const panel=document.querySelector('.rightpanel');
  return !!(panel&&(panel.classList.contains('mobile-open')||!document.querySelector('.layout')?.classList.contains('workspace-panel-collapsed')));
}

function _branchLocalName(remoteName){
  const parts=String(remoteName||'').split('/');
  if(parts.length<=1)return remoteName;
  return parts.slice(1).join('/');
}

function _isSelectableRemoteBranch(item){
  const name=String((item&&item.name)||'').trim();
  if(!name || !name.includes('/'))return false;
  if(name.endsWith('/HEAD'))return false;
  return true;
}

function _branchMeta(item){
  const bits=[];
  if(item.author)bits.push(item.author);
  if(item.updated_relative)bits.push(item.updated_relative);
  if(item.upstream)bits.push(item.upstream);
  if(item.ahead)bits.push(`\u2191${item.ahead}`);
  if(item.behind)bits.push(`\u2193${item.behind}`);
  if(item.subject)bits.push(item.subject);
  return bits.join(' \u00b7 ');
}

function _branchSearchText(item){
  return [item.name,item.upstream,item.author,item.updated_relative,item.subject].filter(Boolean).join(' ').toLowerCase();
}

function _currentBranchItem(branches,current){
  return (branches.local||[]).find(item=>item.name===current) || {name:current,upstream:branches.upstream||'',ahead:branches.ahead||0,behind:branches.behind||0};
}

function _allBranchRows(branches,current,filterText){
  const query=String(filterText||'').trim().toLowerCase();
  const matches=item=>!query||_branchSearchText(item).includes(query);
  return {
    current: [_currentBranchItem(branches,current)].filter(matches),
    local: (branches.local||[]).filter(item=>item.name!==current&&matches(item)),
    remote: (branches.remote||[]).filter(item=>_isSelectableRemoteBranch(item)&&matches(item)),
  };
}

function _defaultGitDiffMode(){
  if(window.matchMedia&&window.matchMedia('(max-width: 760px)').matches)return 'unified';
  return 'split';
}

function _currentGitDiffMode(){
  const mode=(_ensureGitState().diffMode||'').trim();
  return mode==='split'||mode==='unified'?mode:_defaultGitDiffMode();
}

function setGitDiffMode(mode){
  const git=_ensureGitState();
  git.diffMode=mode==='split'?'split':'unified';
  try{localStorage.setItem('hermes-webui-git-diff-mode',git.diffMode);}catch(e){}
  if(git.selectedDiff)openGitDiff(git.selectedDiff.path,git.selectedDiff.kind);
}

async function refreshGitBranches(){
  const git=_ensureGitState();
  if(!S.session||!git.status||!git.status.is_git){
    git.branches=null;
    renderGitBranchControl();
    return null;
  }
  git.branchLoading=true;
  renderGitBranchControl();
  try{
    const data=await api(`/api/git/branches?session_id=${encodeURIComponent(S.session.session_id)}`);
    git.branches=data.branches||null;
    renderGitBranchControl();
    return git.branches;
  }catch(e){
    git.branches=null;
    renderGitBranchControl(e.message);
    return null;
  }finally{
    git.branchLoading=false;
    renderGitBranchControl();
  }
}

function renderGitBranchControl(errorMessage){
  const git=_ensureGitState();
  const control=$('gitBranchControl'), label=$('gitBranchLabel'), menu=$('gitBranchMenu'), btn=$('btnGitBranchMenu');
  if(!control||!label||!menu||!btn)return;
  if(!git.status||!git.status.is_git){
    control.style.display='none';
    control.classList.remove('is-open');
    menu.hidden=true;
    btn.setAttribute('aria-expanded','false');
    return;
  }
  control.style.display='';
  const current=(git.branches&&git.branches.current)||git.status.branch||'HEAD';
  label.textContent=current;
  btn.title=`${t('git_current_branch')||'Current branch'}: ${current}`;
  btn.setAttribute('aria-label',`${t('git_current_branch')||'Current branch'}: ${current}`);
  btn.setAttribute('aria-expanded',git.branchMenuOpen?'true':'false');
  control.classList.toggle('is-open',!!git.branchMenuOpen);
  menu.hidden=!git.branchMenuOpen;
  if(!git.branchMenuOpen)return;
  menu.innerHTML='';
  const branches=git.branches||{};
  const filterText=String(git.branchFilter||'');
  const searchWrap=document.createElement('div');
  searchWrap.className='git-branch-search';
  const search=document.createElement('input');
  search.id='gitBranchSearchInput';
  search.type='search';
  search.placeholder='Switch branch...';
  search.autocomplete='off';
  search.spellcheck=false;
  search.value=filterText;
  search.addEventListener('input',()=>{
    git.branchFilter=search.value;
    renderGitBranchControl();
    requestAnimationFrame(()=>{
      const next=$('gitBranchSearchInput');
      if(next){
        next.focus();
        next.selectionStart=next.selectionEnd=next.value.length;
      }
    });
  });
  search.addEventListener('keydown',e=>{
    if(e.key==='Escape'){
      e.preventDefault();
      closeGitBranchMenu();
      return;
    }
    if(e.key!=='Enter')return;
    const rows=_allBranchRows(branches,current,search.value);
    const first=[...rows.local.map(item=>({item,mode:'local'})),...rows.remote.map(item=>({item,mode:'remote'}))][0];
    if(first){
      e.preventDefault();
      checkoutGitBranch(first.item.name,first.mode);
      return;
    }
    const name=search.value.trim();
    if(name){
      e.preventDefault();
      checkoutGitBranch(name,'new');
    }
  });
  searchWrap.appendChild(search);
  menu.appendChild(searchWrap);
  if(git.branchLoading){
    const loading=document.createElement('div');
    loading.className='git-empty';
    loading.textContent=t('loading')||'Loading...';
    menu.appendChild(loading);
    return;
  }
  if(errorMessage){
    const err=document.createElement('div');
    err.className='git-empty error';
    err.textContent=errorMessage;
    menu.appendChild(err);
  }
  const rows=_allBranchRows(branches,current,filterText);
  const addSection=(title,items,mode)=>{
    const section=document.createElement('section');
    section.className='git-branch-section';
    const h=document.createElement('div');
    h.className='git-branch-title';
    h.textContent=title;
    section.appendChild(h);
    if(!items||!items.length){
      const empty=document.createElement('div');
      empty.className='git-empty';
      empty.textContent=t('git_no_branches')||'No branches';
      section.appendChild(empty);
    }else{
      for(const item of items){
        const row=document.createElement('button');
        row.type='button';
        row.className='git-branch-item'+(item.name===current?' current':'');
        row.setAttribute('role','menuitem');
        row.disabled=item.name===current;
        row.onclick=()=>checkoutGitBranch(item.name,mode);
        const mark=document.createElement('span');
        mark.className='git-branch-check';
        mark.textContent=item.name===current?'\u2713':'';
        row.appendChild(mark);
        const body=document.createElement('span');
        body.className='git-branch-body';
        const name=document.createElement('span');
        name.className='git-branch-name';
        name.textContent=item.name;
        body.appendChild(name);
        const meta=_branchMeta(item);
        if(meta){
          const m=document.createElement('span');
          m.className='git-branch-meta';
          m.textContent=meta;
          body.appendChild(m);
        }
        row.appendChild(body);
        if(item.name===current){
          const currentMeta=document.createElement('span');
          currentMeta.className='git-branch-current-mark';
          currentMeta.textContent=t('git_current_branch')||'Current branch';
          row.appendChild(currentMeta);
        }
        section.appendChild(row);
      }
    }
    menu.appendChild(section);
  };
  addSection(t('git_current_branch')||'Current branch',rows.current,'local');
  addSection(t('git_local_branches')||'Local branches',rows.local,'local');
  addSection(t('git_remote_branches')||'Remote branches',rows.remote,'remote');
  const exactExists=[...rows.current,...rows.local,...rows.remote].some(item=>item.name===filterText.trim()||_branchLocalName(item.name)===filterText.trim());
  if(filterText.trim()&&!exactExists){
    const create=document.createElement('section');
    create.className='git-branch-section';
    const createBtn=document.createElement('button');
    createBtn.type='button';
    createBtn.className='git-branch-create-row';
    createBtn.onclick=()=>checkoutGitBranch(filterText,'new');
    createBtn.textContent=`${t('git_create_branch')||'Create branch'} "${filterText.trim()}"`;
    create.appendChild(createBtn);
    menu.appendChild(create);
  }
  const fetchBtn=document.createElement('button');
  fetchBtn.type='button';
  fetchBtn.className='git-branch-fetch';
  fetchBtn.textContent=t('git_fetch_refresh')||'Fetch refresh';
  fetchBtn.onclick=async()=>{await runGitRemoteAction('fetch');await refreshGitBranches();};
  menu.appendChild(fetchBtn);
  requestAnimationFrame(()=>{
    const active=document.activeElement;
    if(active&&menu.contains(active))return;
    const input=$('gitBranchSearchInput');
    if(input)input.focus();
  });
}

function closeGitBranchMenu(){
  const git=_ensureGitState();
  if(!git.branchMenuOpen)return;
  git.branchMenuOpen=false;
  git.branchFilter='';
  renderGitBranchControl();
}

async function toggleGitBranchMenu(event){
  if(event)event.stopPropagation();
  const git=_ensureGitState();
  git.branchMenuOpen=!git.branchMenuOpen;
  renderGitBranchControl();
  if(git.branchMenuOpen&&!git.branches)await refreshGitBranches();
}

async function checkoutGitBranch(ref,mode,opts={}){
  if(!S.session)return;
  ref=String(ref||'').trim();
  if(!ref)return;
  if(_previewDirty){
    const ok=await showConfirmDialog({title:t('unsaved_confirm'),message:'',confirmLabel:t('discard'),danger:true,focusCancel:true});
    if(!ok)return;
    cancelEditMode();
  }
  const git=_ensureGitState();
  git.mutating=true;
  git.branchMenuOpen=false;
  renderGitChanges();
  renderGitBranchControl();
  try{
    const endpoint=opts.stash?'/api/git/stash-checkout':'/api/git/checkout';
    const data=await api(endpoint,{method:'POST',body:JSON.stringify({
      session_id:S.session.session_id,
      ref,
      mode:mode==='remote'?'remote':mode==='new'?'new':'local',
      new_branch:mode==='new'?ref:null,
      track:mode==='remote',
      dirty_mode:'block'
    })});
    _setGitStatus(data.git);
    git.branches=data.branches||null;
    const stashNote=data.stash_name?` · ${t('git_stashed')||'Stashed'}: ${data.stash_name}`:'';
    showToast(data.current_branch?`${t('git_checked_out')||'Checked out'} ${data.current_branch}${stashNote}`:`${t('git_checked_out')||'Checked out'}${stashNote}`,3600);
    _closePreviewSurface();
    await loadDir('.');
    await refreshGitBranches();
  }catch(e){
    let code='';
    try{code=JSON.parse(e.body||'{}').code||'';}catch(_e){}
    if(!opts.stash&&code==='dirty_worktree'){
      const ok=await showConfirmDialog({
        title:t('git_checkout_dirty_title')||'Checkout blocked',
        message:t('git_checkout_dirty_message')||'The worktree has uncommitted changes. Stash local changes and switch branch?',
        confirmLabel:t('git_stash_and_switch')||'Stash and switch',
        danger:false,
        focusCancel:true
      });
      if(ok)return checkoutGitBranch(ref,mode,{stash:true});
    }
    const msg=e.message||t('git_checkout_failed')||'Checkout failed';
    showToast(`${t('git_checkout_failed')||'Checkout failed'}: ${msg}`,5000,'error');
  }finally{
    git.mutating=false;
    renderGitChanges();
    renderGitBranchControl();
  }
}

function renderGitBadge(status){
  const badge=$('gitBadge');
  const tabs=$('workspaceGitTabs');
  if(!badge)return;
  if(!status||!status.is_git){
    badge.style.display='none';
    badge.textContent='';
    if(tabs)tabs.hidden=true;
    _renderGitAutoFetchStatus();
    renderGitBranchControl();
    return;
  }
  if(tabs)tabs.hidden=false;
  const totals=status.totals||{};
  let text=status.branch||'git';
  if((totals.changed||0)>0) text+=` \u00b7 ${totals.changed}\u2206`;
  if(status.behind>0) text+=` \u2193${status.behind}`;
  if(status.ahead>0) text+=` \u2191${status.ahead}`;
  badge.textContent=text;
  badge.className='git-badge'+((totals.changed||0)>0?' dirty':'');
  badge.style.display='';
  _renderGitAutoFetchStatus();
  renderGitBranchControl();
  const changesTab=$('btnWorkspaceChangesTab');
  if(changesTab){
    changesTab.textContent=(totals.changed||0)>0?`${t('git_changes')} ${totals.changed}`:t('git_changes');
  }
}

function _relativeAutoFetchTime(ts){
  if(!ts)return '';
  const seconds=Math.max(0,Math.round((Date.now()-ts)/1000));
  if(seconds<10)return 'just now';
  if(seconds<60)return `${seconds}s ago`;
  const minutes=Math.round(seconds/60);
  if(minutes<60)return `${minutes}m ago`;
  return `${Math.round(minutes/60)}h ago`;
}

function _autoFetchPauseReason(){
  const git=_ensureGitState();
  if(!_workspacePanelOpenForAutoRefresh())return 'paused while hidden';
  if(git.mutating||git.syncing||git.autoFetching)return 'syncing';
  if(git.generatingCommitMessage)return 'commit message in progress';
  if(git.branchMenuOpen)return 'branch menu open';
  if(typeof _previewDirty!=='undefined'&&_previewDirty)return 'paused while editing';
  return '';
}

function _renderGitAutoFetchStatus(){
  const el=$('gitAutoFetchStatus');
  if(!el)return;
  const git=_ensureGitState();
  if(!git.status||!git.status.is_git){
    el.hidden=true;
    el.textContent='';
    el.title='';
    return;
  }
  const pause=_autoFetchPauseReason();
  let text='';
  let title='Workspace Git auto-fetch updates remote refs only; it never pulls or changes files.';
  if(git.autoFetching){
    text='Fetching…';
  }else if(git.lastAutoFetchError){
    text=git.autoFetchFailureCount>1?`Auto-fetch failed (${git.autoFetchFailureCount})`:'Auto-fetch failed';
    title=`${title}\nLast error: ${git.lastAutoFetchError}`;
  }else if(git.lastAutoFetchAt){
    text=`Last fetched ${_relativeAutoFetchTime(git.lastAutoFetchAt)}`;
  }else if(pause){
    text=`Auto-fetch ${pause}`;
  }else{
    text='Auto-fetch ready';
  }
  if(pause&&!git.autoFetching&&!git.lastAutoFetchError)title=`${title}\nAuto-fetch ${pause}.`;
  el.hidden=false;
  el.textContent=text;
  el.title=title;
}

async function refreshGitStatus(opts={}){
  const git=_ensureGitState();
  if(!S.session){
    git.status=null;
    git.selectedTab='files';
    renderGitBadge(null);
    renderWorkspacePanelTabState();
    return null;
  }
  if(git.loading&&opts.auto)return git.status;
  git.loading=true;
  try{
    const priorSignature=_gitStatusSignature(git.status);
    const data=await api(`/api/git/status?session_id=${encodeURIComponent(S.session.session_id)}`);
    git.status=data.git||null;
    if(!git.status||!git.status.is_git)git.selectedTab='files';
    _reconcileGitSelection();
    const changed=priorSignature!==_gitStatusSignature(git.status);
    if(changed||!opts.auto){
      renderGitBadge(git.status);
      renderGitChanges();
      if(opts.refreshBranches!==false)refreshGitBranches();
      if(typeof renderFileTree==='function')renderFileTree();
    }
    return git.status;
  }catch(e){
    renderGitBadge(git.status);
    renderGitChanges();
    return git.status;
  }finally{
    git.loading=false;
    _renderGitAutoFetchStatus();
    renderWorkspacePanelTabState();
  }
}

function renderWorkspacePanelTabState(){
  const git=_ensureGitState();
  const filesTab=$('btnWorkspaceFilesTab'), changesTab=$('btnWorkspaceChangesTab');
  const changesView=$('gitChangesView'), fileTree=$('fileTree'), emptyEl=$('wsEmptyState');
  const previewVisible=$('previewArea')&&$('previewArea').classList.contains('visible');
  if(filesTab)filesTab.classList.toggle('active',git.selectedTab!=='changes');
  if(changesTab)changesTab.classList.toggle('active',git.selectedTab==='changes');
  if(git.selectedTab==='changes'){
    if(fileTree)fileTree.style.display='none';
    if(emptyEl)emptyEl.style.display='none';
    if(changesView)changesView.style.display=previewVisible?'none':'flex';
  }else{
    if(changesView)changesView.style.display='none';
    if(fileTree&&!previewVisible)fileTree.style.display='';
  }
}

function switchWorkspacePanelTab(tab){
  const git=_ensureGitState();
  git.selectedTab=tab==='changes'?'changes':'files';
  if(git.selectedTab==='changes'){
    if($('previewArea'))$('previewArea').classList.remove('visible');
    git.selectedDiff=null;
    renderGitChanges();
  }
  renderWorkspacePanelTabState();
}

function _gitFiles(){
  const status=_ensureGitState().status;
  return (status&&Array.isArray(status.files))?status.files:[];
}

function _gitStatusForPath(path){
  const normalized=String(path||'');
  return _gitFiles().find(f=>f.path===normalized||(f.ignored&&f.path===`${normalized}/`))||null;
}

function _gitGroupFiles(kind){
  return _gitFiles().filter(f=>{
    if(kind==='conflicts')return f.conflict;
    if(f.ignored)return false;
    if(kind==='tracked')return !f.untracked&&!f.conflict&&(f.staged||f.unstaged);
    if(kind==='staged')return f.staged&&!f.conflict;
    if(kind==='untracked')return f.untracked&&!f.conflict;
    return (f.unstaged&&!f.untracked&&!f.conflict);
  });
}

function _gitStageableFiles(){
  return _gitFiles().filter(f=>!f.ignored&&!f.conflict&&(f.unstaged||f.untracked));
}

function _gitCommittableFiles(){
  return _gitFiles().filter(f=>!f.ignored&&!f.conflict&&(f.staged||f.unstaged||f.untracked));
}

function _reconcileGitSelection(){
  const git=_ensureGitState();
  const selectable=_gitCommittableFiles();
  const allowed=new Set(selectable.map(f=>f.path));
  if(!git.selectionTouched){
    git.selectedPaths=new Set(selectable.filter(f=>f.staged&&!f.conflict).map(f=>f.path));
    return;
  }
  for(const path of [...git.selectedPaths]){
    if(!allowed.has(path))git.selectedPaths.delete(path);
  }
}

function _gitSelectedFiles(){
  const git=_ensureGitState();
  return _gitCommittableFiles().filter(f=>git.selectedPaths.has(f.path));
}

function _setGitPathSelected(path, selected){
  const git=_ensureGitState();
  git.selectionTouched=true;
  if(selected)git.selectedPaths.add(path);
  else git.selectedPaths.delete(path);
  renderGitChanges();
}

function _setGitGroupSelected(files, selected){
  const git=_ensureGitState();
  git.selectionTouched=true;
  for(const file of files){
    if(selected)git.selectedPaths.add(file.path);
    else git.selectedPaths.delete(file.path);
  }
  renderGitChanges();
}

function _setGitStatus(status){
  const git=_ensureGitState();
  git.status=status||null;
  _reconcileGitSelection();
  renderGitBadge(git.status);
  renderGitChanges();
  if(typeof renderFileTree==='function')renderFileTree();
}

function _gitStatsEl(file){
  const stats=document.createElement('span');
  stats.className='git-change-stats';
  if(file.binary){
    stats.textContent=t('git_binary_file');
    return stats;
  }
  const additions=document.createElement('span');
  additions.className='git-stat-add';
  additions.textContent=`+${file.additions||0}`;
  const deletions=document.createElement('span');
  deletions.className='git-stat-del';
  deletions.textContent=`-${file.deletions||0}`;
  stats.append(additions,' ',deletions);
  return stats;
}

function _gitGroupHeader(kind, label, files){
  const header=document.createElement('div');
  header.className='git-change-group-title';
  if(kind==='tracked'||kind==='untracked'){
    const selected=files.filter(f=>_ensureGitState().selectedPaths.has(f.path)).length;
    const checkbox=document.createElement('input');
    checkbox.type='checkbox';
    checkbox.className='git-select-checkbox git-select-group';
    checkbox.checked=selected>0&&selected===files.length;
    checkbox.indeterminate=selected>0&&selected<files.length;
    checkbox.disabled=!!_ensureGitState().mutating;
    checkbox.setAttribute('aria-label',`${label}: select ${files.length} file${files.length===1?'':'s'}`);
    checkbox.onclick=e=>{e.stopPropagation();_setGitGroupSelected(files,checkbox.checked);};
    header.appendChild(checkbox);
    const text=document.createElement('span');
    text.textContent=`${label} ${selected}/${files.length}`;
    header.appendChild(text);
  }else{
    header.textContent=label;
  }
  return header;
}

function _gitSyncLabel(action,status){
  if(action==='push'){
    const ahead=Number(status&&status.ahead||0);
    return ahead>0?`${t('git_push')} \u2191${ahead}`:t('git_push');
  }
  if(action==='pull'){
    const behind=Number(status&&status.behind||0);
    return behind>0?`${t('git_pull')} \u2193${behind}`:t('git_pull');
  }
  return t('git_fetch');
}

function _gitStatusLabel(file){
  const code=String(file&&file.status||'').trim();
  if(file&&file.conflict)return 'Conflict';
  if(file&&file.ignored||code==='Ignored'||code==='!!')return 'Ignored';
  if(file&&file.untracked||code==='??')return 'New';
  if(code==='R')return 'R';
  if(code==='D')return 'D';
  if(code==='A')return 'A';
  if(code==='C')return 'C';
  if(code==='U'||code.includes('U'))return 'Conflict';
  if(code==='T')return 'T';
  if(code==='M'||!code)return 'M';
  return code;
}

function _gitStatusTitle(file){
  const code=String(file&&file.status||'').trim();
  if(file&&file.conflict)return 'Conflict';
  if(file&&file.ignored||code==='Ignored'||code==='!!')return 'Ignored by Git';
  if(file&&file.untracked||code==='??')return 'New file';
  if(code==='R')return 'Renamed';
  if(code==='D')return 'Deleted';
  if(code==='A')return 'Added';
  if(code==='C')return 'Copied';
  if(code==='U'||code.includes('U'))return 'Conflict';
  if(code==='T')return 'Type changed';
  if(code==='M'||!code)return 'Modified';
  return `Git status: ${code}`;
}

function _gitChangeRow(file, kind){
  const row=document.createElement('div');
  const selectable=kind==='tracked'||kind==='untracked';
  row.className='git-change-row'+(selectable?' selectable':'');
  row.tabIndex=0;
  row.onclick=()=>openGitDiff(file.path,(kind==='staged'||(kind==='tracked'&&file.staged&&!file.unstaged))?'staged':'unstaged');
  row.onkeydown=e=>{if(e.key==='Enter'||e.key===' '){e.preventDefault();row.click();}};
  if(selectable){
    const checkbox=document.createElement('input');
    checkbox.type='checkbox';
    checkbox.className='git-select-checkbox';
    checkbox.checked=_ensureGitState().selectedPaths.has(file.path);
    checkbox.disabled=!!_ensureGitState().mutating;
    checkbox.setAttribute('aria-label',`Select ${file.path} for commit`);
    checkbox.onclick=e=>{e.stopPropagation();_setGitPathSelected(file.path,checkbox.checked);};
    row.appendChild(checkbox);
  }
  const status=document.createElement('span');
  status.className='git-change-status'+(file.untracked?' untracked':file.conflict?' conflict':'');
  status.textContent=_gitStatusLabel(file);
  status.title=_gitStatusTitle(file);
  status.setAttribute('aria-label',_gitStatusTitle(file));
  row.appendChild(status);
  const name=document.createElement('span');
  name.className='git-change-path';
  name.textContent=file.old_path?`${file.old_path} \u2192 ${file.path}`:file.path;
  row.appendChild(name);
  row.appendChild(_gitStatsEl(file));
  const actions=document.createElement('span');
  actions.className='git-change-actions';
  const mutating=!!_ensureGitState().mutating;
  const stagedOnly=(kind==='tracked'&&file.staged&&!file.unstaged&&!file.untracked);
  if(kind==='staged'||stagedOnly){
    const unstage=document.createElement('button');
    unstage.className='mini-btn';
    unstage.textContent=t('git_unstage');
    unstage.disabled=mutating;
    unstage.onclick=e=>{e.stopPropagation();unstageGitPath(file.path);};
    actions.appendChild(unstage);
  }else if(kind==='conflicts'){
    const open=document.createElement('button');
    open.className='mini-btn';
    open.textContent=t('open');
    open.onclick=e=>{e.stopPropagation();openFile(file.path,{returnTo:'changes'});};
    actions.appendChild(open);
  }
  if(actions.childNodes.length){
    row.classList.add('has-actions');
    row.appendChild(actions);
  }
  return row;
}

function renderGitChanges(){
  const list=$('gitChangesList');
  const commitBox=$('gitCommitBox');
  const commitBtn=$('btnGitCommit');
  const generateBtn=$('btnGitGenerateCommitMessage');
  const selectionSummary=$('gitSelectionSummary');
  if(!list)return;
  list.innerHTML='';
  const status=_ensureGitState().status;
  if(!status||!status.is_git){
    const empty=document.createElement('div');
    empty.className='git-empty';
    empty.textContent=t('git_not_repo');
    list.appendChild(empty);
    if(commitBox)commitBox.style.display='none';
    renderWorkspacePanelTabState();
    return;
  }
  const summary=document.createElement('div');
  summary.className='git-summary';
  const totals=status.totals||{};
  const summaryText=document.createElement('span');
  summaryText.className='git-summary-text';
  summaryText.textContent=[status.branch||'HEAD',status.upstream,`${totals.changed||0} ${t('git_changed')}`,status.ahead?`\u2191${status.ahead}`:'',status.behind?`\u2193${status.behind}`:''].filter(Boolean).join(' \u00b7 ');
  summary.appendChild(summaryText);
  const selectedFiles=_gitSelectedFiles();
  const selectedCount=selectedFiles.length;
  const selectedStageable=selectedFiles.filter(f=>!f.conflict&&(f.unstaged||f.untracked));
  const selectedDiscardable=selectedFiles.filter(f=>!f.conflict&&(f.unstaged||f.untracked));
  const summaryActions=document.createElement('span');
  summaryActions.className='git-summary-actions';
  for(const action of ['fetch','pull','push']){
    const btn=document.createElement('button');
    btn.className='mini-btn git-sync-btn';
    btn.type='button';
    btn.textContent=_gitSyncLabel(action,status);
    btn.title=action==='push'
      ? 'Push local commits'
      : action==='pull'
        ? 'Pull remote commits'
        : t('git_fetch');
    btn.disabled=!!_ensureGitState().mutating||_ensureGitState().syncing===action;
    btn.onclick=()=>runGitRemoteAction(action);
    summaryActions.appendChild(btn);
  }
  const stageable=_gitStageableFiles();
  if(selectedCount){
    const stageSelected=document.createElement('button');
    stageSelected.className='mini-btn git-stage-selected-btn';
    stageSelected.type='button';
    stageSelected.textContent=`${t('git_stage_selected')} (${selectedStageable.length})`;
    stageSelected.title=t('git_stage_selected_title');
    stageSelected.disabled=!!_ensureGitState().mutating||!selectedStageable.length;
    stageSelected.onclick=()=>stageGitSelectedChanges();
    summaryActions.appendChild(stageSelected);

    const discardSelected=document.createElement('button');
    discardSelected.className='mini-btn danger git-discard-selected-btn';
    discardSelected.type='button';
    discardSelected.textContent=`${t('git_discard_selected')} (${selectedDiscardable.length})`;
    discardSelected.title=t('git_discard_selected_title');
    discardSelected.disabled=!!_ensureGitState().mutating||!selectedDiscardable.length;
    discardSelected.onclick=()=>discardGitSelectedChanges();
    summaryActions.appendChild(discardSelected);
  }else if(stageable.length){
    const stageAll=document.createElement('button');
    stageAll.className='mini-btn git-stage-all-btn';
    stageAll.type='button';
    stageAll.textContent=t('git_stage_all')||'Stage all';
    stageAll.disabled=!!_ensureGitState().mutating;
    stageAll.onclick=()=>stageGitAllChanges();
    summaryActions.appendChild(stageAll);
  }
  summary.appendChild(summaryActions);
  list.appendChild(summary);
  const groups=[
    ['conflicts',t('git_conflicts')],
    ['tracked',t('git_tracked')||'Tracked'],
    ['untracked',t('git_untracked')],
  ];
  let rendered=0;
  for(const [kind,label] of groups){
    const files=_gitGroupFiles(kind);
    if(!files.length)continue;
    const group=document.createElement('section');
    group.className='git-change-group';
    group.appendChild(_gitGroupHeader(kind,label,files));
    files.forEach(file=>group.appendChild(_gitChangeRow(file,kind)));
    list.appendChild(group);
    rendered+=files.length;
  }
  if(!rendered){
    const empty=document.createElement('div');
    empty.className='git-empty';
    empty.textContent=t('git_no_changes');
    list.appendChild(empty);
  }
  const committableCount=_gitCommittableFiles().length;
  if(commitBox)commitBox.style.display=committableCount?'flex':'none';
  if(selectionSummary){
    selectionSummary.textContent=selectedCount
      ? `${selectedCount} of ${committableCount} committable file${committableCount===1?'':'s'} selected`
      : `${committableCount} committable file${committableCount===1?'':'s'} available`;
  }
  const hasSelection=selectedCount>0;
  const generating=!!_ensureGitState().generatingCommitMessage;
  if(commitBtn){
    commitBtn.disabled=!hasSelection||generating||!!_ensureGitState().mutating;
    commitBtn.textContent=hasSelection?`${t('git_commit')} ${selectedCount} ${selectedCount===1?'file':'files'}`:t('git_commit');
  }
  if(generateBtn){
    generateBtn.disabled=!hasSelection||generating||!!_ensureGitState().mutating;
    generateBtn.textContent=generating?'Generating...':'Generate message';
  }
  renderWorkspacePanelTabState();
}

function navigateUp(){
  if(!S.session||S.currentDir==='.')return;
  const parts=S.currentDir.split('/');
  parts.pop();
  loadDir(parts.length?parts.join('/'):'.');
}

// File extension sets for preview routing (must match server-side sets)
const IMAGE_EXTS  = new Set(['.png','.jpg','.jpeg','.gif','.svg','.webp','.ico','.bmp']);
const MD_EXTS     = new Set(['.md','.markdown','.mdown']);
const HTML_EXTS   = new Set(['.html','.htm']);
const PDF_EXTS    = new Set(['.pdf']);
const AUDIO_EXTS  = new Set(['.mp3','.wav','.m4a','.aac','.ogg','.oga','.opus','.flac']);
const VIDEO_EXTS  = new Set(['.mp4','.mov','.m4v','.webm','.ogv','.avi','.mkv']);
// Binary formats that should download rather than preview
const DOWNLOAD_EXTS = new Set([
  '.docx','.doc','.xlsx','.xls','.pptx','.ppt','.odt','.ods','.odp',
  '.zip','.tar','.gz','.bz2','.7z','.rar',
  '.exe','.dmg','.pkg','.deb','.rpm',
  '.woff','.woff2','.ttf','.otf','.eot',
  '.bin','.dat','.db','.sqlite','.pyc','.class','.so','.dylib','.dll',
]);

function fileExt(p){ const i=p.lastIndexOf('.'); return i>=0?p.slice(i).toLowerCase():''; }

let _previewCurrentPath = '';  // relative path of currently previewed file
let _previewCurrentMode = '';  // 'code' | 'md' | 'image' | 'html' | 'pdf' | 'audio' | 'video'
let _previewDirty = false;     // true when edits are unsaved
let _previewReturnTarget = 'files'; // 'files' | 'changes'
let _editorSoftWrap = false;

function _setPreviewReturnTarget(target){
  _previewReturnTarget=target==='changes'?'changes':'files';
  const btn=$('btnPreviewBack');
  const label=$('previewBackLabel');
  if(label)label.textContent=_previewReturnTarget==='changes'?t('git_changes'):t('git_files');
  if(btn){
    btn.style.display='inline-flex';
    btn.title=_previewReturnTarget==='changes'?t('git_changes'):t('git_files');
  }
}

function showPreview(mode){
  // mode: 'code' | 'image' | 'md' | 'html' | 'pdf' | 'audio' | 'video' | 'gitdiff'
  const editorShell=$('workspaceEditorShell'); if(editorShell) editorShell.style.display = mode==='code' ? 'flex' : 'none';
  const readShell=$('previewReadShell'); if(readShell) readShell.style.display = mode==='code' ? 'grid' : 'none';
  const editShell=$('previewEditShell'); if(editShell) editShell.style.display = 'none';
  $('previewImgWrap').style.display  = mode==='image' ? '' : 'none';
  const mediaWrap=$('previewMediaWrap'); if(mediaWrap) mediaWrap.style.display = (mode==='audio'||mode==='video') ? '' : 'none';
  const pdfWrap=$('previewPdfWrap'); if(pdfWrap) pdfWrap.style.display = mode==='pdf' ? '' : 'none';
  $('previewMd').style.display       = mode==='md'    ? '' : 'none';
  $('previewHtmlWrap').style.display = mode==='html'  ? '' : 'none';
  const diffView=$('gitDiffView'); if(diffView) diffView.style.display = mode==='gitdiff' ? 'flex' : 'none';
  const editArea=$('previewEditArea'); if(editArea) editArea.onkeydown=null;  // start in read-only
  const badge=$('previewBadge');
  badge.className='preview-badge '+mode;
  badge.textContent = mode==='image'?'image':mode==='audio'?'audio':mode==='video'?'video':mode==='pdf'?'pdf':mode==='md'?'md':mode==='html'?'html':mode==='gitdiff'?'diff':fileExt($('previewPathText').textContent)||'text';
  _previewCurrentMode = mode;
  _previewDirty = false;
  refreshEditorChrome();
  updateEditBtn();
  // Show "Open in browser" button for iframe-backed document previews
  const openBtn=$('btnOpenInBrowser');
  if(openBtn) openBtn.style.display = (mode==='html'||mode==='pdf')?'inline-flex':'none';
  const mdPopoutBtn=$('btnMarkdownPopout');
  if(mdPopoutBtn) mdPopoutBtn.style.display = mode==='md'?'inline-flex':'none';
  const downloadBtn=$('btnDownloadFile');
  if(downloadBtn) downloadBtn.style.display = mode==='gitdiff'?'none':'inline-flex';
}

function setEditorSoftWrap(on){
  _editorSoftWrap=!!on;
  const shell=$('workspaceEditorShell');
  if(shell)shell.classList.toggle('soft-wrap',_editorSoftWrap);
  try{localStorage.setItem('hermes-webui-editor-soft-wrap',_editorSoftWrap?'1':'0');}catch(e){}
}

function _initEditorPrefs(){
  try{_editorSoftWrap=localStorage.getItem('hermes-webui-editor-soft-wrap')==='1';}catch(e){_editorSoftWrap=false;}
  const toggle=$('previewWrapToggle');
  if(toggle)toggle.checked=_editorSoftWrap;
  setEditorSoftWrap(_editorSoftWrap);
}

function _lineCount(text){return Math.max(1,String(text||'').split('\n').length);}

function renderEditorGutter(targetId,text,activeLine){
  const gutter=$(targetId);
  if(!gutter)return;
  const count=_lineCount(text);
  let html='';
  for(let i=1;i<=count;i++)html+=`<div class="workspace-editor-line-number${i===activeLine?' active':''}">${i}</div>`;
  gutter.innerHTML=html;
}

function _editorIndentColumns(line,tabSize){
  let cols=0;
  for(const ch of String(line||'')){
    if(ch===' ')cols+=1;
    else if(ch==='\t')cols+=tabSize;
    else break;
  }
  return cols;
}

function _editorLineIndentDepths(text,tabSize){
  const lines=String(text||'').split('\n');
  return lines.map((line,idx)=>{
    if(line.trim())return Math.floor(_editorIndentColumns(line,tabSize)/tabSize);
    for(let next=idx+1;next<lines.length;next++){
      if(lines[next].trim())return Math.floor(_editorIndentColumns(lines[next],tabSize)/tabSize);
    }
    for(let prev=idx-1;prev>=0;prev--){
      if(lines[prev].trim())return Math.floor(_editorIndentColumns(lines[prev],tabSize)/tabSize);
    }
    return 0;
  });
}

function renderEditorIndentGuides(targetId,text,scrollTop){
  const guides=$(targetId);
  if(!guides)return;
  const tabSize=2;
  const depths=_editorLineIndentDepths(text,tabSize);
  let html='';
  for(const depth of depths){
    html+='<div class="workspace-editor-guide-line">';
    for(let level=1;level<=depth;level++){
      html+=`<span style="left:${(level-1)*tabSize}ch"></span>`;
    }
    html+='</div>';
  }
  guides.innerHTML=html;
  guides.style.transform=`translateY(-${scrollTop||0}px)`;
}

function _editorCursorPosition(text,pos){
  const before=String(text||'').slice(0,pos);
  const lines=before.split('\n');
  return {line:lines.length,col:(lines[lines.length-1]||'').length+1};
}

function refreshEditorChrome(){
  const editing=_isPreviewEditing();
  const area=$('previewEditArea');
  const code=$('previewCode');
  const text=editing&&area?area.value:(code?code.textContent:'');
  const cursor=editing&&area?_editorCursorPosition(area.value,area.selectionStart||0):{line:1,col:1};
  renderEditorGutter(editing?'previewEditGutter':'previewCodeGutter',text,cursor.line);
  renderEditorIndentGuides(editing?'previewEditGuides':'previewCodeGuides',text,editing&&area?area.scrollTop:(code?code.scrollTop:0));
  const status=$('previewEditorStatus');
  if(status)status.textContent=`Ln ${cursor.line}, Col ${cursor.col}`;
  const dirty=$('editorDirtyState');
  if(dirty)dirty.textContent=_previewDirty?`• ${t('unsaved')||'Unsaved'}`:'';
  const editShell=$('previewEditShell');
  if(editShell)editShell.classList.toggle('is-dirty',!!_previewDirty);
  const actions=$('previewEditorActions');
  if(actions)actions.hidden=!editing;
  const cancelBtn=$('btnEditorCancel');
  if(cancelBtn)cancelBtn.disabled=!editing;
  const saveBtn=$('btnEditorSave');
  if(saveBtn)saveBtn.disabled=!editing||!_previewDirty;
}

function syncEditorScroll(mode){
  if(mode==='edit'){
    const area=$('previewEditArea'), gutter=$('previewEditGutter'), guides=$('previewEditGuides');
    if(area&&gutter)gutter.scrollTop=area.scrollTop;
    if(area&&guides)guides.style.transform=`translateY(-${area.scrollTop}px)`;
    return;
  }
  const code=$('previewCode'), gutter=$('previewCodeGutter'), guides=$('previewCodeGuides');
  if(code&&gutter)gutter.scrollTop=code.scrollTop;
  if(code&&guides)guides.style.transform=`translateY(-${code.scrollTop}px)`;
}

function handleEditorInput(){
  _previewDirty=true;
  updateEditBtn();
  refreshEditorChrome();
}

function _isPreviewEditing(){
  const editShell=$('previewEditShell');
  return !!(editShell&&editShell.style.display!=='none');
}

async function requestCancelEditMode(){
  if(_previewDirty){
    const ok=await showConfirmDialog({title:t('unsaved_confirm'),message:'',confirmLabel:t('discard'),danger:true,focusCancel:true});
    if(!ok)return false;
  }
  cancelEditMode();
  return true;
}

function _indentSelection(text,start,end,indent){
  const lineStart=text.lastIndexOf('\n',Math.max(0,start-1))+1;
  const selected=text.slice(lineStart,end);
  const lines=selected.split('\n');
  const out=lines.map(line=>indent+line).join('\n');
  return {text:text.slice(0,lineStart)+out+text.slice(end),start:start+indent.length,end:end+(indent.length*lines.length)};
}

function _unindentSelection(text,start,end,tabSize){
  const lineStart=text.lastIndexOf('\n',Math.max(0,start-1))+1;
  const selected=text.slice(lineStart,end);
  let deltaStart=0,deltaEnd=0;
  const out=selected.split('\n').map((line,idx)=>{
    let remove=0;
    if(line.startsWith('\t'))remove=1;
    else{
      const m=line.match(/^ +/);
      remove=Math.min(tabSize,m?m[0].length:0);
    }
    if(idx===0)deltaStart=remove;
    deltaEnd+=remove;
    return line.slice(remove);
  }).join('\n');
  return {text:text.slice(0,lineStart)+out+text.slice(end),start:Math.max(lineStart,start-deltaStart),end:Math.max(lineStart,end-deltaEnd)};
}

function handleEditorKeydown(e){
  const area=$('previewEditArea');
  if(!area)return;
  const tabSize=2;
  const indent=' '.repeat(tabSize);
  if(e.key==='Tab'){
    e.preventDefault();
    const start=area.selectionStart,end=area.selectionEnd;
    const next=e.shiftKey?_unindentSelection(area.value,start,end,tabSize):_indentSelection(area.value,start,end,indent);
    area.value=next.text;
    area.selectionStart=next.start;area.selectionEnd=next.end;
    handleEditorInput();
  }else if(e.key==='Enter'){
    e.preventDefault();
    const start=area.selectionStart,end=area.selectionEnd;
    const lineStart=area.value.lastIndexOf('\n',Math.max(0,start-1))+1;
    const currentLine=area.value.slice(lineStart,start);
    const match=currentLine.match(/^\s*/);
    const insertion='\n'+(match?match[0]:'');
    area.value=area.value.slice(0,start)+insertion+area.value.slice(end);
    area.selectionStart=area.selectionEnd=start+insertion.length;
    handleEditorInput();
  }else if(e.key==='Escape'){
    e.preventDefault();
    requestCancelEditMode();
  }else{
    setTimeout(refreshEditorChrome,0);
  }
}

function _closePreviewSurface(){
  const pa=$('previewArea');if(pa)pa.classList.remove('visible');
  const pi=$('previewImg');if(pi){pi.onerror=null;pi.src='';}
  const pdf=$('previewPdfFrame');if(pdf)pdf.src='';
  const html=$('previewHtmlIframe');if(html)html.src='';
  const pm=$('previewMd');if(pm)pm.innerHTML='';
  const pc=$('previewCode');if(pc)pc.textContent='';
  const shell=$('workspaceEditorShell');if(shell)shell.style.display='none';
  const pp=$('previewPathText');if(pp)pp.textContent='';
  const back=$('btnPreviewBack');if(back)back.style.display='none';
  _previewCurrentPath='';_previewCurrentMode='';_previewDirty=false;
}

async function returnFromPreview(){
  if(typeof _previewDirty!=='undefined'&&_previewDirty){
    const ok=await showConfirmDialog({title:t('unsaved_confirm'),message:'',confirmLabel:'Discard',danger:true,focusCancel:true});
    if(!ok)return;
  }
  if(_previewReturnTarget==='changes'){
    if(S.git){
      S.git.selectedTab='changes';
      S.git.selectedDiff=null;
    }
    _closePreviewSurface();
    renderGitChanges();
    renderWorkspacePanelTabState();
    return;
  }
  if(typeof clearPreview==='function')clearPreview({keepPanelOpen:true});
}

function updateEditBtn(){
  const btn=$('btnEditFile');
  if(!btn)return;
  const editable = _previewCurrentMode==='code'||_previewCurrentMode==='md';
  const editing = _isPreviewEditing();
  btn.style.display = editable&&!editing?'':'none';
  btn.innerHTML = `&#9998; ${t('edit')}`;
  btn.title = t('edit_title');
  btn.style.color = '';
}

async function toggleEditMode(){
  const editing = _isPreviewEditing();
  if(editing){
    // Save
    if(!S.session||!_previewCurrentPath)return;
    const content=$('previewEditArea').value;
    try{
      await api('/api/file/save',{method:'POST',body:JSON.stringify({
        session_id:S.session.session_id, path:_previewCurrentPath, content
      })});
      _previewDirty=false;
      _previewRawContent=content;
      // Update read-only views
      if(_previewCurrentMode==='code') $('previewCode').textContent=content;
      else { renderWorkspaceMarkdown(content); }
      const editShell=$('previewEditShell'); if(editShell)editShell.style.display='none';
      const readShell=$('previewReadShell'); if(readShell&&_previewCurrentMode==='code')readShell.style.display='grid';
      else { const editorShell=$('workspaceEditorShell'); if(editorShell)editorShell.style.display='none'; $('previewMd').style.display=''; }
      showToast(t('saved'));
      refreshGitStatus();
    }catch(e){setStatus(t('save_failed')+e.message);}
  }else{
    // Enter edit mode: populate textarea with current content
    const currentText = _previewCurrentMode==='code'
      ? $('previewCode').textContent
      : _previewRawContent||'';
    _initEditorPrefs();
    const editorShell=$('workspaceEditorShell'); if(editorShell)editorShell.style.display='flex';
    $('previewEditArea').value=currentText;
    const editShell=$('previewEditShell'); if(editShell)editShell.style.display='grid';
    const readShell=$('previewReadShell'); if(readShell&&_previewCurrentMode==='code')readShell.style.display='none';
    else $('previewMd').style.display='none';
    $('previewEditArea').onkeydown=handleEditorKeydown;
    $('previewEditArea').onkeyup=refreshEditorChrome;
    $('previewEditArea').onclick=refreshEditorChrome;
    $('previewEditArea').focus();
  }
  refreshEditorChrome();
  updateEditBtn();
}

let _previewRawContent = '';  // raw text for md files (to populate editor)

function renderWorkspaceMarkdown(content){
  const target=$('previewMd');
  if(!target)return;
  target.innerHTML=renderMd(content||'');
  postProcessWorkspaceMarkdown(target);
}

function postProcessWorkspaceMarkdown(container){
  requestAnimationFrame(()=>{
    if(typeof postProcessRenderedMessages==='function')postProcessRenderedMessages(container);
    else{
      if(typeof highlightCode==='function')highlightCode(container);
      if(typeof addCopyButtons==='function')addCopyButtons(container);
      if(typeof renderMermaidBlocks==='function')renderMermaidBlocks(container);
      if(typeof renderKatexBlocks==='function')renderKatexBlocks(container);
      if(typeof initTreeViews==='function')initTreeViews(container);
    }
  });
}

function cancelEditMode(){
  // Discard changes and return to read-only view
  const editShell=$('previewEditShell'); if(editShell)editShell.style.display='none';
  $('previewEditArea').onkeydown=null;
  if(_previewCurrentMode==='code'){const readShell=$('previewReadShell');if(readShell)readShell.style.display='grid';}
  else {const editorShell=$('workspaceEditorShell');if(editorShell)editorShell.style.display='none';$('previewMd').style.display='';}
  _previewDirty=false;
  refreshEditorChrome();
  updateEditBtn();
}

async function openFile(path,opts={}){
  if(!S.session)return;
  const ext=fileExt(path);

  // Binary/download-only formats: trigger browser download, don't preview
  if(DOWNLOAD_EXTS.has(ext)){
    downloadFile(path);
    return;
  }

  $('previewPathText').textContent=path;
  $('previewArea').classList.add('visible');
  $('fileTree').style.display='none';
  const returnTarget=opts.returnTo||(S.git&&S.git.selectedTab==='changes'?'changes':'files');
  _setPreviewReturnTarget(returnTarget);

  _previewCurrentPath = path;
  renderFileBreadcrumb(path);
  if(IMAGE_EXTS.has(ext)){
    // Image: load via raw endpoint, show as <img>
    showPreview('image');
    const url=`api/file/raw?session_id=${encodeURIComponent(S.session.session_id)}&path=${encodeURIComponent(path)}`;
    $('previewImg').alt=path;
    $('previewImg').src=url;
    $('previewImg').onerror=()=>setStatus(t('image_load_failed'));
  } else if(AUDIO_EXTS.has(ext)||VIDEO_EXTS.has(ext)){
    const mode=VIDEO_EXTS.has(ext)?'video':'audio';
    showPreview(mode);
    const url=`api/file/raw?session_id=${encodeURIComponent(S.session.session_id)}&path=${encodeURIComponent(path)}&inline=1`;
    const wrap=$('previewMediaWrap');
    if(wrap){
      wrap.innerHTML=(typeof _mediaPlayerHtml==='function')
        ? _mediaPlayerHtml(mode,url,path.split('/').pop()||path)
        : `<${mode} src="${url.replace(/"/g,'%22')}" controls preload="metadata"></${mode}>`;
      if(typeof _applyMediaPlaybackPreferences==='function') _applyMediaPlaybackPreferences(wrap);
    }
  } else if(PDF_EXTS.has(ext)){
    showPreview('pdf');
    const url=`api/file/raw?session_id=${encodeURIComponent(S.session.session_id)}&path=${encodeURIComponent(path)}&inline=1`;
    const frame=$('previewPdfFrame');
    if(frame){
      frame.src=''; // clear first to avoid stale content
      frame.src=url;
      frame.title=`PDF preview: ${path.split('/').pop()||path}`;
    }
  } else if(MD_EXTS.has(ext)){
    // Markdown: fetch text, render with renderMd, display as formatted HTML
    try{
      const data=await api(`/api/file?session_id=${encodeURIComponent(S.session.session_id)}&path=${encodeURIComponent(path)}`);
      showPreview('md');
      _previewRawContent = data.content;
      const previewMd=$('previewMd');
      if(previewMd){
        previewMd.innerHTML=renderMd(data.content);
        requestAnimationFrame(()=>{
          if(typeof postProcessRenderedMessages==='function')postProcessRenderedMessages(previewMd);
          else{
            if(typeof highlightCode==='function')highlightCode(previewMd);
            if(typeof addCopyButtons==='function')addCopyButtons(previewMd);
            if(typeof renderMermaidBlocks==='function')renderMermaidBlocks(previewMd);
            if(typeof renderKatexBlocks==='function')renderKatexBlocks(previewMd);
            if(typeof initTreeViews==='function')initTreeViews(previewMd);
          }
        });
      }
    }catch(e){setStatus(t('file_open_failed'));}
  } else if(HTML_EXTS.has(ext)){
    // HTML: render in sandboxed iframe via raw endpoint.
    // SECURITY TRADEOFF: We use sandbox="allow-scripts" which lets inline JS run
    // but prevents access to the parent frame (origin isolation). This is a
    // deliberate choice — the user is previewing their own workspace files, so
    // blocking scripts entirely would break most HTML documents. The sandbox
    // still prevents the preview from navigating the parent, accessing cookies,
    // or reading other origin data. If a stricter mode is needed, remove
    // allow-scripts (or add sandbox="") to disable all JS execution.
    showPreview('html');
    const url=`api/file/raw?session_id=${encodeURIComponent(S.session.session_id)}&path=${encodeURIComponent(path)}&inline=1`;
    const iframe=$('previewHtmlIframe');
    if(iframe){
      iframe.src=''; // clear first to avoid stale content
      iframe.src=url;
    }
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
      $('previewCode').onscroll=()=>syncEditorScroll('read');
      refreshEditorChrome();
      if(window.Prism&&typeof Prism.highlightElement==='function')Prism.highlightElement($('previewCode'));
    }catch(e){
      // If it's a 400/too-large error, offer download instead
      downloadFile(path);
    }
  }
}

function downloadFile(path){
  if(!S.session)return;
  // Trigger browser download via the raw file endpoint with content-disposition attachment
  const url=`api/file/raw?session_id=${encodeURIComponent(S.session.session_id)}&path=${encodeURIComponent(path)}&download=1`;
  const filename=path.split('/').pop();
  const a=document.createElement('a');
  a.href=url;a.download=filename;
  document.body.appendChild(a);a.click();
  setTimeout(()=>document.body.removeChild(a),100);
  showToast(t('downloading',filename),2000);
}


// ── Render breadcrumb for file preview mode ──────────────────────────────────
function renderFileBreadcrumb(filePath) {
  const bar = $('breadcrumbBar');
  if (!bar) return;
  bar.style.display = 'flex';
  const upBtn = $('btnUpDir');
  if (upBtn) upBtn.style.display = '';

  bar.innerHTML = '';
  // Root
  const root = document.createElement('span');
  root.className = 'breadcrumb-seg breadcrumb-link';
  root.textContent = '~';
  root.onclick = () => { loadDir('.'); };
  bar.appendChild(root);

  const parts = filePath.split('/');
  let accumulated = '';
  for (let i = 0; i < parts.length; i++) {
    const sep = document.createElement('span');
    sep.className = 'breadcrumb-sep';
    sep.textContent = '/';
    bar.appendChild(sep);

    accumulated += (accumulated ? '/' : '') + parts[i];
    const seg = document.createElement('span');
    seg.textContent = parts[i];
    if (i < parts.length - 1) {
      seg.className = 'breadcrumb-seg breadcrumb-link';
      const target = accumulated;
      seg.onclick = () => { loadDir(target); };
    } else {
      seg.className = 'breadcrumb-seg breadcrumb-current';
    }
    bar.appendChild(seg);
  }
}

function openInBrowser(){
  if(!_previewCurrentPath||!S.session) return;
  const url=`api/file/raw?session_id=${encodeURIComponent(S.session.session_id)}&path=${encodeURIComponent(_previewCurrentPath)}`;
  window.open(url,'_blank');
}

function openMarkdownPopout(){
  if(_previewCurrentMode!=='md'||!_previewCurrentPath)return;
  const bodyHtml=$('previewMd')?$('previewMd').innerHTML:'';
  const title=(_previewCurrentPath.split('/').pop()||_previewCurrentPath).replace(/[<>&"]/g,c=>({'<':'&lt;','>':'&gt;','&':'&amp;','"':'&quot;'}[c]));
  const html=`<!doctype html><html><head><meta charset="utf-8"><title>${title}</title><style>
    :root{color-scheme:dark light;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",system-ui,sans-serif;background:#11100e;color:#eae0d5}
    body{margin:0;padding:28px;line-height:1.7}
    main{max-width:920px;margin:0 auto}
    pre{overflow:auto;padding:12px;border:1px solid rgba(255,255,255,.14);border-radius:8px;background:rgba(0,0,0,.24)}
    code{font-family:"SF Mono",ui-monospace,monospace}
    table{border-collapse:collapse;width:100%;margin:12px 0}th,td{border:1px solid rgba(255,255,255,.16);padding:6px 10px;text-align:left}
    a{color:#8ab4f8} img{max-width:100%;height:auto}
  </style></head><body><main class="preview-md">${bodyHtml}</main></body></html>`;
  const url=URL.createObjectURL(new Blob([html],{type:'text/html'}));
  window.open(url,'_blank','noopener,noreferrer');
  setTimeout(()=>URL.revokeObjectURL(url),60000);
}

async function openGitDiff(path,kind='unstaged'){
  if(!S.session)return;
  const git=_ensureGitState();
  git.selectedTab='changes';
  git.selectedDiff={path,kind};
  $('previewPathText').textContent=`Changes / ${path}`;
  $('previewArea').classList.add('visible');
  $('fileTree').style.display='none';
  _setPreviewReturnTarget('changes');
  const changesView=$('gitChangesView'); if(changesView) changesView.style.display='none';
  const diffView=$('gitDiffView');
  if(diffView){
    diffView.innerHTML='';
    const loading=document.createElement('div');
    loading.className='git-empty';
    loading.textContent='Loading diff...';
    diffView.appendChild(loading);
  }
  _previewCurrentPath=path;
  showPreview('gitdiff');
  renderWorkspacePanelTabState();
  try{
    const data=await api(`/api/git/diff?session_id=${encodeURIComponent(S.session.session_id)}&path=${encodeURIComponent(path)}&kind=${encodeURIComponent(kind)}`);
    renderGitDiff(data.diff);
  }catch(e){
    if(diffView){
      diffView.innerHTML='';
      const err=document.createElement('div');
      err.className='git-empty error';
      err.textContent=e.message||t('git_commit_failed');
      diffView.appendChild(err);
    }
  }
}

function renderGitDiff(diff){
  const view=$('gitDiffView');
  if(!view)return;
  view.innerHTML='';
  view.className='git-diff-view '+_currentGitDiffMode();
  if(!diff)return;
  if(diff.binary){
    const msg=document.createElement('div');
    msg.className='git-empty';
    msg.textContent=t('git_binary_file');
    view.appendChild(msg);
    return;
  }
  if(diff.too_large){
    const msg=document.createElement('div');
    msg.className='git-diff-warning';
    msg.textContent=t('git_diff_too_large');
    view.appendChild(msg);
  }
  const text=diff.diff||'';
  if(!text.trim()){
    const msg=document.createElement('div');
    msg.className='git-empty';
    msg.textContent=t('git_no_changes');
    view.appendChild(msg);
    return;
  }
  const toolbar=document.createElement('div');
  toolbar.className='git-diff-toolbar';
  const actions=document.createElement('div');
  actions.className='git-diff-actions';
  const state=_gitStatusForPath(diff.path);
  if(diff.kind==='staged'){
    const unstage=document.createElement('button');
    unstage.className='mini-btn';
    unstage.textContent=t('git_unstage');
    unstage.onclick=()=>unstageGitPath(diff.path);
    actions.appendChild(unstage);
  }else{
    const stage=document.createElement('button');
    stage.className='mini-btn';
    stage.textContent=t('git_stage');
    stage.onclick=()=>stageGitPath(diff.path);
    actions.appendChild(stage);
    if(state){
      const discard=document.createElement('button');
      discard.className='mini-btn danger';
      discard.textContent=state.untracked?t('delete_title'):t('git_discard');
      discard.onclick=()=>discardGitPath(diff.path,{untracked:!!state.untracked});
      actions.appendChild(discard);
    }
  }
  const open=document.createElement('button');
  open.className='mini-btn';
  open.textContent=t('open_file');
  open.onclick=()=>openFile(diff.path,{returnTo:'changes'});
  actions.appendChild(open);
  const copy=document.createElement('button');
  copy.className='mini-btn';
  copy.textContent=t('copy_file_path')||'Copy path';
  copy.onclick=async()=>{try{await navigator.clipboard.writeText(diff.path);showToast(t('path_copied')||'Copied');}catch(e){showToast(t('path_copy_failed')||'Copy failed',2600,'error');}};
  actions.appendChild(copy);
  const modes=document.createElement('div');
  modes.className='git-diff-mode-controls';
  for(const mode of ['unified','split']){
    const btn=document.createElement('button');
    btn.className='mini-btn git-diff-mode-btn'+(_currentGitDiffMode()===mode?' active':'');
    btn.type='button';
    btn.textContent=mode==='split'?(t('git_diff_split')||'Split'):(t('git_diff_unified')||'Unified');
    btn.onclick=()=>setGitDiffMode(mode);
    modes.appendChild(btn);
  }
  toolbar.append(actions,modes);
  view.appendChild(toolbar);
  const parsed=parseUnifiedDiff(text,diff.path);
  renderParsedGitDiff(view,parsed,_currentGitDiffMode());
}

function _parseHunkHeader(line){
  const m=line.match(/^@@\s+-(\d+)(?:,(\d+))?\s+\+(\d+)(?:,(\d+))?/);
  if(!m)return null;
  return {oldStart:parseInt(m[1],10),oldCount:parseInt(m[2]||'1',10),newStart:parseInt(m[3],10),newCount:parseInt(m[4]||'1',10)};
}

function parseUnifiedDiff(text,path){
  const file={oldPath:path,newPath:path,hunks:[],meta:[]};
  let hunk=null,oldLine=0,newLine=0;
  for(const raw of String(text||'').split('\n')){
    if(raw.startsWith('--- ')){file.oldPath=raw.slice(4).replace(/^a\//,'');file.meta.push(raw);continue;}
    if(raw.startsWith('+++ ')){file.newPath=raw.slice(4).replace(/^b\//,'');file.meta.push(raw);continue;}
    if(raw.startsWith('diff --git')||raw.startsWith('index ')||raw.startsWith('new file mode')||raw.startsWith('deleted file mode')||raw.startsWith('rename ')){file.meta.push(raw);continue;}
    if(raw.startsWith('@@')){
      const parsed=_parseHunkHeader(raw);
      oldLine=parsed?parsed.oldStart:0;
      newLine=parsed?parsed.newStart:0;
      hunk={header:raw,oldStart:oldLine,newStart:newLine,lines:[]};
      file.hunks.push(hunk);
      continue;
    }
    if(!hunk)continue;
    const sign=raw[0]||' ';
    const content=raw.length?raw.slice(1):'';
    if(sign==='+'){
      hunk.lines.push({type:'add',oldLine:null,newLine:newLine++,text:content});
    }else if(sign==='-'){
      hunk.lines.push({type:'del',oldLine:oldLine++,newLine:null,text:content});
    }else if(sign==='\\'){
      hunk.lines.push({type:'meta',oldLine:null,newLine:null,text:raw});
    }else{
      hunk.lines.push({type:'ctx',oldLine:oldLine++,newLine:newLine++,text:raw.startsWith(' ')?content:raw});
    }
  }
  return file;
}

function _diffCell(text,cls=''){
  const el=document.createElement('span');
  el.className=cls;
  el.textContent=text==null?'':String(text);
  return el;
}

function renderParsedGitDiff(view,file,mode){
  const wrap=document.createElement('div');
  wrap.className='git-diff-file';
  const header=document.createElement('div');
  header.className='git-diff-file-header';
  header.append(_diffCell('old','git-diff-ln'),_diffCell('new','git-diff-ln'),_diffCell(`${file.oldPath} → ${file.newPath}`,'git-diff-code'));
  wrap.appendChild(header);
  for(const hunk of file.hunks){
    const h=document.createElement('div');
    h.className='git-diff-hunk';
    h.append(_diffCell('', 'git-diff-ln'),_diffCell('', 'git-diff-ln'),_diffCell(hunk.header,'git-diff-code'));
    wrap.appendChild(h);
    const rows=mode==='split'?_splitDiffRows(hunk.lines):hunk.lines;
    for(const rowData of rows){
      wrap.appendChild(mode==='split'?_renderSplitDiffRow(rowData):_renderUnifiedDiffRow(rowData));
    }
  }
  view.appendChild(wrap);
}

function _renderUnifiedDiffRow(line){
  const row=document.createElement('div');
  row.className=`git-diff-row ${line.type}`;
  const prefix=line.type==='add'?'+':line.type==='del'?'-':' ';
  row.append(_diffCell(line.oldLine||'', 'git-diff-ln'),_diffCell(line.newLine||'', 'git-diff-ln'),_diffCell(prefix+line.text,'git-diff-code'));
  return row;
}

function _splitDiffRows(lines){
  const rows=[];
  for(let i=0;i<lines.length;i++){
    const line=lines[i];
    if(line.type==='del'&&lines[i+1]&&lines[i+1].type==='add'){
      rows.push({type:'change',old:line,new:lines[i+1]});
      i++;
    }else if(line.type==='del')rows.push({type:'del',old:line,new:null});
    else if(line.type==='add')rows.push({type:'add',old:null,new:line});
    else rows.push({type:line.type,old:line,new:line});
  }
  return rows;
}

function _renderSplitDiffRow(pair){
  const row=document.createElement('div');
  row.className=`git-diff-split-row ${pair.type}`;
  const oldLine=pair.old, newLine=pair.new;
  row.append(
    _diffCell(oldLine&&oldLine.oldLine||'', 'git-diff-ln'),
    _diffCell(oldLine?((oldLine.type==='del'?'-':' ')+oldLine.text):'', 'git-diff-code old-code'),
    _diffCell(newLine&&newLine.newLine||'', 'git-diff-ln'),
    _diffCell(newLine?((newLine.type==='add'?'+':' ')+newLine.text):'', 'git-diff-code new-code')
  );
  return row;
}

async function stageGitPath(path){
  if(!S.session)return;
  const git=_ensureGitState();
  git.mutating=true;renderGitChanges();
  try{
    const data=await api('/api/git/stage',{method:'POST',body:JSON.stringify({session_id:S.session.session_id,paths:[path]})});
    _setGitStatus(data.git);
    if(S.git.selectedDiff&&S.git.selectedDiff.path===path)openGitDiff(path,'staged');
  }catch(e){showToast(e.message||t('git_commit_failed'),3000,'error');}
  finally{git.mutating=false;renderGitChanges();}
}

async function stageGitAllChanges(){
  if(!S.session)return;
  const paths=_gitStageableFiles().map(f=>f.path);
  if(!paths.length)return;
  const git=_ensureGitState();
  git.mutating=true;renderGitChanges();
  try{
    const data=await api('/api/git/stage',{method:'POST',body:JSON.stringify({session_id:S.session.session_id,paths})});
    _setGitStatus(data.git);
    if(S.git.selectedDiff&&paths.includes(S.git.selectedDiff.path))openGitDiff(S.git.selectedDiff.path,'staged');
  }catch(e){showToast(e.message||t('git_commit_failed'),3000,'error');}
  finally{git.mutating=false;renderGitChanges();}
}

async function stageGitSelectedChanges(){
  if(!S.session)return;
  const paths=_gitSelectedFiles().filter(f=>!f.conflict&&(f.unstaged||f.untracked)).map(f=>f.path);
  if(!paths.length){showToast(t('git_select_files')||'Select files',2200);return;}
  const git=_ensureGitState();
  git.mutating=true;renderGitChanges();
  try{
    const data=await api('/api/git/stage',{method:'POST',body:JSON.stringify({session_id:S.session.session_id,paths})});
    _setGitStatus(data.git);
    if(S.git.selectedDiff&&paths.includes(S.git.selectedDiff.path))openGitDiff(S.git.selectedDiff.path,'staged');
  }catch(e){showToast(e.message||t('git_commit_failed'),3000,'error');}
  finally{git.mutating=false;renderGitChanges();}
}

async function unstageGitPath(path){
  if(!S.session)return;
  const git=_ensureGitState();
  git.mutating=true;renderGitChanges();
  try{
    const data=await api('/api/git/unstage',{method:'POST',body:JSON.stringify({session_id:S.session.session_id,paths:[path]})});
    _setGitStatus(data.git);
    if(S.git.selectedDiff&&S.git.selectedDiff.path===path)openGitDiff(path,'unstaged');
  }catch(e){showToast(e.message||t('git_commit_failed'),3000,'error');}
  finally{git.mutating=false;renderGitChanges();}
}

async function discardGitPath(path,opts={}){
  if(!S.session)return;
  const untracked=!!opts.untracked;
  const ok=await showConfirmDialog({
    title:untracked?t('delete_confirm',path):t('git_discard_confirm_title'),
    message:untracked?t('git_delete_untracked_confirm',path):t('git_discard_confirm_message',path),
    confirmLabel:untracked?t('delete_title'):t('git_discard'),
    danger:true,
    focusCancel:true
  });
  if(!ok)return;
  const git=_ensureGitState();
  git.mutating=true;renderGitChanges();
  try{
    const data=await api('/api/git/discard',{method:'POST',body:JSON.stringify({session_id:S.session.session_id,paths:[path],delete_untracked:untracked})});
    _setGitStatus(data.git);
    if(S.git.selectedDiff&&S.git.selectedDiff.path===path){
      $('previewArea').classList.remove('visible');
      S.git.selectedDiff=null;
      renderWorkspacePanelTabState();
    }
    await loadDir(S.currentDir);
  }catch(e){showToast(e.message||t('git_commit_failed'),3000,'error');}
  finally{git.mutating=false;renderGitChanges();}
}

async function discardGitSelectedChanges(){
  if(!S.session)return;
  const selected=_gitSelectedFiles().filter(f=>!f.conflict&&(f.unstaged||f.untracked));
  const paths=selected.map(f=>f.path);
  if(!paths.length){showToast(t('git_select_files')||'Select files',2200);return;}
  const hasUntracked=selected.some(f=>f.untracked);
  const ok=await showConfirmDialog({
    title:t('git_discard_selected_confirm_title'),
    message:t('git_discard_selected_confirm_message',paths.length),
    confirmLabel:hasUntracked?t('git_discard_delete_selected'):t('git_discard_selected'),
    danger:true,
    focusCancel:true
  });
  if(!ok)return;
  const git=_ensureGitState();
  git.mutating=true;renderGitChanges();
  try{
    const data=await api('/api/git/discard',{method:'POST',body:JSON.stringify({session_id:S.session.session_id,paths,delete_untracked:hasUntracked})});
    _setGitStatus(data.git);
    if(S.git.selectedDiff&&paths.includes(S.git.selectedDiff.path)){
      $('previewArea').classList.remove('visible');
      S.git.selectedDiff=null;
      renderWorkspacePanelTabState();
    }
    await loadDir(S.currentDir);
  }catch(e){showToast(e.message||t('git_commit_failed'),3000,'error');}
  finally{git.mutating=false;renderGitChanges();}
}

async function commitGitChanges(){
  if(!S.session)return;
  const input=$('gitCommitMessage');
  const message=(input&&input.value||'').trim();
  if(!message){showToast(t('git_commit_message'),2200);return;}
  const selected=_gitSelectedFiles().map(f=>f.path);
  if(!selected.length){showToast(t('git_select_files')||'Select files to commit',2200);return;}
  const git=_ensureGitState();
  git.mutating=true;renderGitChanges();
  try{
    const data=await api('/api/git/commit-selected',{method:'POST',body:JSON.stringify({session_id:S.session.session_id,message,paths:selected})});
    if(input)input.value='';
    _setGitStatus(data.status);
    showToast(`${t('git_committed')} ${data.commit}`,2600);
    await loadDir(S.currentDir);
  }catch(e){showToast(`${t('git_commit_failed')}: ${e.message}`,4000,'error');}
  finally{git.mutating=false;renderGitChanges();}
}

async function generateGitCommitMessage(){
  if(!S.session)return;
  const git=_ensureGitState();
  if(git.generatingCommitMessage)return;
  const selected=_gitSelectedFiles().map(f=>f.path);
  if(!selected.length){showToast(t('git_select_files')||'Select files to commit',2200);return;}
  git.generatingCommitMessage=true;
  renderGitChanges();
  try{
    const data=await api('/api/git/commit-message-selected',{method:'POST',body:JSON.stringify({session_id:S.session.session_id,paths:selected})});
    const input=$('gitCommitMessage');
    if(input){
      input.value=data.message||'';
      input.focus();
      input.selectionStart=input.selectionEnd=input.value.length;
    }
    if(data.truncated)showToast('Generated from a truncated selected diff',3200);
  }catch(e){
    showToast(`Commit message generation failed: ${e.message}`,4000,'error');
  }finally{
    git.generatingCommitMessage=false;
    renderGitChanges();
  }
}

function _gitRemoteActionLabel(action){
  return action==='fetch'?t('git_fetched'):action==='pull'?t('git_pulled'):t('git_pushed');
}

function _gitRemoteToastMessage(action,data){
  const label=_gitRemoteActionLabel(action);
  const raw=String(data&&data.message?data.message:'').trim();
  if(!raw)return label;
  const lines=raw.split(/\r?\n/).map(line=>line.trim()).filter(Boolean);
  const refs=[];
  let remote='';
  const fallback=[];
  lines.forEach(line=>{
    if(line.startsWith('From ')){
      remote=line.slice(5).trim().replace(/^https?:\/\//,'').replace(/\.git$/,'');
      return;
    }
    const compact=line.replace(/\s+/g,' ');
    const refLine=compact.replace(/^[*+=!-]\s+/,'').replace(/^\[[^\]]+\]\s+/,'');
    const match=refLine.match(/^(?:(\S+\.\.\S+)\s+)?(\S+)\s+->\s+(\S+)(?:\s+\(.+\))?$/);
    if(match){
      const range=match[1];
      const src=match[2];
      const dst=match[3];
      refs.push(`${src} -> ${dst}${range?` (${range})`:''}`);
    }else{
      fallback.push(compact);
    }
  });
  if(refs.length){
    const shown=refs.slice(0,3);
    if(refs.length>shown.length)shown.push(`+${refs.length-shown.length} more refs`);
    return [label, remote?`Remote ${remote}`:null, ...shown].filter(Boolean).join('\n');
  }
  return [label, ...fallback.slice(0,3)].filter(Boolean).join('\n');
}

async function runGitRemoteAction(action){
  if(!S.session||!['fetch','pull','push'].includes(action))return;
  const git=_ensureGitState();
  git.syncing=action;
  git.mutating=true;
  renderGitChanges();
  try{
    const data=await api(`/api/git/${action}`,{method:'POST',body:JSON.stringify({session_id:S.session.session_id})});
    _setGitStatus(data.status);
    showToast(_gitRemoteToastMessage(action,data),4200);
    await loadDir(S.currentDir);
  }catch(e){
    showToast(`${t('git_sync_failed')}: ${e.message}`,4000,'error');
  }finally{
    git.syncing=null;
    git.mutating=false;
    renderGitChanges();
  }
}

function _installWorkspaceInteractionGuards(){
  if(window.__hermesWorkspaceInteractionGuardsInstalled)return;
  window.__hermesWorkspaceInteractionGuardsInstalled=true;
  document.addEventListener('click',event=>{
    const git=_ensureGitState();
    if(!git.branchMenuOpen)return;
    const control=$('gitBranchControl');
    if(control&&control.contains(event.target))return;
    closeGitBranchMenu();
  });
  document.addEventListener('keydown',event=>{
    if(event.key!=='Escape')return;
    const git=_ensureGitState();
    if(!git.branchMenuOpen)return;
    event.preventDefault();
    closeGitBranchMenu();
    const btn=$('btnGitBranchMenu');
    if(btn)btn.focus();
  });
}

_installWorkspaceInteractionGuards();

async function _autoRefreshWorkspaceGitStatus(){
  if(!S.session)return;
  if(!_workspacePanelOpenForAutoRefresh())return;
  const git=_ensureGitState();
  if(git.mutating||git.syncing||git.generatingCommitMessage||git.branchMenuOpen)return;
  if(typeof _previewDirty!=='undefined'&&_previewDirty)return;
  await refreshGitStatus({auto:true,refreshBranches:false});
}

function _shouldAutoFetchWorkspaceGit(){
  if(!S.session)return false;
  if(!_workspacePanelOpenForAutoRefresh())return false;
  const git=_ensureGitState();
  if(!git.status||!git.status.is_git)return false;
  if(git.mutating||git.syncing||git.autoFetching||git.generatingCommitMessage||git.branchMenuOpen)return false;
  if(typeof _previewDirty!=='undefined'&&_previewDirty)return false;
  const now=Date.now();
  if(git.lastAutoFetchAt&&now-git.lastAutoFetchAt<GIT_AUTO_FETCH_MS)return false;
  if(git.lastAutoFetchErrorAt&&now-git.lastAutoFetchErrorAt<GIT_AUTO_FETCH_MIN_ERROR_BACKOFF_MS)return false;
  return true;
}

async function _autoFetchWorkspaceGit(){
  if(!_shouldAutoFetchWorkspaceGit())return null;
  const git=_ensureGitState();
  git.autoFetching=true;
  _renderGitAutoFetchStatus();
  try{
    const priorSignature=_gitStatusSignature(git.status);
    const data=await api(`/api/git/fetch`,{method:'POST',body:JSON.stringify({session_id:S.session.session_id})});
    git.lastAutoFetchAt=Date.now();
    git.lastAutoFetchErrorAt=0;
    git.lastAutoFetchError='';
    git.autoFetchFailureCount=0;
    _setGitStatus(data.status);
    const changed=priorSignature!==_gitStatusSignature(git.status);
    if(changed&&(git.branchMenuOpen||git.branches))refreshGitBranches();
    return data;
  }catch(e){
    git.lastAutoFetchErrorAt=Date.now();
    git.lastAutoFetchError=e&&e.message?e.message:String(e||'Auto-fetch failed');
    git.autoFetchFailureCount=(git.autoFetchFailureCount||0)+1;
    if(git.autoFetchFailureCount>=3&&typeof showToast==='function'){
      showToast(`Git auto-fetch failed: ${git.lastAutoFetchError}`,4000,'warning');
    }
    _renderGitAutoFetchStatus();
    return null;
  }finally{
    git.autoFetching=false;
    _renderGitAutoFetchStatus();
  }
}

function _visibleWorkspaceDirsForRefresh(){
  const dirs=[];
  const add=dir=>{
    dir=_normalizeWorkspaceDirPath(dir);
    if(!dirs.includes(dir))dirs.push(dir);
  };
  add(S.currentDir||'.');
  for(const dir of [...(S._expandedDirs||new Set())]){
    add(dir);
    if(dirs.length>=WORKSPACE_TREE_AUTO_REFRESH_MAX_DIRS)break;
  }
  return dirs.slice(0,WORKSPACE_TREE_AUTO_REFRESH_MAX_DIRS);
}

function _shouldRefreshWorkspaceTree(){
  if(!S.session)return false;
  if(!_workspacePanelOpenForAutoRefresh())return false;
  if(typeof _previewDirty!=='undefined'&&_previewDirty)return false;
  if(S._treeRefreshing)return false;
  const git=_ensureGitState();
  if(git.mutating||git.syncing)return false;
  return true;
}

async function _refreshWorkspaceTreeIfChanged(){
  if(!_shouldRefreshWorkspaceTree())return;
  S._treeRefreshing=true;
  try{
    const signatures=_ensureWorkspaceDirMetadata();
    const dirs=_visibleWorkspaceDirsForRefresh();
    let changed=false;
    for(const dir of dirs){
      let data;
      try{data=await _fetchWorkspaceDir(dir);}catch(e){continue;}
      const nextSignature=typeof data?.signature==='string'?data.signature:null;
      if(nextSignature&&signatures[dir]&&signatures[dir]===nextSignature)continue;
      _storeWorkspaceDirListing(dir,data);
      changed=true;
    }
    if(changed){
      const scrollEl=$('fileTree');
      const scrollTop=scrollEl?scrollEl.scrollTop:0;
      renderBreadcrumb();
      renderFileTree();
      renderWorkspacePanelTabState();
      if(scrollEl)scrollEl.scrollTop=scrollTop;
    }
  }finally{
    S._treeRefreshing=false;
  }
}

function _installWorkspaceGitAutoRefresh(){
  if(window.__hermesWorkspaceGitAutoRefreshInstalled)return;
  window.__hermesWorkspaceGitAutoRefreshInstalled=true;
  window.setInterval(()=>{_autoRefreshWorkspaceGitStatus();},GIT_AUTO_REFRESH_MS);
  window.setInterval(()=>{_refreshWorkspaceTreeIfChanged();},WORKSPACE_TREE_AUTO_REFRESH_MS);
  window.setInterval(()=>{_autoFetchWorkspaceGit();},GIT_AUTO_FETCH_MS);
  document.addEventListener('visibilitychange',()=>{
    if(document.visibilityState==='visible'){
      _autoRefreshWorkspaceGitStatus();
      _refreshWorkspaceTreeIfChanged();
      _autoFetchWorkspaceGit();
    }
  });
}

_installWorkspaceGitAutoRefresh();
